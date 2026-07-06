from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from . import session
from .pathing import resolve_path
from .tool_context import LOCAL_STATE_TOOL, READ_ONLY_TOOL, ToolContext


def register_core_tools(mcp: Any, ctx: ToolContext) -> dict[str, object]:
    """Register server metadata and cwd tools."""

    @mcp.tool(
        name="server_info",
        title="Server Info",
        annotations=READ_ONLY_TOOL,
        description=(
            "Return server metadata: app name, host/port, workspace root, state dir, "
            "timeouts, auth mode, and the list of registered tools. Useful as a first "
            "call to confirm which bridge you are connected to and what it can do."
        ),
    )
    async def server_info() -> dict[str, object]:
        list_tools = getattr(mcp, "_list_tools")
        try:
            registered = await list_tools()
        except TypeError:
            # fastmcp 2.14 requires a context arg; None works for server-side listing.
            registered = await list_tools(None)
        tools = sorted(tool.name for tool in registered)
        session_cwd = session.get_default_cwd()
        return {
            "success": True,
            "app_name": ctx.app_name,
            "host": ctx.host,
            "port": ctx.port,
            "workspace_root": str(ctx.workspace_root),
            "session_cwd": str(session_cwd) if session_cwd else None,
            "state_dir": str(ctx.state_dir),
            "command_timeout_seconds": ctx.command_timeout,
            "delegate_timeout_seconds": ctx.delegate_timeout,
            "auth": ctx.current_oauth_config().normalized_auth_mode,
            "debug_mcp_logging": ctx.debug_mcp_logging,
            "codex_command": ctx.codex_command,
            "routing_contract": {
                "chatgpt_web_role": "architect_manager_reviewer",
                "codex_delegate_role": "single_bounded_execution_slice",
                "default_flow": [
                    "ChatGPT Web inspects and reasons with direct MCP tools.",
                    "ChatGPT Web creates a small Codex Execution Prompt when local execution needs a delegate.",
                    "delegate_task runs exactly one serialized execution slice.",
                    "ChatGPT Web reviews the result, logs, and local verification evidence before deciding the next step.",
                ],
                "delegate_task_should_not": [
                    "Perform broad opaque planning or research loops.",
                    "Replace direct MCP inspection, patching, git checks, or short shell verification.",
                    "Declare work complete without local verification evidence.",
                ],
            },
            "delegate_mode": {
                "executor": "codex",
                "serial": True,
                "background_tasks": False,
                "default_wait_seconds": 180,
                "continuation": "call delegate_task again when status is running",
                "audit_logs": "system temp / chatgpt-web-oauth-mcp / codex-delegates",
            },
            "obsidian_proxy": {
                "enabled": ctx.enable_obsidian,
                "configured": bool(ctx.obsidian_api_key.strip()),
                "mcp_url": ctx.current_obsidian_config().mcp_url,
                "mode": "native_mcp_proxy",
                "tool_prefix": "obsidian_",
            },
            "tools": tools,
            "tool_count": len(tools),
        }

    @mcp.tool(
        name="set_default_cwd",
        title="Set Default CWD",
        annotations=LOCAL_STATE_TOOL,
        description=(
            "Set the session-wide default working directory used whenever a tool call "
            "omits `cwd`. Pass null (or omit path) to clear the override and fall back to "
            "the server's workspace root. Useful when running many commands in the same "
            "repo: set it once instead of passing `cwd` on every call."
        ),
    )
    def set_default_cwd(
        path: Annotated[
            str | None,
            Field(
                description=(
                    "Directory to use as the session default cwd. Pass null or omit to clear "
                    "the override and use the server workspace_root."
                )
            ),
        ] = None
    ) -> dict[str, object]:
        if not path:
            session.set_default_cwd(None)
            return {
                "success": True,
                "session_cwd": None,
                "workspace_root": str(ctx.workspace_root),
                "cleared": True,
            }
        target = resolve_path(path, ctx.workspace_root)
        if not target.exists():
            return {
                "success": False,
                "error": {
                    "code": "cwd_not_found",
                    "message": f"Path does not exist: {target}",
                },
                "path": str(target),
            }
        if not target.is_dir():
            return {
                "success": False,
                "error": {
                    "code": "cwd_not_directory",
                    "message": f"Path is not a directory: {target}",
                },
                "path": str(target),
            }
        session.set_default_cwd(target)
        return {
            "success": True,
            "session_cwd": str(target),
            "workspace_root": str(ctx.workspace_root),
            "cleared": False,
        }

    @mcp.tool(
        name="get_default_cwd",
        title="Get Default CWD",
        annotations=READ_ONLY_TOOL,
        description=(
            "Return the currently active default working directory and whether it comes "
            "from the session override (set_default_cwd) or from the server's workspace root."
        ),
    )
    def get_default_cwd() -> dict[str, object]:
        session_cwd = session.get_default_cwd()
        effective = session_cwd if session_cwd is not None else ctx.workspace_root
        return {
            "success": True,
            "session_cwd": str(session_cwd) if session_cwd else None,
            "workspace_root": str(ctx.workspace_root),
            "effective_cwd": str(effective),
            "source": "session" if session_cwd else "workspace_root",
        }

    return {
        "server_info": server_info,
        "set_default_cwd": set_default_cwd,
        "get_default_cwd": get_default_cwd,
    }
