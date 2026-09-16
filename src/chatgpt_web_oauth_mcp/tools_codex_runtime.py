from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, cast

from pydantic import Field

from .codex_runtime.errors import CodexRuntimeError
from .codex_runtime.models import SandboxMode
from .pathing import resolve_cwd
from .response_budget import ResponseBudget, with_budget_metadata
from .tool_context import OPEN_WORLD_WRITE_TOOL, READ_ONLY_TOOL, LOCAL_STATE_TOOL, ToolContext


_MAX_TEXT_BYTES = 256 * 1024


def register_codex_runtime_tools(mcp: Any, ctx: ToolContext) -> dict[str, object]:
    """Register the persistent Codex runtime surface."""

    @mcp.tool(
        name="codex_runtime_open",
        title="Open Codex Runtime",
        annotations=LOCAL_STATE_TOOL,
        description=(
            "Create a persistent Codex App Server thread for one cwd and sandbox policy. "
            "This starts the runtime thread but never starts a Codex LLM turn. "
            "Omit sandbox to use the server-configured default policy."
        ),
    )
    def codex_runtime_open(
        cwd: Annotated[
            str,
            Field(description="Directory to bind to this runtime."),
        ],
        sandbox: Annotated[
            SandboxMode | None,
            Field(description="Sandbox policy: read-only, workspace-write, or full-access; omit for server default."),
        ] = None,
        name: Annotated[
            str | None,
            Field(description="Optional human-readable runtime name."),
        ] = None,
    ) -> dict[str, object]:
        manager = _manager_or_error(ctx)
        if isinstance(manager, dict):
            return manager
        effective_sandbox = sandbox or cast(SandboxMode, ctx.codex_runtime_default_sandbox)
        return _invoke(
            lambda: manager.open_runtime(
                cwd=resolve_cwd(cwd, ctx.workspace_root),
                sandbox=effective_sandbox,
                name=name,
            )
        )

    @mcp.tool(
        name="codex_runtime_resume",
        title="Resume Codex Runtime",
        annotations=LOCAL_STATE_TOOL,
        description=(
            "Resume a persisted Codex runtime by runtime_id, or bind an explicit thread_id. "
            "A runtime_id uses its persisted cwd and sandbox and rejects mismatches. "
            "When the original thread has no rollout, the runtime_id is retained and a "
            "same-policy replacement thread is created explicitly in the result. "
            "An unknown thread_id requires cwd and sandbox so the project binding is verified."
        ),
    )
    def codex_runtime_resume(
        runtime_id: Annotated[
            str | None,
            Field(description="Persisted runtime identity; mutually exclusive with thread_id."),
        ] = None,
        thread_id: Annotated[
            str | None,
            Field(description="Codex thread identity; requires cwd and sandbox when unbound."),
        ] = None,
        cwd: Annotated[
            str | None,
            Field(description="Expected bound cwd, required when resuming an unbound thread_id."),
        ] = None,
        sandbox: Annotated[
            SandboxMode | None,
            Field(description="Expected sandbox, required when resuming an unbound thread_id."),
        ] = None,
    ) -> dict[str, object]:
        manager = _manager_or_error(ctx)
        if isinstance(manager, dict):
            return manager
        resolved_cwd = resolve_cwd(cwd, ctx.workspace_root) if cwd else None
        return _invoke(
            lambda: manager.resume_runtime(
                runtime_id=runtime_id,
                thread_id=thread_id,
                cwd=resolved_cwd,
                sandbox=sandbox,
            )
        )

    @mcp.tool(
        name="codex_runtime_status",
        title="Codex Runtime Status",
        annotations=READ_ONLY_TOOL,
        description=(
            "Return bounded metadata for one Codex runtime without returning thread history. "
            "Persisted runtimes are detached after an MCP server restart and can be resumed explicitly."
        ),
    )
    def codex_runtime_status(
        runtime_id: Annotated[str, Field(description="Runtime identity to inspect.")],
    ) -> dict[str, object]:
        manager = _manager_or_error(ctx)
        if isinstance(manager, dict):
            return manager
        return _invoke(lambda: manager.runtime_status(runtime_id))

    @mcp.tool(
        name="codex_runtime_close",
        title="Close Codex Runtime",
        annotations=LOCAL_STATE_TOOL,
        description=(
            "Mark a local Codex runtime binding detached while preserving its runtime_id and Codex thread. "
            "This is detach-only: it does not call thread/delete or thread/archive, and the same runtime_id "
            "can be resumed later."
        ),
    )
    def codex_runtime_close(
        runtime_id: Annotated[str, Field(description="Runtime identity to detach.")],
    ) -> dict[str, object]:
        manager = _manager_or_error(ctx)
        if isinstance(manager, dict):
            return manager
        return _invoke(lambda: manager.close_runtime(runtime_id))

    @mcp.tool(
        name="codex_exec",
        title="Execute In Codex Runtime",
        annotations=OPEN_WORLD_WRITE_TOOL,
        description=(
            "Run an argv list through Codex App Server command/exec using the selected runtime's "
            "sandbox policy. Outputs are bounded, timeout is bounded, and timeout termination is "
            "attempted with command/exec/terminate. This never uses thread/shellCommand or a shell string."
        ),
    )
    def codex_exec(
        runtime_id: Annotated[str, Field(description="Runtime identity that owns the sandbox and cwd.")],
        command: Annotated[
            list[str],
            Field(min_length=1, max_length=128, description="Non-empty argv vector; shell syntax is not evaluated."),
        ],
        timeout_ms: Annotated[
            int | None,
            Field(ge=1, description="Optional bounded command timeout in milliseconds."),
        ] = None,
        cwd: Annotated[
            str | None,
            Field(description="Optional descendant of the runtime cwd."),
        ] = None,
    ) -> dict[str, object]:
        manager = _manager_or_error(ctx)
        if isinstance(manager, dict):
            return manager
        resolved_cwd = resolve_cwd(cwd, ctx.workspace_root) if cwd else None
        payload = _invoke(
            lambda: manager.exec_command(
                runtime_id=runtime_id,
                command=command,
                timeout_ms=timeout_ms,
                cwd=resolved_cwd,
            )
        )
        return _bounded(payload, ctx.tool_output_token_budget, fields=("stdout", "stderr"))

    @mcp.tool(
        name="codex_mcp_inventory",
        title="List Codex MCP Tools",
        annotations=READ_ONLY_TOOL,
        description=(
            "List the MCP servers, auth status, resources, and tools connected to one Codex thread. "
            "The upstream catalog is paginated and the response is bounded; use next_cursor to continue. "
            "Optional server and tool_query filters are applied without starting a Codex LLM turn."
        ),
    )
    def codex_mcp_inventory(
        runtime_id: Annotated[str, Field(description="Runtime identity whose Codex thread is queried.")],
        server: Annotated[
            str | None,
            Field(description="Optional exact MCP server name filter."),
        ] = None,
        tool_query: Annotated[
            str | None,
            Field(description="Optional case-insensitive tool name or description filter."),
        ] = None,
        cursor: Annotated[
            str | None,
            Field(description="Opaque cursor returned by a prior inventory call."),
        ] = None,
        limit: Annotated[
            int,
            Field(ge=1, le=100, description="Maximum upstream server page size."),
        ] = 20,
    ) -> dict[str, object]:
        manager = _manager_or_error(ctx)
        if isinstance(manager, dict):
            return manager
        payload = _invoke(
            lambda: manager.mcp_inventory(
                runtime_id=runtime_id,
                server=server,
                tool_query=tool_query,
                cursor=cursor,
                limit=limit,
            )
        )
        return _bounded(payload, ctx.tool_output_token_budget, fields=("servers",))

    @mcp.tool(
        name="codex_mcp_call",
        title="Call Codex MCP Tool",
        annotations=OPEN_WORLD_WRITE_TOOL,
        description=(
            "Call one MCP tool connected to the selected Codex thread without starting turn/start. "
            "The downstream content, structuredContent, isError, and _meta fields are retained. "
            "Approval, elicitation, and auth-required requests are never silently auto-approved."
        ),
    )
    def codex_mcp_call(
        runtime_id: Annotated[str, Field(description="Runtime identity whose Codex thread is used.")],
        server: Annotated[str, Field(description="Connected Codex MCP server name.")],
        tool: Annotated[str, Field(description="Connected MCP tool name.")],
        arguments: Annotated[
            dict[str, Any] | None,
            Field(description="JSON object passed to the downstream MCP tool."),
        ] = None,
        _meta: Annotated[
            dict[str, Any] | None,
            Field(description="Optional MCP _meta object passed to the downstream tool."),
        ] = None,
    ) -> dict[str, object]:
        manager = _manager_or_error(ctx)
        if isinstance(manager, dict):
            return manager
        payload = _invoke(
            lambda: manager.mcp_call(
                runtime_id=runtime_id,
                server=server,
                tool=tool,
                arguments=arguments,
                meta=_meta,
            )
        )
        return _bounded(payload, ctx.tool_output_token_budget, fields=("content", "structuredContent", "_meta"))

    return {
        "codex_runtime_open": codex_runtime_open,
        "codex_runtime_resume": codex_runtime_resume,
        "codex_runtime_status": codex_runtime_status,
        "codex_runtime_close": codex_runtime_close,
        "codex_exec": codex_exec,
        "codex_mcp_inventory": codex_mcp_inventory,
        "codex_mcp_call": codex_mcp_call,
    }


