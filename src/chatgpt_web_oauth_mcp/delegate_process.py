from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .delegate_models import DelegateTask


TIMEOUT_EXIT_CODE = -1


@dataclass(frozen=True)
class Invocation:
    args: list[str] | str
    use_shell: bool
    stdin: bytes | None = None


def decode_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return value.decode("utf-8", errors="replace")


def extract_structured_output(text: str) -> object | None:
    stripped = (text or "").strip()
    if not stripped:
        return None
    matches = re.findall(r"```(?:json)?\s*([\s\S]*?)```", stripped, re.IGNORECASE)
    candidates = [matches[-1].strip()] if matches else []
    candidates.append(stripped)
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def safe_chmod(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        pass


def write_private_text(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    safe_chmod(path, 0o600)


def write_private_json(path: Path, payload: dict[str, object]) -> None:
    write_private_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def log_read_hint(task: DelegateTask) -> dict[str, object]:
    return {
        "tool": "read_text",
        "paths": [
            str(task.log_paths.stdout),
            str(task.log_paths.stderr),
            str(task.log_paths.metadata),
        ],
        "message": "Use read_text on stdout/stderr/metadata to inspect live delegate progress.",
    }


def harness_display_name(name: str) -> str:
    if name.lower() == "codex":
        return "Codex"
    if name.lower() == "pi":
        return "Pi"
    return name


class DelegateProcessRunner:
    """CLI harness subprocess lifecycle, log streaming, timeout, and cancellation."""

    def __init__(
        self,
        *,
        popen_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    ) -> None:
        self.popen_factory = popen_factory

    def run(
        self,
        task: DelegateTask,
        *,
        invocation_builder: Callable[[DelegateTask], Invocation],
    ) -> dict[str, object]:
        before_status = self._git_status(task) if task.kind == "explore" else None
        if task.kind == "explore" and task.project.git_common_dir is not None and before_status is None:
            result = self._result(
                task,
                status="failed",
                exit_code=TIMEOUT_EXIT_CODE,
                error={
                    "code": "readonly_audit_unavailable",
                    "message": "Could not capture repository state before read-only exploration.",
                },
                structured_output=None,
                duration_seconds=0.0,
            )
            self._write_final_metadata(task, result)
            return result
        try:
            invocation = invocation_builder(task)
            self._write_started_metadata(task, invocation)
            popen_kwargs: dict[str, object] = {
                "cwd": str(task.cwd),
                "shell": invocation.use_shell,
                "text": False,
                "stdin": subprocess.PIPE if invocation.stdin is not None else None,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
            }
            if os.name == "posix":
                popen_kwargs["start_new_session"] = True
            elif os.name == "nt":  # pragma: no cover - Windows runtime only
                popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            process = self.popen_factory(invocation.args, **popen_kwargs)
            with task.output_lock:
                task.process = process
            if task.cancel_requested:
                self._terminate_process_group(process, task.cancel_grace_seconds)
            if invocation.stdin is not None and getattr(process, "stdin", None) is not None:
                threading.Thread(
                    target=self._write_process_stdin,
                    args=(process, invocation.stdin),
                    daemon=True,
                ).start()
        except OSError as exc:
            result = self._result(
                task,
                status="failed",
                exit_code=TIMEOUT_EXIT_CODE,
                error={"code": "process_start_failed", "message": str(exc)},
                structured_output=None,
                duration_seconds=0.0,
            )
            self._write_final_metadata(task, result)
            return result

        stdout_raw, stderr_raw, timed_out = self._wait_and_capture(task)
        stdout = decode_output(stdout_raw)
        stderr = decode_output(stderr_raw)
        structured_output = None
        if task.parse_structured_output:
            structured_output = extract_structured_output(stdout) or extract_structured_output(stderr)

        readonly_violation = False
        readonly_audit_unavailable = False
        if task.kind == "explore" and before_status is not None:
            after_status = self._git_status(task)
            readonly_audit_unavailable = after_status is None
            readonly_violation = after_status is not None and after_status != before_status

        duration = time.monotonic() - (task.started_monotonic or time.monotonic())
        exit_code = getattr(task.process, "returncode", None)
        exit_code = int(exit_code) if isinstance(exit_code, int) else TIMEOUT_EXIT_CODE
        if task.cancel_requested:
            status = "cancelled"
            error = {
                "code": "cancelled",
                "message": f"{harness_display_name(task.harness)} delegate was cancelled.",
            }
        elif timed_out:
            status = "timed_out"
            error = {
                "code": "timed_out",
                "message": (
                    f"{harness_display_name(task.harness)} delegate exceeded the "
                    f"{task.execution_timeout_seconds}s execution timeout."
                ),
            }
            exit_code = TIMEOUT_EXIT_CODE
        elif readonly_violation:
            status = "failed"
            error = {
                "code": "readonly_violation",
                "message": "Repository state changed during a read-only exploration task.",
            }
        elif readonly_audit_unavailable:
            status = "failed"
            error = {
                "code": "readonly_audit_unavailable",
                "message": "Could not capture repository state after read-only exploration.",
            }
        else:
            status = "succeeded" if exit_code == 0 else "failed"
            error = None
            if status == "failed":
                error = {
                    "code": "process_failed",
                    "message": (
                        f"{harness_display_name(task.harness)} delegate exited with code {exit_code}."
                    ),
                }

        result = self._result(
            task,
            status=status,
            exit_code=exit_code,
            error=error,
            structured_output=structured_output,
            duration_seconds=duration,
        )
        self._write_final_metadata(task, result)
        return result

    def cancel(self, task: DelegateTask) -> None:
        task.cancel_requested = True
        process = task.process
        if process is None or getattr(process, "returncode", None) is not None:
            return
        self._terminate_process_group(process, task.cancel_grace_seconds)

    def _wait_and_capture(self, task: DelegateTask) -> tuple[bytes, bytes, bool]:
        process = task.process
        if process is None:
            return b"", b"", False
        stdout_stream = getattr(process, "stdout", None)
        stderr_stream = getattr(process, "stderr", None)
        timed_out = False
        stdout_thread: threading.Thread | None = None
        stderr_thread: threading.Thread | None = None
        try:
            if stdout_stream is None or stderr_stream is None:
                stdout_raw, stderr_raw = process.communicate(
                    timeout=task.execution_timeout_seconds
                )
                self._record_fallback_output(task, "stdout", stdout_raw, task.log_paths.stdout)
                self._record_fallback_output(task, "stderr", stderr_raw, task.log_paths.stderr)
            else:
                task.log_paths.stdout.touch()
                task.log_paths.stderr.touch()
                safe_chmod(task.log_paths.stdout, 0o600)
                safe_chmod(task.log_paths.stderr, 0o600)
                stdout_thread = threading.Thread(
                    target=self._read_stream_to_log,
                    args=(task, "stdout", stdout_stream, task.log_paths.stdout),
                    daemon=True,
                )
                stderr_thread = threading.Thread(
                    target=self._read_stream_to_log,
                    args=(task, "stderr", stderr_stream, task.log_paths.stderr),
                    daemon=True,
                )
                stdout_thread.start()
                stderr_thread.start()
                process.wait(timeout=task.execution_timeout_seconds)
                stdout_thread.join(timeout=5)
                stderr_thread.join(timeout=5)
                stdout_raw = b"".join(task.stdout_chunks)
                stderr_raw = b"".join(task.stderr_chunks)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._terminate_process_group(process, task.cancel_grace_seconds)
            if stdout_stream is None or stderr_stream is None:
                try:
                    stdout_raw, stderr_raw = process.communicate(timeout=task.cancel_grace_seconds + 1)
                except (subprocess.TimeoutExpired, AttributeError):
                    stdout_raw, stderr_raw = b"", b""
                self._record_fallback_output(task, "stdout", stdout_raw, task.log_paths.stdout)
                self._record_fallback_output(task, "stderr", stderr_raw, task.log_paths.stderr)
            else:
                self._close_blocked_stream(stdout_thread, stdout_stream)
                self._close_blocked_stream(stderr_thread, stderr_stream)
                if stdout_thread is not None:
                    stdout_thread.join(timeout=5)
                if stderr_thread is not None:
                    stderr_thread.join(timeout=5)
                stdout_raw = b"".join(task.stdout_chunks)
                stderr_raw = b"".join(task.stderr_chunks)
        return stdout_raw, stderr_raw, timed_out

    def _close_blocked_stream(self, thread: threading.Thread | None, stream) -> None:
        if thread is None or not thread.is_alive():
            return
        thread.join(timeout=0.5)
        if not thread.is_alive():
            return
        try:
            stream.close()
        except (AttributeError, OSError):
            pass

    def _terminate_process_group(
        self,
        process: subprocess.Popen[bytes],
        grace_seconds: float,
    ) -> None:
        if getattr(process, "returncode", None) is not None:
            return
        if os.name == "nt" and isinstance(getattr(process, "pid", None), int):  # pragma: no cover - Windows only
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    check=False,
                    timeout=max(1.0, grace_seconds + 1),
                )
                return
            except (OSError, subprocess.TimeoutExpired):
                pass
        terminated = False
        if os.name == "posix" and isinstance(getattr(process, "pid", None), int):
            try:
                os.killpg(process.pid, signal.SIGTERM)
                terminated = True
            except (OSError, ProcessLookupError):
                pass
        if not terminated:
            try:
                process.terminate()
            except (AttributeError, OSError):
                try:
                    process.kill()
                except (AttributeError, OSError):
                    return
        try:
            process.wait(timeout=max(0.0, grace_seconds))
            return
        except (subprocess.TimeoutExpired, AttributeError):
            pass
        if os.name == "posix" and isinstance(getattr(process, "pid", None), int):
            try:
                os.killpg(process.pid, signal.SIGKILL)
                return
            except (OSError, ProcessLookupError):
                pass
        try:
            process.kill()
        except (AttributeError, OSError):
            pass

    def _write_process_stdin(
        self,
        process: subprocess.Popen[bytes],
        payload: bytes,
    ) -> None:
        if process.stdin is None:
            return
        try:
            process.stdin.write(payload)
            process.stdin.close()
        except OSError:
            pass

    def _read_stream_to_log(self, task: DelegateTask, stream_name: str, stream, path: Path) -> None:
        chunks = task.stdout_chunks if stream_name == "stdout" else task.stderr_chunks
        with path.open("ab") as handle:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    break
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8", errors="replace")
                handle.write(chunk)
                handle.flush()
                with task.output_lock:
                    chunks.append(chunk)
                    if stream_name == "stdout":
                        task.stdout_bytes += len(chunk)
                    else:
                        task.stderr_bytes += len(chunk)
                    task.last_output_at = time.monotonic()

    def _record_fallback_output(
        self,
        task: DelegateTask,
        stream_name: str,
        value: str | bytes | None,
        path: Path,
    ) -> None:
        chunk = value or b""
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8", errors="replace")
        path.write_bytes(chunk)
        safe_chmod(path, 0o600)
        chunks = task.stdout_chunks if stream_name == "stdout" else task.stderr_chunks
        with task.output_lock:
            chunks.append(chunk)
            if stream_name == "stdout":
                task.stdout_bytes += len(chunk)
            else:
                task.stderr_bytes += len(chunk)
            if chunk:
                task.last_output_at = time.monotonic()

    def _git_status(self, task: DelegateTask) -> bytes | None:
        if task.project.git_common_dir is None:
            return None
        try:
            completed = subprocess.run(
                ["git", "-C", str(task.cwd), "status", "--porcelain=v1", "-z"],
                capture_output=True,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return completed.stdout if completed.returncode == 0 else None

    def _result(
        self,
        task: DelegateTask,
        *,
        status: str,
        exit_code: int,
        error: dict[str, object] | None,
        structured_output: object | None,
        duration_seconds: float,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "success": status == "succeeded",
            "status": status,
            "completed": True,
            "in_progress": False,
            "executor": task.harness,
            "harness": task.harness,
            "cwd": str(task.cwd),
            "delegate_id": task.delegate_id,
            "group_id": task.group_id,
            "kind": task.kind,
            "lane": task.lane,
            "concurrency_scope": "project",
            "serial": task.serial,
            "sandbox_mode": task.sandbox_mode,
            "commit_mode": task.commit_mode,
            "project": task.project.as_payload(),
            "logs": task.log_paths.as_payload(),
            "log_read_hint": log_read_hint(task),
            "exit_code": exit_code,
            "summary": self._result_summary(task, status, error),
            "output_omitted": True,
            "timed_out": status == "timed_out",
            "wait_timed_out": False,
            "timeout": task.execution_timeout_seconds,
            "execution_timeout_seconds": task.execution_timeout_seconds,
            "duration_seconds": round(duration_seconds, 3),
            "structured_output": structured_output,
            "output_schema": task.output_schema,
            "request_fingerprint": task.request_fingerprint,
            "model": task.model,
            "reasoning_effort": task.reasoning_effort,
        }
        if task.task_id is not None:
            payload["task_id"] = task.task_id
        if error is not None:
            payload["error"] = error
        return payload

    def _result_summary(
        self,
        task: DelegateTask,
        status: str,
        error: dict[str, object] | None,
    ) -> str:
        display_name = harness_display_name(task.harness)
        if error and error.get("message"):
            return f"{display_name} delegate {status}: {error['message']}"
        return f"{display_name} delegate {status}. Process output is stored in logs."

    def _write_started_metadata(self, task: DelegateTask, invocation: Invocation) -> None:
        try:
            write_private_json(
                task.log_paths.metadata,
                {
                    "delegate_id": task.delegate_id,
                    "executor": task.harness,
                    "harness": task.harness,
                    "group_id": task.group_id,
                    "status": "running",
                    "kind": task.kind,
                    "lane": task.lane,
                    "cwd": str(task.cwd),
                    "project": task.project.as_payload(),
                    "execution_timeout_seconds": task.execution_timeout_seconds,
                    "commit_mode": task.commit_mode,
                    "sandbox_mode": task.sandbox_mode,
                    "model": task.model,
                    "reasoning_effort": task.reasoning_effort,
                    "task_id": task.task_id,
                    "request_fingerprint": task.request_fingerprint,
                    "started_at_epoch": task.started_at,
                    "command_kind": "shell" if invocation.use_shell else "argv",
                },
            )
        except OSError:
            pass

    def _write_final_metadata(self, task: DelegateTask, result: dict[str, object]) -> None:
        payload = {
            key: value
            for key, value in result.items()
            if key not in {"structured_output", "output_schema", "log_read_hint"}
        }
        payload.update(
            {
                "stdout_bytes": task.stdout_bytes,
                "stderr_bytes": task.stderr_bytes,
                "logs": task.log_paths.as_payload(),
            }
        )
        if task.process is not None and getattr(task.process, "pid", None) is not None:
            payload["pid"] = task.process.pid
        try:
            write_private_json(task.log_paths.metadata, payload)
        except OSError:
            pass
