from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import deque
from dataclasses import dataclass
import os
import secrets
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Literal, Mapping


# Sentinel exit code used when the process did not produce a real return code
# (e.g. it timed out or never started). Keeping this as an int (not None) so
# callers can always do numeric comparisons like `exit_code == 0` without
# special-casing `None`.
TIMEOUT_EXIT_CODE = -1
MAX_COMMAND_TIMEOUT_SECONDS = 300
MAX_COMMAND_BATCH_CONCURRENCY = 3
MAX_COMMAND_BATCH_SIZE = 20
MAX_JOB_TAIL_LINES = 500


@dataclass
class JobRecord:
    job_id: str
    name: str | None
    command: str
    cwd: Path
    process: subprocess.Popen[bytes]
    stdout_log: Path
    stderr_log: Path
    started_at: float
    started_at_epoch: float
    completed_at: float | None = None
    kill_signal: str | None = None


class JobRegistry:
    """Small in-process background subprocess registry."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, JobRecord] = {}

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
        try:
            job_dir = _jobs_runtime_dir(state_dir) / job_id
            job_dir.mkdir(parents=True, exist_ok=False)
            try:
                job_dir.chmod(0o700)
            except OSError:
                pass
            stdout_log = job_dir / "stdout.log"
            stderr_log = job_dir / "stderr.log"
            with stdout_log.open("ab") as stdout_file, stderr_log.open("ab") as stderr_file:
                popen_kwargs: dict[str, object] = {}
                if os.name == "posix":
                    popen_kwargs["start_new_session"] = True
                process = subprocess.Popen(
                    normalized_command,
                    cwd=str(cwd),
                    shell=True,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    env=env_result,
                    **popen_kwargs,
                )
        except (OSError, ValueError) as exc:
            return _job_error(
                "job_start_failed",
                f"Failed to start job: {exc}",
                cwd=str(cwd),
                command=normalized_command,
            )

        record = JobRecord(
            job_id=job_id,
            name=(name or "").strip() or None,
            command=normalized_command,
            cwd=cwd,
            process=process,
            stdout_log=stdout_log,
            stderr_log=stderr_log,
            started_at=time.monotonic(),
            started_at_epoch=time.time(),
        )
        with self._lock:
            self._jobs[job_id] = record
        payload = self._status_payload(record)
        payload["success"] = True
        return payload

    def job_status(self, *, job_id: str) -> dict[str, object]:
        record = self._find_job(job_id)
        if record is None:
            return _job_not_found(job_id)
        payload = self._status_payload(record)
        payload["success"] = True
        return payload

    def tail_job(self, *, job_id: str, stream: Literal["stdout", "stderr"], lines: int) -> dict[str, object]:
        record = self._find_job(job_id)
        if record is None:
            return _job_not_found(job_id)
        if stream not in {"stdout", "stderr"}:
            return _job_error("invalid_arguments", "stream must be one of: stdout, stderr.")
        requested_lines = int(lines)
        effective_lines = max(1, min(requested_lines, MAX_JOB_TAIL_LINES))
        log_path = record.stdout_log if stream == "stdout" else record.stderr_log
        try:
            with log_path.open("r", encoding="utf-8", errors="replace") as handle:
                tail = deque((line.rstrip("\n") for line in handle), maxlen=effective_lines)
        except OSError as exc:
            return _job_error("log_read_failed", f"Failed to read job log: {exc}", job_id=record.job_id)
        payload = self._status_payload(record)
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

    def kill_job(self, *, job_id: str, signal_name: Literal["TERM", "KILL"] = "TERM") -> dict[str, object]:
        record = self._find_job(job_id)
        if record is None:
            return _job_not_found(job_id)
        if signal_name not in {"TERM", "KILL"}:
            return _job_error("invalid_arguments", "signal must be one of: TERM, KILL.")
        exit_code = self._refresh(record)
        if exit_code is not None:
            payload = self._status_payload(record)
            payload.update(
                {
                    "success": True,
                    "signal": signal_name,
                    "signal_sent": False,
                    "already_completed": True,
                }
            )
            return payload

        signum = signal.SIGTERM if signal_name == "TERM" else getattr(signal, "SIGKILL", signal.SIGTERM)
        try:
            _send_job_signal(record.process, signum)
            record.kill_signal = signal_name
            try:
                record.process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                pass
        except ProcessLookupError:
            self._refresh(record)
        except OSError as exc:
            return _job_error("job_kill_failed", f"Failed to signal job: {exc}", job_id=record.job_id)

        payload = self._status_payload(record)
        payload.update(
            {
                "success": True,
                "signal": signal_name,
                "signal_sent": True,
                "already_completed": False,
            }
        )
        return payload

    def _find_job(self, job_id: str) -> JobRecord | None:
        normalized = (job_id or "").strip()
        with self._lock:
            return self._jobs.get(normalized)

    def _refresh(self, record: JobRecord) -> int | None:
        exit_code = record.process.poll()
        if exit_code is not None and record.completed_at is None:
            record.completed_at = time.monotonic()
        return exit_code

    def _status_payload(self, record: JobRecord) -> dict[str, object]:
        exit_code = self._refresh(record)
        running = exit_code is None
        cpu_percent, memory_mb = _process_stats(record.process.pid) if running else (None, None)
        if running:
            status = "running"
            elapsed_until = time.monotonic()
        else:
            status = _terminal_job_status(record, exit_code)
            elapsed_until = record.completed_at or time.monotonic()
        return {
            "job_id": record.job_id,
            "name": record.name,
            "command": record.command,
            "cwd": str(record.cwd),
            "status": status,
            "pid": record.process.pid,
            "elapsed_seconds": round(elapsed_until - record.started_at, 3),
            "exit_code": exit_code,
            "cpu_percent": cpu_percent,
            "memory_mb": memory_mb,
            "stdout_log": str(record.stdout_log),
            "stderr_log": str(record.stderr_log),
            "last_output_at": _last_job_output_at(record),
        }


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


def _jobs_runtime_dir(state_dir: Path) -> Path:
    jobs_dir = state_dir / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    try:
        jobs_dir.chmod(0o700)
    except OSError:
        pass
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


def _send_job_signal(process: subprocess.Popen[bytes], signum: int) -> None:
    process.poll()
    if process.returncode is not None:
        return
    if os.name == "posix" and hasattr(os, "killpg"):
        pgid = os.getpgid(process.pid)
        if pgid == process.pid:
            os.killpg(pgid, signum)
            return
    process.send_signal(signum)


def _process_stats(pid: int) -> tuple[float | None, float | None]:
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


def _last_job_output_at(record: JobRecord) -> float | None:
    mtimes: list[float] = []
    for path in [record.stdout_log, record.stderr_log]:
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_size > 0:
            mtimes.append(stat.st_mtime)
    if not mtimes:
        return None
    return round(max(mtimes), 3)


def _terminal_job_status(record: JobRecord, exit_code: int | None) -> str:
    if record.kill_signal is not None:
        return "killed"
    if exit_code == 0:
        return "succeeded"
    return "failed"


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


def run_command(*, command: str, cwd: Path, timeout: int, force: bool = False) -> dict[str, object]:
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

    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            shell=True,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        return {
            "success": completed.returncode == 0,
            "command": command,
            "cwd": str(cwd),
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "timed_out": False,
            "timeout": timeout,
            "force": force,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "success": False,
            "command": command,
            "cwd": str(cwd),
            "exit_code": TIMEOUT_EXIT_CODE,
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "",
            "timed_out": True,
            "timeout": timeout,
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


def run_commands(
    *,
    commands: list[str],
    cwd: Path,
    timeout: int,
    force: bool = False,
    mode: Literal["sequential", "parallel"] = "sequential",
    max_concurrency: int = MAX_COMMAND_BATCH_CONCURRENCY,
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

    if mode == "sequential":
        results = [
            _with_batch_index(
                index,
                run_command(command=command, cwd=cwd, timeout=timeout, force=force),
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
                ): index
                for index, command in enumerate(commands)
            }
            for future in as_completed(futures):
                index = futures[future]
                ordered_results[index] = _with_batch_index(index, future.result())
        results = [item for item in ordered_results if item is not None]

    failed_count = sum(1 for item in results if not item.get("success"))
    timed_out_count = sum(1 for item in results if item.get("timed_out"))
    return {
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