def _manager_or_error(ctx: ToolContext) -> Any:
    manager = ctx.codex_runtime_manager
    if manager is None:
        return {
            "success": False,
            "error": {
                "code": "runtime_disabled",
                "message": "Codex runtime support is not configured.",
            },
        }
    return manager


def _invoke(operation: Callable[[], dict[str, object]]) -> dict[str, object]:
    try:
        return operation()
    except CodexRuntimeError as exc:
        payload: dict[str, object] = {
            "success": False,
            "error": {
                "code": exc.code,
                "message": exc.message,
                "retryable": exc.retryable,
            },
        }
        if exc.details:
            payload["error"]["details"] = exc.details  # type: ignore[index]
        if exc.side_effects:
            payload["side_effects"] = exc.side_effects
        return payload
    except (OSError, TypeError, ValueError) as exc:
        return {
            "success": False,
            "error": {
                "code": "runtime_invalid_operation",
                "message": str(exc),
            },
        }
    except Exception:
        return {
            "success": False,
            "error": {
                "code": "runtime_internal_error",
                "message": "The Codex runtime operation failed unexpectedly.",
            },
        }


def _bounded(
    payload: dict[str, object],
    max_tokens: int,
    *,
    fields: tuple[str, ...],
) -> dict[str, object]:
    budget = ResponseBudget(max_tokens=max_tokens)
    complete, measurement = with_budget_metadata(
        payload,
        budget=budget,
        truncated=False,
        stop_reason="end_of_result",
    )
    if measurement.fits:
        return complete
    for fraction in (0.75, 0.5, 0.35, 0.2, 0.1, 0.05, 0.0):
        candidate = dict(payload)
        for field in fields:
            if field in candidate:
                candidate[field] = (
                    _scale_inventory(candidate[field], fraction)
                    if field == "servers"
                    else _scale_value(candidate[field], fraction)
                )
        candidate["truncated_fields"] = list(fields)
        result, result_measurement = with_budget_metadata(
            candidate,
            budget=budget,
            truncated=True,
            stop_reason="token_budget",
        )
        if result_measurement.fits:
            return result
    return complete


