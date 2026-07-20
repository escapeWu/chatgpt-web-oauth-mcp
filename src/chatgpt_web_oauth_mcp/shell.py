from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import deque
import copy
import json
import os
import secrets
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import BinaryIO, Literal, Mapping

from .job_supervisor import (
    JOB_METADATA_SCHEMA_VERSION,
    TERMINAL_JOB_STATUSES,
    ensure_private_directory,
    ensure_private_file,
    mutate_job_metadata,
    process_group_exists,
    process_group_matches_snapshot,
    process_identity_matches,
    read_job_metadata,
    snapshot_process_group,
    update_job_metadata,
    write_job_metadata,
)
from .response_budget import (
    BudgetMeasurement,
    DEFAULT_TOOL_OUTPUT_TOKEN_BUDGET,
    ResponseBudget,
    with_budget_metadata,
)


# Sentinel exit code used when the process did not produce a real return code
# (e.g. it timed out or never started). Keeping this as an int (not None) so
# callers can always do numeric comparisons like `exit_code == 0` without
# special-casing `None`.
TIMEOUT_EXIT_CODE = -1
MAX_COMMAND_TIMEOUT_SECONDS = 300
MAX_COMMAND_BATCH_CONCURRENCY = 3
MAX_COMMAND_BATCH_SIZE = 20
MAX_JOB_TAIL_LINES = 500
MAX_JOB_LIST_LIMIT = 200
MAX_JOB_OUTPUT_BYTES = 262144
DEFAULT_RUN_CAPTURE_MAX_BYTES = 1024 * 1024
_PIPE_READ_CHUNK_BYTES = 64 * 1024
_PROCESS_TREE_TERM_GRACE_SECONDS = 0.25
_JOB_SUPERVISOR_START_TIMEOUT_SECONDS = 5.0
_JOB_STARTUP_CLEANUP_TIMEOUT_SECONDS = 2.0
_JOB_STARTING_STALE_GRACE_SECONDS = 5.0
_JOB_TERM_GRACE_SECONDS = 0.5
_JOB_TERMINAL_RECORD_WAIT_SECONDS = 1.0
_JOB_LIST_COMMAND_MAX_CHARACTERS = 512
_JOB_LIST_WARNING_LIMIT = 20
_JOB_LIST_WARNING_MESSAGE_MAX_CHARACTERS = 240
_JOB_OUTPUT_POLL_SECONDS = 0.02
_JOB_STATUSES = frozenset({"running", *TERMINAL_JOB_STATUSES})
_DURABLE_JOB_STATUSES = frozenset({"starting", "running", *TERMINAL_JOB_STATUSES})


class _BoundedStreamCapture:
    """Drain one byte stream while retaining fixed-size head and tail windows."""

    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max(0, int(max_bytes))
        self._head_limit = (self.max_bytes + 1) // 2
        self._tail_limit = self.max_bytes - self._head_limit
        self._head = bytearray()
        self._tail = bytearray()
        self.total_bytes = 0
        self._line_breaks = 0
        self._last_byte: int | None = None

    def add(self, chunk: bytes) -> None:
        if not chunk:
            return
        previous_last = self._last_byte
        self.total_bytes += len(chunk)
        self._line_breaks += chunk.count(b"\n") + chunk.count(b"\r") - chunk.count(b"\r\n")
        if previous_last == ord("\r") and chunk[0] == ord("\n"):
            self._line_breaks -= 1
        self._last_byte = chunk[-1]

        head_remaining = self._head_limit - len(self._head)
        if head_remaining > 0:
            self._head.extend(chunk[:head_remaining])
            chunk = chunk[head_remaining:]
        if not chunk or self._tail_limit <= 0:
            return
        self._tail.extend(chunk)
        overflow = len(self._tail) - self._tail_limit
        if overflow > 0:
            del self._tail[:overflow]

    @property
    def head(self) -> bytes:
        return bytes(self._head)

    @property
    def tail(self) -> bytes:
        return bytes(self._tail)

    @property
    def retained_bytes(self) -> int:
        return len(self._head) + len(self._tail)

    @property
    def dropped_bytes(self) -> int:
        return max(0, self.total_bytes - self.retained_bytes)

    @property
    def total_lines(self) -> int:
        if self.total_bytes == 0:
            return 0
        return self._line_breaks + int(self._last_byte not in {ord("\n"), ord("\r")})

    @property
    def retained_lines(self) -> int:
        if self.dropped_bytes == 0:
            return self.total_lines
        return min(
            self.total_lines,
            _count_lines(self.head) + _count_lines(self.tail),
        )


def _count_lines(content: bytes | str) -> int:
    if not content:
        return 0
    if isinstance(content, bytes):
        breaks = content.count(b"\n") + content.count(b"\r") - content.count(b"\r\n")
        return breaks + int(content[-1] not in {ord("\n"), ord("\r")})
    breaks = content.count("\n") + content.count("\r") - content.count("\r\n")
    return breaks + int(content[-1] not in {"\n", "\r"})


class _JobSignalRefused(RuntimeError):
    pass


