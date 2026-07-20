from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from pathlib import PureWindowsPath

from .response_budget import (
    DEFAULT_TOOL_OUTPUT_TOKEN_BUDGET,
    ResponseBudget,
    with_budget_metadata,
)


ALLOWED_COMMIT_MODES = {"allowed", "required", "forbidden"}
ALLOWED_REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
DEFAULT_MODEL = "default"
DEFAULT_REASONING_EFFORT = "default"
IS_WINDOWS = os.name == "nt"
TIMEOUT_EXIT_CODE = -1
DEFAULT_DELEGATE_WAIT_SECONDS = 300.0
DELEGATE_STALL_HINT_SECONDS = 180.0
DEFAULT_DELEGATE_HISTORY_LIMIT = 20
DEFAULT_DELEGATE_STATUS_POLL_SECONDS = 5.0
MAX_DELEGATE_STATUS_WATCH_SECONDS = 300.0


def _split_command(command: str) -> list[str]:
    return shlex.split(command)


def _binary_name(binary: str) -> str:
    if IS_WINDOWS:
        return PureWindowsPath(binary).stem.lower()
    return Path(binary).stem.lower()


def _resolve_delegate_command_parts(command: str) -> list[str]:
    parts = _split_command(command)
    if not IS_WINDOWS or not parts:
        return parts
    if _binary_name(parts[0]) != "codex":
        return parts
    resolved = shutil.which(parts[0])
    if resolved:
        parts[0] = resolved
    return parts


def _decode_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return value.decode("utf-8", errors="replace")


def _command_available(command: str | None) -> bool:
    if not command:
        return False
    parts = _split_command(command)
    if not parts:
        return False
    binary = parts[0]
    if Path(binary).exists():
        return True
    return shutil.which(binary) is not None


def _normalize_reasoning_effort(reasoning_effort: str | None) -> str | None:
    normalized = (reasoning_effort or "").strip().lower()
    if not normalized or normalized == DEFAULT_REASONING_EFFORT:
        return None
    return normalized


def _normalize_model(model: str | None) -> str | None:
    normalized = (model or "").strip()
    if not normalized or normalized.lower() == DEFAULT_MODEL:
        return None
    return normalized


def _result_summary(status: str, error: dict[str, object] | None) -> str:
    if error and error.get("message"):
        return f"Codex delegate {status}: {error['message']}"
    if status == "succeeded":
        return "Codex delegate succeeded. Process output is stored in logs."
    return "Codex delegate failed. Process output is stored in logs."


_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def _extract_structured_output(text: str) -> object | None:
    """Best-effort JSON extraction from delegate output."""
    stripped = (text or "").strip()
    if not stripped:
        return None

    matches = _JSON_BLOCK_RE.findall(stripped)
    if matches:
        candidate = matches[-1].strip()
        if candidate:
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass

    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def _delegate_log_root() -> Path:
    return Path(tempfile.gettempdir()) / "chatgpt-web-oauth-mcp" / "codex-delegates"


