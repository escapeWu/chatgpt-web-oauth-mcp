from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import threading
import time
from typing import Any
import uuid

from .errors import (
    AppServerCapabilityError,
    AppServerInteractionRequiredError,
    AppServerProtocolError,
    AppServerRequestTimeoutError,
    AppServerRpcError,
    AppServerUnavailableError,
    CommandExecutionTimeoutError,
)
from .models import SandboxMode, sandbox_policy


DEFAULT_STARTUP_TIMEOUT_SECONDS = 15.0
DEFAULT_MAX_MESSAGE_BYTES = 8 * 1024 * 1024


@dataclass
class _PendingRequest:
    request_id: int
    method: str
    event: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: BaseException | None = None
    interactions: list[dict[str, Any]] = field(default_factory=list)


class CodexAppServerAdapter:
    """Small JSON-RPC-over-stdio adapter for the Codex App Server."""

    REQUIRED_METHODS = (
        "thread/start",
        "thread/resume",
        "command/exec",
        "mcpServerStatus/list",
        "mcpServer/tool/call",
    )

    def __init__(
        self,
        *,
        command: str | Sequence[str] = "codex",
        cwd: Path | None = None,
        startup_timeout_seconds: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
        max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
    ) -> None:
        if isinstance(command, str):
            argv = shlex.split(command)
        else:
            argv = [str(item) for item in command]
        if not argv:
            raise ValueError("Codex command must not be empty.")
        if startup_timeout_seconds <= 0:
            raise ValueError("startup_timeout_seconds must be positive.")
        if max_message_bytes <= 0:
            raise ValueError("max_message_bytes must be positive.")
        self._argv = [*argv, "app-server", "--listen", "stdio://"]
        self._cwd = Path(cwd).expanduser().resolve() if cwd is not None else None
        self._startup_timeout_seconds = float(startup_timeout_seconds)
        self._max_message_bytes = int(max_message_bytes)
        self._lifecycle_lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[int, _PendingRequest] = {}
        self._next_request_id = 1
        self._process: subprocess.Popen[bytes] | None = None
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._started = False
        self._capabilities: set[str] = set()
        self._stderr_tail: deque[str] = deque(maxlen=8)

    @property
    def capabilities(self) -> tuple[str, ...]:
        return tuple(sorted(self._capabilities))

    @property
    def command_name(self) -> str:
        return self._argv[0]

    def is_running(self) -> bool:
        process = self._process
        return bool(process is not None and process.poll() is None and self._started)

    def info(self) -> dict[str, object]:
        return {
            "process_status": "running" if self.is_running() else "stopped",
            "command": self.command_name,
            "capabilities": list(self.capabilities),
        }

    def start(self) -> None:
        with self._lifecycle_lock:
            if self.is_running():
                return
            self._cleanup_dead_process_locked()
            try:
                process = subprocess.Popen(
                    self._argv,
                    cwd=str(self._cwd) if self._cwd is not None else None,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=0,
                    close_fds=True,
                    start_new_session=os.name == "posix",
                )
            except (OSError, ValueError) as exc:
                raise AppServerUnavailableError(
                    f"Unable to start Codex App Server: {type(exc).__name__}."
                ) from None
            self._process = process
            self._started = False
            self._capabilities.clear()
            assert process.stdout is not None
            assert process.stderr is not None
            self._reader_thread = threading.Thread(
                target=self._read_stdout,
                args=(process,),
                name="codex-app-server-reader",
                daemon=True,
            )
            self._reader_thread.start()
            self._stderr_thread = threading.Thread(
                target=self._read_stderr,
                args=(process,),
                name="codex-app-server-stderr",
                daemon=True,
            )
            self._stderr_thread.start()
            try:
                self._request_raw(
                    "initialize",
                    {
                        "clientInfo": {
                            "name": "chatgpt-web-oauth-mcp",
                            "version": "0.1.0",
                        },
                        "capabilities": {},
                    },
                    timeout_seconds=self._startup_timeout_seconds,
                )
                self._send_notification("initialized", {})
                self._probe_capabilities()
                self._started = True
            except Exception:
                self._close_process_locked()
                raise

    def shutdown(self) -> None:
        with self._lifecycle_lock:
            self._close_process_locked()

    def thread_start(self, *, cwd: str, sandbox: SandboxMode) -> dict[str, Any]:
        self.start()
        result = self._request_raw(
            "thread/start",
            {
                "cwd": cwd,
                "sandbox": sandbox,
                "approvalPolicy": "on-request",
            },
            timeout_seconds=self._startup_timeout_seconds,
        )
        return _require_object(result, "thread/start")

    def thread_resume(
        self,
        *,
        thread_id: str,
        cwd: str,
        sandbox: SandboxMode,
    ) -> dict[str, Any]:
        self.start()
        result = self._request_raw(
            "thread/resume",
            {
                "threadId": thread_id,
                "cwd": cwd,
                "sandbox": sandbox,
                "approvalPolicy": "on-request",
            },
            timeout_seconds=self._startup_timeout_seconds,
        )
        return _require_object(result, "thread/resume")

    def command_exec(
        self,
        *,
        command: list[str],
        cwd: str,
        sandbox: SandboxMode,
        timeout_ms: int,
        output_bytes_cap: int,
    ) -> dict[str, Any]:
        self.start()
        process_id = f"mcp-exec-{uuid.uuid4().hex}"
        pending = self._submit_request(
            "command/exec",
            {
                "command": command,
                "processId": process_id,
                "cwd": cwd,
                "timeoutMs": timeout_ms,
                "outputBytesCap": output_bytes_cap,
                "sandboxPolicy": sandbox_policy(sandbox),
            },
        )
        wait_timeout = (timeout_ms / 1000.0) + 0.5
        try:
            result = self._wait_pending(
                pending,
                timeout_seconds=wait_timeout,
                remove_on_timeout=False,
            )
        except AppServerRequestTimeoutError:
            terminated = False
            try:
                terminate_result = self._request_raw(
                    "command/exec/terminate",
                    {"processId": process_id},
                    timeout_seconds=min(5.0, self._startup_timeout_seconds),
                )
                terminated = isinstance(terminate_result, dict)
            except Exception:
                terminated = False
            pending.event.wait(timeout=1.0)
            self._remove_pending(pending.request_id)
            raise CommandExecutionTimeoutError(process_id, terminated=terminated) from None
        finally:
            self._remove_pending(pending.request_id)
        return _require_object(result, "command/exec")

    def mcp_inventory(
        self,
        *,
        thread_id: str,
        cursor: str | None,
        limit: int,
    ) -> dict[str, Any]:
        self.start()
        params: dict[str, Any] = {
            "threadId": thread_id,
            "detail": "full",
            "limit": limit,
        }
        if cursor:
            params["cursor"] = cursor
        result = self._request_raw(
            "mcpServerStatus/list",
            params,
            timeout_seconds=self._startup_timeout_seconds,
        )
        return _require_object(result, "mcpServerStatus/list")

    def mcp_call(
        self,
        *,
        thread_id: str,
        server: str,
        tool: str,
        arguments: dict[str, Any],
        meta: dict[str, Any] | None,
    ) -> dict[str, Any]:
        self.start()
        params: dict[str, Any] = {
            "threadId": thread_id,
            "server": server,
            "tool": tool,
            "arguments": arguments,
        }
        if meta is not None:
            params["_meta"] = meta
        result = self._request_raw(
            "mcpServer/tool/call",
            params,
            timeout_seconds=self._startup_timeout_seconds,
        )
        return _require_object(result, "mcpServer/tool/call")

    def _probe_capabilities(self) -> None:
        probe_params: dict[str, dict[str, Any]] = {
            # Thread/start accepts an all-optional object, so use a wrong type
            # to exercise method dispatch without creating a probe thread.
            "thread/start": {"cwd": 1},
            "thread/resume": {},
            "command/exec": {},
            "mcpServerStatus/list": {},
            "mcpServer/tool/call": {},
        }
        for method in self.REQUIRED_METHODS:
            try:
                self._request_raw(
                    method,
                    probe_params[method],
                    timeout_seconds=self._startup_timeout_seconds,
                )
            except AppServerRpcError as exc:
                upstream_code = exc.details.get("upstream_code")
                if upstream_code == -32601:
                    raise AppServerCapabilityError(method) from None
                self._capabilities.add(method)
            except AppServerInteractionRequiredError:
                self._capabilities.add(method)
            else:
                self._capabilities.add(method)

    def _request_raw(self, method: str, params: dict[str, Any], *, timeout_seconds: float) -> Any:
        pending = self._submit_request(method, params)
        return self._wait_pending(pending, timeout_seconds=timeout_seconds)

    def _submit_request(self, method: str, params: dict[str, Any]) -> _PendingRequest:
        process = self._process
        if process is None or process.poll() is not None or process.stdin is None:
            raise AppServerUnavailableError()
        with self._pending_lock:
            request_id = self._next_request_id
            self._next_request_id += 1
            pending = _PendingRequest(request_id=request_id, method=method)
            self._pending[request_id] = pending
        try:
            self._send_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                }
            )
        except Exception:
            self._remove_pending(request_id)
            raise
        return pending

    def _wait_pending(
        self,
        pending: _PendingRequest,
        *,
        timeout_seconds: float,
        remove_on_timeout: bool = True,
    ) -> Any:
        if not pending.event.wait(timeout=max(0.001, timeout_seconds)):
            if remove_on_timeout:
                self._remove_pending(pending.request_id)
            raise AppServerRequestTimeoutError(pending.method, timeout_seconds) from None
        self._remove_pending(pending.request_id)
        if pending.error is not None:
            raise pending.error
        return pending.result

    def _send_notification(self, method: str, params: dict[str, Any]) -> None:
        self._send_message({"jsonrpc": "2.0", "method": method, "params": params})

    def _send_message(self, message: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.poll() is not None or process.stdin is None:
            raise AppServerUnavailableError()
        encoded = (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        with self._write_lock:
            try:
                process.stdin.write(encoded)
                process.stdin.flush()
            except (BrokenPipeError, OSError):
                raise AppServerUnavailableError("Codex App Server stdin closed.") from None

    def _read_stdout(self, process: subprocess.Popen[bytes]) -> None:
        assert process.stdout is not None
        protocol_failure = False
        try:
            while True:
                line = process.stdout.readline()
                if not line:
                    break
                if len(line) > self._max_message_bytes:
                    protocol_failure = True
                    self._fail_pending(AppServerProtocolError("Codex App Server response exceeded the message limit."))
                    break
                try:
                    message = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    protocol_failure = True
                    self._fail_pending(AppServerProtocolError("Codex App Server returned invalid JSON."))
                    break
                if not isinstance(message, dict):
                    protocol_failure = True
                    self._fail_pending(AppServerProtocolError("Codex App Server returned a non-object message."))
                    break
                if "id" in message and ("result" in message or "error" in message):
                    self._resolve_response(message)
                elif "id" in message and "method" in message:
                    self._reject_server_request(message)
        finally:
            if protocol_failure:
                with self._lifecycle_lock:
                    if self._process is process:
                        self._close_process_locked()
            self._fail_pending(AppServerUnavailableError("Codex App Server exited."))
            with self._lifecycle_lock:
                if self._process is process:
                    self._started = False
                    self._process = None

    def _read_stderr(self, process: subprocess.Popen[bytes]) -> None:
        assert process.stderr is not None
        while True:
            line = process.stderr.readline()
            if not line:
                return
            try:
                decoded = line.decode("utf-8", errors="replace").strip()
            except Exception:
                decoded = "<unreadable stderr>"
            if decoded:
                self._stderr_tail.append(decoded[-512:])

    def _resolve_response(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        if not isinstance(request_id, int):
            return
        with self._pending_lock:
            pending = self._pending.get(request_id)
        if pending is None:
            return
        if "error" in message:
            error_value = message.get("error")
            error_code: int | str | None = None
            error_message = "Codex App Server returned an RPC error."
            error_data: Any = None
            if isinstance(error_value, dict):
                error_code = error_value.get("code")
                if isinstance(error_value.get("message"), str):
                    error_message = error_value["message"]
                error_data = error_value.get("data")
            if pending.interactions:
                pending.error = AppServerInteractionRequiredError(
                    pending.method,
                    pending.interactions,
                )
            else:
                pending.error = AppServerRpcError(
                    pending.method,
                    error_code,
                    error_message,
                    data=_bounded_error_data(error_data),
                )
        else:
            pending.result = message.get("result")
        pending.event.set()

    def _reject_server_request(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        request_id = message.get("id")
        interaction = {
            "method": method if isinstance(method, str) else "unknown",
            "request_id": request_id,
        }
        with self._pending_lock:
            pending = min(self._pending.values(), key=lambda item: item.request_id, default=None)
            if pending is not None:
                pending.interactions.append(interaction)
        self._send_message(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32001,
                    "message": "Approval or elicitation is required; this runtime never auto-approves.",
                },
            }
        )

    def _fail_pending(self, error: BaseException) -> None:
        with self._pending_lock:
            pending = list(self._pending.values())
        for item in pending:
            if not item.event.is_set():
                item.error = error
                item.event.set()

    def _remove_pending(self, request_id: int) -> None:
        with self._pending_lock:
            self._pending.pop(request_id, None)

    def _cleanup_dead_process_locked(self) -> None:
        process = self._process
        if process is None or process.poll() is None:
            return
        self._process = None
        self._started = False
        try:
            process.stdin.close() if process.stdin is not None else None
        except OSError:
            pass

    def _close_process_locked(self) -> None:
        process = self._process
        self._started = False
        self._capabilities.clear()
        self._fail_pending(AppServerUnavailableError("Codex App Server is shutting down."))
        if process is None:
            return
        self._process = None
        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass
        if process.poll() is None:
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGTERM)
                else:  # pragma: no cover - Windows is not the deployment target.
                    process.terminate()
            except (OSError, ProcessLookupError):
                pass
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                try:
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGKILL)
                    else:  # pragma: no cover
                        process.kill()
                except (OSError, ProcessLookupError):
                    pass
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    pass


def _require_object(value: Any, method: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AppServerProtocolError(f"Codex App Server returned a non-object result for {method}.")
    return value


def _bounded_error_data(value: Any) -> Any:
    if value is None:
        return None
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return str(value)[:4096]
    if len(encoded) <= 4096:
        return value
    return encoded[:4093] + "..."