class JobRegistry:
    """Disk-backed registry for independently supervised background jobs."""

    def __init__(self) -> None:
        # The registry intentionally owns no process lifecycle state. Every
        # operation resolves a durable record under the supplied state_dir.
        pass

    def start_job(
        self,
        *,
        command: str,
        cwd: Path,
        state_dir: Path,
        env: Mapping[str, str] | None = None,
        name: str | None = None,
    ) -> dict[str, object]:
        normalized_command = command.strip()
        if not normalized_command:
            return _job_error("invalid_arguments", "command must be a non-empty string.")
        cwd_error = _cwd_error(cwd)
        if cwd_error:
            return cwd_error
        env_result = _merged_job_env(env)
        if isinstance(env_result, dict) and env_result.get("success") is False:
            return env_result

        job_id = _new_job_id()
        started_at = time.time()
        startup_deadline = time.monotonic() + _JOB_SUPERVISOR_START_TIMEOUT_SECONDS
        supervisor: subprocess.Popen[bytes] | None = None
        try:
            job_dir = _jobs_runtime_dir(state_dir) / job_id
            job_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
            ensure_private_directory(job_dir)
            stdout_log = job_dir / "stdout.log"
            stderr_log = job_dir / "stderr.log"
            ensure_private_file(stdout_log)
            ensure_private_file(stderr_log)
            write_job_metadata(
                job_dir,
                {
                    "schema_version": JOB_METADATA_SCHEMA_VERSION,
                    "job_id": job_id,
                    "name": (name or "").strip() or None,
                    "command": normalized_command,
                    "cwd": str(cwd),
                    "status": "starting",
                    "pid": None,
                    "pgid": None,
                    "process_identity": None,
                    "supervisor_pid": None,
                    "supervisor_identity": None,
                    "started_at": started_at,
                    "completed_at": None,
                    "updated_at": started_at,
                    "exit_code": None,
                    "kill_signal": None,
                    "stdout_log": str(stdout_log),
                    "stderr_log": str(stderr_log),
                },
            )
            supervisor_script = Path(__file__).with_name("job_supervisor.py")
            supervisor_args = [
                sys.executable,
                str(supervisor_script),
                "--detach",
                "--job-dir",
                str(job_dir),
                "--command",
                normalized_command,
                "--cwd",
                str(cwd),
            ]
            supervisor_kwargs: dict[str, object] = {"close_fds": True}
            if os.name == "posix":
                supervisor_kwargs["start_new_session"] = True
            elif os.name == "nt":  # pragma: no cover
                supervisor_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            with stderr_log.open("ab", buffering=0) as supervisor_stderr:
                supervisor = subprocess.Popen(
                    supervisor_args,
                    cwd=str(job_dir),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=supervisor_stderr,
                    env=env_result,
                    **supervisor_kwargs,
                )
        except (OSError, ValueError) as exc:
            if "job_dir" in locals():
                try:
                    update_job_metadata(
                        job_dir,
                        status="failed",
                        exit_code=TIMEOUT_EXIT_CODE,
                        completed_at=time.time(),
                        updated_at=time.time(),
                    )
                except (OSError, ValueError):
                    pass
            return _job_error(
                "job_start_failed",
                f"Failed to start job: {exc}",
                cwd=str(cwd),
                command=normalized_command,
            )

        assert supervisor is not None
        bootstrap_exit_code: int | None = None
        metadata: dict[str, object] | None = None
        metadata_error: OSError | ValueError | None = None
        startup_state = "waiting_for_bootstrap_and_metadata"
        while True:
            if bootstrap_exit_code is None:
                bootstrap_exit_code = supervisor.poll()
                if bootstrap_exit_code is not None:
                    # poll() reaps on POSIX; wait() also closes the process
                    # handle promptly on platforms where that is separate.
                    supervisor.wait()
                    if bootstrap_exit_code != 0:
                        return self._job_start_failure(
                            job_dir=job_dir,
                            supervisor=supervisor,
                            exit_code=bootstrap_exit_code,
                            message=(
                                "Detached job supervisor bootstrap exited with "
                                f"code {bootstrap_exit_code}; see {stderr_log}."
                            ),
                            job_id=job_id,
                            cwd=cwd,
                            command=normalized_command,
                            stdout_log=stdout_log,
                            stderr_log=stderr_log,
                        )
            try:
                snapshot = read_job_metadata(job_dir)
                if snapshot is not None:
                    metadata = snapshot
                metadata_error = None
            except (OSError, ValueError) as exc:
                metadata_error = exc

            metadata_ready = metadata is not None and metadata.get("status") in {
                "running",
                *TERMINAL_JOB_STATUSES,
            }
            if bootstrap_exit_code == 0 and metadata_ready:
                startup_state = "ready"
                break
            if bootstrap_exit_code == 0:
                startup_state = "waiting_for_metadata"
            elif metadata_ready:
                startup_state = "waiting_for_bootstrap"

            remaining = startup_deadline - time.monotonic()
            if remaining <= 0:
                metadata_status = metadata.get("status") if metadata is not None else None
                detail = (
                    f"state={startup_state}, bootstrap_exit_code={bootstrap_exit_code}, "
                    f"metadata_status={metadata_status!r}"
                )
                if metadata_error is not None:
                    detail += f", metadata_error={metadata_error}"
                return self._job_start_failure(
                    job_dir=job_dir,
                    supervisor=supervisor,
                    exit_code=TIMEOUT_EXIT_CODE,
                    message=(
                        "Detached job startup did not reach a successful bootstrap "
                        "and published running or terminal record within "
                        f"{_JOB_SUPERVISOR_START_TIMEOUT_SECONDS:.1f}s ({detail}); "
                        f"see {stderr_log}."
                    ),
                    job_id=job_id,
                    cwd=cwd,
                    command=normalized_command,
                    stdout_log=stdout_log,
                    stderr_log=stderr_log,
                )
            time.sleep(min(0.01, remaining))

        assert startup_state == "ready"
        assert metadata is not None
        metadata = self._reconcile_metadata(job_dir, metadata)
        payload = self._status_payload(job_dir, metadata)
        payload["success"] = True
        return payload

    def _job_start_failure(
        self,
        *,
        job_dir: Path,
        supervisor: subprocess.Popen[bytes],
        exit_code: int,
        message: str,
        job_id: str,
        cwd: Path,
        command: str,
        stdout_log: Path,
        stderr_log: Path,
    ) -> dict[str, object]:
        cleanup = self._cleanup_startup_attempt(job_dir, supervisor)
        completed_at = time.time()
        try:
            update_job_metadata(
                job_dir,
                status="failed",
                exit_code=exit_code,
                completed_at=completed_at,
                updated_at=completed_at,
            )
        except (OSError, ValueError):
            pass
        return _job_error(
            "job_start_failed",
            f"{message} Cleanup: {cleanup}.",
            job_id=job_id,
            cwd=str(cwd),
            command=command,
            stdout_log=str(stdout_log),
            stderr_log=str(stderr_log),
        )

    def _cleanup_startup_attempt(
        self,
        job_dir: Path,
        supervisor: subprocess.Popen[bytes],
    ) -> str:
        cleanup_deadline = time.monotonic() + _JOB_STARTUP_CLEANUP_TIMEOUT_SECONDS
        evidence: list[str] = []

        # If bootstrap is still alive it remains the direct parent/guardian of
        # the detached supervisor and will terminate and reap that child.
        evidence.append(_terminate_and_reap_bootstrap(supervisor, cleanup_deadline))

        # A successfully exited bootstrap may already have detached its child,
        # so use the durable identities for any remaining supervisor/job tree.
        try:
            latest = read_job_metadata(job_dir)
        except (OSError, ValueError):
            latest = None
        if latest is not None:
            evidence.append(_terminate_recorded_job_group(latest, cleanup_deadline))
            evidence.append(_terminate_recorded_supervisor(latest, cleanup_deadline))
        return ", ".join(dict.fromkeys(evidence))

    def job_status(self, *, job_id: str, state_dir: Path) -> dict[str, object]:
        loaded = self._load_job(job_id=job_id, state_dir=state_dir)
        if isinstance(loaded, dict):
            return loaded
        job_dir, metadata = loaded
        metadata = self._reconcile_metadata(job_dir, metadata)
        payload = self._status_payload(job_dir, metadata)
        payload["success"] = True
        return payload

    def list_jobs(
        self,
        *,
        state_dir: Path,
        status: Literal["all", "running", "succeeded", "failed", "killed", "interrupted"] = "all",
        offset: int = 0,
        limit: int = 50,
        max_tokens: int = DEFAULT_TOOL_OUTPUT_TOKEN_BUDGET,
    ) -> dict[str, object]:
        if status not in {"all", *_JOB_STATUSES}:
            return _job_error(
                "invalid_arguments",
                "status must be one of: all, running, succeeded, failed, killed, interrupted.",
            )
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            return _job_error("invalid_arguments", "offset must be an integer greater than or equal to 0.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_JOB_LIST_LIMIT:
            return _job_error(
                "invalid_arguments",
                f"limit must be an integer between 1 and {MAX_JOB_LIST_LIMIT}.",
            )

        response_budget = ResponseBudget(max_tokens=max_tokens)
        jobs_dir = _jobs_registry_path(state_dir)
        if jobs_dir.is_symlink():
            return _job_error(
                "job_registry_invalid",
                "The durable jobs registry must not be a symbolic link.",
                jobs_dir=str(jobs_dir),
            )
        if not jobs_dir.exists():
            measurement = _measure_job_list_response(
                jobs=[],
                total=0,
                offset=offset,
                limit=limit,
                status_filter=status,
                response_budget=response_budget,
                stop_reason="end_of_results",
                skipped_count=0,
                warnings=[],
                warnings_truncated=False,
                oversized_item=False,
            )
            return _job_list_payload(
                jobs=[],
                total=0,
                offset=offset,
                limit=limit,
                status_filter=status,
                response_budget=response_budget,
                measurement=measurement,
                stop_reason="end_of_results",
                skipped_count=0,
                warnings=[],
                warnings_truncated=False,
                oversized_item=False,
            )
        if not jobs_dir.is_dir():
            return _job_error(
                "job_registry_invalid",
                "The durable jobs registry path is not a directory.",
                jobs_dir=str(jobs_dir),
            )

        discovered: list[tuple[float, str, Path, dict[str, object]]] = []
        warnings: list[dict[str, str]] = []
        skipped_count = 0
        try:
            entries = sorted(os.scandir(jobs_dir), key=lambda item: item.name)
        except OSError as exc:
            return _job_error(
                "job_registry_read_failed",
                f"Failed to read durable jobs registry: {exc}",
                jobs_dir=str(jobs_dir),
            )

        for entry in entries:
            warning_code: str | None = None
            warning_message: str | None = None
            try:
                if entry.is_symlink():
                    warning_code = "symlink_record"
                    warning_message = "Skipped symbolic-link registry entry."
                elif not entry.name.startswith("job_") or not entry.is_dir(follow_symlinks=False):
                    warning_code = "non_job_record"
                    warning_message = "Skipped entry that is not a durable job directory."
                else:
                    job_dir = Path(entry.path)
                    files_error = _job_record_files_validation_error(job_dir)
                    if files_error is not None:
                        warning_code, warning_message = files_error
                    else:
                        loaded = self._load_job(job_id=entry.name, state_dir=state_dir)
                        if isinstance(loaded, dict):
                            error = loaded.get("error")
                            warning_code = str(error.get("code")) if isinstance(error, dict) else "job_record_invalid"
                            warning_message = (
                                str(error.get("message"))
                                if isinstance(error, dict)
                                else "Skipped invalid durable job record."
                            )
                        else:
                            job_dir, metadata = loaded
                            validation_error = _job_metadata_validation_error(metadata)
                            if validation_error is not None:
                                warning_code = "job_metadata_invalid"
                                warning_message = validation_error
                            else:
                                metadata = self._reconcile_metadata(job_dir, metadata)
                                current_status = _durable_status(metadata)
                                if status == "all" or current_status == status:
                                    started_at = _metadata_float(metadata.get("started_at"))
                                    assert started_at is not None
                                    discovered.append((started_at, entry.name, job_dir, metadata))
            except OSError as exc:
                warning_code = "job_record_unreadable"
                warning_message = f"Skipped unreadable durable job record: {exc}"

            if warning_code is not None:
                skipped_count += 1
                if len(warnings) < _JOB_LIST_WARNING_LIMIT:
                    warnings.append(
                        {
                            "entry": entry.name,
                            "code": warning_code,
                            "message": _truncate_warning_message(warning_message or "Skipped durable job record."),
                        }
                    )

        discovered.sort(key=lambda item: (-item[0], item[1]))
        total = len(discovered)
        selected = discovered[offset : offset + limit]
        summaries = [self._list_summary(job_dir, metadata) for _started, _job_id, job_dir, metadata in selected]
        page_warnings = list(warnings)
        stop_reason = "item_limit" if offset + len(summaries) < total else "end_of_results"
        oversized_item = False
        measurement = _measure_job_list_response(
            jobs=summaries,
            total=total,
            offset=offset,
            limit=limit,
            status_filter=status,
            response_budget=response_budget,
            stop_reason=stop_reason,
            skipped_count=skipped_count,
            warnings=page_warnings,
            warnings_truncated=skipped_count > len(page_warnings),
            oversized_item=False,
        )
        while not measurement.fits and page_warnings:
            page_warnings.pop()
            measurement = _measure_job_list_response(
                jobs=summaries,
                total=total,
                offset=offset,
                limit=limit,
                status_filter=status,
                response_budget=response_budget,
                stop_reason=stop_reason,
                skipped_count=skipped_count,
                warnings=page_warnings,
                warnings_truncated=True,
                oversized_item=False,
            )
        while not measurement.fits and len(summaries) > 1:
            summaries.pop()
            stop_reason = "token_budget"
            measurement = _measure_job_list_response(
                jobs=summaries,
                total=total,
                offset=offset,
                limit=limit,
                status_filter=status,
                response_budget=response_budget,
                stop_reason=stop_reason,
                skipped_count=skipped_count,
                warnings=page_warnings,
                warnings_truncated=skipped_count > len(page_warnings),
                oversized_item=False,
            )
        if not measurement.fits and summaries:
            stop_reason = "token_budget"
            oversized_item = True
            measurement = _measure_job_list_response(
                jobs=summaries,
                total=total,
                offset=offset,
                limit=limit,
                status_filter=status,
                response_budget=response_budget,
                stop_reason=stop_reason,
                skipped_count=skipped_count,
                warnings=page_warnings,
                warnings_truncated=skipped_count > len(page_warnings),
                oversized_item=True,
            )
        return _job_list_payload(
            jobs=summaries,
            total=total,
            offset=offset,
            limit=limit,
            status_filter=status,
            response_budget=response_budget,
            measurement=measurement,
            stop_reason=stop_reason,
            skipped_count=skipped_count,
            warnings=page_warnings,
            warnings_truncated=skipped_count > len(page_warnings),
            oversized_item=oversized_item,
        )

    def output_job(
        self,
        *,
        job_id: str,
        state_dir: Path,
        stream: Literal["stdout", "stderr"] = "stdout",
        cursor: int = 0,
        max_bytes: int = 65536,
        wait_ms: int = 0,
        max_tokens: int = DEFAULT_TOOL_OUTPUT_TOKEN_BUDGET,
    ) -> dict[str, object]:
        if stream not in {"stdout", "stderr"}:
            return _job_error("invalid_arguments", "stream must be one of: stdout, stderr.")
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            return _job_error("invalid_arguments", "cursor must be an integer greater than or equal to 0.")
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or not 1 <= max_bytes <= MAX_JOB_OUTPUT_BYTES
        ):
            return _job_error(
                "invalid_arguments",
                f"max_bytes must be an integer between 1 and {MAX_JOB_OUTPUT_BYTES}.",
            )
        if isinstance(wait_ms, bool) or not isinstance(wait_ms, int) or not 0 <= wait_ms <= 30000:
            return _job_error("invalid_arguments", "wait_ms must be an integer between 0 and 30000.")

        response_budget = ResponseBudget(max_tokens=max_tokens)
        loaded = self._load_job(job_id=job_id, state_dir=state_dir)
        if isinstance(loaded, dict):
            return loaded
        job_dir, metadata = loaded
        validation_error = _job_metadata_validation_error(metadata)
        if validation_error is not None:
            return _job_error("job_metadata_invalid", validation_error, job_id=job_id)
        metadata = self._reconcile_metadata(job_dir, metadata)
        log_path = job_dir / f"{stream}.log"
        opened = _open_job_log(log_path)
        if isinstance(opened, dict):
            opened["job_id"] = job_id
            opened["stream"] = stream
            return opened

        waited_ms = 0
        with opened as handle:
            try:
                file_size = os.fstat(handle.fileno()).st_size
            except OSError as exc:
                return _job_error("log_read_failed", f"Failed to inspect job log: {exc}", job_id=job_id)
            if cursor > file_size:
                return _job_error(
                    "invalid_cursor",
                    "cursor is beyond the current selected log size.",
                    job_id=job_id,
                    stream=stream,
                    cursor=cursor,
                    file_size=file_size,
                )

            if cursor == file_size and metadata.get("status") not in TERMINAL_JOB_STATUSES and wait_ms > 0:
                wait_started = time.monotonic()
                deadline = wait_started + wait_ms / 1000
                while True:
                    try:
                        file_size = os.fstat(handle.fileno()).st_size
                    except OSError as exc:
                        return _job_error("log_read_failed", f"Failed to inspect job log: {exc}", job_id=job_id)
                    if file_size > cursor:
                        break
                    metadata = self._refresh_metadata(job_dir, metadata)
                    if metadata.get("status") in TERMINAL_JOB_STATUSES:
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    time.sleep(min(_JOB_OUTPUT_POLL_SECONDS, remaining))
                waited_ms = min(wait_ms, max(0, round((time.monotonic() - wait_started) * 1000)))

            metadata = self._refresh_metadata(job_dir, metadata)
            try:
                file_size = os.fstat(handle.fileno()).st_size
            except OSError as exc:
                return _job_error("log_read_failed", f"Failed to inspect job log: {exc}", job_id=job_id)
            if cursor > file_size:
                return _job_error(
                    "invalid_cursor",
                    "cursor is beyond the current selected log size.",
                    job_id=job_id,
                    stream=stream,
                    cursor=cursor,
                    file_size=file_size,
                )

            try:
                handle.seek(cursor)
                raw = handle.read(min(max_bytes, file_size - cursor))
            except OSError as exc:
                return _job_error("log_read_failed", f"Failed to read job log: {exc}", job_id=job_id)
            try:
                file_size = os.fstat(handle.fileno()).st_size
            except OSError:
                pass

        terminal = metadata.get("status") in TERMINAL_JOB_STATUSES
        final_chunk = terminal and cursor + len(raw) >= file_size
        rendered = _render_job_output(raw, final=final_chunk, response_budget=response_budget)
        next_cursor = cursor + rendered["bytes_returned"]
        has_more = next_cursor < file_size
        caught_up = next_cursor == file_size
        eof = caught_up and terminal
        stop_reason = _job_output_stop_reason(
            rendered=rendered,
            raw_bytes=len(raw),
            requested_max_bytes=max_bytes,
            has_more=has_more,
            caught_up=caught_up,
            terminal=terminal,
            wait_ms=wait_ms,
            waited_ms=waited_ms,
        )
        payload = self._status_payload(job_dir, metadata)
        payload.update(
            {
                "success": True,
                "stream": stream,
                "log_path": str(log_path),
                "content": rendered["content"],
                "cursor": cursor,
                "next_cursor": next_cursor,
                "bytes_returned": rendered["bytes_returned"],
                "file_size": file_size,
                "has_more": has_more,
                "caught_up": caught_up,
                "eof": eof,
                "waited_ms": waited_ms,
                "truncated": has_more,
                "stop_reason": stop_reason,
                "estimated_tokens": rendered["estimated_tokens"],
                "effective_budgets": {
                    "bytes": max_bytes,
                    "tokens": response_budget.max_tokens,
                },
                "encoding": "utf-8",
                "token_encoding": response_budget.encoding_name,
                "decoding": {
                    "valid_utf8": rendered["replacement_count"] == 0,
                    "errors": "replace" if rendered["replacement_count"] else "strict",
                    "replacement_count": rendered["replacement_count"],
                    "replacement_used": rendered["replacement_count"] > 0,
                    "incomplete_trailing_bytes": rendered["incomplete_trailing_bytes"],
                    "next_unit_bytes_required": rendered["next_unit_bytes_required"],
                },
                "minimum_max_bytes_for_progress": (
                    rendered["next_unit_bytes_required"]
                    if rendered["bytes_returned"] == 0
                    and rendered["incomplete_trailing_bytes"]
                    else None
                ),
                "oversized_unit": rendered["oversized_unit"],
                "oversized_unit_marker": (
                    "first_complete_unit_exceeds_token_budget"
                    if rendered["oversized_unit"]
                    else None
                ),
            }
        )
        return payload

    def _refresh_metadata(self, job_dir: Path, metadata: dict[str, object]) -> dict[str, object]:
        try:
            snapshot = read_job_metadata(job_dir)
        except (OSError, ValueError):
            snapshot = None
        if snapshot is None:
            return metadata
        return self._reconcile_metadata(job_dir, snapshot)

    def _list_summary(self, job_dir: Path, metadata: dict[str, object]) -> dict[str, object]:
        payload = self._status_payload(job_dir, metadata)
        command = str(metadata.get("command") or "")
        command_truncated = len(command) > _JOB_LIST_COMMAND_MAX_CHARACTERS
        if command_truncated:
            command = command[: _JOB_LIST_COMMAND_MAX_CHARACTERS - 1] + "…"
        payload.update(
            {
                "command": command,
                "command_truncated": command_truncated,
                "command_characters": len(str(metadata.get("command") or "")),
                "started_at": _metadata_float(metadata.get("started_at")),
                "completed_at": _metadata_float(metadata.get("completed_at")),
            }
        )
        return payload

    def tail_job(
        self,
        *,
        job_id: str,
        state_dir: Path,
        stream: Literal["stdout", "stderr"],
        lines: int,
    ) -> dict[str, object]:
        loaded = self._load_job(job_id=job_id, state_dir=state_dir)
        if isinstance(loaded, dict):
            return loaded
        job_dir, metadata = loaded
        if stream not in {"stdout", "stderr"}:
            return _job_error("invalid_arguments", "stream must be one of: stdout, stderr.")
        requested_lines = int(lines)
        effective_lines = max(1, min(requested_lines, MAX_JOB_TAIL_LINES))
        log_path = job_dir / f"{stream}.log"
        try:
            with log_path.open("r", encoding="utf-8", errors="replace") as handle:
                tail = deque((line.rstrip("\n") for line in handle), maxlen=effective_lines)
        except OSError as exc:
            return _job_error("log_read_failed", f"Failed to read job log: {exc}", job_id=job_id)
        metadata = self._reconcile_metadata(job_dir, metadata)
        payload = self._status_payload(job_dir, metadata)
        tail_lines = list(tail)
        payload.update(
            {
                "success": True,
                "stream": stream,
                "log_path": str(log_path),
                "requested_lines": requested_lines,
                "max_lines": MAX_JOB_TAIL_LINES,
                "lines_returned": len(tail_lines),
                "truncated_to_max": requested_lines > MAX_JOB_TAIL_LINES,
                "lines": tail_lines,
                "content": "\n".join(tail_lines),
            }
        )
        return payload

    def kill_job(
        self,
        *,
        job_id: str,
        state_dir: Path,
        signal_name: Literal["TERM", "KILL"] = "TERM",
    ) -> dict[str, object]:
        loaded = self._load_job(job_id=job_id, state_dir=state_dir)
        if isinstance(loaded, dict):
            return loaded
        job_dir, metadata = loaded
        if signal_name not in {"TERM", "KILL"}:
            return _job_error("invalid_arguments", "signal must be one of: TERM, KILL.")
        metadata = self._reconcile_metadata(job_dir, metadata)
        if metadata.get("status") in TERMINAL_JOB_STATUSES:
            payload = self._status_payload(job_dir, metadata)
            payload.update(
                {
                    "success": True,
                    "signal": signal_name,
                    "signal_sent": False,
                    "already_completed": True,
                }
            )
            return payload

        pid = _metadata_int(metadata.get("pid"))
        pgid = _metadata_int(metadata.get("pgid"))
        identity_match = process_identity_matches(pid, metadata.get("process_identity"))
        if identity_match is not True:
            metadata = self._wait_for_terminal_record(job_dir, metadata)
            if metadata.get("status") in TERMINAL_JOB_STATUSES:
                payload = self._status_payload(job_dir, metadata)
                payload.update(
                    {
                        "success": True,
                        "signal": signal_name,
                        "signal_sent": False,
                        "already_completed": True,
                    }
                )
                return payload
            code = "job_identity_unverified" if identity_match is None else "job_not_running"
            return _job_error(
                code,
                "Refusing to signal a job whose recorded process identity is no longer verifiable.",
                job_id=job_id,
            )

        if os.name != "posix" or not hasattr(os, "killpg"):
            return _job_error(
                "job_kill_failed",
                "Durable process-group termination is unavailable on this platform.",
                job_id=job_id,
            )
        try:
            current_pgid = os.getpgid(pid)
        except OSError as exc:
            return _job_error("job_kill_failed", f"Failed to verify job process group: {exc}", job_id=job_id)
        if pgid is None or current_pgid != pgid or pgid != pid:
            return _job_error(
                "job_identity_mismatch",
                "Refusing to signal a process group that does not match the durable job record.",
                job_id=job_id,
            )
        if process_identity_matches(pid, metadata.get("process_identity")) is not True:
            return _job_error(
                "job_identity_mismatch",
                "Refusing to signal a job whose process identity changed during verification.",
                job_id=job_id,
            )

        signum = signal.SIGTERM if signal_name == "TERM" else getattr(signal, "SIGKILL", signal.SIGTERM)
        signal_sent = False
        verified_group_members: dict[int, str | None] = {}

        def signal_and_mark_kill_requested(current: dict[str, object]) -> dict[str, object]:
            nonlocal signal_sent, verified_group_members
            if current.get("status") in TERMINAL_JOB_STATUSES:
                return current
            current_pid = _metadata_int(current.get("pid"))
            current_pgid = _metadata_int(current.get("pgid"))
            if current_pid != pid or current_pgid != pgid:
                raise _JobSignalRefused("The durable process-group record changed during termination.")
            if process_identity_matches(current_pid, current.get("process_identity")) is not True:
                raise _JobSignalRefused("The recorded process identity changed during termination.")
            try:
                live_pgid = os.getpgid(current_pid)
            except OSError as exc:
                raise _JobSignalRefused(f"The recorded process group is no longer available: {exc}") from exc
            if live_pgid != current_pgid or live_pgid != current_pid:
                raise _JobSignalRefused("The recorded process group no longer matches the job leader.")
            verified_group_members = snapshot_process_group(live_pgid)
            if verified_group_members.get(current_pid) != current.get("process_identity"):
                raise _JobSignalRefused("The full job process group could not be verified before termination.")
            os.killpg(live_pgid, signum)
            signal_sent = True
            current["kill_signal"] = signal_name
            current["updated_at"] = time.time()
            return current

        try:
            # Signal while holding the metadata lock, then publish kill_signal
            # before the supervisor can record the resulting process exit.
            metadata = mutate_job_metadata(job_dir, signal_and_mark_kill_requested)
            if metadata.get("status") in TERMINAL_JOB_STATUSES:
                payload = self._status_payload(job_dir, metadata)
                payload.update(
                    {
                        "success": True,
                        "signal": signal_name,
                        "signal_sent": False,
                        "already_completed": True,
                    }
                )
                return payload
            if signal_name == "TERM":
                deadline = time.monotonic() + _JOB_TERM_GRACE_SECONDS
                while time.monotonic() < deadline and process_group_exists(pgid):
                    time.sleep(0.01)
                if process_group_exists(pgid):
                    if not process_group_matches_snapshot(pgid, verified_group_members):
                        return _job_error(
                            "job_identity_mismatch",
                            "Refusing KILL escalation because the surviving process group no longer matches the verified job tree.",
                            job_id=job_id,
                        )
                    os.killpg(pgid, getattr(signal, "SIGKILL", signal.SIGTERM))
        except (ProcessLookupError, _JobSignalRefused):
            metadata = self._wait_for_terminal_record(job_dir, metadata)
            if metadata.get("status") not in TERMINAL_JOB_STATUSES:
                return _job_error(
                    "job_not_running",
                    "The verified job process exited before it could be signaled.",
                    job_id=job_id,
                )
        except OSError as exc:
            return _job_error("job_kill_failed", f"Failed to signal job: {exc}", job_id=job_id)

        metadata = self._wait_for_terminal_record(job_dir, metadata)
        payload = self._status_payload(job_dir, metadata)
        payload.update(
            {
                "success": True,
                "signal": signal_name,
                "signal_sent": signal_sent,
                "already_completed": not signal_sent,
            }
        )
        return payload
    def _load_job(
        self,
        *,
        job_id: str,
        state_dir: Path,
    ) -> tuple[Path, dict[str, object]] | dict[str, object]:
        normalized = (job_id or "").strip()
        if not normalized or Path(normalized).name != normalized or not normalized.startswith("job_"):
            return _job_not_found(job_id)
        jobs_dir = _jobs_registry_path(state_dir)
        if jobs_dir.is_symlink():
            return _job_not_found(job_id)
        job_dir = jobs_dir / normalized
        if job_dir.is_symlink() or not job_dir.is_dir():
            return _job_not_found(job_id)
        metadata_path = job_dir / "metadata.json"
        try:
            metadata_stat = metadata_path.lstat()
        except FileNotFoundError:
            return _job_not_found(job_id)
        except OSError as exc:
            return _job_error(
                "job_metadata_invalid",
                f"Failed to inspect durable job metadata: {exc}",
                job_id=normalized,
            )
        if stat.S_ISLNK(metadata_stat.st_mode) or not stat.S_ISREG(metadata_stat.st_mode):
            return _job_error(
                "job_metadata_invalid",
                "Durable job metadata must be a regular non-symlink file.",
                job_id=normalized,
            )
        try:
            metadata = read_job_metadata(job_dir)
        except (OSError, ValueError) as exc:
            return _job_error(
                "job_metadata_invalid",
                f"Failed to read durable job metadata: {exc}",
                job_id=normalized,
            )
        if metadata is None or metadata.get("job_id") != normalized:
            return _job_not_found(job_id)
        return job_dir, metadata

    def _reconcile_metadata(self, job_dir: Path, metadata: dict[str, object]) -> dict[str, object]:
        if metadata.get("status") in TERMINAL_JOB_STATUSES:
            return metadata
        command_state = process_identity_matches(
            _metadata_int(metadata.get("pid")),
            metadata.get("process_identity"),
        )
        supervisor_state = process_identity_matches(
            _metadata_int(metadata.get("supervisor_pid")),
            metadata.get("supervisor_identity"),
        )
        updated_at = _metadata_float(metadata.get("updated_at")) or 0.0
        if (
            metadata.get("status") == "starting"
            and command_state is False
            and supervisor_state is False
            and time.time() - updated_at < _JOB_STARTING_STALE_GRACE_SECONDS
        ):
            return metadata
        if command_state is False and supervisor_state is False:
            return self._interrupt_stale_record(job_dir, metadata)
        return metadata

    def _interrupt_stale_record(self, job_dir: Path, metadata: dict[str, object]) -> dict[str, object]:
        def interrupt_if_still_stale(current: dict[str, object]) -> dict[str, object]:
            if current.get("status") in TERMINAL_JOB_STATUSES:
                return current
            command_state = process_identity_matches(
                _metadata_int(current.get("pid")),
                current.get("process_identity"),
            )
            supervisor_state = process_identity_matches(
                _metadata_int(current.get("supervisor_pid")),
                current.get("supervisor_identity"),
            )
            if command_state is False and supervisor_state is False:
                completed_at = time.time()
                current.update(
                    {
                        "status": "interrupted",
                        "completed_at": completed_at,
                        "updated_at": completed_at,
                        "interruption_reason": "supervisor_and_command_gone_without_terminal_record",
                    }
                )
            return current

        try:
            return mutate_job_metadata(job_dir, interrupt_if_still_stale)
        except (OSError, ValueError):
            return metadata

    def _wait_for_terminal_record(
        self,
        job_dir: Path,
        metadata: dict[str, object],
    ) -> dict[str, object]:
        deadline = time.monotonic() + _JOB_TERMINAL_RECORD_WAIT_SECONDS
        latest = metadata
        while time.monotonic() < deadline:
            try:
                snapshot = read_job_metadata(job_dir)
            except (OSError, ValueError):
                snapshot = None
            if snapshot is not None:
                latest = self._reconcile_metadata(job_dir, snapshot)
                if latest.get("status") in TERMINAL_JOB_STATUSES:
                    return latest
            time.sleep(0.01)
        return self._reconcile_metadata(job_dir, latest)

    def _status_payload(self, job_dir: Path, metadata: dict[str, object]) -> dict[str, object]:
        status_value = metadata.get("status")
        status = str(status_value) if status_value in TERMINAL_JOB_STATUSES else "running"
        pid = _metadata_int(metadata.get("pid"))
        running_identity = process_identity_matches(pid, metadata.get("process_identity"))
        cpu_percent, memory_mb = _process_stats(pid) if status == "running" and running_identity is True else (None, None)
        started_at = _metadata_float(metadata.get("started_at")) or time.time()
        completed_at = _metadata_float(metadata.get("completed_at"))
        elapsed_until = completed_at if status in TERMINAL_JOB_STATUSES and completed_at is not None else time.time()
        stdout_log = job_dir / "stdout.log"
        stderr_log = job_dir / "stderr.log"
        return {
            "job_id": metadata.get("job_id"),
            "name": metadata.get("name"),
            "command": metadata.get("command"),
            "cwd": metadata.get("cwd"),
            "status": status,
            "pid": pid,
            "elapsed_seconds": round(max(0.0, elapsed_until - started_at), 3),
            "exit_code": _metadata_int(metadata.get("exit_code")),
            "cpu_percent": cpu_percent,
            "memory_mb": memory_mb,
            "stdout_log": str(stdout_log),
            "stderr_log": str(stderr_log),
            "last_output_at": _last_job_output_at(stdout_log, stderr_log),
        }


