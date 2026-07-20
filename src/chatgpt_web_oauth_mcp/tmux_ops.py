from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
from typing import Sequence

from .response_budget import (
    DEFAULT_TOOL_OUTPUT_TOKEN_BUDGET,
    ResponseBudget,
    with_budget_metadata,
)


FIELD_SEPARATOR = "\x1f"
MAX_CAPTURE_LINES = 500
MAX_CAPTURE_OUTPUT_LINES = 700
MAX_CAPTURE_BYTES = 256 * 1024
MAX_SEND_TEXT_BYTES = 64 * 1024
MAX_ENTER_COUNT = 3
MAX_KEYS_PER_CALL = 20
TMUX_CONTROL_TIMEOUT_SECONDS = 10
SESSION_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
SOCKET_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
ALLOWED_KEYS = frozenset(
    {
        "Enter",
        "C-m",
        "C-c",
        "C-d",
        "Escape",
        "Tab",
        "BSpace",
        "Delete",
        "Up",
        "Down",
        "Left",
        "Right",
        "Home",
        "End",
        "PageUp",
        "PageDown",
    }
)

_PANE_FIELDS = (
    "session_name",
    "session_id",
    "session_attached",
    "session_windows",
    "session_created",
    "window_id",
    "window_index",
    "window_name",
    "pane_id",
    "pane_index",
    "pane_active",
    "pane_pid",
    "pane_current_command",
    "pane_current_path",
    "pane_dead",
    "pane_dead_status",
    "pane_dead_signal",
    "pane_width",
    "pane_height",
    "m5local_managed",
)
_PANE_FORMAT = FIELD_SEPARATOR.join(
    f"#{{@m5local_managed}}" if field == "m5local_managed" else f"#{{{field}}}"
    for field in _PANE_FIELDS
)
_CREATE_FORMAT = FIELD_SEPARATOR.join(("#{session_id}", "#{window_id}", "#{pane_id}"))


@dataclass(frozen=True)
class _RunResult:
    exit_code: int
    stdout: bytes
    stderr: bytes


class TmuxControlError(RuntimeError):
    def __init__(self, code: str, message: str, **extra: object) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra

    def as_payload(self) -> dict[str, object]:
        return {
            "success": False,
            "error": {
                "code": self.code,
                "message": self.message,
            },
            **self.extra,
        }


def _decode(value: bytes) -> str:
    return value.decode("utf-8", errors="replace")


def _int_or_none(value: str) -> int | None:
    if value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _bool_value(value: str) -> bool:
    return value == "1"


def _is_no_server(stderr: str) -> bool:
    normalized = stderr.lower()
    return (
        "no server running" in normalized
        or "no sessions" in normalized
        or ("error connecting to" in normalized and "no such file or directory" in normalized)
    )


def _validate_session_name(session: str) -> str:
    normalized = (session or "").strip()
    if not SESSION_NAME_PATTERN.fullmatch(normalized):
        raise TmuxControlError(
            "invalid_session_name",
            "session must match ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$.",
            session=session,
        )
    return normalized


def _validate_socket_name(socket_name: str) -> str:
    normalized = (socket_name or "").strip()
    if not SOCKET_NAME_PATTERN.fullmatch(normalized):
        raise TmuxControlError(
            "invalid_socket_name",
            "tmux socket name must match ^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$.",
            socket_name=socket_name,
        )
    return normalized


def _pane_payload(row: dict[str, object]) -> dict[str, object]:
    return {
        "window_id": row["window_id"],
        "window_index": row["window_index"],
        "window_name": row["window_name"],
        "pane_id": row["pane_id"],
        "pane_index": row["pane_index"],
        "active": row["pane_active"],
        "pane_pid": row["pane_pid"],
        "current_command": row["pane_current_command"],
        "current_path": row["pane_current_path"],
        "pane_dead": row["pane_dead"],
        "exit_code": row["pane_dead_status"],
        "exit_signal": row["pane_dead_signal"],
        "width": row["pane_width"],
        "height": row["pane_height"],
    }


