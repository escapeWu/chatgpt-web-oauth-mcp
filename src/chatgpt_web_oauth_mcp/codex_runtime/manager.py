from __future__ import annotations

from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
import threading
import time
from typing import Any

from .app_server import CodexAppServerAdapter
from .bindings import BindingStore
from .errors import AppServerRpcError, BindingStoreError, CodexRuntimeError
from .models import (
    RuntimeBinding,
    SandboxMode,
    new_runtime_id,
    runtime_public_dict,
    sandbox_matches,
    validate_sandbox,
)


MAX_COMMAND_ARGUMENTS = 128
MAX_COMMAND_ARGUMENT_BYTES = 64 * 1024
MAX_RUNTIME_NAME_LENGTH = 128
MAX_FILTER_LENGTH = 256
MAX_INVENTORY_LIMIT = 100


class CodexRuntimeManager:
    """Own runtime bindings and one reusable Codex App Server process."""

    def __init__(
        self,
        *,
        state_dir: Path,
        codex_command: str | Sequence[str] = "codex",
        workspace_root: Path | None = None,
        max_concurrency: int = 4,
        max_runtimes: int = 64,
        startup_timeout_seconds: float = 15.0,
        default_timeout_ms: int = 120_000,
        max_timeout_ms: int = 300_000,
        output_bytes_cap: int = 1024 * 1024,
        max_message_bytes: int = 8 * 1024 * 1024,
        adapter: CodexAppServerAdapter | Any | None = None,
    ) -> None:
        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive.")
        if max_runtimes <= 0:
            raise ValueError("max_runtimes must be positive.")
        if default_timeout_ms <= 0 or max_timeout_ms <= 0:
            raise ValueError("runtime timeouts must be positive.")
        if default_timeout_ms > max_timeout_ms:
            raise ValueError("default_timeout_ms cannot exceed max_timeout_ms.")
        if output_bytes_cap <= 0:
            raise ValueError("output_bytes_cap must be positive.")
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.max_concurrency = int(max_concurrency)
        self.max_runtimes = int(max_runtimes)
        self.startup_timeout_seconds = float(startup_timeout_seconds)
        self.default_timeout_ms = int(default_timeout_ms)
        self.max_timeout_ms = int(max_timeout_ms)
        self.output_bytes_cap = int(output_bytes_cap)
        self._lock = threading.RLock()
        self._slots = threading.BoundedSemaphore(self.max_concurrency)
        self._store = BindingStore(self.state_dir)
        self._load_error: BindingStoreError | None = None
        self._persistence_warning: str | None = None
        try:
            self._bindings = self._store.load()
        except BindingStoreError as exc:
            self._bindings = {}
            self._load_error = exc
        self._adapter = adapter or CodexAppServerAdapter(
            command=codex_command,
            cwd=workspace_root,
            startup_timeout_seconds=self.startup_timeout_seconds,
            max_message_bytes=max_message_bytes,
        )
        self._live_thread_ids: set[str] = set()
        self._observed_connection_generation = getattr(
            self._adapter,
            "connection_generation",
            None,
        )

    @property
    def adapter(self) -> Any:
        return self._adapter

    def info(self) -> dict[str, object]:
        with self._lock:
            binding_store: dict[str, object] = {
                "available": self._load_error is None,
                "path": str(self._store.path),
            }
            if self._load_error is not None:
                binding_store["error"] = self._load_error.code
            if self._persistence_warning is not None:
                binding_store["warning"] = self._persistence_warning
            adapter_info = self._adapter.info() if hasattr(self._adapter, "info") else {}
            return {
                "enabled": True,
                "process_status": adapter_info.get("process_status", "stopped"),
                "command": adapter_info.get("command", "codex"),
                "capabilities": list(adapter_info.get("capabilities", [])),
                "runtime_count": len(self._bindings),
                "max_runtimes": self.max_runtimes,
                "max_concurrency": self.max_concurrency,
                "default_timeout_ms": self.default_timeout_ms,
                "max_timeout_ms": self.max_timeout_ms,
                "output_bytes_cap": self.output_bytes_cap,
                "binding_store": binding_store,
            }

    def open_runtime(
        self,
        *,
        cwd: Path,
        sandbox: SandboxMode,
        name: str | None,
    ) -> dict[str, object]:
        self._ensure_store()
        target_cwd = self._validate_cwd(cwd)
        target_sandbox = validate_sandbox(sandbox)
        normalized_name = self._normalize_name(name)
        with self._lock:
            if len(self._bindings) >= self.max_runtimes:
                raise CodexRuntimeError(
                    "runtime_limit_reached",
                    "The maximum number of Codex runtimes is already open.",
                    retryable=True,
                    details={"max_runtimes": self.max_runtimes},
                )
        with self._request_slot():
            result = self._adapter.thread_start(
                cwd=str(target_cwd),
                sandbox=target_sandbox,
            )
        thread_id, returned_cwd, returned_sandbox = self._verify_thread_metadata(
            result,
            expected_cwd=target_cwd,
            expected_sandbox=target_sandbox,
            method="thread/start",
        )
        now = time.time()
        binding = RuntimeBinding(
            runtime_id=new_runtime_id(),
            thread_id=thread_id,
            cwd=str(returned_cwd),
            sandbox=target_sandbox,
            name=normalized_name,
            created_at=now,
            last_used_at=now,
            status="ready",
        )
        try:
            with self._lock:
                if len(self._bindings) >= self.max_runtimes:
                    raise CodexRuntimeError(
                        "runtime_limit_reached",
                        "The maximum number of Codex runtimes is already open.",
                        retryable=True,
                        details={"max_runtimes": self.max_runtimes},
                    )
                updated = {**self._bindings, binding.runtime_id: binding}
                self._save_bindings(updated)
        except CodexRuntimeError as exc:
            exc.side_effects["thread_created"] = True
            exc.side_effects["thread_id"] = thread_id
            raise
        self._sync_connection_generation()
        self._live_thread_ids.add(thread_id)
        return {"success": True, **runtime_public_dict(binding)}

    def resume_runtime(
        self,
        *,
        runtime_id: str | None,
        thread_id: str | None,
        cwd: Path | None,
        sandbox: SandboxMode | None,
    ) -> dict[str, object]:
        self._ensure_store()
        has_runtime_id = bool(runtime_id and runtime_id.strip())
        has_thread_id = bool(thread_id and thread_id.strip())
        if has_runtime_id == has_thread_id:
            raise CodexRuntimeError(
                "invalid_arguments",
                "Pass exactly one of runtime_id or thread_id.",
            )

        existing: RuntimeBinding | None = None
        if has_runtime_id:
            assert runtime_id is not None
            with self._lock:
                existing = self._bindings.get(runtime_id)
            if existing is None:
                raise CodexRuntimeError(
                    "runtime_not_found",
                    f"Unknown runtime_id: {runtime_id}.",
                    details={"runtime_id": runtime_id},
                )
            expected_cwd = self._validate_cwd(cwd) if cwd is not None else Path(existing.cwd)
            if expected_cwd != Path(existing.cwd).resolve():
                raise CodexRuntimeError(
                    "runtime_metadata_mismatch",
                    "The supplied cwd does not match the persisted runtime binding.",
                    details={"runtime_id": runtime_id, "expected_cwd": existing.cwd},
                )
            expected_sandbox = validate_sandbox(sandbox) if sandbox is not None else existing.sandbox
            if expected_sandbox != existing.sandbox:
                raise CodexRuntimeError(
                    "runtime_metadata_mismatch",
                    "The supplied sandbox does not match the persisted runtime binding.",
                    details={"runtime_id": runtime_id, "expected_sandbox": existing.sandbox},
                )
            target_thread_id = existing.thread_id
        else:
            assert thread_id is not None
            target_thread_id = thread_id.strip()
            with self._lock:
                existing = next(
                    (item for item in self._bindings.values() if item.thread_id == target_thread_id),
                    None,
                )
            if existing is not None:
                expected_cwd = self._validate_cwd(cwd) if cwd is not None else Path(existing.cwd)
                if expected_cwd != Path(existing.cwd).resolve():
                    raise CodexRuntimeError(
                        "runtime_metadata_mismatch",
                        "The supplied cwd does not match the persisted runtime binding.",
                        details={"runtime_id": existing.runtime_id, "expected_cwd": existing.cwd},
                    )
                expected_sandbox = validate_sandbox(sandbox) if sandbox is not None else existing.sandbox
                if expected_sandbox != existing.sandbox:
                    raise CodexRuntimeError(
                        "runtime_metadata_mismatch",
                        "The supplied sandbox does not match the persisted runtime binding.",
                        details={"runtime_id": existing.runtime_id, "expected_sandbox": existing.sandbox},
                    )
            else:
                if cwd is None or sandbox is None:
                    raise CodexRuntimeError(
                        "resume_metadata_required",
                        "Resuming an unbound thread_id requires both cwd and sandbox for binding verification.",
                    )
                expected_cwd = self._validate_cwd(cwd)
                expected_sandbox = validate_sandbox(sandbox)

        self._sync_connection_generation()
        reused_connection = (
            existing is not None
            and self._adapter.is_running()
            and target_thread_id in self._live_thread_ids
        )
        requested_thread_id = target_thread_id
        thread_recreated = False
        previous_thread_id: str | None = None
        if reused_connection:
            resumed_thread_id = target_thread_id
            returned_cwd = expected_cwd
        else:
            with self._request_slot():
                try:
                    result = self._adapter.thread_resume(
                        thread_id=target_thread_id,
                        cwd=str(expected_cwd),
                        sandbox=expected_sandbox,
                    )
                except AppServerRpcError as exc:
                    if not _is_no_rollout_error(exc):
                        raise
                    previous_thread_id = target_thread_id
                    result = self._adapter.thread_start(
                        cwd=str(expected_cwd),
                        sandbox=expected_sandbox,
                    )
                    thread_recreated = True
            resumed_thread_id, returned_cwd, _returned_sandbox = self._verify_thread_metadata(
                result,
                expected_cwd=expected_cwd,
                expected_sandbox=expected_sandbox,
                method="thread/start" if thread_recreated else "thread/resume",
            )
        if not thread_recreated and resumed_thread_id != requested_thread_id:
            raise CodexRuntimeError(
                "runtime_metadata_mismatch",
                "Codex resumed a different thread_id than requested.",
                details={"requested_thread_id": requested_thread_id, "returned_thread_id": resumed_thread_id},
            )
        if thread_recreated:
            target_thread_id = resumed_thread_id
        self._sync_connection_generation()
        now = time.time()
        if existing is None:
            binding = RuntimeBinding(
                runtime_id=new_runtime_id(),
                thread_id=target_thread_id,
                cwd=str(returned_cwd),
                sandbox=expected_sandbox,
                name=None,
                created_at=now,
                last_used_at=now,
                status="ready",
            )
        else:
            binding = replace(
                existing,
                thread_id=target_thread_id,
                cwd=str(returned_cwd),
                last_used_at=now,
                status="ready",
            )
        try:
            with self._lock:
                if existing is None and len(self._bindings) >= self.max_runtimes:
                    raise CodexRuntimeError(
                        "runtime_limit_reached",
                        "The maximum number of Codex runtimes is already open.",
                        retryable=True,
                        details={"max_runtimes": self.max_runtimes},
                    )
                updated = dict(self._bindings)
                updated[binding.runtime_id] = binding
                self._save_bindings(updated)
        except CodexRuntimeError as exc:
            exc.side_effects["thread_resumed"] = True
            exc.side_effects["thread_id"] = target_thread_id
            raise
        self._sync_connection_generation()
        if previous_thread_id is not None:
            self._live_thread_ids.discard(previous_thread_id)
        self._live_thread_ids.add(binding.thread_id)
        payload: dict[str, object] = {"success": True, **runtime_public_dict(binding)}
        payload["resume_mode"] = "reattached" if reused_connection else "resumed"
        if thread_recreated:
            payload["resume_mode"] = "recreated"
            payload["thread_recreated"] = True
            payload["previous_thread_id"] = previous_thread_id
        return payload

    def runtime_status(self, runtime_id: str) -> dict[str, object]:
        self._ensure_store()
        with self._lock:
            binding = self._bindings.get(runtime_id)
            if binding is None:
                raise CodexRuntimeError("runtime_not_found", f"Unknown runtime_id: {runtime_id}.")
            status = binding.status
            if status == "ready" and not self._adapter.is_running():
                status = "detached"
                self._bindings[runtime_id] = replace(binding, status=status)
            current = self._bindings[runtime_id]
            return {
                "success": True,
                **runtime_public_dict(current),
                "process_status": "running" if self._adapter.is_running() else "stopped",
                "recoverable": True,
                "recovery": "Call codex_runtime_resume with this runtime_id.",
            }

    def close_runtime(self, runtime_id: str) -> dict[str, object]:
        self._ensure_store()
        with self._lock:
            binding = self._bindings.get(runtime_id)
            if binding is None:
                raise CodexRuntimeError("runtime_not_found", f"Unknown runtime_id: {runtime_id}.")
            detached_binding = replace(binding, status="detached", last_used_at=time.time())
            updated = dict(self._bindings)
            updated[runtime_id] = detached_binding
            self._save_bindings(updated)
        return {
            "success": True,
            **runtime_public_dict(detached_binding),
            "preserved_thread": True,
            "binding_preserved": True,
            "destructive_action": False,
        }

    def exec_command(
        self,
        *,
        runtime_id: str,
        command: list[str],
        timeout_ms: int | None,
        cwd: Path | None,
    ) -> dict[str, object]:
        binding = self._active_binding(runtime_id)
        normalized_command = self._validate_command(command)
        effective_timeout = self.default_timeout_ms if timeout_ms is None else self._validate_timeout(timeout_ms)
        effective_cwd = Path(binding.cwd).resolve()
        if cwd is not None:
            requested_cwd = self._validate_cwd(cwd)
            try:
                requested_cwd.relative_to(effective_cwd)
            except ValueError:
                raise CodexRuntimeError(
                    "runtime_cwd_outside_binding",
                    "cwd must remain inside the runtime's bound working directory.",
                    details={"runtime_id": runtime_id, "bound_cwd": str(effective_cwd)},
                ) from None
            effective_cwd = requested_cwd
        with self._request_slot():
            result = self._adapter.command_exec(
                command=normalized_command,
                cwd=str(effective_cwd),
                sandbox=binding.sandbox,
                timeout_ms=effective_timeout,
                output_bytes_cap=self.output_bytes_cap,
            )
        self._touch(runtime_id)
        stdout = result.get("stdout", "")
        stderr = result.get("stderr", "")
        exit_code = result.get("exitCode")
        if not isinstance(stdout, str) or not isinstance(stderr, str) or not isinstance(exit_code, int):
            raise CodexRuntimeError(
                "codex_protocol_error",
                "Codex returned an invalid command/exec result.",
            )
        return {
            "success": True,
            "runtime_id": runtime_id,
            "thread_id": binding.thread_id,
            "cwd": str(effective_cwd),
            "sandbox": binding.sandbox,
            "command": normalized_command,
            "timeout_ms": effective_timeout,
            "output_bytes_cap": self.output_bytes_cap,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "timed_out": False,
        }

    def mcp_inventory(
        self,
        *,
        runtime_id: str,
        server: str | None,
        tool_query: str | None,
        cursor: str | None,
        limit: int,
    ) -> dict[str, object]:
        binding = self._active_binding(runtime_id)
        normalized_server = self._normalize_filter(server, "server")
        normalized_query = self._normalize_filter(tool_query, "tool_query")
        normalized_cursor = self._normalize_cursor(cursor)
        if limit <= 0 or limit > MAX_INVENTORY_LIMIT:
            raise CodexRuntimeError(
                "invalid_arguments",
                f"limit must be between 1 and {MAX_INVENTORY_LIMIT}.",
            )
        with self._request_slot():
            result = self._adapter.mcp_inventory(
                thread_id=binding.thread_id,
                cursor=normalized_cursor,
                limit=limit,
            )
        data = result.get("data", [])
        if not isinstance(data, list):
            raise CodexRuntimeError("codex_protocol_error", "Codex returned an invalid MCP inventory.")
        servers: list[dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if normalized_server and name != normalized_server:
                continue
            tools_value = item.get("tools", {})
            tools: dict[str, Any] = {}
            if isinstance(tools_value, dict):
                for tool_name, tool_value in tools_value.items():
                    if not isinstance(tool_name, str):
                        continue
                    if normalized_query:
                        description = tool_value.get("description", "") if isinstance(tool_value, dict) else ""
                        searchable = f"{tool_name} {description}".casefold()
                        if normalized_query.casefold() not in searchable:
                            continue
                    tools[tool_name] = tool_value
            server_payload = dict(item)
            server_payload["server"] = name
            server_payload["runtimeStatus"] = item.get("runtimeStatus", item.get("status"))
            server_payload["authStatus"] = item.get("authStatus")
            server_payload["serverCapabilities"] = item.get("serverCapabilities")
            server_payload["tools"] = tools
            servers.append(server_payload)
        self._touch(runtime_id, mcp_server_count=len(data))
        return {
            "success": True,
            "runtime_id": runtime_id,
            "thread_id": binding.thread_id,
            "servers": servers,
            "next_cursor": result.get("nextCursor"),
            "cursor": normalized_cursor,
            "limit": limit,
            "server": normalized_server,
            "tool_query": normalized_query,
        }

    def mcp_call(
        self,
        *,
        runtime_id: str,
        server: str,
        tool: str,
        arguments: dict[str, Any] | None,
        meta: dict[str, Any] | None,
    ) -> dict[str, object]:
        binding = self._active_binding(runtime_id)
        normalized_server = self._normalize_required_name(server, "server")
        normalized_tool = self._normalize_required_name(tool, "tool")
        if arguments is not None and not isinstance(arguments, dict):
            raise CodexRuntimeError("invalid_arguments", "arguments must be a JSON object or null.")
        if meta is not None and not isinstance(meta, dict):
            raise CodexRuntimeError("invalid_arguments", "_meta must be a JSON object or null.")
        with self._request_slot():
            result = self._adapter.mcp_call(
                thread_id=binding.thread_id,
                server=normalized_server,
                tool=normalized_tool,
                arguments=dict(arguments or {}),
                meta=dict(meta) if meta is not None else None,
            )
        self._touch(runtime_id)
        is_error = result.get("isError")
        if is_error not in {True, False, None}:
            raise CodexRuntimeError("codex_protocol_error", "Codex returned an invalid MCP tool result.")
        payload: dict[str, object] = {
            "success": not bool(is_error),
            "runtime_id": runtime_id,
            "thread_id": binding.thread_id,
            "server": normalized_server,
            "tool": normalized_tool,
            "content": result.get("content", []),
            "structuredContent": result.get("structuredContent"),
            "isError": is_error,
            "_meta": result.get("_meta"),
        }
        if is_error:
            payload["error"] = {
                "code": "mcp_tool_error",
                "message": "The downstream MCP tool reported an error.",
            }
        return payload

    def shutdown(self) -> None:
        with self._lock:
            for runtime_id, binding in list(self._bindings.items()):
                self._bindings[runtime_id] = replace(binding, status="detached")
            self._live_thread_ids.clear()
        self._adapter.shutdown()

    def _ensure_store(self) -> None:
        if self._load_error is not None:
            raise self._load_error

    def _sync_connection_generation(self) -> None:
        generation = getattr(self._adapter, "connection_generation", None)
        if generation is None:
            return
        with self._lock:
            if generation != self._observed_connection_generation:
                self._live_thread_ids.clear()
                self._observed_connection_generation = generation

    @contextmanager
    def _request_slot(self):
        acquired = self._slots.acquire(timeout=max(1.0, self.default_timeout_ms / 1000.0))
        if not acquired:
            raise CodexRuntimeError(
                "runtime_concurrency_limit",
                "The Codex runtime concurrency limit is currently full.",
                retryable=True,
                details={"max_concurrency": self.max_concurrency},
            )
        try:
            yield
        finally:
            self._slots.release()

    def _active_binding(self, runtime_id: str) -> RuntimeBinding:
        self._ensure_store()
        with self._lock:
            binding = self._bindings.get(runtime_id)
            if binding is None:
                raise CodexRuntimeError("runtime_not_found", f"Unknown runtime_id: {runtime_id}.")
            if not self._adapter.is_running():
                self._bindings[runtime_id] = replace(binding, status="detached")
                raise CodexRuntimeError(
                    "runtime_not_active",
                    "The Codex runtime is detached from its App Server process.",
                    retryable=True,
                    details={"runtime_id": runtime_id, "recovery": "Call codex_runtime_resume with runtime_id."},
                )
            if binding.status != "ready":
                raise CodexRuntimeError(
                    "runtime_not_active",
                    "The Codex runtime is not active.",
                    retryable=True,
                    details={"runtime_id": runtime_id, "recovery": "Call codex_runtime_resume with runtime_id."},
                )
            return binding

    def _touch(self, runtime_id: str, *, mcp_server_count: int | None = None) -> None:
        with self._lock:
            binding = self._bindings.get(runtime_id)
            if binding is None:
                return
            updated_binding = replace(
                binding,
                last_used_at=time.time(),
                status="ready",
                mcp_server_count=(
                    mcp_server_count if mcp_server_count is not None else binding.mcp_server_count
                ),
            )
            updated = {**self._bindings, runtime_id: updated_binding}
            try:
                self._store.save(updated)
                self._persistence_warning = None
            except BindingStoreError as exc:
                self._persistence_warning = exc.code
            self._bindings = updated

    def _save_bindings(self, bindings: dict[str, RuntimeBinding]) -> None:
        try:
            self._store.save(bindings)
        except BindingStoreError as exc:
            raise exc
        self._bindings = bindings
        self._persistence_warning = None

    @staticmethod
    def _validate_cwd(cwd: Path) -> Path:
        target = Path(cwd).expanduser().resolve()
        if not target.exists():
            raise CodexRuntimeError("cwd_not_found", f"Directory does not exist: {target}.")
        if not target.is_dir():
            raise CodexRuntimeError("cwd_not_directory", f"Path is not a directory: {target}.")
        return target

    @staticmethod
    def _normalize_name(name: str | None) -> str | None:
        if name is None:
            return None
        normalized = name.strip()
        if not normalized:
            return None
        if len(normalized) > MAX_RUNTIME_NAME_LENGTH:
            raise CodexRuntimeError(
                "invalid_arguments",
                f"name must be at most {MAX_RUNTIME_NAME_LENGTH} characters.",
            )
        return normalized

    @staticmethod
    def _normalize_required_name(value: str, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise CodexRuntimeError("invalid_arguments", f"{field} must be a non-empty string.")
        normalized = value.strip()
        if len(normalized) > MAX_FILTER_LENGTH:
            raise CodexRuntimeError("invalid_arguments", f"{field} is too long.")
        return normalized

    @classmethod
    def _normalize_filter(cls, value: str | None, field: str) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            return None
        if len(normalized) > MAX_FILTER_LENGTH:
            raise CodexRuntimeError("invalid_arguments", f"{field} is too long.")
        return normalized

    @staticmethod
    def _normalize_cursor(cursor: str | None) -> str | None:
        if cursor is None:
            return None
        normalized = cursor.strip()
        if not normalized:
            return None
        if len(normalized) > 4096:
            raise CodexRuntimeError("invalid_arguments", "cursor is too long.")
        return normalized

    @staticmethod
    def _validate_command(command: list[str]) -> list[str]:
        if not isinstance(command, list) or not command:
            raise CodexRuntimeError("invalid_arguments", "command must be a non-empty argv list.")
        if len(command) > MAX_COMMAND_ARGUMENTS:
            raise CodexRuntimeError("invalid_arguments", "command contains too many arguments.")
        normalized: list[str] = []
        total_bytes = 0
        for argument in command:
            if not isinstance(argument, str) or not argument:
                raise CodexRuntimeError("invalid_arguments", "command arguments must be non-empty strings.")
            total_bytes += len(argument.encode("utf-8"))
            if total_bytes > MAX_COMMAND_ARGUMENT_BYTES:
                raise CodexRuntimeError("invalid_arguments", "command arguments exceed the size limit.")
            normalized.append(argument)
        return normalized

    def _validate_timeout(self, timeout_ms: int) -> int:
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms <= 0:
            raise CodexRuntimeError("invalid_arguments", "timeout_ms must be a positive integer.")
        if timeout_ms > self.max_timeout_ms:
            raise CodexRuntimeError(
                "invalid_arguments",
                f"timeout_ms must be at most {self.max_timeout_ms}.",
            )
        return timeout_ms

    @staticmethod
    def _verify_thread_metadata(
        result: dict[str, Any],
        *,
        expected_cwd: Path,
        expected_sandbox: SandboxMode,
        method: str,
    ) -> tuple[str, Path, object]:
        thread = result.get("thread")
        if not isinstance(thread, dict):
            raise CodexRuntimeError("codex_protocol_error", f"Codex returned no thread for {method}.")
        thread_id = thread.get("id") or result.get("threadId")
        returned_cwd_value = result.get("cwd") or thread.get("cwd")
        returned_sandbox = result.get("sandbox") or thread.get("sandbox")
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise CodexRuntimeError("codex_protocol_error", f"Codex returned no thread_id for {method}.")
        if not isinstance(returned_cwd_value, str) or not returned_cwd_value.strip():
            raise CodexRuntimeError("codex_protocol_error", f"Codex returned no cwd for {method}.")
        try:
            returned_cwd = Path(returned_cwd_value).expanduser().resolve()
        except (OSError, RuntimeError):
            raise CodexRuntimeError("codex_protocol_error", f"Codex returned an invalid cwd for {method}.") from None
        if returned_cwd != expected_cwd.resolve():
            raise CodexRuntimeError(
                "runtime_metadata_mismatch",
                f"Codex returned a different cwd for {method}.",
                details={"expected_cwd": str(expected_cwd), "returned_cwd": str(returned_cwd)},
            )
        if not sandbox_matches(expected_sandbox, returned_sandbox):
            raise CodexRuntimeError(
                "runtime_metadata_mismatch",
                f"Codex returned a different sandbox for {method}.",
                details={"expected_sandbox": expected_sandbox},
            )
        return thread_id.strip(), returned_cwd, returned_sandbox


def _is_no_rollout_error(error: AppServerRpcError) -> bool:
    message = error.message.casefold()
    return "no rollout found" in message or "no rollout" in message