def _durable_status(metadata: Mapping[str, object]) -> str:
    value = metadata.get("status")
    return str(value) if value in TERMINAL_JOB_STATUSES else "running"


def _job_metadata_validation_error(metadata: Mapping[str, object]) -> str | None:
    if metadata.get("schema_version") != JOB_METADATA_SCHEMA_VERSION:
        return "Skipped durable job record with an unsupported metadata schema version."
    if metadata.get("status") not in _DURABLE_JOB_STATUSES:
        return "Skipped durable job record with an invalid status."
    if _metadata_float(metadata.get("started_at")) is None:
        return "Skipped durable job record without a valid started_at timestamp."
    completed_at = metadata.get("completed_at")
    if completed_at is not None and _metadata_float(completed_at) is None:
        return "Skipped durable job record with an invalid completed_at timestamp."
    if not isinstance(metadata.get("command"), str) or not isinstance(metadata.get("cwd"), str):
        return "Skipped durable job record with invalid command or cwd fields."
    if metadata.get("name") is not None and not isinstance(metadata.get("name"), str):
        return "Skipped durable job record with an invalid name field."
    return None


def _job_record_files_validation_error(job_dir: Path) -> tuple[str, str] | None:
    for filename in ("metadata.json", "stdout.log", "stderr.log"):
        path = job_dir / filename
        try:
            file_stat = path.lstat()
        except OSError as exc:
            return "job_record_invalid", f"Skipped durable job record with an unreadable {filename}: {exc}"
        if stat.S_ISLNK(file_stat.st_mode):
            return "symlink_record", f"Skipped durable job record with symbolic-link {filename}."
        if not stat.S_ISREG(file_stat.st_mode):
            return "job_record_invalid", f"Skipped durable job record whose {filename} is not a regular file."
    return None


