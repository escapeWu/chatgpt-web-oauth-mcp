from __future__ import annotations

import base64
from collections import deque
from fnmatch import fnmatch
import json
from pathlib import Path
import subprocess
import threading
from typing import Any

from .files import (
    DEFAULT_EXCLUDE_DIR_NAMES,
    _find_git_root,
    _git_tracked_allowed_paths,
    _iter_filtered,
)
from .response_budget import (
    DEFAULT_TOOL_OUTPUT_TOKEN_BUDGET,
    ResponseBudget,
    with_budget_metadata,
)


_MAX_RIPGREP_EVENT_BYTES = 1024 * 1024
_MAX_RIPGREP_STDERR_BYTES = 64 * 1024
_MAX_SEARCH_CONTEXT_LINES = 1000
_MAX_PENDING_CONTENT_MATCHES = 1000


def _error(code: str, message: str, **extra: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "success": False,
        "error": {
            "code": code,
            "message": message,
        },
    }
    payload.update(extra)
    return payload


def _validate_existing_path(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return _error(
            "path_not_found",
            f"Path not found: {path}",
            resolved_path=str(path),
        )
    return None


def _validate_directory(path: Path) -> dict[str, object] | None:
    validation_error = _validate_existing_path(path)
    if validation_error:
        return validation_error
    if not path.is_dir():
        return _error(
            "not_a_directory",
            f"Path is not a directory: {path}",
            resolved_path=str(path),
        )
    return None


def _paginate(items: list[object], *, offset: int, limit: int) -> tuple[list[object], bool, int | None]:
    start = max(offset, 0)
    if limit == 0:
        selected = items[start:]
    else:
        selected = items[start : start + max(limit, 0)]
    truncated = start + len(selected) < len(items)
    next_offset = start + len(selected) if truncated else None
    return selected, truncated, next_offset


def _resolve_allowed_paths(base_path: Path, *, respect_gitignore: bool) -> tuple[set[Path] | None, bool]:
    if not respect_gitignore:
        return None, False

    repo_root = _find_git_root(base_path)
    if repo_root is None:
        return None, False

    allowed = _git_tracked_allowed_paths(repo_root)
    return allowed, allowed is not None


def _matches_exclude_patterns(
    path: Path,
    *,
    base_path: Path,
    exclude_patterns: tuple[str, ...],
) -> bool:
    if not exclude_patterns:
        return False
    try:
        relative = str(path.relative_to(base_path if base_path.is_dir() else base_path.parent))
    except ValueError:
        relative = path.name
    return any(fnmatch(path.name, pattern) or fnmatch(relative, pattern) for pattern in exclude_patterns)


def _glob_matches(path: Path, *, base_path: Path, pattern: str) -> bool:
    try:
        relative = str(path.relative_to(base_path if base_path.is_dir() else base_path.parent))
    except ValueError:
        relative = path.name
    return fnmatch(relative, pattern) or fnmatch(path.name, pattern)


def _iter_matching_entries(
    base_path: Path,
    *,
    pattern: str,
    include_hidden: bool,
    respect_gitignore: bool,
    exclude_patterns: tuple[str, ...],
) -> tuple[list[Path], bool]:
    allowed, gitignore_applied = _resolve_allowed_paths(
        base_path,
        respect_gitignore=respect_gitignore,
    )

    if base_path.is_file():
        if not include_hidden and base_path.name.startswith("."):
            return [], gitignore_applied
        if _matches_exclude_patterns(base_path, base_path=base_path, exclude_patterns=exclude_patterns):
            return [], gitignore_applied
        if allowed is not None and base_path.resolve() not in allowed:
            return [], gitignore_applied
        return ([base_path] if _glob_matches(base_path, base_path=base_path, pattern=pattern) else []), gitignore_applied

    entries = sorted(
        _iter_filtered(
            base_path,
            recursive=True,
            include_hidden=include_hidden,
            exclude_dir_names=DEFAULT_EXCLUDE_DIR_NAMES,
            exclude_patterns=exclude_patterns,
            allowed=allowed,
        ),
        key=lambda item: str(item),
    )
    matches = [entry for entry in entries if _glob_matches(entry, base_path=base_path, pattern=pattern)]
    return matches, gitignore_applied


def _iter_matching_files(
    base_path: Path,
    *,
    glob_pattern: str | None,
    include_hidden: bool,
    respect_gitignore: bool,
    exclude_patterns: tuple[str, ...],
) -> tuple[list[Path], bool]:
    pattern = glob_pattern or "*"
    entries, gitignore_applied = _iter_matching_entries(
        base_path,
        pattern=pattern,
        include_hidden=include_hidden,
        respect_gitignore=respect_gitignore,
        exclude_patterns=exclude_patterns,
    )
    return [path for path in entries if path.is_file()], gitignore_applied


def glob_files(
    base_path: Path,
    *,
    pattern: str,
    limit: int,
    offset: int,
    include_hidden: bool = False,
    respect_gitignore: bool = True,
    exclude_patterns: list[str] | tuple[str, ...] | None = None,
    max_tokens: int = DEFAULT_TOOL_OUTPUT_TOKEN_BUDGET,
) -> dict[str, object]:
    validation_error = _validate_existing_path(base_path)
    if validation_error:
        return validation_error
    if limit < 0 or offset < 0:
        return _error("invalid_arguments", "limit and offset must be non-negative integers.")

    matches, gitignore_applied = _iter_matching_entries(
        base_path,
        pattern=pattern,
        include_hidden=include_hidden,
        respect_gitignore=respect_gitignore,
        exclude_patterns=tuple(exclude_patterns or ()),
    )
    base_payload: dict[str, object] = {
        "success": True,
        "base_path": str(base_path),
        "pattern": pattern,
        "filters": {
            "include_hidden": include_hidden,
            "respect_gitignore": respect_gitignore,
            "gitignore_applied": gitignore_applied,
            "exclude_patterns": list(exclude_patterns or ()),
        },
    }
    page = _PageCollector(
        offset=offset,
        limit=limit,
        result_key="matches",
        base_payload=base_payload,
        budget=ResponseBudget(max_tokens=max_tokens),
    )
    for path in matches:
        page.add({"path": str(path), "is_dir": path.is_dir()})
        if page.should_stop:
            break
    return page.payload()


class _RipgrepOutputError(ValueError):
    pass


def _backend_metadata(binary: str, *, status: str = "ok", **extra: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": "ripgrep",
        "binary": binary,
        "status": status,
    }
    payload.update(extra)
    return payload


def _decode_json_value(value: object, *, label: str) -> str:
    if not isinstance(value, dict):
        raise _RipgrepOutputError(f"ripgrep JSON field {label!r} is not an object")
    text = value.get("text")
    if isinstance(text, str):
        return text
    encoded = value.get("bytes")
    if not isinstance(encoded, str):
        raise _RipgrepOutputError(f"ripgrep JSON field {label!r} has neither text nor bytes")
    try:
        raw = base64.b64decode(encoded, validate=True)
        return raw.decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise _RipgrepOutputError(
            f"ripgrep returned non-UTF-8 {label}; this search API only returns UTF-8 text"
        ) from exc


def _decode_stderr(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="backslashreplace")


class _BoundedBytesCapture:
    """Thread-safe prefix capture that never retains more than ``max_bytes``."""

    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        self._buffer = bytearray()
        self.total_bytes = 0
        self._lock = threading.Lock()

    def append(self, chunk: bytes) -> None:
        if not chunk:
            return
        with self._lock:
            self.total_bytes += len(chunk)
            remaining = self.max_bytes - len(self._buffer)
            if remaining > 0:
                self._buffer.extend(chunk[:remaining])

    @property
    def truncated(self) -> bool:
        return self.total_bytes > len(self._buffer)

    def bytes(self) -> bytes:
        with self._lock:
            return bytes(self._buffer)


def _drain_stream(stream: Any, capture: _BoundedBytesCapture) -> None:
    try:
        while True:
            chunk = stream.read(8192)
            if not chunk:
                break
            capture.append(chunk)
    finally:
        stream.close()


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


class _PageCollector:
    """Collect only the requested page and stop before retaining excess items."""

    def __init__(
        self,
        *,
        offset: int,
        limit: int,
        result_key: str,
        base_payload: dict[str, object],
        budget: ResponseBudget,
    ) -> None:
        self.offset = max(offset, 0)
        self.limit = max(limit, 0)
        self.result_key = result_key
        self.base_payload = base_payload
        self.budget = budget
        self.seen = 0
        self.items: list[object] = []
        self.truncated = False
        self.stop_reason = "end_of_results"
        self.should_stop = False

    def add(self, item: object) -> None:
        if self.should_stop:
            return
        ordinal = self.seen
        self.seen += 1
        if ordinal < self.offset:
            return
        if self.limit and len(self.items) >= self.limit:
            self.truncated = True
            self.stop_reason = "limit"
            self.should_stop = True
            return

        candidate_items = [*self.items, item]
        candidate = dict(self.base_payload)
        candidate[self.result_key] = candidate_items
        candidate["next_offset"] = self.offset + len(candidate_items)
        _candidate_payload, measurement = with_budget_metadata(
            candidate,
            budget=self.budget,
            truncated=True,
            stop_reason="token_budget",
        )
        if not measurement.fits:
            self.truncated = True
            self.stop_reason = measurement.stop_reason or "token_budget"
            self.should_stop = True
            return
        self.items.append(item)

    def payload(self) -> dict[str, object]:
        result = dict(self.base_payload)
        result[self.result_key] = self.items
        result["next_offset"] = (
            self.offset + len(self.items) if self.truncated else None
        )
        rendered, _measurement = with_budget_metadata(
            result,
            budget=self.budget,
            truncated=self.truncated,
            stop_reason=self.stop_reason,
        )
        return rendered

    def force_stop(self, reason: str) -> None:
        self.truncated = True
        self.stop_reason = reason
        self.should_stop = True


class _ContentCollector:
    """Incrementally assemble context without retaining all ripgrep events."""

    def __init__(
        self,
        *,
        page: _PageCollector,
        before: int,
        after: int,
        multiline: bool,
        only_matching: bool,
    ) -> None:
        self.page = page
        self.before = max(before, 0)
        self.after = max(after, 0)
        self.multiline = multiline
        self.only_matching = only_matching
        self.current_path: str | None = None
        self.recent_lines: dict[int, str] = {}
        self.recent_order: deque[int] = deque()
        self.pending: deque[dict[str, object]] = deque()

    def _queue(self, item: dict[str, object]) -> None:
        if self.page.should_stop:
            return
        # Context for entries skipped by offset is never returned, so advancing
        # those ordinals immediately avoids an offset-sized pending buffer.
        if self.page.seen < self.page.offset and not self.pending:
            self.page.add(item)
            return
        if len(self.pending) >= _MAX_PENDING_CONTENT_MATCHES:
            self.pending.clear()
            self.page.force_stop("context_buffer_limit")
            return
        self.pending.append(item)

    def _flush_pending(self, *, before_line: int | None = None) -> None:
        while self.pending:
            threshold = int(self.pending[0]["_context_through"])
            if before_line is not None and threshold >= before_line:
                break
            item = self.pending.popleft()
            item.pop("_context_through", None)
            item.pop("_context_line_numbers", None)
            self.page.add(item)
            if self.page.should_stop:
                self.pending.clear()
                return

    def _record_recent(self, line_number: int, text: str) -> None:
        if line_number not in self.recent_lines:
            self.recent_order.append(line_number)
        self.recent_lines[line_number] = text

    def _trim_recent(self, current_end: int) -> None:
        keep_from = max(current_end - self.before + 1, 1)
        while self.recent_order and self.recent_order[0] < keep_from:
            old = self.recent_order.popleft()
            self.recent_lines.pop(old, None)

    def _context_before(self, start_line: int) -> list[str]:
        return [
            self.recent_lines[line_number]
            for line_number in range(max(start_line - self.before, 1), start_line)
            if line_number in self.recent_lines
        ]

    def feed(self, event_type: str, data: dict[str, object], *, cwd: Path) -> None:
        if self.page.should_stop:
            return
        if event_type == "end":
            self.finish_file()
            return
        if event_type not in {"match", "context"}:
            return

        raw_path = _decode_json_value(data.get("path"), label="path")
        path = _absolute_result_path(raw_path, cwd=cwd)
        text = _decode_json_value(data.get("lines"), label="lines")
        line_number = data.get("line_number")
        if not isinstance(line_number, int):
            raise _RipgrepOutputError("ripgrep JSON event is missing an integer line_number")

        if self.current_path != path:
            self.finish_file()
            self.current_path = path

        event_lines = text.splitlines()
        if not event_lines and text == "":
            event_lines = [""]
        event_end = line_number + len(event_lines) - 1
        self._flush_pending(before_line=line_number)

        # A later matching line is still context for an earlier match, matching
        # the previous line-map based behavior.
        for pending in self.pending:
            end_line = int(pending["end_line_number"] if "end_line_number" in pending else pending["line_number"])
            through = int(pending["_context_through"])
            context_line_numbers = pending["_context_line_numbers"]
            assert isinstance(context_line_numbers, set)
            for index, line in enumerate(event_lines):
                number = line_number + index
                if end_line < number <= through and number not in context_line_numbers:
                    context_after = pending["context_after"]
                    assert isinstance(context_after, list)
                    context_after.append(line)
                    context_line_numbers.add(number)

        if event_type == "match":
            submatches = data.get("submatches", [])
            if not isinstance(submatches, list):
                raise _RipgrepOutputError("ripgrep JSON submatches field is not a list")
            event = {
                "path": path,
                "line_number": line_number,
                "text": text,
                "absolute_offset": int(data.get("absolute_offset", 0)),
                "submatches": submatches,
            }
            if self.multiline or self.only_matching:
                for submatch in submatches:
                    start_line, end_line, match_text, _submatch_start = _submatch_details(event, submatch)
                    item: dict[str, object] = {
                        "path": path,
                        "line_number": start_line,
                        "line": match_text,
                        "context_before": self._context_before(start_line),
                        "context_after": [],
                        "_context_through": end_line + self.after,
                        "_context_line_numbers": set(),
                    }
                    if self.multiline:
                        item["end_line_number"] = end_line
                    self._queue(item)
                    if self.page.should_stop:
                        break
            else:
                self._queue(
                    {
                        "path": path,
                        "line_number": line_number,
                        "line": event_lines[0],
                        "context_before": self._context_before(line_number),
                        "context_after": [],
                        "_context_through": line_number + self.after,
                        "_context_line_numbers": set(),
                    }
                )

        for index, line in enumerate(event_lines):
            self._record_recent(line_number + index, line)
        self._trim_recent(event_end)
        self._flush_pending(before_line=event_end + 1)

    def finish_file(self) -> None:
        self._flush_pending()
        self.recent_lines.clear()
        self.recent_order.clear()
        self.current_path = None

    def finish(self) -> None:
        self.finish_file()


def _path_is_in_git_metadata(path: Path) -> bool:
    return ".git" in path.parts


def _append_exclude_glob(command: list[str], pattern: str) -> None:
    command.extend(["--glob", f"!{pattern}"])


def _build_ripgrep_command(
    *,
    rg_binary: str,
    pattern: str,
    target: str,
    glob_pattern: str | None,
    before: int,
    after: int,
    ignore_case: bool,
    multiline: bool,
    include_hidden: bool,
    respect_gitignore: bool,
    exclude_patterns: tuple[str, ...],
    file_type: str | None,
    fixed_strings: bool,
    regex_engine: str,
) -> list[str]:
    command = [
        rg_binary,
        "--json",
        "--no-config",
        "--line-number",
        "--with-filename",
        "--color=never",
        "--sort=path",
    ]
    if fixed_strings:
        command.append("--fixed-strings")
    else:
        command.append(f"--engine={regex_engine}")
    if ignore_case:
        command.append("--ignore-case")
    if multiline:
        command.extend(["--multiline", "--multiline-dotall"])
    if include_hidden:
        command.append("--hidden")
    if not respect_gitignore:
        command.append("--no-ignore")
    if before > 0:
        command.extend(["--before-context", str(before)])
    if after > 0:
        command.extend(["--after-context", str(after)])
    if glob_pattern:
        command.extend(["--glob", glob_pattern])
    if file_type:
        command.extend(["--type", file_type])

    for directory_name in sorted(DEFAULT_EXCLUDE_DIR_NAMES):
        _append_exclude_glob(command, f"{directory_name}/**")
        _append_exclude_glob(command, f"**/{directory_name}/**")
    for exclude_pattern in exclude_patterns:
        _append_exclude_glob(command, exclude_pattern)

    # Explicit globs and --no-ignore must never make repository metadata searchable.
    for git_pattern in (".git", ".git/**", "**/.git", "**/.git/**"):
        _append_exclude_glob(command, git_pattern)
    if not include_hidden:
        for hidden_pattern in (".*", ".*/**", "**/.*", "**/.*/**"):
            _append_exclude_glob(command, hidden_pattern)

    command.extend(["-e", pattern, "--", target])
    return command


def _absolute_result_path(raw_path: str, *, cwd: Path) -> str:
    path = Path(raw_path)
    if not path.is_absolute():
        path = cwd / path
    return str(path.resolve(strict=False))


def _submatch_details(event: dict[str, Any], submatch: object) -> tuple[int, int, str, int]:
    if not isinstance(submatch, dict):
        raise _RipgrepOutputError("ripgrep JSON submatch is not an object")
    start = submatch.get("start")
    end = submatch.get("end")
    if not isinstance(start, int) or not isinstance(end, int):
        raise _RipgrepOutputError("ripgrep JSON submatch has invalid byte offsets")
    match_text = _decode_json_value(submatch.get("match"), label="submatch")
    event_bytes = str(event["text"]).encode("utf-8")
    if start < 0 or end < start or end > len(event_bytes):
        raise _RipgrepOutputError("ripgrep JSON submatch byte offsets are out of range")
    start_line = int(event["line_number"]) + event_bytes[:start].count(b"\n")
    end_line = start_line + match_text.count("\n")
    return start_line, end_line, match_text, start


def _grep_filters(
    *,
    include_hidden: bool,
    respect_gitignore: bool,
    gitignore_applied: bool,
    exclude_patterns: tuple[str, ...],
    file_type: str | None,
) -> dict[str, object]:
    return {
        "include_hidden": include_hidden,
        "respect_gitignore": respect_gitignore,
        "gitignore_applied": gitignore_applied,
        "exclude_patterns": list(exclude_patterns),
        "file_type": file_type,
    }


def _empty_grep_result(
    *,
    base_path: Path,
    pattern: str,
    output_mode: str,
    filters: dict[str, object],
    only_matching: bool,
    rg_binary: str,
    max_tokens: int,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "success": True,
        "base_path": str(base_path),
        "pattern": pattern,
        "output_mode": output_mode,
        "truncated": False,
        "next_offset": None,
        "filters": filters,
        "only_matching": only_matching,
        "backend": _backend_metadata(rg_binary),
    }
    if output_mode == "content":
        payload["matches"] = []
    elif output_mode == "files_with_matches":
        payload["files"] = []
    elif output_mode == "count":
        payload["counts"] = []
    else:
        payload["summary"] = {"occurrences": 0, "matched_files": 0}
    rendered, _measurement = with_budget_metadata(
        payload,
        budget=ResponseBudget(max_tokens=max_tokens),
        truncated=False,
        stop_reason="end_of_results",
    )
    return rendered


def grep_files(
    base_path: Path,
    *,
    pattern: str,
    glob_pattern: str | None,
    output_mode: str,
    before: int = 0,
    after: int = 0,
    ignore_case: bool = False,
    head_limit: int,
    offset: int,
    multiline: bool = False,
    include_hidden: bool = False,
    respect_gitignore: bool = True,
    exclude_patterns: list[str] | tuple[str, ...] | None = None,
    file_type: str | None = None,
    only_matching: bool = False,
    fixed_strings: bool = False,
    regex_engine: str = "auto",
    rg_binary: str = "rg",
    max_tokens: int = DEFAULT_TOOL_OUTPUT_TOKEN_BUDGET,
) -> dict[str, object]:
    validation_error = _validate_existing_path(base_path)
    if validation_error:
        return validation_error

    if output_mode not in {"content", "files_with_matches", "count", "summary"}:
        return _error("invalid_output_mode", f"Unsupported output_mode: {output_mode}")
    if head_limit < 0 or offset < 0:
        return _error("invalid_arguments", "head_limit and offset must be non-negative integers.")
    if before < 0 or after < 0:
        return _error("invalid_context", "before and after must be non-negative integers.")
    if before > _MAX_SEARCH_CONTEXT_LINES or after > _MAX_SEARCH_CONTEXT_LINES:
        return _error(
            "context_limit_exceeded",
            f"before and after are capped at {_MAX_SEARCH_CONTEXT_LINES} lines.",
        )

    matches_exclude = tuple(exclude_patterns or ())
    gitignore_applied = bool(respect_gitignore and _find_git_root(base_path) is not None)
    filters = _grep_filters(
        include_hidden=include_hidden,
        respect_gitignore=respect_gitignore,
        gitignore_applied=gitignore_applied,
        exclude_patterns=matches_exclude,
        file_type=file_type,
    )

    if _path_is_in_git_metadata(base_path):
        return _empty_grep_result(
            base_path=base_path,
            pattern=pattern,
            output_mode=output_mode,
            filters=filters,
            only_matching=only_matching,
            rg_binary=rg_binary,
            max_tokens=max_tokens,
        )

    if base_path.is_file():
        candidate_files, gitignore_applied = _iter_matching_files(
            base_path,
            glob_pattern=glob_pattern,
            include_hidden=include_hidden,
            respect_gitignore=respect_gitignore,
            exclude_patterns=matches_exclude,
        )
        filters["gitignore_applied"] = gitignore_applied
        if not candidate_files:
            return _empty_grep_result(
                base_path=base_path,
                pattern=pattern,
                output_mode=output_mode,
                filters=filters,
                only_matching=only_matching,
                rg_binary=rg_binary,
                max_tokens=max_tokens,
            )
        cwd = base_path.parent
        target = base_path.name
    else:
        cwd = base_path
        target = "."

    command = _build_ripgrep_command(
        rg_binary=rg_binary,
        pattern=pattern,
        target=target,
        glob_pattern=glob_pattern,
        before=max(before, 0),
        after=max(after, 0),
        ignore_case=ignore_case,
        multiline=multiline,
        include_hidden=include_hidden,
        respect_gitignore=respect_gitignore,
        exclude_patterns=matches_exclude,
        file_type=file_type,
        fixed_strings=fixed_strings,
        regex_engine=regex_engine,
    )
    response_budget = ResponseBudget(max_tokens=max_tokens)
    base_payload: dict[str, object] = {
        "success": True,
        "base_path": str(base_path),
        "pattern": pattern,
        "output_mode": output_mode,
        "filters": filters,
        "only_matching": only_matching,
        "backend": _backend_metadata(rg_binary),
    }
    result_key = {
        "content": "matches",
        "files_with_matches": "files",
        "count": "counts",
    }.get(output_mode)
    page = (
        _PageCollector(
            offset=offset,
            limit=head_limit,
            result_key=result_key,
            base_payload=base_payload,
            budget=response_budget,
        )
        if result_key is not None
        else None
    )
    content_collector = (
        _ContentCollector(
            page=page,
            before=before,
            after=after,
            multiline=multiline,
            only_matching=only_matching,
        )
        if output_mode == "content" and page is not None
        else None
    )

    stderr_capture = _BoundedBytesCapture(_MAX_RIPGREP_STDERR_BYTES)
    process: subprocess.Popen[bytes]
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
    except FileNotFoundError:
        return _error(
            "backend_unavailable",
            (
                f"ripgrep binary {rg_binary!r} was not found. Install ripgrep or set "
                "CHATGPT_MCP_RIPGREP_BINARY to its executable path."
            ),
            backend=_backend_metadata(rg_binary, status="unavailable"),
        )
    except OSError as exc:
        return _error(
            "backend_unavailable",
            f"Could not execute ripgrep binary {rg_binary!r}: {exc}",
            backend=_backend_metadata(rg_binary, status="unavailable", os_error=str(exc)),
        )

    assert process.stdout is not None
    assert process.stderr is not None
    stderr_thread = threading.Thread(
        target=_drain_stream,
        args=(process.stderr, stderr_capture),
        name="ripgrep-stderr-drain",
        daemon=True,
    )
    stderr_thread.start()

    parse_error: Exception | None = None
    intentional_stop = False
    current_count_path: str | None = None
    current_count = 0
    summary_occurrences = 0
    summary_matched_files = 0

    def finish_count_path() -> None:
        nonlocal current_count_path, current_count
        if current_count_path is None or page is None:
            return
        if output_mode == "files_with_matches":
            page.add(current_count_path)
        elif output_mode == "count":
            page.add({"path": current_count_path, "count": current_count})
        current_count_path = None
        current_count = 0

    try:
        while True:
            raw_line = process.stdout.readline(_MAX_RIPGREP_EVENT_BYTES + 1)
            if not raw_line:
                break
            if len(raw_line) > _MAX_RIPGREP_EVENT_BYTES and not raw_line.endswith(b"\n"):
                raise _RipgrepOutputError(
                    "ripgrep emitted a JSON event larger than the bounded event limit"
                )
            try:
                event = json.loads(raw_line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise _RipgrepOutputError(f"ripgrep emitted invalid JSON: {exc}") from exc
            if not isinstance(event, dict):
                continue
            event_type = event.get("type")
            if event_type not in {"match", "context", "end"}:
                continue
            data = event.get("data")
            if not isinstance(data, dict):
                raise _RipgrepOutputError("ripgrep JSON event data is not an object")

            if content_collector is not None:
                content_collector.feed(str(event_type), data, cwd=cwd)
            elif event_type == "match":
                raw_path = _decode_json_value(data.get("path"), label="path")
                event_path = _absolute_result_path(raw_path, cwd=cwd)
                submatches = data.get("submatches", [])
                if not isinstance(submatches, list):
                    raise _RipgrepOutputError("ripgrep JSON submatches field is not a list")
                occurrence_count = len(submatches)
                if output_mode == "summary":
                    if current_count_path != event_path:
                        summary_matched_files += 1
                        current_count_path = event_path
                    summary_occurrences += occurrence_count
                else:
                    if current_count_path != event_path:
                        finish_count_path()
                        if page is not None and page.should_stop:
                            intentional_stop = True
                            break
                        current_count_path = event_path
                    current_count += occurrence_count

            if page is not None and page.should_stop:
                intentional_stop = True
                break
    except (KeyError, TypeError, ValueError) as exc:
        parse_error = exc
    finally:
        process.stdout.close()

    if parse_error is None:
        if content_collector is not None:
            content_collector.finish()
        elif output_mode in {"files_with_matches", "count"}:
            finish_count_path()
        if page is not None and page.should_stop:
            intentional_stop = True

    if parse_error is not None or intentional_stop:
        _terminate_process(process)
    else:
        process.wait()
    stderr_thread.join(timeout=2)

    stderr = _decode_stderr(stderr_capture.bytes()).strip()
    return_code = process.returncode if process.returncode is not None else -1
    if parse_error is not None:
        return _error(
            "backend_error",
            f"Could not parse ripgrep machine-readable output: {parse_error}",
            backend=_backend_metadata(
                rg_binary,
                status="error",
                exit_code=return_code,
                stderr=stderr,
                **({"stderr_truncated": True} if stderr_capture.truncated else {}),
            ),
        )

    if not intentional_stop and return_code not in {0, 1}:
        lowered = stderr.lower()
        backend = _backend_metadata(
            rg_binary,
            status="error",
            exit_code=return_code,
            stderr=stderr,
            **({"stderr_truncated": True} if stderr_capture.truncated else {}),
        )
        if "regex parse error" in lowered or "error parsing regex" in lowered:
            return _error(
                "invalid_pattern",
                (
                    "ripgrep rejected the pattern using Rust regex syntax. Look-around and "
                    f"backreferences are not supported. {stderr}"
                ).strip(),
                pattern=pattern,
                backend=backend,
            )
        if "unrecognized file type" in lowered:
            return _error(
                "invalid_file_type",
                f"ripgrep does not recognize file_type={file_type!r}. {stderr}".strip(),
                file_type=file_type,
                backend=backend,
            )
        return _error(
            "backend_error",
            f"ripgrep exited unexpectedly with code {return_code}. {stderr}".strip(),
            backend=backend,
        )

    if output_mode == "summary":
        summary_payload = dict(base_payload)
        summary_payload["summary"] = {
            "occurrences": summary_occurrences,
            "matched_files": summary_matched_files,
        }
        summary_payload["next_offset"] = None
        rendered, _measurement = with_budget_metadata(
            summary_payload,
            budget=response_budget,
            truncated=False,
            stop_reason="end_of_results",
        )
        return rendered

    assert page is not None
    return page.payload()


def search_files(
    base_path: Path,
    *,
    query: str,
    glob_pattern: str | None,
    limit: int,
    include_hidden: bool = False,
    respect_gitignore: bool = True,
    exclude_patterns: list[str] | tuple[str, ...] | None = None,
    rg_binary: str = "rg",
    offset: int = 0,
    max_tokens: int = DEFAULT_TOOL_OUTPUT_TOKEN_BUDGET,
) -> dict[str, object]:
    result = grep_files(
        base_path,
        pattern=query,
        glob_pattern=glob_pattern,
        output_mode="content",
        before=0,
        after=0,
        ignore_case=False,
        head_limit=limit,
        offset=offset,
        multiline=False,
        include_hidden=include_hidden,
        respect_gitignore=respect_gitignore,
        exclude_patterns=exclude_patterns,
        fixed_strings=True,
        rg_binary=rg_binary,
        max_tokens=max_tokens,
    )
    if not result["success"]:
        return result
    payload = {
        "success": True,
        "matches": [
            {
                "path": match["path"],
                "line_number": match["line_number"],
                "line": match["line"],
            }
            for match in result["matches"]
        ],
        "truncated": result["truncated"],
        "next_offset": result.get("next_offset"),
        "filters": result["filters"],
    }
    rendered, _measurement = with_budget_metadata(
        payload,
        budget=ResponseBudget(max_tokens=max_tokens),
        truncated=bool(result.get("truncated")),
        stop_reason=str(result.get("stop_reason", "end_of_results")),
    )
    return rendered