def _session_summary(rows: list[dict[str, object]], *, include_panes: bool) -> dict[str, object]:
    first = rows[0]
    payload: dict[str, object] = {
        "session_name": first["session_name"],
        "session_id": first["session_id"],
        "attached": first["session_attached"],
        "window_count": first["session_windows"],
        "pane_count": len(rows),
        "created_at_epoch": first["session_created"],
        "managed": first["m5local_managed"],
    }
    if include_panes:
        payload["panes"] = [_pane_payload(row) for row in rows]
    else:
        payload["primary_pane"] = _pane_payload(rows[0])
    return payload


class TmuxClient:
    """Small structured wrapper around one tmux server selected by socket name."""

    def __init__(
        self,
        *,
        binary: str = "tmux",
        socket_name: str = "default",
        timeout: int = TMUX_CONTROL_TIMEOUT_SECONDS,
    ) -> None:
        self.binary = (binary or "tmux").strip() or "tmux"
        self.socket_name = _validate_socket_name(socket_name)
        self.timeout = max(1, int(timeout))

    def _client_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.pop("TMUX", None)
        env.pop("TMUX_PANE", None)
        return env

    def _run(self, args: Sequence[str], *, input_bytes: bytes | None = None) -> _RunResult:
        argv = [self.binary, "-L", self.socket_name, *args]
        try:
            completed = subprocess.run(
                argv,
                input=input_bytes,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout,
                shell=False,
                check=False,
                env=self._client_env(),
            )
        except FileNotFoundError as exc:
            raise TmuxControlError(
                "tmux_not_found",
                f"tmux executable was not found: {self.binary}",
                binary=self.binary,
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise TmuxControlError(
                "tmux_timeout",
                f"tmux control command exceeded {self.timeout} seconds.",
                timeout_seconds=self.timeout,
            ) from exc
        except OSError as exc:
            raise TmuxControlError(
                "tmux_command_failed",
                f"Failed to run tmux: {exc}",
            ) from exc
        return _RunResult(completed.returncode, completed.stdout, completed.stderr)

    def _require_ok(self, result: _RunResult, *, operation: str) -> None:
        if result.exit_code == 0:
            return
        stderr = _decode(result.stderr).strip()
        raise TmuxControlError(
            "tmux_command_failed",
            stderr or f"tmux {operation} failed with exit code {result.exit_code}.",
            operation=operation,
            exit_code=result.exit_code,
        )

    def _best_effort(self, args: Sequence[str]) -> None:
        try:
            self._run(args)
        except TmuxControlError:
            pass

    def _parse_rows(self, stdout: bytes) -> list[dict[str, object]]:
        content = _decode(stdout)
        rows: list[dict[str, object]] = []
        for line in content.splitlines():
            if not line:
                continue
            parts = line.split(FIELD_SEPARATOR)
            if len(parts) != len(_PANE_FIELDS):
                raise TmuxControlError(
                    "tmux_parse_failed",
                    "tmux returned an unexpected pane status format.",
                    field_count=len(parts),
                    expected_field_count=len(_PANE_FIELDS),
                )
            raw = dict(zip(_PANE_FIELDS, parts, strict=True))
            rows.append(
                {
                    "session_name": raw["session_name"],
                    "session_id": raw["session_id"],
                    "session_attached": _bool_value(raw["session_attached"]),
                    "session_windows": _int_or_none(raw["session_windows"]),
                    "session_created": _int_or_none(raw["session_created"]),
                    "window_id": raw["window_id"],
                    "window_index": _int_or_none(raw["window_index"]),
                    "window_name": raw["window_name"],
                    "pane_id": raw["pane_id"],
                    "pane_index": _int_or_none(raw["pane_index"]),
                    "pane_active": _bool_value(raw["pane_active"]),
                    "pane_pid": _int_or_none(raw["pane_pid"]),
                    "pane_current_command": raw["pane_current_command"],
                    "pane_current_path": raw["pane_current_path"],
                    "pane_dead": _bool_value(raw["pane_dead"]),
                    "pane_dead_status": _int_or_none(raw["pane_dead_status"]),
                    "pane_dead_signal": _int_or_none(raw["pane_dead_signal"]),
                    "pane_width": _int_or_none(raw["pane_width"]),
                    "pane_height": _int_or_none(raw["pane_height"]),
                    "m5local_managed": raw["m5local_managed"] == "1",
                }
            )
        rows.sort(
            key=lambda row: (
                str(row["session_name"]),
                int(row["window_index"] or 0),
                int(row["pane_index"] or 0),
            )
        )
        return rows

    def _all_panes(self) -> list[dict[str, object]]:
        result = self._run(["list-panes", "-a", "-F", _PANE_FORMAT])
        if result.exit_code != 0:
            stderr = _decode(result.stderr).strip()
            if _is_no_server(stderr):
                return []
            self._require_ok(result, operation="list-panes")
        return self._parse_rows(result.stdout)

    def _session_rows(self, session: str) -> list[dict[str, object]]:
        normalized = _validate_session_name(session)
        return [row for row in self._all_panes() if row["session_name"] == normalized]

    def _require_session_rows(self, session: str) -> list[dict[str, object]]:
        normalized = _validate_session_name(session)
        rows = self._session_rows(normalized)
        if not rows:
            raise TmuxControlError(
                "session_not_found",
                f"tmux session not found: {normalized}",
                session=normalized,
            )
        return rows

    def list_sessions(self, *, include_panes: bool = False) -> dict[str, object]:
        try:
            grouped: dict[str, list[dict[str, object]]] = {}
            for row in self._all_panes():
                grouped.setdefault(str(row["session_id"]), []).append(row)
            sessions = [
                _session_summary(rows, include_panes=include_panes)
                for _, rows in sorted(grouped.items(), key=lambda item: str(item[1][0]["session_name"]))
            ]
            return {
                "success": True,
                "socket_name": self.socket_name,
                "session_count": len(sessions),
                "sessions": sessions,
            }
        except TmuxControlError as exc:
            return exc.as_payload()

    def status(self, *, session: str) -> dict[str, object]:
        try:
            rows = self._require_session_rows(session)
            return {
                "success": True,
                "socket_name": self.socket_name,
                "session": _session_summary(rows, include_panes=True),
            }
        except TmuxControlError as exc:
            return exc.as_payload()

    def start(
        self,
        *,
        session: str,
        cwd: Path,
        command: str | None = None,
        width: int = 180,
        height: int = 50,
        remain_on_exit: bool = True,
    ) -> dict[str, object]:
        try:
            normalized = _validate_session_name(session)
            if self._session_rows(normalized):
                raise TmuxControlError(
                    "session_exists",
                    f"tmux session already exists: {normalized}",
                    session=normalized,
                )
            if not cwd.exists():
                raise TmuxControlError(
                    "cwd_not_found",
                    f"Working directory not found: {cwd}",
                    cwd=str(cwd),
                )
            if not cwd.is_dir():
                raise TmuxControlError(
                    "cwd_not_directory",
                    f"Working directory is not a directory: {cwd}",
                    cwd=str(cwd),
                )
            if not 40 <= int(width) <= 400:
                raise TmuxControlError("invalid_arguments", "width must be between 40 and 400.")
            if not 10 <= int(height) <= 200:
                raise TmuxControlError("invalid_arguments", "height must be between 10 and 200.")
            normalized_command = None
            if command is not None:
                normalized_command = command.strip()
                if not normalized_command:
                    raise TmuxControlError("invalid_arguments", "command must be non-empty when provided.")

            create = self._run(
                [
                    "new-session",
                    "-d",
                    "-P",
                    "-F",
                    _CREATE_FORMAT,
                    "-s",
                    normalized,
                    "-c",
                    str(cwd),
                    "-x",
                    str(int(width)),
                    "-y",
                    str(int(height)),
                ]
            )
            if create.exit_code != 0 and "duplicate session" in _decode(create.stderr).lower():
                raise TmuxControlError(
                    "session_exists",
                    f"tmux session already exists: {normalized}",
                    session=normalized,
                )
            self._require_ok(create, operation="new-session")
            created_parts = _decode(create.stdout).strip().split(FIELD_SEPARATOR)
            if len(created_parts) != 3 or not all(created_parts):
                for row in self._session_rows(normalized):
                    self._best_effort(["kill-session", "-t", str(row["session_id"])])
                    break
                raise TmuxControlError(
                    "tmux_parse_failed",
                    "tmux returned an unexpected new-session format.",
                )
            session_id, window_id, pane_id = created_parts

            try:
                managed = self._run(["set-option", "-t", session_id, "@m5local_managed", "1"])
                self._require_ok(managed, operation="set-option")
                owner = self._run(
                    ["set-option", "-t", session_id, "@m5local_created_by", "chatgpt-web-oauth-mcp"]
                )
                self._require_ok(owner, operation="set-option")
                remain = self._run(
                    [
                        "set-option",
                        "-w",
                        "-t",
                        pane_id,
                        "remain-on-exit",
                        "on" if remain_on_exit else "off",
                    ]
                )
                self._require_ok(remain, operation="set-option")
                if normalized_command is not None:
                    respawn = self._run(
                        [
                            "respawn-pane",
                            "-k",
                            "-t",
                            pane_id,
                            "-c",
                            str(cwd),
                            "--",
                            normalized_command,
                        ]
                    )
                    self._require_ok(respawn, operation="respawn-pane")
            except TmuxControlError:
                self._best_effort(["kill-session", "-t", session_id])
                raise

            payload: dict[str, object] = {
                "success": True,
                "socket_name": self.socket_name,
                "session": normalized,
                "session_id": session_id,
                "window_id": window_id,
                "pane_id": pane_id,
                "cwd": str(cwd),
                "width": int(width),
                "height": int(height),
                "remain_on_exit": bool(remain_on_exit),
                "command_started": normalized_command is not None,
            }
            if normalized_command is not None:
                payload["command_length"] = len(normalized_command)
                payload["command_sha256"] = hashlib.sha256(normalized_command.encode("utf-8")).hexdigest()
            return payload
        except TmuxControlError as exc:
            return exc.as_payload()

    def capture(
        self,
        *,
        session: str,
        lines: int = 100,
        join_wrapped: bool = True,
        max_tokens: int = DEFAULT_TOOL_OUTPUT_TOKEN_BUDGET,
    ) -> dict[str, object]:
        try:
            requested_lines = int(lines)
            if requested_lines < 1:
                raise TmuxControlError("invalid_arguments", "lines must be at least 1.")
            effective_lines = min(requested_lines, MAX_CAPTURE_LINES)
            rows = self._require_session_rows(session)
            primary = rows[0]
            args = ["capture-pane", "-p"]
            if join_wrapped:
                args.append("-J")
            args.extend(["-t", str(primary["pane_id"]), "-S", f"-{effective_lines}"])
            captured = self._run(args)
            self._require_ok(captured, operation="capture-pane")

            raw_lines = _decode(captured.stdout).splitlines()
            bounded_lines = raw_lines[-MAX_CAPTURE_OUTPUT_LINES:]
            truncated_by_line_limit = (
                requested_lines > MAX_CAPTURE_LINES or len(raw_lines) > MAX_CAPTURE_OUTPUT_LINES
            )
            truncated_by_byte_limit = False
            selected_reversed: list[str] = []
            byte_count = 0
            for line in reversed(bounded_lines):
                line_bytes = len(line.encode("utf-8", errors="replace")) + 1
                if selected_reversed and byte_count + line_bytes > MAX_CAPTURE_BYTES:
                    truncated_by_byte_limit = True
                    break
                if not selected_reversed and line_bytes > MAX_CAPTURE_BYTES:
                    encoded = line.encode("utf-8", errors="replace")[-MAX_CAPTURE_BYTES:]
                    selected_reversed.append(encoded.decode("utf-8", errors="replace"))
                    byte_count = len(encoded)
                    truncated_by_byte_limit = True
                    break
                selected_reversed.append(line)
                byte_count += line_bytes
            output_lines = list(reversed(selected_reversed))
            content = "\n".join(output_lines)
            payload: dict[str, object] = {
                "success": True,
                "socket_name": self.socket_name,
                "session": str(primary["session_name"]),
                "pane_id": primary["pane_id"],
                "current_command": primary["pane_current_command"],
                "current_path": primary["pane_current_path"],
                "pane_dead": primary["pane_dead"],
                "exit_code": primary["pane_dead_status"],
                "requested_lines": requested_lines,
                "effective_line_limit": effective_lines,
                "max_output_lines": MAX_CAPTURE_OUTPUT_LINES,
                "lines_returned": len(output_lines),
                "bytes_returned": len(content.encode("utf-8", errors="replace")),
                "truncated_by_line_limit": truncated_by_line_limit,
                "truncated_by_byte_limit": truncated_by_byte_limit,
                "truncated_by_token_budget": False,
                "join_wrapped": bool(join_wrapped),
                "lines": output_lines,
                "content": content,
                "next_offset": None,
            }
            budget = ResponseBudget(max_tokens=max_tokens)
            initially_truncated = truncated_by_line_limit or truncated_by_byte_limit
            rendered, measurement = with_budget_metadata(
                payload,
                budget=budget,
                truncated=initially_truncated,
                stop_reason=(
                    "byte_budget" if truncated_by_byte_limit else "line_limit"
                )
                if initially_truncated
                else "snapshot_complete",
            )
            token_truncated = False
            while not measurement.fits and output_lines:
                token_truncated = True
                output_lines.pop(0)
                content = "\n".join(output_lines)
                payload["lines"] = output_lines
                payload["content"] = content
                payload["lines_returned"] = len(output_lines)
                payload["bytes_returned"] = len(content.encode("utf-8", errors="replace"))
                payload["truncated_by_token_budget"] = True
                rendered, measurement = with_budget_metadata(
                    payload,
                    budget=budget,
                    truncated=True,
                    stop_reason="token_budget",
                )
            if token_truncated:
                rendered["truncated_by_token_budget"] = True
            return rendered
        except TmuxControlError as exc:
            return exc.as_payload()

    def send(
        self,
        *,
        session: str,
        text: str | None = None,
        keys: list[str] | None = None,
        enter_count: int = 0,
    ) -> dict[str, object]:
        try:
            normalized_keys = list(keys or [])
            requested_enter_count = int(enter_count)
            if not 0 <= requested_enter_count <= MAX_ENTER_COUNT:
                raise TmuxControlError(
                    "invalid_arguments",
                    f"enter_count must be between 0 and {MAX_ENTER_COUNT}.",
                )
            if len(normalized_keys) > MAX_KEYS_PER_CALL:
                raise TmuxControlError(
                    "invalid_arguments",
                    f"keys may contain at most {MAX_KEYS_PER_CALL} entries.",
                )
            invalid_keys = [key for key in normalized_keys if key not in ALLOWED_KEYS]
            if invalid_keys:
                raise TmuxControlError(
                    "invalid_keys",
                    "keys contains unsupported tmux key names.",
                    invalid_keys=invalid_keys,
                    allowed_keys=sorted(ALLOWED_KEYS),
                )
            text_bytes: bytes | None = None
            if text is not None:
                if "\x00" in text:
                    raise TmuxControlError("invalid_arguments", "text must not contain NUL characters.")
                text_bytes = text.encode("utf-8")
                if len(text_bytes) > MAX_SEND_TEXT_BYTES:
                    raise TmuxControlError(
                        "text_too_large",
                        f"text exceeds the {MAX_SEND_TEXT_BYTES}-byte limit.",
                        text_bytes=len(text_bytes),
                    )
            if not text_bytes and not normalized_keys and requested_enter_count == 0:
                raise TmuxControlError(
                    "invalid_arguments",
                    "Provide non-empty text, at least one key, or enter_count greater than zero.",
                )

            rows = self._require_session_rows(session)
            primary = rows[0]
            if bool(primary["pane_dead"]):
                raise TmuxControlError(
                    "pane_dead",
                    f"Cannot send input because the primary pane is dead: {primary['pane_id']}",
                    session=primary["session_name"],
                    pane_id=primary["pane_id"],
                    exit_code=primary["pane_dead_status"],
                )
            pane_id = str(primary["pane_id"])

            if text_bytes:
                buffer_name = f"m5local_{secrets.token_hex(8)}"
                loaded = self._run(["load-buffer", "-b", buffer_name, "-"], input_bytes=text_bytes)
                self._require_ok(loaded, operation="load-buffer")
                try:
                    pasted = self._run(["paste-buffer", "-b", buffer_name, "-t", pane_id, "-d"])
                    self._require_ok(pasted, operation="paste-buffer")
                finally:
                    self._best_effort(["delete-buffer", "-b", buffer_name])

            sent_keys = ["Enter" if key == "C-m" else key for key in normalized_keys]
            sent_keys.extend(["Enter"] * requested_enter_count)
            if sent_keys:
                sent = self._run(["send-keys", "-t", pane_id, *sent_keys])
                self._require_ok(sent, operation="send-keys")

            return {
                "success": True,
                "socket_name": self.socket_name,
                "session": primary["session_name"],
                "pane_id": pane_id,
                "text_sent": bool(text_bytes),
                "text_bytes": len(text_bytes or b""),
                "keys_sent": ["Enter" if key == "C-m" else key for key in normalized_keys],
                "enter_count": requested_enter_count,
                "accepted_by_tmux": True,
            }
        except TmuxControlError as exc:
            return exc.as_payload()

    def kill(self, *, session: str) -> dict[str, object]:
        try:
            normalized = _validate_session_name(session)
            rows = self._session_rows(normalized)
            if not rows:
                return {
                    "success": True,
                    "socket_name": self.socket_name,
                    "session": normalized,
                    "killed": False,
                    "already_absent": True,
                }
            session_id = str(rows[0]["session_id"])
            killed = self._run(["kill-session", "-t", session_id])
            self._require_ok(killed, operation="kill-session")
            return {
                "success": True,
                "socket_name": self.socket_name,
                "session": normalized,
                "session_id": session_id,
                "killed": True,
                "already_absent": False,
            }
        except TmuxControlError as exc:
            return exc.as_payload()


def tmux_runtime_info(*, binary: str = "tmux", socket_name: str = "default") -> dict[str, object]:
    executable = shutil.which(binary)
    payload: dict[str, object] = {
        "available": executable is not None,
        "binary": executable or binary,
        "socket_name": socket_name,
        "version": None,
    }
    if executable is None:
        return payload
    env = os.environ.copy()
    env.pop("TMUX", None)
    env.pop("TMUX_PANE", None)
    try:
        completed = subprocess.run(
            [executable, "-V"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=TMUX_CONTROL_TIMEOUT_SECONDS,
            shell=False,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return payload
    if completed.returncode == 0:
        payload["version"] = _decode(completed.stdout).strip() or None
    return payload