def _safe_chmod(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        pass


def _write_private_text(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    _safe_chmod(path, 0o600)


def _write_private_json(path: Path, payload: dict[str, object]) -> None:
    _write_private_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _format_epoch_seconds(value: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value))


def _status_entry_lifecycle_signature(entry: dict[str, object] | None) -> tuple[object, ...]:
    if not entry:
        return ("none",)
    error = entry.get("error")
    error_outcome: object = error
    if isinstance(error, dict):
        error_outcome = (error.get("code"), error.get("message"))
    return (
        entry.get("delegate_id"),
        entry.get("status"),
        entry.get("completed"),
        entry.get("in_progress"),
        entry.get("success"),
        entry.get("exit_code"),
        entry.get("timed_out"),
        error_outcome,
    )


def _status_response_lifecycle_signature(payload: dict[str, object]) -> tuple[object, ...]:
    if payload.get("delegate"):
        return ("delegate", *_status_entry_lifecycle_signature(payload.get("delegate")))  # type: ignore[arg-type]
    latest = payload.get("active") or payload.get("latest")
    return ("list", *_status_entry_lifecycle_signature(latest if isinstance(latest, dict) else None))


def _status_focus_entry(payload: dict[str, object]) -> dict[str, object] | None:
    delegate = payload.get("delegate")
    if isinstance(delegate, dict):
        return delegate
    active = payload.get("active")
    if isinstance(active, dict):
        return active
    latest = payload.get("latest")
    if isinstance(latest, dict):
        return latest
    return None


@dataclass(frozen=True)
class DelegateLogPaths:
    log_dir: Path
    prompt: Path
    stdout: Path
    stderr: Path
    metadata: Path

    def as_payload(self) -> dict[str, str]:
        return {
            "log_dir": str(self.log_dir),
            "prompt": str(self.prompt),
            "stdout": str(self.stdout),
            "stderr": str(self.stderr),
            "metadata": str(self.metadata),
        }


def _log_read_hint(log_paths: DelegateLogPaths) -> dict[str, object]:
    return {
        "tool": "read_text",
        "paths": [
            str(log_paths.stdout),
            str(log_paths.stderr),
            str(log_paths.metadata),
        ],
        "message": "Use read_text on stdout/stderr/metadata to inspect live delegate progress.",
    }


def _create_delegate_logs(delegate_id: str) -> DelegateLogPaths:
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    log_dir = _delegate_log_root() / f"{timestamp}-{delegate_id}"
    log_dir.mkdir(parents=True, exist_ok=False)
    _safe_chmod(log_dir, 0o700)
    return DelegateLogPaths(
        log_dir=log_dir,
        prompt=log_dir / "prompt.txt",
        stdout=log_dir / "stdout.log",
        stderr=log_dir / "stderr.log",
        metadata=log_dir / "metadata.json",
    )


def _cwd_error(cwd: Path) -> dict[str, object] | None:
    if not cwd.exists():
        return {
            "success": False,
            "status": "failed",
            "error": {
                "code": "cwd_not_found",
                "message": f"Working directory not found: {cwd}",
            },
            "cwd": str(cwd),
            "executor": "codex",
            "exit_code": TIMEOUT_EXIT_CODE,
            "summary": "",
            "timed_out": False,
            "serial": True,
        }
    if not cwd.is_dir():
        return {
            "success": False,
            "status": "failed",
            "error": {
                "code": "cwd_not_directory",
                "message": f"Working directory is not a directory: {cwd}",
            },
            "cwd": str(cwd),
            "executor": "codex",
            "exit_code": TIMEOUT_EXIT_CODE,
            "summary": "",
            "timed_out": False,
            "serial": True,
        }
    return None


def _delegate_request_fingerprint(
    *,
    task: str | None,
    goal: str | None,
    task_id: str | None,
    cwd: Path,
    files_in_scope: list[str] | None,
    out_of_scope: list[str] | None,
    context_files: list[str] | None,
    acceptance_criteria: list[str] | None,
    done_means: list[str] | None,
    verification_commands: list[str] | None,
    commit_mode: str,
    model: str | None,
    reasoning_effort: str | None,
    output_schema: dict[str, object] | None,
    parse_structured_output: bool,
) -> str:
    payload = {
        "task": task or "",
        "goal": goal or "",
        "task_id": task_id or "",
        "cwd": str(cwd),
        "files_in_scope": files_in_scope or [],
        "out_of_scope": out_of_scope or [],
        "context_files": context_files or [],
        "acceptance_criteria": acceptance_criteria or [],
        "done_means": done_means or [],
        "verification_commands": verification_commands or [],
        "commit_mode": commit_mode,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "output_schema": output_schema or None,
        "parse_structured_output": parse_structured_output,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _delegate_argument_error(
    *,
    cwd: Path,
    timeout: int,
    code: str,
    message: str,
    details: dict[str, object] | None = None,
) -> dict[str, object]:
    error: dict[str, object] = {
        "code": code,
        "message": message,
    }
    if details:
        error.update(details)
    return {
        "success": False,
        "status": "failed",
        "completed": True,
        "in_progress": False,
        "error": error,
        "cwd": str(cwd),
        "executor": "codex",
        "exit_code": TIMEOUT_EXIT_CODE,
        "summary": message,
        "timed_out": False,
        "wait_timed_out": False,
        "timeout": timeout,
        "serial": True,
    }


@dataclass(frozen=True)
class Invocation:
    args: list[str] | str
    use_shell: bool
    stdin: bytes | None = None


@dataclass
class ActiveDelegate:
    delegate_id: str
    cwd: Path
    timeout: int
    output_schema: dict[str, object] | None
    model: str | None
    reasoning_effort: str | None
    process: subprocess.Popen[bytes] | None
    completed_event: threading.Event
    started_at: float
    log_paths: DelegateLogPaths
    lock_wait_seconds: float = 0.0
    request_fingerprint: str | None = None
    request_task_id: str | None = None
    started_at_epoch: float = field(default_factory=time.time)
    result: dict[str, object] | None = None
    output_lock: threading.Lock = field(default_factory=threading.Lock)
    stdout_chunks: list[bytes] = field(default_factory=list)
    stderr_chunks: list[bytes] = field(default_factory=list)
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    last_output_at: float | None = None


class ExecutorRegistry:
    """Run one Codex delegate at a time with long-poll result checks."""

    def __init__(self, *, codex_command: str | None) -> None:
        self.codex_command = codex_command
        self._lock = threading.Lock()
        self._active: ActiveDelegate | None = None
        self._history: deque[dict[str, object]] = deque(maxlen=DEFAULT_DELEGATE_HISTORY_LIMIT)

    def run_codex(
        self,
        *,
        task: str | None,
        goal: str | None = None,
        task_id: str | None = None,
        cwd: Path,
        timeout: int,
        wait_seconds: float = DEFAULT_DELEGATE_WAIT_SECONDS,
        files_in_scope: list[str] | None = None,
        out_of_scope: list[str] | None = None,
        context_files: list[str] | None = None,
        acceptance_criteria: list[str] | None = None,
        done_means: list[str] | None = None,
        verification_commands: list[str] | None = None,
        commit_mode: str = "allowed",
        model: str | None = None,
        reasoning_effort: str | None = None,
        output_schema: dict[str, object] | None = None,
        parse_structured_output: bool = True,
    ) -> dict[str, object]:
        normalized_task = (task or "").strip()
        normalized_goal = (goal or "").strip()
        normalized_task_id = (task_id or "").strip() or None
        normalized_model = _normalize_model(model)
        normalized_reasoning_effort = _normalize_reasoning_effort(reasoning_effort)
        if normalized_reasoning_effort is not None and normalized_reasoning_effort not in ALLOWED_REASONING_EFFORTS:
            return _delegate_argument_error(
                cwd=cwd,
                timeout=timeout,
                code="unsupported_reasoning_effort",
                message=f"Unsupported reasoning_effort: {reasoning_effort}",
                details={"allowed_reasoning_efforts": [DEFAULT_REASONING_EFFORT, *ALLOWED_REASONING_EFFORTS]},
            )
        request_fingerprint = None
        if normalized_task or normalized_goal:
            request_fingerprint = _delegate_request_fingerprint(
                task=normalized_task or None,
                goal=normalized_goal or None,
                task_id=normalized_task_id,
                cwd=cwd,
                files_in_scope=files_in_scope,
                out_of_scope=out_of_scope,
                context_files=context_files,
                acceptance_criteria=acceptance_criteria,
                done_means=done_means,
                verification_commands=verification_commands,
                commit_mode=commit_mode,
                model=normalized_model,
                reasoning_effort=normalized_reasoning_effort,
                output_schema=output_schema,
                parse_structured_output=parse_structured_output,
            )
        active = self._current_active()
        if active is not None:
            if active.result is not None:
                if request_fingerprint is None or active.request_fingerprint == request_fingerprint:
                    self._remember_delegate_result(active)
                    self._clear_active(active)
                    return active.result
                self._remember_delegate_result(active)
                self._clear_active(active)
            elif request_fingerprint is not None and active.request_fingerprint != request_fingerprint:
                return self._running_result(
                    active,
                    wait_seconds=0.0,
                    attached=True,
                    request_conflict=True,
                    requested_task_id=normalized_task_id,
                )
            else:
                return self._wait_for_active(
                    active,
                    wait_seconds=wait_seconds,
                    attached=True,
                )

        if request_fingerprint is None:
            # A completed active delegate may have been cleared above; without a new
            # task/goal there is now nothing to continue waiting on.
            return _delegate_argument_error(
                cwd=cwd,
                timeout=timeout,
                code="missing_task_or_goal",
                message="delegate_task requires task or goal when no delegate is already running.",
            )

        if commit_mode not in ALLOWED_COMMIT_MODES:
            return _delegate_argument_error(
                cwd=cwd,
                timeout=timeout,
                code="unsupported_commit_mode",
                message=f"Unsupported commit_mode: {commit_mode}",
                details={"allowed_commit_modes": sorted(ALLOWED_COMMIT_MODES)},
            )

        cwd_error = _cwd_error(cwd)
        if cwd_error:
            return cwd_error
        if not _command_available(self.codex_command):
            return {
                "success": False,
                "status": "failed",
                "error": {
                    "code": "codex_unavailable",
                    "message": "Codex command is not available.",
                },
                "executor": "codex",
                "cwd": str(cwd),
                "exit_code": TIMEOUT_EXIT_CODE,
                "summary": "",
                "timed_out": False,
                "serial": True,
                "completed": True,
            }

        wait_start = time.monotonic()
        lock_wait_seconds = time.monotonic() - wait_start
        active = self._start_codex_delegate(
            task=normalized_task or None,
            goal=normalized_goal or None,
            task_id=normalized_task_id,
            request_fingerprint=request_fingerprint,
            cwd=cwd,
            timeout=timeout,
            files_in_scope=files_in_scope or [],
            out_of_scope=out_of_scope or [],
            context_files=context_files or [],
            acceptance_criteria=acceptance_criteria or [],
            done_means=done_means or [],
            verification_commands=verification_commands or [],
            commit_mode=commit_mode,
            model=normalized_model,
            reasoning_effort=normalized_reasoning_effort,
            output_schema=output_schema or None,
            parse_structured_output=parse_structured_output,
            lock_wait_seconds=lock_wait_seconds,
        )
        if active.request_fingerprint != request_fingerprint:
            if active.result is not None:
                self._remember_delegate_result(active)
                self._clear_active(active)
                return self.run_codex(
                    task=task,
                    goal=goal,
                    task_id=task_id,
                    cwd=cwd,
                    timeout=timeout,
                    wait_seconds=wait_seconds,
                    files_in_scope=files_in_scope,
                    out_of_scope=out_of_scope,
                    context_files=context_files,
                    acceptance_criteria=acceptance_criteria,
                    done_means=done_means,
                    verification_commands=verification_commands,
                    commit_mode=commit_mode,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    output_schema=output_schema,
                    parse_structured_output=parse_structured_output,
                )
            return self._running_result(
                active,
                wait_seconds=0.0,
                attached=True,
                request_conflict=True,
                requested_task_id=normalized_task_id,
            )
        if active.result is not None:
            self._remember_delegate_result(active)
            self._clear_active(active)
            return active.result
        return self._wait_for_active(
            active,
            wait_seconds=wait_seconds,
            attached=False,
        )

    def _current_active(self) -> ActiveDelegate | None:
        with self._lock:
            return self._active

    def _clear_active(self, active: ActiveDelegate) -> None:
        with self._lock:
            if self._active is active:
                self._active = None

    def _delegate_snapshot(
        self,
        active: ActiveDelegate,
        *,
        result: dict[str, object] | None = None,
    ) -> dict[str, object]:
        with active.output_lock:
            stdout_bytes = active.stdout_bytes
            stderr_bytes = active.stderr_bytes
            last_output_seconds_ago = self._last_output_seconds_ago(active)
        status = str(result.get("status")) if result else "running"
        payload: dict[str, object] = {
            "success": bool(result.get("success")) if result else True,
            "status": status,
            "completed": bool(result.get("completed")) if result else False,
            "in_progress": bool(result.get("in_progress")) if result else True,
            "executor": "codex",
            "cwd": str(active.cwd),
            "delegate_id": active.delegate_id,
            "task_id": active.request_task_id,
            "request_fingerprint": active.request_fingerprint,
            "serial": True,
            "started_at": _format_epoch_seconds(active.started_at_epoch),
            "started_at_epoch": active.started_at_epoch,
            "elapsed_seconds": round(time.monotonic() - active.started_at, 3),
            "timeout": active.timeout,
            "stdout_bytes": stdout_bytes,
            "stderr_bytes": stderr_bytes,
            "last_output_seconds_ago": last_output_seconds_ago,
            "logs": active.log_paths.as_payload(),
            "log_read_hint": _log_read_hint(active.log_paths),
            "output_omitted": True,
        }
        if active.model is not None:
            payload["model"] = active.model
        if active.reasoning_effort is not None:
            payload["reasoning_effort"] = active.reasoning_effort
        if result:
            for key in [
                "exit_code",
                "summary",
                "timed_out",
                "wait_timed_out",
                "lock_wait_seconds",
                "duration_seconds",
                "soft_timeout_elapsed",
                "structured_output",
                "output_schema",
                "error",
            ]:
                if key in result:
                    payload[key] = result[key]
        else:
            payload.update(
                {
                    "pid": getattr(active.process, "pid", None),
                    "activity_state": self._activity_state(active, last_output_seconds_ago),
                    "timed_out": False,
                    "wait_timed_out": False,
                    "soft_timeout_elapsed": (time.monotonic() - active.started_at) >= active.timeout,
                }
            )
        return payload

    def _remember_delegate_result(self, active: ActiveDelegate) -> None:
        if active.result is None:
            return
        snapshot = self._delegate_snapshot(active, result=active.result)
        with self._lock:
            maxlen = self._history.maxlen or DEFAULT_DELEGATE_HISTORY_LIMIT
            self._history = deque(
                (
                    item
                    for item in self._history
                    if item.get("delegate_id") != active.delegate_id
                ),
                maxlen=maxlen,
            )
            self._history.append(snapshot)

    def _delegate_status_once(
        self,
        *,
        delegate_id: str | None,
        limit: int,
        offset: int = 0,
    ) -> dict[str, object]:
        limit = max(1, min(int(limit), DEFAULT_DELEGATE_HISTORY_LIMIT))
        offset = max(0, int(offset))
        with self._lock:
            active = self._active
            history = list(self._history)
        active_snapshot = None
        if active is not None:
            active_snapshot = self._delegate_snapshot(active, result=active.result)

        if delegate_id:
            normalized_delegate_id = delegate_id.strip()
            if active_snapshot and active_snapshot.get("delegate_id") == normalized_delegate_id:
                return {"success": True, "delegate": active_snapshot}
            for item in reversed(history):
                if item.get("delegate_id") == normalized_delegate_id:
                    return {"success": True, "delegate": item}
            return {
                "success": False,
                "error": {
                    "code": "delegate_not_found",
                    "message": f"Delegate not found: {normalized_delegate_id}",
                },
                "active": active_snapshot if active_snapshot and active_snapshot.get("in_progress") else None,
                "recent": list(reversed(history))[offset : offset + limit],
            }

        all_recent: list[dict[str, object]] = []
        seen: set[str] = set()
        if active_snapshot is not None:
            all_recent.append(active_snapshot)
            seen.add(str(active_snapshot.get("delegate_id")))
        for item in reversed(history):
            item_id = str(item.get("delegate_id"))
            if item_id in seen:
                continue
            all_recent.append(item)
            seen.add(item_id)
        recent = all_recent[offset : offset + limit]
        active_running = active_snapshot if active_snapshot and active_snapshot.get("in_progress") else None
        return {
            "success": True,
            "active": active_running,
            "latest": all_recent[0] if all_recent else None,
            "recent": recent,
            "history_limit": DEFAULT_DELEGATE_HISTORY_LIMIT,
            "truncated": offset + len(recent) < len(all_recent),
            "next_offset": offset + len(recent) if offset + len(recent) < len(all_recent) else None,
        }

    def _budget_delegate_status(
        self,
        payload: dict[str, object],
        *,
        max_tokens: int,
        offset: int,
    ) -> dict[str, object]:
        budget = ResponseBudget(max_tokens=max_tokens)
        truncated = bool(payload.get("truncated"))
        rendered, measurement = with_budget_metadata(
            payload,
            budget=budget,
            truncated=truncated,
            stop_reason="limit" if truncated else "end_of_results",
        )
        recent = rendered.get("recent")
        while not measurement.fits and isinstance(recent, list) and recent:
            recent.pop()
            rendered["next_offset"] = offset + len(recent)
            rendered, measurement = with_budget_metadata(
                rendered,
                budget=budget,
                truncated=True,
                stop_reason="token_budget",
            )
            recent = rendered.get("recent")
        return rendered

    def delegate_status(
        self,
        *,
        delegate_id: str | None = None,
        limit: int = 10,
        offset: int = 0,
        watch_seconds: float = 0.0,
        poll_seconds: float = DEFAULT_DELEGATE_STATUS_POLL_SECONDS,
        max_tokens: int = DEFAULT_TOOL_OUTPUT_TOKEN_BUDGET,
    ) -> dict[str, object]:
        watch_seconds = max(0.0, min(float(watch_seconds), MAX_DELEGATE_STATUS_WATCH_SECONDS))
        poll_seconds = max(0.1, min(float(poll_seconds), 60.0))
        def finalize(payload: dict[str, object]) -> dict[str, object]:
            return self._budget_delegate_status(
                payload,
                max_tokens=max_tokens,
                offset=max(0, int(offset)),
            )

        def status_once() -> dict[str, object]:
            if offset:
                return self._delegate_status_once(
                    delegate_id=delegate_id,
                    limit=limit,
                    offset=offset,
                )
            return self._delegate_status_once(delegate_id=delegate_id, limit=limit)

        initial = status_once()
        if watch_seconds <= 0:
            return finalize(initial)

        started_at = time.monotonic()
        if initial.get("success") is False:
            initial["watch"] = {
                "enabled": True,
                "status_changed": False,
                "timed_out": False,
                "error_returned": True,
                "watch_seconds": watch_seconds,
                "poll_seconds": poll_seconds,
                "elapsed_seconds": round(time.monotonic() - started_at, 3),
            }
            return finalize(initial)

        initial_focus = _status_focus_entry(initial)
        if initial_focus and initial_focus.get("completed"):
            initial["watch"] = {
                "enabled": True,
                "status_changed": False,
                "timed_out": False,
                "already_terminal": True,
                "watch_seconds": watch_seconds,
                "poll_seconds": poll_seconds,
                "elapsed_seconds": round(time.monotonic() - started_at, 3),
            }
            return finalize(initial)

        deadline = started_at + watch_seconds
        initial_signature = _status_response_lifecycle_signature(initial)
        current = initial
        while time.monotonic() < deadline:
            sleep_for = min(poll_seconds, max(0.0, deadline - time.monotonic()))
            if sleep_for > 0:
                time.sleep(sleep_for)
            current = status_once()
            if _status_response_lifecycle_signature(current) != initial_signature:
                current["watch"] = {
                    "enabled": True,
                    "status_changed": True,
                    "timed_out": False,
                    "watch_seconds": watch_seconds,
                    "poll_seconds": poll_seconds,
                    "elapsed_seconds": round(time.monotonic() - started_at, 3),
                }
                return finalize(current)

        current["watch"] = {
            "enabled": True,
            "status_changed": False,
            "timed_out": True,
            "watch_seconds": watch_seconds,
            "poll_seconds": poll_seconds,
            "elapsed_seconds": round(time.monotonic() - started_at, 3),
        }
        return finalize(current)

    def _wait_for_active(
        self,
        active: ActiveDelegate,
        *,
        wait_seconds: float,
        attached: bool,
    ) -> dict[str, object]:
        wait_seconds = min(max(float(wait_seconds), 0.0), max(float(active.timeout), 0.0))
        active.completed_event.wait(timeout=wait_seconds)
        if active.result is not None:
            self._remember_delegate_result(active)
            self._clear_active(active)
            return active.result
        return self._running_result(
            active,
            wait_seconds=wait_seconds,
            attached=attached,
        )

    def _start_codex_delegate(
        self,
        *,
        task: str | None,
        goal: str | None,
        task_id: str | None,
        request_fingerprint: str,
        cwd: Path,
        timeout: int,
        files_in_scope: list[str],
        out_of_scope: list[str],
        context_files: list[str],
        acceptance_criteria: list[str],
        done_means: list[str],
        verification_commands: list[str],
        commit_mode: str,
        model: str | None,
        reasoning_effort: str | None,
        output_schema: dict[str, object] | None,
        parse_structured_output: bool,
        lock_wait_seconds: float,
    ) -> ActiveDelegate:
        try:
            with self._lock:
                if self._active is not None:
                    if (
                        self._active.result is not None
                        and self._active.request_fingerprint != request_fingerprint
                    ):
                        self._active = None
                    else:
                        return self._active
                active = self._start_codex_delegate_impl(
                    task=task,
                    goal=goal,
                    task_id=task_id,
                    request_fingerprint=request_fingerprint,
                    cwd=cwd,
                    timeout=timeout,
                    files_in_scope=files_in_scope,
                    out_of_scope=out_of_scope,
                    context_files=context_files,
                    acceptance_criteria=acceptance_criteria,
                    done_means=done_means,
                    verification_commands=verification_commands,
                    commit_mode=commit_mode,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    output_schema=output_schema,
                    parse_structured_output=parse_structured_output,
                    lock_wait_seconds=lock_wait_seconds,
                )
                self._active = active
        except OSError as exc:
            completed = threading.Event()
            delegate_id = uuid.uuid4().hex[:12]
            log_paths = _create_delegate_logs(delegate_id)
            _write_private_json(
                log_paths.metadata,
                {
                    "delegate_id": delegate_id,
                    "status": "failed",
                    "error": "process_start_failed",
                    "cwd": str(cwd),
                    "timeout": timeout,
                    "task_id": task_id,
                    "model": model,
                    "reasoning_effort": reasoning_effort,
                    "request_fingerprint": request_fingerprint,
                    "started_at_monotonic": time.monotonic(),
                },
            )
            active = ActiveDelegate(
                delegate_id=delegate_id,
                cwd=cwd,
                timeout=timeout,
                output_schema=output_schema,
                model=model,
                reasoning_effort=reasoning_effort,
                process=None,
                completed_event=completed,
                started_at=time.monotonic(),
                log_paths=log_paths,
                lock_wait_seconds=lock_wait_seconds,
                request_fingerprint=request_fingerprint,
                request_task_id=task_id,
            )
            active.result = self._result(
                status="failed",
                exit_code=TIMEOUT_EXIT_CODE,
                stdout="",
                stderr=str(exc),
                timed_out=False,
                cwd=cwd,
                timeout=timeout,
                output_schema=output_schema,
                structured_output=None,
                lock_wait_seconds=lock_wait_seconds,
                duration_seconds=0.0,
                delegate_id=active.delegate_id,
                log_paths=active.log_paths,
                task_id=task_id,
                model=model,
                reasoning_effort=reasoning_effort,
                request_fingerprint=request_fingerprint,
                error={"code": "process_start_failed", "message": str(exc)},
            )
            completed.set()
            return active
        thread = threading.Thread(
            target=self._communicate_active,
            args=(active, parse_structured_output),
            daemon=True,
        )
        thread.start()
        return active

    def _start_codex_delegate_impl(
        self,
        *,
        task: str | None,
        goal: str | None,
        task_id: str | None,
        request_fingerprint: str,
        cwd: Path,
        timeout: int,
        files_in_scope: list[str],
        out_of_scope: list[str],
        context_files: list[str],
        acceptance_criteria: list[str],
        done_means: list[str],
        verification_commands: list[str],
        commit_mode: str,
        model: str | None,
        reasoning_effort: str | None,
        output_schema: dict[str, object] | None,
        parse_structured_output: bool,
        lock_wait_seconds: float,
    ) -> ActiveDelegate:
        prompt = self._build_prompt(
            task=task,
            goal=goal,
            task_id=task_id,
            files_in_scope=files_in_scope,
            out_of_scope=out_of_scope,
            context_files=context_files,
            acceptance_criteria=acceptance_criteria,
            done_means=done_means,
            verification_commands=verification_commands,
            commit_mode=commit_mode,
        )
        invocation = self._build_invocation_from_prompt(
            command=self.codex_command or "",
            cwd=cwd,
            prompt=prompt,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        delegate_id = uuid.uuid4().hex[:12]
        log_paths = _create_delegate_logs(delegate_id)
        _write_private_text(log_paths.prompt, prompt)
        _write_private_json(
            log_paths.metadata,
            {
                "delegate_id": delegate_id,
                "status": "started",
                "cwd": str(cwd),
                "timeout": timeout,
                "commit_mode": commit_mode,
                "model": model,
                "reasoning_effort": reasoning_effort,
                "task_id": task_id,
                "request_fingerprint": request_fingerprint,
                "files_in_scope": files_in_scope,
                "out_of_scope": out_of_scope,
                "context_files": context_files,
                "acceptance_criteria": acceptance_criteria,
                "done_means": done_means,
                "verification_commands": verification_commands,
                "started_at_monotonic": time.monotonic(),
                "command_kind": "shell" if invocation.use_shell else "argv",
            },
        )
        started_at = time.monotonic()
        try:
            process = subprocess.Popen(
                invocation.args,
                cwd=str(cwd),
                shell=invocation.use_shell,
                text=False,
                stdin=subprocess.PIPE if invocation.stdin is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            completed = threading.Event()
            active = ActiveDelegate(
                delegate_id=delegate_id,
                cwd=cwd,
                timeout=timeout,
                output_schema=output_schema,
                model=model,
                reasoning_effort=reasoning_effort,
                process=None,
                completed_event=completed,
                started_at=started_at,
                log_paths=log_paths,
                lock_wait_seconds=lock_wait_seconds,
                request_fingerprint=request_fingerprint,
                request_task_id=task_id,
            )
            active.result = self._result(
                status="failed",
                exit_code=TIMEOUT_EXIT_CODE,
                stdout="",
                stderr=str(exc),
                timed_out=False,
                cwd=cwd,
                timeout=timeout,
                output_schema=output_schema,
                structured_output=None,
                lock_wait_seconds=lock_wait_seconds,
                duration_seconds=0.0,
                delegate_id=delegate_id,
                log_paths=log_paths,
                task_id=task_id,
                model=model,
                reasoning_effort=reasoning_effort,
                request_fingerprint=request_fingerprint,
                error={"code": "process_start_failed", "message": str(exc)},
            )
            _write_private_json(
                log_paths.metadata,
                {
                    "delegate_id": delegate_id,
                    "status": "failed",
                    "error": {"code": "process_start_failed", "message": str(exc)},
                    "cwd": str(cwd),
                    "timeout": timeout,
                    "task_id": task_id,
                    "model": model,
                    "reasoning_effort": reasoning_effort,
                    "request_fingerprint": request_fingerprint,
                    "logs": log_paths.as_payload(),
                },
            )
            completed.set()
            return active
        if invocation.stdin is not None and getattr(process, "stdin", None) is not None:
            threading.Thread(
                target=self._write_process_stdin,
                args=(process, invocation.stdin),
                daemon=True,
            ).start()
        return ActiveDelegate(
            delegate_id=delegate_id,
            cwd=cwd,
            timeout=timeout,
            output_schema=output_schema,
            model=model,
            reasoning_effort=reasoning_effort,
            process=process,
            completed_event=threading.Event(),
            started_at=started_at,
            log_paths=log_paths,
            lock_wait_seconds=lock_wait_seconds,
            request_fingerprint=request_fingerprint,
            request_task_id=task_id,
        )

    def _build_invocation_from_prompt(
        self,
        *,
        command: str,
        cwd: Path,
        prompt: str,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> Invocation:
        parts = _resolve_delegate_command_parts(command)
        if parts and _binary_name(parts[0]) == "codex":
            args = [*parts, "exec"]
            if model:
                args.extend(["--model", model])
            if reasoning_effort:
                args.extend(["-c", f"model_reasoning_effort={json.dumps(reasoning_effort)}"])
            args.extend(
                [
                    "--dangerously-bypass-approvals-and-sandbox",
                    "-C",
                    str(cwd),
                ]
            )
            if not (cwd / ".git").exists():
                args.append("--skip-git-repo-check")
            args.append("-")
            return Invocation(args=args, use_shell=False, stdin=prompt.encode("utf-8"))
        return Invocation(args=command, use_shell=True)

    def _write_process_stdin(self, process: subprocess.Popen[bytes], payload: bytes) -> None:
        if process.stdin is None:
            return
        try:
            process.stdin.write(payload)
            process.stdin.close()
        except OSError:
            pass

    def _communicate_active(self, active: ActiveDelegate, parse_structured_output: bool) -> None:
        if active.process is None:
            active.completed_event.set()
            return
        stdout_stream = getattr(active.process, "stdout", None)
        stderr_stream = getattr(active.process, "stderr", None)
        stdout_thread: threading.Thread | None = None
        stderr_thread: threading.Thread | None = None
        soft_timeout_elapsed = False
        try:
            if stdout_stream is None or stderr_stream is None:
                stdout_raw, stderr_raw = active.process.communicate(timeout=active.timeout)
                self._record_fallback_output(active, "stdout", stdout_raw, active.log_paths.stdout)
                self._record_fallback_output(active, "stderr", stderr_raw, active.log_paths.stderr)
            else:
                active.log_paths.stdout.touch()
                active.log_paths.stderr.touch()
                _safe_chmod(active.log_paths.stdout, 0o600)
                _safe_chmod(active.log_paths.stderr, 0o600)
                stdout_thread = threading.Thread(
                    target=self._read_stream_to_log,
                    args=(active, "stdout", stdout_stream, active.log_paths.stdout),
                    daemon=True,
                )
                stderr_thread = threading.Thread(
                    target=self._read_stream_to_log,
                    args=(active, "stderr", stderr_stream, active.log_paths.stderr),
                    daemon=True,
                )
                stdout_thread.start()
                stderr_thread.start()
                active.process.wait(timeout=active.timeout)
                stdout_thread.join(timeout=5)
                stderr_thread.join(timeout=5)
                stdout_raw = b"".join(active.stdout_chunks)
                stderr_raw = b"".join(active.stderr_chunks)
        except subprocess.TimeoutExpired:
            soft_timeout_elapsed = True
            self._mark_active_running_after_soft_timeout(active)
            if stdout_stream is None or stderr_stream is None:
                stdout_raw, stderr_raw = active.process.communicate()
                self._record_fallback_output(active, "stdout", stdout_raw, active.log_paths.stdout)
                self._record_fallback_output(active, "stderr", stderr_raw, active.log_paths.stderr)
            else:
                active.process.wait()
                if stdout_thread is not None:
                    stdout_thread.join(timeout=5)
                if stderr_thread is not None:
                    stderr_thread.join(timeout=5)
                stdout_raw = b"".join(active.stdout_chunks)
                stderr_raw = b"".join(active.stderr_chunks)
        timed_out = False

        stdout = _decode_output(stdout_raw)
        stderr = _decode_output(stderr_raw)
        structured_output = None
        if parse_structured_output:
            structured_output = _extract_structured_output(stdout) or _extract_structured_output(stderr)

        exit_code = active.process.returncode if not timed_out else TIMEOUT_EXIT_CODE
        status = "succeeded" if exit_code == 0 else "failed"
        error = None
        if timed_out:
            error = {
                "code": "timed_out",
                "message": f"Codex delegate exceeded the {active.timeout}s timeout.",
            }

        active.result = self._result(
            status=status,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            cwd=active.cwd,
            timeout=active.timeout,
            output_schema=active.output_schema,
            structured_output=structured_output,
            lock_wait_seconds=active.lock_wait_seconds,
            duration_seconds=time.monotonic() - active.started_at,
            delegate_id=active.delegate_id,
            log_paths=active.log_paths,
            task_id=active.request_task_id,
            model=active.model,
            reasoning_effort=active.reasoning_effort,
            request_fingerprint=active.request_fingerprint,
            error=error,
            soft_timeout_elapsed=soft_timeout_elapsed,
        )
        self._remember_delegate_result(active)
        self._finalize_log_metadata(
            active=active,
            status=status,
            exit_code=exit_code,
            timed_out=timed_out,
            duration_seconds=time.monotonic() - active.started_at,
            error=error,
        )
        active.completed_event.set()

    def _mark_active_running_after_soft_timeout(self, active: ActiveDelegate) -> None:
        payload: dict[str, object] = {
            "delegate_id": active.delegate_id,
            "status": "running",
            "timed_out": False,
            "soft_timeout_elapsed": True,
            "cwd": str(active.cwd),
            "timeout": active.timeout,
            "task_id": active.request_task_id,
            "model": active.model,
            "reasoning_effort": active.reasoning_effort,
            "request_fingerprint": active.request_fingerprint,
            "elapsed_seconds": round(time.monotonic() - active.started_at, 3),
            "stdout_bytes": active.stdout_bytes,
            "stderr_bytes": active.stderr_bytes,
            "last_output_seconds_ago": self._last_output_seconds_ago(active),
            "logs": active.log_paths.as_payload(),
            "log_read_hint": _log_read_hint(active.log_paths),
            "message": "Codex delegate exceeded the MCP wait timeout but is still running.",
        }
        if active.process is not None and getattr(active.process, "pid", None) is not None:
            payload["pid"] = active.process.pid
        _write_private_json(active.log_paths.metadata, payload)

    def _read_stream_to_log(self, active: ActiveDelegate, stream_name: str, stream, path: Path) -> None:
        chunks = active.stdout_chunks if stream_name == "stdout" else active.stderr_chunks
        with path.open("ab") as handle:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    break
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8", errors="replace")
                handle.write(chunk)
                handle.flush()
                with active.output_lock:
                    chunks.append(chunk)
                    if stream_name == "stdout":
                        active.stdout_bytes += len(chunk)
                    else:
                        active.stderr_bytes += len(chunk)
                    active.last_output_at = time.monotonic()

    def _record_fallback_output(
        self,
        active: ActiveDelegate,
        stream_name: str,
        value: str | bytes | None,
        path: Path,
    ) -> None:
        chunk = value or b""
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8", errors="replace")
        path.write_bytes(chunk)
        _safe_chmod(path, 0o600)
        chunks = active.stdout_chunks if stream_name == "stdout" else active.stderr_chunks
        with active.output_lock:
            chunks.append(chunk)
            if stream_name == "stdout":
                active.stdout_bytes += len(chunk)
            else:
                active.stderr_bytes += len(chunk)
            if chunk:
                active.last_output_at = time.monotonic()

    def _finalize_log_metadata(
        self,
        *,
        active: ActiveDelegate,
        status: str,
        exit_code: int,
        timed_out: bool,
        duration_seconds: float,
        error: dict[str, object] | None,
    ) -> None:
        payload: dict[str, object] = {
            "delegate_id": active.delegate_id,
            "status": status,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "soft_timeout_elapsed": duration_seconds >= active.timeout,
            "cwd": str(active.cwd),
            "timeout": active.timeout,
            "task_id": active.request_task_id,
            "model": active.model,
            "reasoning_effort": active.reasoning_effort,
            "request_fingerprint": active.request_fingerprint,
            "duration_seconds": round(duration_seconds, 3),
            "stdout_bytes": active.stdout_bytes,
            "stderr_bytes": active.stderr_bytes,
            "last_output_seconds_ago": self._last_output_seconds_ago(active),
            "logs": active.log_paths.as_payload(),
            "log_read_hint": _log_read_hint(active.log_paths),
        }
        if active.process is not None and getattr(active.process, "pid", None) is not None:
            payload["pid"] = active.process.pid
        if error is not None:
            payload["error"] = error
        _write_private_json(active.log_paths.metadata, payload)

    def _running_result(
        self,
        active: ActiveDelegate,
        *,
        wait_seconds: float,
        attached: bool,
        request_conflict: bool = False,
        requested_task_id: str | None = None,
    ) -> dict[str, object]:
        quiet_seconds = self._last_output_seconds_ago(active)
        activity_state = "active"
        if active.last_output_at is None:
            activity_state = "starting_or_quiet"
        if quiet_seconds is not None and quiet_seconds >= DELEGATE_STALL_HINT_SECONDS:
            activity_state = "suspected_stalled"
        payload: dict[str, object] = {
            "success": True,
            "status": "running",
            "in_progress": True,
            "completed": False,
            "executor": "codex",
            "cwd": str(active.cwd),
            "delegate_id": active.delegate_id,
            "task_id": active.request_task_id,
            "request_fingerprint": active.request_fingerprint,
            "pid": getattr(active.process, "pid", None),
            "serial": True,
            "attached_to_running_delegate": attached,
            "elapsed_seconds": round(time.monotonic() - active.started_at, 3),
            "activity_state": activity_state,
            "last_output_seconds_ago": quiet_seconds,
            "stdout_bytes": active.stdout_bytes,
            "stderr_bytes": active.stderr_bytes,
            "logs": active.log_paths.as_payload(),
            "log_read_hint": _log_read_hint(active.log_paths),
            "wait_seconds": round(wait_seconds, 3),
            "timeout": active.timeout,
            "timed_out": False,
            "wait_timed_out": True,
            "soft_timeout_elapsed": (time.monotonic() - active.started_at) >= active.timeout,
            "message": "Codex delegate is still running. Call delegate_task again to continue waiting.",
            "next": "call delegate_task again without task/goal, or repeat the same delegate_task call",
        }
        if active.model is not None:
            payload["model"] = active.model
        if active.reasoning_effort is not None:
            payload["reasoning_effort"] = active.reasoning_effort
        if request_conflict:
            payload.update(
                {
                    "request_conflict": True,
                    "new_task_started": False,
                    "requested_task_id": requested_task_id,
                    "message": (
                        "Another Codex delegate is already running; the requested new task was "
                        "not started."
                    ),
                    "next": "call delegate_task without task/goal to wait for the active delegate, then retry the new task",
                }
            )
        return payload

    def _result(
        self,
        *,
        status: str,
        exit_code: int,
        stdout: str,
        stderr: str,
        timed_out: bool,
        cwd: Path,
        timeout: int,
        output_schema: dict[str, object] | None,
        structured_output: object | None,
        lock_wait_seconds: float,
        duration_seconds: float,
        delegate_id: str,
        log_paths: DelegateLogPaths,
        task_id: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        request_fingerprint: str | None = None,
        error: dict[str, object] | None = None,
        soft_timeout_elapsed: bool = False,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "success": status == "succeeded",
            "status": status,
            "completed": True,
            "in_progress": False,
            "executor": "codex",
            "cwd": str(cwd),
            "delegate_id": delegate_id,
            "logs": log_paths.as_payload(),
            "log_read_hint": _log_read_hint(log_paths),
            "exit_code": exit_code,
            "summary": _result_summary(status, error),
            "output_omitted": True,
            "timed_out": timed_out,
            "wait_timed_out": False,
            "timeout": timeout,
            "serial": True,
            "lock_wait_seconds": round(lock_wait_seconds, 3),
            "duration_seconds": round(duration_seconds, 3),
            "soft_timeout_elapsed": soft_timeout_elapsed,
            "structured_output": structured_output,
            "output_schema": output_schema,
        }
        if task_id is not None:
            payload["task_id"] = task_id
        if model is not None:
            payload["model"] = model
        if reasoning_effort is not None:
            payload["reasoning_effort"] = reasoning_effort
        if request_fingerprint is not None:
            payload["request_fingerprint"] = request_fingerprint
        if error is not None:
            payload["error"] = error
        return payload

    def _last_output_seconds_ago(self, active: ActiveDelegate) -> float | None:
        reference = active.last_output_at or active.started_at
        return round(time.monotonic() - reference, 3)

    def _activity_state(self, active: ActiveDelegate, quiet_seconds: float | None) -> str:
        if active.last_output_at is None:
            return "starting_or_quiet"
        if quiet_seconds is not None and quiet_seconds >= DELEGATE_STALL_HINT_SECONDS:
            return "suspected_stalled"
        return "active"

    def _build_invocation(
        self,
        *,
        command: str,
        task: str | None,
        goal: str | None,
        task_id: str | None = None,
        cwd: Path,
        files_in_scope: list[str] | None = None,
        out_of_scope: list[str] | None = None,
        context_files: list[str] | None = None,
        acceptance_criteria: list[str] | None = None,
        done_means: list[str] | None = None,
        verification_commands: list[str] | None = None,
        commit_mode: str,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> Invocation:
        prompt = self._build_prompt(
            task=task,
            goal=goal,
            task_id=task_id,
            files_in_scope=files_in_scope or [],
            out_of_scope=out_of_scope or [],
            context_files=context_files or [],
            acceptance_criteria=acceptance_criteria or [],
            done_means=done_means or [],
            verification_commands=verification_commands or [],
            commit_mode=commit_mode,
        )
        return self._build_invocation_from_prompt(
            command=command,
            cwd=cwd,
            prompt=prompt,
            model=_normalize_model(model),
            reasoning_effort=_normalize_reasoning_effort(reasoning_effort),
        )

    def _build_prompt(
        self,
        *,
        task: str | None,
        goal: str | None,
        task_id: str | None = None,
        files_in_scope: list[str] | None = None,
        out_of_scope: list[str] | None = None,
        context_files: list[str],
        acceptance_criteria: list[str],
        done_means: list[str] | None = None,
        verification_commands: list[str],
        commit_mode: str,
    ) -> str:
        lines: list[str] = [
            "Architecture contract:",
            "- ChatGPT Web is the architect/manager/reviewer.",
            "- Codex is the local executor for exactly one bounded execution slice.",
            "- Execute only the scoped task below; do not expand into a broad planning or research loop.",
            "- If the task is too broad or underspecified, stop and report blocked with the smallest useful next execution prompt.",
            "",
        ]
        if task_id:
            lines.extend(["Task ID:", task_id, ""])
        if goal:
            lines.extend(["Goal:", goal, ""])
        if task:
            lines.extend(["Task:", task, ""])
        if files_in_scope:
            lines.append("Files in scope:")
            lines.extend(f"- {item}" for item in files_in_scope)
            lines.append("")
        if out_of_scope:
            lines.append("Out of scope:")
            lines.extend(f"- {item}" for item in out_of_scope)
            lines.append("")
        if acceptance_criteria:
            lines.append("Acceptance criteria:")
            lines.extend(f"- {item}" for item in acceptance_criteria)
            lines.append("")
        if done_means:
            lines.append("Done means:")
            lines.extend(f"- {item}" for item in done_means)
            lines.append("")
        if verification_commands:
            lines.append("Verification commands:")
            lines.extend(f"- {item}" for item in verification_commands)
            lines.append("")
        lines.append(f"Commit mode: {commit_mode}")
        if context_files:
            lines.extend(["", "Context files:"])
            lines.extend(f"- {path}" for path in context_files)
        lines.extend(
            [
                "",
                "Progress logging contract:",
                "- Print concise progress updates to stderr as work advances, especially before long-running commands.",
                "- Keep stdout quiet unless emitting the compact final manifest or requested structured JSON.",
                "- The MCP server persists stdout/stderr to local delegate logs, and the caller may inspect them with read_text while status=running.",
                "",
                "Output contract:",
                "- Return a compact execution manifest: status, files changed, commands run, verification result, deviations or blockers.",
                "- Do not claim done unless the acceptance criteria passed locally, or clearly state which checks were not run.",
                "- Suggest at most one next small execution prompt if more work remains.",
            ]
        )
        return "\n".join(lines)