def _truncate_warning_message(message: str) -> str:
    if len(message) <= _JOB_LIST_WARNING_MESSAGE_MAX_CHARACTERS:
        return message
    return message[: _JOB_LIST_WARNING_MESSAGE_MAX_CHARACTERS - 1] + "…"


def _job_list_payload(
    *,
    jobs: list[dict[str, object]],
    total: int,
    offset: int,
    limit: int,
    status_filter: str,
    response_budget: ResponseBudget,
    measurement: BudgetMeasurement,
    stop_reason: str,
    skipped_count: int,
    warnings: list[dict[str, str]],
    warnings_truncated: bool,
    oversized_item: bool,
) -> dict[str, object]:
    returned = len(jobs)
    truncated = offset + returned < total
    next_offset = offset + returned if truncated and returned else None
    return {
        "success": True,
        "jobs": jobs,
        "total": total,
        "returned": returned,
        "returned_count": returned,
        "offset": offset,
        "limit": limit,
        "truncated": truncated,
        "next_offset": next_offset,
        "filter": status_filter,
        "skipped": {
            "count": skipped_count,
            "reported": len(warnings),
            "truncated": warnings_truncated,
            "warnings": warnings,
        },
        "page": {
            "state": "truncated" if truncated else "complete",
            "stop_reason": stop_reason,
            "returned_count": returned,
            "estimated_tokens": measurement.estimated_tokens,
            "token_encoding": response_budget.encoding_name,
            "effective_budgets": {
                "items": limit,
                "tokens": response_budget.max_tokens,
            },
            "budget_exceeded": {
                "tokens": measurement.exceeds_token_budget,
            },
            "oversized_item": oversized_item,
            "continuation": {
                "has_more": truncated,
                "next_offset": next_offset,
            },
        },
    }