def _scale_inventory(value: Any, fraction: float) -> list[Any]:
    if not isinstance(value, list):
        return []
    if fraction >= 1:
        return value
    keep = int(len(value) * fraction)
    if fraction > 0 and keep == 0 and value:
        keep = 1
    scaled_servers: list[Any] = []
    for server in value[:keep]:
        if not isinstance(server, dict):
            scaled_servers.append(server)
            continue
        scaled: dict[Any, Any] = {}
        for key, item in server.items():
            # Server identity and tool names are used as the next call's
            # arguments, so budget reduction must never corrupt them.
            if key in {"server", "name", "authStatus", "runtimeStatus"}:
                scaled[key] = item
            elif key == "tools" and isinstance(item, dict):
                scaled[key] = _scale_inventory_tools(item, fraction)
            else:
                scaled[key] = _scale_value(item, fraction)
        scaled_servers.append(scaled)
    return scaled_servers


def _scale_inventory_tools(value: dict[Any, Any], fraction: float) -> dict[Any, Any]:
    scaled_tools: dict[Any, Any] = {}
    for tool_name, tool in value.items():
        if not isinstance(tool, dict):
            scaled_tools[tool_name] = tool
            continue
        scaled_tool: dict[Any, Any] = {}
        for key, item in tool.items():
            if key == "name":
                scaled_tool[key] = item
            else:
                scaled_tool[key] = _scale_value(item, fraction)
        scaled_tools[tool_name] = scaled_tool
    return scaled_tools


def _scale_value(value: Any, fraction: float) -> Any:
    if fraction >= 1:
        return value
    if isinstance(value, str):
        return _truncate_text(value, int(len(value.encode("utf-8")) * fraction))
    if isinstance(value, list):
        if not value:
            return []
        keep = int(len(value) * fraction)
        if fraction > 0 and keep == 0:
            keep = 1
        return [_scale_value(item, fraction) for item in value[:keep]]
    if isinstance(value, dict):
        if not value:
            return {}
        keep = int(len(value) * fraction)
        if fraction > 0 and keep == 0:
            keep = 1
        keys = list(value)[:keep]
        return {key: _scale_value(value[key], fraction) for key in keys}
    return value


def _truncate_text(value: str, max_bytes: int) -> str:
    max_bytes = max(0, min(max_bytes, _MAX_TEXT_BYTES))
    raw = value.encode("utf-8")
    if len(raw) <= max_bytes:
        return value
    marker = b"\n...[truncated]...\n"
    if max_bytes <= len(marker):
        return marker[:max_bytes].decode("utf-8", errors="ignore")
    head_size = (max_bytes - len(marker)) // 2
    tail_size = max_bytes - len(marker) - head_size
    head = raw[:head_size].decode("utf-8", errors="ignore")
    tail = raw[-tail_size:].decode("utf-8", errors="ignore") if tail_size else ""
    return head + marker.decode("ascii") + tail
