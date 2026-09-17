from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from . import session
from .delegate_guidance import (
    FILE_USE_URI,
    GIT_USE_URI,
    PROCESS_USE_URI,
    SKILL_INDEX_URI,
)
from .envtools import env_diff as env_diff_impl
from .envtools import env_snapshot as env_snapshot_impl
from .pathing import resolve_cwd, resolve_path
from .tmux_ops import tmux_runtime_info
from .tool_context import LOCAL_STATE_TOOL, READ_ONLY_TOOL, ToolContext


def register_core_tools(mcp: Any, ctx: ToolContext) -> dict[str, object]:
    """Register server metadata and cwd tools."""

    @mcp.tool(
        name="server_info",
        title="Server Info",
        annotations=READ_ONLY_TOOL,
        description=(
            "Return server metadata: app name, host/port, workspace root, state dir, "
            "timeouts, auth mode, and registered tools/resources. Useful as a first "
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
        registered_resources = await mcp.list_resources()
        resource_uris = sorted(str(resource.uri) for resource in registered_resources)
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
            "auth": ctx.current_oauth_config().normalized_auth_mode,
            "debug_mcp_logging": ctx.debug_mcp_logging,
            "codex_command": ctx.codex_command,
            "pi_command": ctx.pi_command,
            "codex_runtime": (
                {
                    **ctx.codex_runtime_manager.info(),
                    "default_sandbox": ctx.codex_runtime_default_sandbox,
                }
                if ctx.codex_runtime_manager is not None
                else {"enabled": False, "default_sandbox": ctx.codex_runtime_default_sandbox}
            ),
            "tmux": tmux_runtime_info(
                binary=ctx.tmux_binary,
                socket_name=ctx.tmux_socket_name,
            ),
            "routing_contract": {
                "chatgpt_web_role": "architect_manager_reviewer",
                "codex_runtime_role": "persistent_runtime_and_connected_mcp_access",
                "default_flow": [
                    "ChatGPT Web inspects and reasons with direct MCP tools.",
                    "Use codex_runtime_* and codex_mcp_* when persistent Codex runtime access is needed.",
                    "Use direct file, process, and Git tools for deterministic local operations.",
                ],
            },
            "skill_guidance": {
                "discovery_tool": "get_skill_index",
                "index_resource": SKILL_INDEX_URI,
                "guide_tools": {
                    "file-use": "get_file_use",
                    "process-use": "get_process_use",
                    "git-use": "get_git_use",
                },
                "guide_resources": {
                    "file-use": FILE_USE_URI,
                    "process-use": PROCESS_USE_URI,
                    "git-use": GIT_USE_URI,
                },
                "progressive_disclosure": True,
            },
            "resources": resource_uris,
            "resource_count": len(resource_uris),
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

    @mcp.tool(
        name="env_snapshot",
        title="Environment Snapshot",
        annotations=READ_ONLY_TOOL,
        description=(
            "Collect a small read-only environment snapshot for cwd: platform, Python runtime, "
            "git, Node/npm, Java, common dependency/config file hashes, and optionally bounded "
            "pip freeze output. Does not source shell profiles, activate venvs, install packages, "
            "or modify the local environment."
        ),
    )
    def env_snapshot(
        cwd: Annotated[
            str | None,
            Field(description="Working directory to inspect. Defaults to the session cwd or workspace root."),
        ] = None,
        include_packages: Annotated[
            bool,
            Field(description="When true, include bounded `python -m pip freeze` output for the server Python."),
        ] = False,
    ) -> dict[str, object]:
        resolved_cwd = resolve_cwd(cwd, ctx.workspace_root)
        return env_snapshot_impl(cwd=resolved_cwd, include_packages=include_packages)

    @mcp.tool(
        name="env_diff",
        title="Environment Diff",
        annotations=READ_ONLY_TOOL,
        description=(
            "Compare two inline env_snapshot-like JSON objects and return changed nested keys "
            "using dot-path notation. This tool does not read snapshot files from disk."
        ),
    )
    def env_diff(
        left: Annotated[
            dict[str, Any],
            Field(description="Left environment snapshot object to compare."),
        ],
        right: Annotated[
            dict[str, Any],
            Field(description="Right environment snapshot object to compare."),
        ],
    ) -> dict[str, object]:
        return env_diff_impl(left=left, right=right)

    return {
        "server_info": server_info,
        "set_default_cwd": set_default_cwd,
        "get_default_cwd": get_default_cwd,
        "env_snapshot": env_snapshot,
        "env_diff": env_diff,
    }