def _measure_job_list_response(
    *,
    jobs: list[dict[str, object]],
    total: int,
    offset: int,
    limit: int,
    status_filter: str,
    response_budget: ResponseBudget,
    stop_reason: str,
    skipped_count: int,
    warnings: list[dict[str, str]],
    warnings_truncated: bool,
    oversized_item: bool,
) -> BudgetMeasurement:
    measurement = response_budget.measure("")
    for _ in range(4):
        payload = _job_list_payload(
            jobs=jobs,
            total=total,
            offset=offset,
            limit=limit,
            status_filter=status_filter,
            response_budget=response_budget,
            measurement=measurement,
            stop_reason=stop_reason,
            skipped_count=skipped_count,
            warnings=warnings,
            warnings_truncated=warnings_truncated,
            oversized_item=oversized_item,
        )
        updated = response_budget.measure(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
        if updated == measurement:
            break
        measurement = updated
    return measurement


def _open_job_log(log_path: Path) -> BinaryIO | dict[str, object]:
    if log_path.is_symlink():
        return _job_error("job_log_invalid", "The selected job log must not be a symbolic link.")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(log_path, flags)
    except OSError as exc:
        return _job_error("log_read_failed", f"Failed to open job log: {exc}")
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return _job_error("job_log_invalid", "The selected job log must be a regular file.")
        return os.fdopen(fd, "rb")
    except OSError as exc:
        os.close(fd)
        return _job_error("log_read_failed", f"Failed to inspect job log: {exc}")


def _utf8_expected_length(first: int) -> int | None:
    if first <= 0x7F:
        return 1
    if 0xC2 <= first <= 0xDF:
        return 2
    if 0xE0 <= first <= 0xEF:
        return 3
    if 0xF0 <= first <= 0xF4:
        return 4
    return None


def _utf8_prefix_is_valid(data: bytes, start: int, expected: int) -> bool:
    available = min(expected, len(data) - start)
    if available <= 1:
        return True
    first = data[start]
    second = data[start + 1]
    if first == 0xE0 and not 0xA0 <= second <= 0xBF:
        return False
    if first == 0xED and not 0x80 <= second <= 0x9F:
        return False
    if first == 0xF0 and not 0x90 <= second <= 0xBF:
        return False
    if first == 0xF4 and not 0x80 <= second <= 0x8F:
        return False
    if first not in {0xE0, 0xED, 0xF0, 0xF4} and not 0x80 <= second <= 0xBF:
        return False
    return all(0x80 <= data[start + index] <= 0xBF for index in range(2, available))


def _decode_utf8_units(
    data: bytes,
    *,
    final: bool,
) -> tuple[list[tuple[bytes, str, bool]], int]:
    units: list[tuple[bytes, str, bool]] = []
    index = 0
    while index < len(data):
        expected = _utf8_expected_length(data[index])
        if expected is None:
            units.append((data[index : index + 1], "\ufffd", True))
            index += 1
            continue
        remaining = len(data) - index
        if remaining < expected:
            if _utf8_prefix_is_valid(data, index, expected) and not final:
                break
            units.append((data[index : index + 1], "\ufffd", True))
            index += 1
            continue
        if not _utf8_prefix_is_valid(data, index, expected):
            units.append((data[index : index + 1], "\ufffd", True))
            index += 1
            continue
        raw_unit = data[index : index + expected]
        units.append((raw_unit, raw_unit.decode("utf-8"), False))
        index += expected
    return units, len(data) - index


def _render_job_output(
    raw: bytes,
    *,
    final: bool,
    response_budget: ResponseBudget,
) -> dict[str, object]:
    units, incomplete_trailing_bytes = _decode_utf8_units(raw, final=final)
    next_unit_bytes_required: int | None = None
    if incomplete_trailing_bytes:
        first = raw[len(raw) - incomplete_trailing_bytes]
        next_unit_bytes_required = _utf8_expected_length(first)
    rendered: list[str] = []
    bytes_returned = 0
    replacement_count = 0
    oversized_unit = False
    token_truncated = False
    measurement = response_budget.measure("")
    for raw_unit, text, replacement in units:
        candidate = "".join([*rendered, text])
        candidate_measurement = response_budget.measure(candidate)
        if candidate_measurement.fits:
            rendered.append(text)
            bytes_returned += len(raw_unit)
            replacement_count += int(replacement)
            measurement = candidate_measurement
            continue
        token_truncated = True
        if not rendered:
            rendered.append(text)
            bytes_returned += len(raw_unit)
            replacement_count += int(replacement)
            measurement = candidate_measurement
            oversized_unit = True
        break
    return {
        "content": "".join(rendered),
        "bytes_returned": bytes_returned,
        "estimated_tokens": measurement.estimated_tokens,
        "replacement_count": replacement_count,
        "incomplete_trailing_bytes": (
            incomplete_trailing_bytes
            if bytes_returned == sum(len(unit[0]) for unit in units)
            else 0
        ),
        "next_unit_bytes_required": next_unit_bytes_required,
        "oversized_unit": oversized_unit,
        "token_truncated": token_truncated,
    }


def _job_output_stop_reason(
    *,
    rendered: Mapping[str, object],
    raw_bytes: int,
    requested_max_bytes: int,
    has_more: bool,
    caught_up: bool,
    terminal: bool,
    wait_ms: int,
    waited_ms: int,
) -> str:
    if rendered.get("oversized_unit"):
        return "oversized_unit"
    if rendered.get("token_truncated"):
        return "token_budget"
    if rendered.get("incomplete_trailing_bytes"):
        if raw_bytes >= requested_max_bytes:
            return "byte_budget_before_utf8_unit"
        return "incomplete_utf8"
    if has_more and raw_bytes >= requested_max_bytes:
        return "byte_budget"
    if has_more:
        return "byte_budget"
    if terminal and caught_up:
        return "end_of_stream"
    if wait_ms > 0 and waited_ms >= wait_ms:
        return "wait_timeout"
    return "caught_up_nonterminal"


def _terminate_recorded_job_group(metadata: Mapping[str, object], deadline: float) -> str:
    pid = _metadata_int(metadata.get("pid"))
    pgid = _metadata_int(metadata.get("pgid"))
    expected_identity = metadata.get("process_identity")
    if pid is None or pgid is None:
        return "job_group=not_recorded"
    identity_match = process_identity_matches(pid, expected_identity)
    if identity_match is False:
        return "job_group=gone"
    if identity_match is not True:
        return "job_group=identity_unverified"
    if os.name != "posix" or not hasattr(os, "killpg") or pgid != pid or pid == os.getpid():
        return "job_group=identity_mismatch"
    try:
        if os.getpgid(pid) != pgid:
            return "job_group=identity_mismatch"
    except OSError:
        return "job_group=gone"

    expected_members = snapshot_process_group(pgid)
    if expected_members.get(pid) != expected_identity:
        return "job_group=identity_mismatch"
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return "job_group=gone"
    except OSError as exc:
        return f"job_group=term_failed({exc})"

    term_deadline = min(deadline, time.monotonic() + _PROCESS_TREE_TERM_GRACE_SECONDS)
    while process_group_exists(pgid) and time.monotonic() < term_deadline:
        time.sleep(0.01)
    if process_group_exists(pgid):
        if not process_group_matches_snapshot(pgid, expected_members):
            return "job_group=kill_refused_identity_changed"
        try:
            os.killpg(pgid, getattr(signal, "SIGKILL", signal.SIGTERM))
        except ProcessLookupError:
            return "job_group=gone"
        except OSError as exc:
            return f"job_group=kill_failed({exc})"
    while process_group_exists(pgid) and time.monotonic() < deadline:
        time.sleep(0.01)
    return "job_group=stopped" if not process_group_exists(pgid) else "job_group=still_alive"


def _terminate_recorded_supervisor(metadata: Mapping[str, object], deadline: float) -> str:
    pid = _metadata_int(metadata.get("supervisor_pid"))
    expected_identity = metadata.get("supervisor_identity")
    if pid is None:
        return "supervisor=not_recorded"
    identity_match = process_identity_matches(pid, expected_identity)
    if identity_match is False:
        return "supervisor=gone"
    if identity_match is not True or pid == os.getpid():
        return "supervisor=identity_unverified"
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return "supervisor=gone"
    except OSError as exc:
        return f"supervisor=term_failed({exc})"

    term_deadline = min(deadline, time.monotonic() + _PROCESS_TREE_TERM_GRACE_SECONDS)
    while process_identity_matches(pid, expected_identity) is True and time.monotonic() < term_deadline:
        time.sleep(0.01)
    if process_identity_matches(pid, expected_identity) is True:
        try:
            os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        except ProcessLookupError:
            return "supervisor=gone"
        except OSError as exc:
            return f"supervisor=kill_failed({exc})"
    while process_identity_matches(pid, expected_identity) is True and time.monotonic() < deadline:
        time.sleep(0.01)
    return "supervisor=stopped" if process_identity_matches(pid, expected_identity) is not True else "supervisor=still_alive"


def _terminate_and_reap_bootstrap(supervisor: subprocess.Popen[bytes], deadline: float) -> str:
    if supervisor.poll() is None:
        try:
            supervisor.terminate()
        except ProcessLookupError:
            pass
        except OSError as exc:
            return f"bootstrap=term_failed({exc})"
    remaining = max(0.0, deadline - time.monotonic())
    try:
        supervisor.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        try:
            supervisor.kill()
        except ProcessLookupError:
            pass
        except OSError as exc:
            return f"bootstrap=kill_failed({exc})"
        try:
            supervisor.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            return "bootstrap=still_alive"
    return f"bootstrap=reaped({supervisor.returncode})"


def _new_job_id() -> str:
    return f"job_{int(time.time() * 1000)}_{secrets.token_urlsafe(6)}"


def _job_error(code: str, message: str, **extra: object) -> dict[str, object]:
    return {
        "success": False,
        "error": {
            "code": code,
            "message": message,
        },
        **extra,
    }


def _job_not_found(job_id: str) -> dict[str, object]:
    return _job_error("job_not_found", f"Job not found: {(job_id or '').strip()}", job_id=job_id)


def _cwd_error(cwd: Path) -> dict[str, object] | None:
    if not cwd.exists():
        return _job_error(
            "cwd_not_found",
            f"Working directory not found: {cwd}",
            cwd=str(cwd),
        )
    if not cwd.is_dir():
        return _job_error(
            "cwd_not_directory",
            f"Working directory is not a directory: {cwd}",
            cwd=str(cwd),
        )
    return None


def _jobs_registry_path(state_dir: Path) -> Path:
    return Path(state_dir).expanduser().resolve() / "jobs"


def _jobs_runtime_dir(state_dir: Path) -> Path:
    private_state_dir = Path(state_dir).expanduser().resolve()
    ensure_private_directory(private_state_dir)
    jobs_dir = private_state_dir / "jobs"
    if jobs_dir.is_symlink():
        raise ValueError("The durable jobs registry must not be a symbolic link.")
    ensure_private_directory(jobs_dir)
    return jobs_dir


def _merged_job_env(env: Mapping[str, str] | None) -> dict[str, str] | dict[str, object] | None:
    if env is None:
        return None
    merged = os.environ.copy()
    for key, value in env.items():
        if not isinstance(key, str) or not key or "\x00" in key or "=" in key:
            return _job_error(
                "invalid_env",
                "Environment variable names must be non-empty strings without NUL bytes or '='.",
            )
        if not isinstance(value, str) or "\x00" in value:
            return _job_error("invalid_env", "Environment variable values must be strings without NUL bytes.")
        merged[key] = value
    return merged


def _metadata_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _metadata_float(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _process_stats(pid: int | None) -> tuple[float | None, float | None]:
    if pid is None:
        return None, None
    try:
        completed = subprocess.run(
            ["ps", "-o", "%cpu=", "-o", "rss=", "-p", str(pid)],
            text=True,
            capture_output=True,
            timeout=1,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    if completed.returncode != 0:
        return None, None
    line = completed.stdout.strip().splitlines()
    if not line:
        return None, None
    parts = line[0].split()
    if len(parts) < 2:
        return None, None
    try:
        return float(parts[0]), round(int(parts[1]) / 1024, 3)
    except ValueError:
        return None, None


def _last_job_output_at(stdout_log: Path, stderr_log: Path) -> float | None:
    mtimes: list[float] = []
    for path in [stdout_log, stderr_log]:
        try:
            file_stat = path.lstat()
        except OSError:
            continue
        if stat.S_ISREG(file_stat.st_mode) and file_stat.st_size > 0:
            mtimes.append(file_stat.st_mtime)
    if not mtimes:
        return None
    return round(max(mtimes), 3)


def _drain_pipe(pipe: BinaryIO, capture: _BoundedStreamCapture) -> None:
    try:
        while True:
            chunk = pipe.read(_PIPE_READ_CHUNK_BYTES)
            if not chunk:
                break
            capture.add(chunk)
    except (OSError, ValueError):
        # A forced process-tree shutdown can close a pipe while a reader is
        # blocked. Output successfully observed before that point is retained.
        pass
    finally:
        try:
            pipe.close()
        except OSError:
            pass


def _process_group_exists(process_group_id: int) -> bool:
    if os.name != "posix" or not hasattr(os, "killpg"):
        return False
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _terminate_process_tree(process: subprocess.Popen[bytes], process_group_id: int | None) -> None:
    """Terminate the foreground command and descendants in its dedicated group."""

    if os.name == "posix" and process_group_id is not None and hasattr(os, "killpg"):
        try:
            os.killpg(process_group_id, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass

        deadline = time.monotonic() + _PROCESS_TREE_TERM_GRACE_SECONDS
        while time.monotonic() < deadline and _process_group_exists(process_group_id):
            time.sleep(0.01)
        if _process_group_exists(process_group_id):
            try:
                os.killpg(process_group_id, getattr(signal, "SIGKILL", signal.SIGTERM))
            except (ProcessLookupError, PermissionError):
                pass
    elif os.name == "nt":
        # CREATE_NEW_PROCESS_GROUP alone does not make terminate() recursive.
        # taskkill /T is the native bounded tree-kill fallback on Windows.
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            if process.poll() is None:
                process.kill()
    elif process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=_PROCESS_TREE_TERM_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()

    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass


def _capture_text(capture: _BoundedStreamCapture, stream: str) -> str:
    if capture.dropped_bytes == 0:
        return (capture.head + capture.tail).decode("utf-8", errors="replace")
    marker = (
        f"\n... [{stream}: {capture.dropped_bytes} bytes omitted by capture limit] ...\n"
    )
    return (
        capture.head.decode("utf-8", errors="replace")
        + marker
        + capture.tail.decode("utf-8", errors="replace")
    )


def _token_omission_marker(max_tokens: int) -> str:
    budget = ResponseBudget(max_tokens=max(1, max_tokens))
    for marker in (
        "\n... [output omitted to fit run token budget] ...\n",
        "\n...\n",
        "…",
    ):
        if budget.count_tokens(marker) <= max_tokens:
            return marker
    return ""


def _truncate_text_to_tokens(text: str, max_tokens: int) -> tuple[str, bool]:
    if not text:
        return "", False
    if max_tokens <= 0:
        return "", True

    budget = ResponseBudget(max_tokens=max_tokens)
    if budget.measure(text).fits:
        return text, False

    marker = _token_omission_marker(max_tokens)

    def candidate(kept_characters: int) -> str:
        head_characters = (kept_characters + 1) // 2
        tail_characters = kept_characters // 2
        tail = text[-tail_characters:] if tail_characters else ""
        return text[:head_characters] + marker + tail

    low = 0
    high = max(0, len(text) - 1)
    while low < high:
        midpoint = (low + high + 1) // 2
        if budget.measure(candidate(midpoint)).fits:
            low = midpoint
        else:
            high = midpoint - 1
    rendered = candidate(low)
    if not budget.measure(rendered).fits:
        rendered = marker if budget.measure(marker).fits else ""
    return rendered, True


def _allocate_stream_token_budgets(needs: dict[str, int], max_tokens: int) -> dict[str, int]:
    allocations = {stream: 0 for stream in needs}
    pending = {stream for stream, needed in needs.items() if needed > 0}
    remaining = max_tokens
    while pending:
        share = remaining // len(pending)
        satisfied = {stream for stream in pending if needs[stream] <= share}
        if not satisfied:
            ordered = sorted(pending)
            for index, stream in enumerate(ordered):
                allocations[stream] = share + int(index < remaining % len(ordered))
            break
        for stream in satisfied:
            allocations[stream] = needs[stream]
            remaining -= needs[stream]
        pending -= satisfied
    return allocations


def _render_captured_output(
    *,
    stdout_capture: _BoundedStreamCapture,
    stderr_capture: _BoundedStreamCapture,
    max_tokens: int,
    capture_max_bytes: int,
) -> tuple[str, str, dict[str, object]]:
    response_budget = ResponseBudget(max_tokens=max_tokens)
    captures = {"stdout": stdout_capture, "stderr": stderr_capture}
    initial = {stream: _capture_text(capture, stream) for stream, capture in captures.items()}
    rendered = dict(initial)
    allocations = {stream: max_tokens for stream in captures}
    budget_truncated = {stream: False for stream in captures}
    oversized_stream: str | None = None

    combined_measurement = response_budget.measure(initial["stdout"] + initial["stderr"])
    active_streams = [stream for stream, capture in captures.items() if capture.total_bytes > 0]
    if not combined_measurement.fits:
        if (
            len(active_streams) == 1
            and captures[active_streams[0]].dropped_bytes == 0
            and captures[active_streams[0]].total_lines == 1
        ):
            oversized_stream = active_streams[0]
        else:
            needs = {
                stream: response_budget.count_tokens(text)
                for stream, text in initial.items()
            }
            allocations = _allocate_stream_token_budgets(needs, max_tokens)
            for stream in captures:
                rendered[stream], budget_truncated[stream] = _truncate_text_to_tokens(
                    initial[stream],
                    allocations[stream],
                )

            # Tokenization can change at the stdout/stderr boundary. Re-measure
            # the actual concatenated payload and tighten the larger stream if
            # that boundary makes it exceed the global ceiling.
            for _ in range(8):
                combined_measurement = response_budget.measure(
                    rendered["stdout"] + rendered["stderr"]
                )
                if combined_measurement.fits:
                    break
                stream = max(
                    captures,
                    key=lambda item: response_budget.count_tokens(rendered[item]),
                )
                excess = combined_measurement.estimated_tokens - max_tokens
                allocations[stream] = max(0, allocations[stream] - max(1, excess))
                rendered[stream], budget_truncated[stream] = _truncate_text_to_tokens(
                    initial[stream],
                    allocations[stream],
                )
            else:
                stream = max(
                    captures,
                    key=lambda item: response_budget.count_tokens(rendered[item]),
                )
                allocations[stream] = 0
                rendered[stream] = ""
                budget_truncated[stream] = bool(initial[stream])

    stream_metadata: dict[str, dict[str, object]] = {}
    for stream, capture in captures.items():
        measurement = response_budget.measure(rendered[stream])
        truncated = capture.dropped_bytes > 0 or budget_truncated[stream]
        effective_budget = allocations[stream]
        stream_metadata[stream] = {
            "total_bytes": capture.total_bytes,
            "total_lines": capture.total_lines,
            "retained_bytes": capture.retained_bytes,
            "retained_lines": capture.retained_lines,
            "displayed_bytes": measurement.rendered_bytes,
            "displayed_lines": _count_lines(rendered[stream]),
            "capture_dropped_bytes": capture.dropped_bytes,
            "dropped": truncated,
            "truncated": truncated,
            "capture_truncated": capture.dropped_bytes > 0,
            "token_truncated": budget_truncated[stream],
            "token_count": measurement.estimated_tokens,
            "effective_token_budget": effective_budget,
            "fits_token_budget": measurement.estimated_tokens <= effective_budget,
            "omission_marker": truncated,
            "oversized_line": oversized_stream == stream,
            "long_line_truncated": capture.total_lines == 1 and truncated,
        }

    combined_measurement = response_budget.measure(rendered["stdout"] + rendered["stderr"])
    aggregate_truncated = any(metadata["truncated"] for metadata in stream_metadata.values())
    aggregate = {
        "total_bytes": sum(capture.total_bytes for capture in captures.values()),
        "total_lines": sum(capture.total_lines for capture in captures.values()),
        "retained_bytes": sum(capture.retained_bytes for capture in captures.values()),
        "retained_lines": sum(capture.retained_lines for capture in captures.values()),
        "displayed_bytes": combined_measurement.rendered_bytes,
        "displayed_lines": sum(_count_lines(rendered[stream]) for stream in captures),
        "capture_dropped_bytes": sum(capture.dropped_bytes for capture in captures.values()),
        "dropped": aggregate_truncated,
        "truncated": aggregate_truncated,
        "capture_truncated": any(capture.dropped_bytes > 0 for capture in captures.values()),
        "token_truncated": any(budget_truncated.values()),
        "token_count": combined_measurement.estimated_tokens,
        "effective_token_budget": max_tokens,
        "fits_token_budget": combined_measurement.fits,
        "displayed_payload_fits": combined_measurement.fits,
        "token_encoding": response_budget.encoding_name,
        "capture_memory_limit_bytes": capture_max_bytes,
        "oversized_line": oversized_stream is not None,
        "oversized_stream": oversized_stream,
    }
    return rendered["stdout"], rendered["stderr"], {
        **stream_metadata,
        "aggregate": aggregate,
    }


def _timeout_limit_error(timeout: int) -> dict[str, object]:
    return {
        "success": False,
        "error": {
            "code": "timeout_exceeds_limit",
            "message": (
                f"run_command timeout is limited to {MAX_COMMAND_TIMEOUT_SECONDS}s. "
                "Complex or long-running tasks should be delegated with delegate_task so Codex can "
                "run them with local audit logs. If run_command is still required, set force=true "
                "only after explicit user approval."
            ),
            "requested_timeout_seconds": timeout,
            "max_timeout_seconds": MAX_COMMAND_TIMEOUT_SECONDS,
            "force_required": True,
            "approval_required": True,
        },
        "exit_code": TIMEOUT_EXIT_CODE,
        "stdout": "",
        "stderr": "",
        "timed_out": False,
        "timeout": timeout,
        "hint": "delegate_task_or_force_after_user_approval",
    }


def run_command(
    *,
    command: str,
    cwd: Path,
    timeout: int,
    force: bool = False,
    max_tokens: int = DEFAULT_TOOL_OUTPUT_TOKEN_BUDGET,
    capture_max_bytes: int = DEFAULT_RUN_CAPTURE_MAX_BYTES,
) -> dict[str, object]:
    if timeout > MAX_COMMAND_TIMEOUT_SECONDS and not force:
        payload = _timeout_limit_error(timeout)
        payload["command"] = command
        payload["cwd"] = str(cwd)
        return payload

    if not cwd.exists():
        return {
            "success": False,
            "error": {
                "code": "cwd_not_found",
                "message": f"Working directory not found: {cwd}",
            },
            "cwd": str(cwd),
            "command": command,
            "exit_code": TIMEOUT_EXIT_CODE,
            "stdout": "",
            "stderr": "",
            "timed_out": False,
            "timeout": timeout,
        }
    if not cwd.is_dir():
        return {
            "success": False,
            "error": {
                "code": "cwd_not_directory",
                "message": f"Working directory is not a directory: {cwd}",
            },
            "cwd": str(cwd),
            "command": command,
            "exit_code": TIMEOUT_EXIT_CODE,
            "stdout": "",
            "stderr": "",
            "timed_out": False,
            "timeout": timeout,
        }

    response_budget = ResponseBudget(max_tokens=max_tokens)
    effective_capture_max_bytes = int(capture_max_bytes)
    if effective_capture_max_bytes <= 0:
        raise ValueError(
            f"capture_max_bytes must be a positive integer; got {capture_max_bytes!r}."
        )

    stdout_capture = _BoundedStreamCapture((effective_capture_max_bytes + 1) // 2)
    stderr_capture = _BoundedStreamCapture(effective_capture_max_bytes // 2)
    popen_kwargs: dict[str, object] = {}
    process_group_id: int | None = None
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    elif os.name == "nt" and hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    try:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **popen_kwargs,
        )
    except (OSError, ValueError) as exc:
        return {
            "success": False,
            "command": command,
            "cwd": str(cwd),
            "exit_code": TIMEOUT_EXIT_CODE,
            "stdout": "",
            "stderr": "",
            "timed_out": False,
            "timeout": timeout,
            "force": force,
            "error": {
                "code": "command_start_failed",
                "message": f"Failed to start command: {exc}",
            },
        }

    if os.name == "posix":
        process_group_id = process.pid
    assert process.stdout is not None
    assert process.stderr is not None
    readers = [
        threading.Thread(
            target=_drain_pipe,
            args=(process.stdout, stdout_capture),
            name=f"run-command-{process.pid}-stdout",
            daemon=True,
        ),
        threading.Thread(
            target=_drain_pipe,
            args=(process.stderr, stderr_capture),
            name=f"run-command-{process.pid}-stderr",
            daemon=True,
        ),
    ]
    for reader in readers:
        reader.start()

    deadline = time.monotonic() + max(0, timeout)
    timed_out = False
    try:
        process.wait(timeout=max(0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        timed_out = True

    if not timed_out:
        for reader in readers:
            reader.join(timeout=max(0, deadline - time.monotonic()))
        timed_out = any(reader.is_alive() for reader in readers)

    if timed_out:
        _terminate_process_tree(process, process_group_id)

    for reader in readers:
        reader.join(timeout=1)
    if any(reader.is_alive() for reader in readers):
        for pipe in (process.stdout, process.stderr):
            try:
                pipe.close()
            except OSError:
                pass
        for reader in readers:
            reader.join(timeout=1)

    stdout, stderr, output_metadata = _render_captured_output(
        stdout_capture=stdout_capture,
        stderr_capture=stderr_capture,
        max_tokens=response_budget.max_tokens,
        capture_max_bytes=effective_capture_max_bytes,
    )

    if timed_out:
        return {
            "success": False,
            "command": command,
            "cwd": str(cwd),
            "exit_code": TIMEOUT_EXIT_CODE,
            "stdout": stdout,
            "stderr": stderr,
            "timed_out": True,
            "timeout": timeout,
            "output_metadata": output_metadata,
            "error": {
                "code": "timed_out",
                "message": (
                    f"Command exceeded the {timeout}s timeout. "
                    "Retry with a larger `timeout` argument, or use "
                    "`delegate_task` for a serialized Codex handoff."
                ),
            },
            "hint": "increase_timeout_or_delegate",
        }

    exit_code = process.returncode
    if exit_code is None:
        exit_code = TIMEOUT_EXIT_CODE
    return {
        "success": exit_code == 0,
        "command": command,
        "cwd": str(cwd),
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "timed_out": False,
        "timeout": timeout,
        "force": force,
        "output_metadata": output_metadata,
    }


def _invalid_batch_arguments(message: str) -> dict[str, object]:
    return {
        "success": False,
        "mode": "batch",
        "error": {
            "code": "invalid_arguments",
            "message": message,
        },
        "results": [],
    }


def _with_batch_index(index: int, result: dict[str, object]) -> dict[str, object]:
    payload = dict(result)
    payload["index"] = index
    return payload


def _fit_command_batch_to_budget(
    payload: dict[str, object],
    *,
    max_tokens: int,
) -> dict[str, object]:
    """Apply one shared response budget across all command results."""
    budget = ResponseBudget(max_tokens=max_tokens)
    working = copy.deepcopy(payload)
    results = working.get("results", [])
    assert isinstance(results, list)

    content_needs: dict[str, int] = {}
    for result in results:
        if not isinstance(result, dict):
            continue
        index = int(result.get("index", 0))
        for stream in ("stdout", "stderr"):
            text = str(result.get(stream, ""))
            content_needs[f"{index}:{stream}"] = budget.count_tokens(text) if text else 0

    # Reserve half of the batch budget for result envelopes and diagnostics.
    allocations = _allocate_stream_token_budgets(content_needs, max_tokens // 2)
    any_truncated = False
    for result in results:
        if not isinstance(result, dict):
            continue
        index = int(result.get("index", 0))
        for stream in ("stdout", "stderr"):
            original = str(result.get(stream, ""))
            rendered, truncated = _truncate_text_to_tokens(
                original,
                allocations.get(f"{index}:{stream}", 0),
            )
            result[stream] = rendered
            any_truncated = any_truncated or truncated
        if any(
            allocations.get(f"{index}:{stream}", 0)
            < content_needs.get(f"{index}:{stream}", 0)
            for stream in ("stdout", "stderr")
        ):
            result["batch_budget_truncated"] = True

    working["next_offset"] = None
    rendered, measurement = with_budget_metadata(
        working,
        budget=budget,
        truncated=any_truncated,
        stop_reason="token_budget" if any_truncated else "end_of_results",
    )
    if measurement.fits:
        return rendered

    # Detailed per-stream capture metadata is useful but secondary to command
    # outcome. Compact it before omitting any result.
    any_truncated = True
    compact_results = rendered.get("results", [])
    assert isinstance(compact_results, list)
    for result in compact_results:
        if not isinstance(result, dict) or "output_metadata" not in result:
            continue
        aggregate = result.get("output_metadata")
        token_count = None
        if isinstance(aggregate, dict):
            aggregate_value = aggregate.get("aggregate")
            if isinstance(aggregate_value, dict):
                token_count = aggregate_value.get("token_count")
        result["output_metadata"] = {
            "aggregate": {
                "token_count": token_count,
                "batch_budget_truncated": True,
            }
        }

    rendered, measurement = with_budget_metadata(
        rendered,
        budget=budget,
        truncated=True,
        stop_reason="token_budget",
    )
    if measurement.fits:
        return rendered

    # Preserve every command's terminal outcome even when the configured
    # budget cannot carry the normal result envelope.
    summaries: list[dict[str, object]] = []
    for result in compact_results:
        if not isinstance(result, dict):
            continue
        summaries.append(
            {
                "index": result.get("index"),
                "success": result.get("success"),
                "exit_code": result.get("exit_code"),
                "timed_out": result.get("timed_out"),
                "output_omitted_by_batch_budget": True,
            }
        )
    rendered["results"] = summaries
    rendered, _measurement = with_budget_metadata(
        rendered,
        budget=budget,
        truncated=True,
        stop_reason="token_budget",
    )
    return rendered


def run_commands(
    *,
    commands: list[str],
    cwd: Path,
    timeout: int,
    force: bool = False,
    mode: Literal["sequential", "parallel"] = "sequential",
    max_concurrency: int = MAX_COMMAND_BATCH_CONCURRENCY,
    max_tokens: int = DEFAULT_TOOL_OUTPUT_TOKEN_BUDGET,
    capture_max_bytes: int = DEFAULT_RUN_CAPTURE_MAX_BYTES,
) -> dict[str, object]:
    if timeout > MAX_COMMAND_TIMEOUT_SECONDS and not force:
        payload = _timeout_limit_error(timeout)
        payload.update(
            {
                "mode": "batch",
                "cwd": str(cwd),
                "results": [],
                "command_count": len(commands),
            }
        )
        return payload

    if not commands:
        return _invalid_batch_arguments("Provide at least one command.")
    if any(not command.strip() for command in commands):
        return _invalid_batch_arguments("Batch commands must be non-empty strings.")
    if len(commands) > MAX_COMMAND_BATCH_SIZE:
        return _invalid_batch_arguments(
            f"At most {MAX_COMMAND_BATCH_SIZE} commands may be executed in one batch."
        )
    if mode not in {"sequential", "parallel"}:
        return _invalid_batch_arguments("mode must be one of: sequential, parallel.")
    if max_concurrency < 1 or max_concurrency > MAX_COMMAND_BATCH_CONCURRENCY:
        return _invalid_batch_arguments(
            f"max_concurrency must be between 1 and {MAX_COMMAND_BATCH_CONCURRENCY}."
        )

    # Each command may provisionally use the full ceiling; the ordered results
    # are then water-filled into one shared batch budget. This lets short or
    # silent commands donate unused capacity instead of enforcing a static split.
    per_command_max_tokens = max_tokens

    if mode == "sequential":
        results = [
            _with_batch_index(
                index,
                run_command(
                    command=command,
                    cwd=cwd,
                    timeout=timeout,
                    force=force,
                    max_tokens=per_command_max_tokens,
                    capture_max_bytes=capture_max_bytes,
                ),
            )
            for index, command in enumerate(commands)
        ]
        effective_concurrency = 1
    else:
        effective_concurrency = min(max_concurrency, len(commands))
        ordered_results: list[dict[str, object] | None] = [None] * len(commands)
        with ThreadPoolExecutor(max_workers=effective_concurrency) as executor:
            futures = {
                executor.submit(
                    run_command,
                    command=command,
                    cwd=cwd,
                    timeout=timeout,
                    force=force,
                    max_tokens=per_command_max_tokens,
                    capture_max_bytes=capture_max_bytes,
                ): index
                for index, command in enumerate(commands)
            }
            for future in as_completed(futures):
                index = futures[future]
                ordered_results[index] = _with_batch_index(index, future.result())
        results = [item for item in ordered_results if item is not None]

    failed_count = sum(1 for item in results if not item.get("success"))
    timed_out_count = sum(1 for item in results if item.get("timed_out"))
    payload: dict[str, object] = {
        "success": failed_count == 0,
        "mode": "batch",
        "execution_mode": mode,
        "command_count": len(commands),
        "completed_count": len(results),
        "failed_count": failed_count,
        "timed_out_count": timed_out_count,
        "max_concurrency": effective_concurrency,
        "timeout": timeout,
        "force": force,
        "results": results,
    }
    return _fit_command_batch_to_budget(payload, max_tokens=max_tokens)
