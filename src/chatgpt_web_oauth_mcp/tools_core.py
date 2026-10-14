from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from pydantic import Field

from . import session
from .guidance import (
    FILE_USE_URI,
    GIT_USE_URI,
    PROCESS_USE_URI,
    RUNTIME_USE_URI,
    SKILL_INDEX_URI,
)
from .envtools import env_diff as env_diff_impl
from .envtools import env_snapshot as env_snapshot_impl
from .pathing import resolve_cwd, resolve_path
from .tmux_ops import TmuxClient, tmux_runtime_info
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
            "codex_runtime": (
                {
                    **ctx.codex_runtime_manager.info(),
                    "default_sandbox": ctx.codex_runtime_default_sandbox,
                    "computer_use_approval_mode": ctx.codex_runtime_cua_approval_mode,
                    "computer_use_allowed_apps": sorted(ctx.codex_runtime_cua_allowed_apps),
                }
                if ctx.codex_runtime_manager is not None
                else {
                    "enabled": False,
                    "default_sandbox": ctx.codex_runtime_default_sandbox,
                    "computer_use_approval_mode": ctx.codex_runtime_cua_approval_mode,
                    "computer_use_allowed_apps": sorted(ctx.codex_runtime_cua_allowed_apps),
                }
            ),
            "tmux": tmux_runtime_info(
                binary=ctx.tmux_binary,
                socket_name=ctx.tmux_socket_name,
            ),
            "routing_contract": {
                "chatgpt_web_role": "architect_manager_reviewer",
                "codex_runtime_role": "persistent_runtime_and_connected_mcp_access",
                "execution_loop": {
                    "after_tool_result": "invoke_next_tool_or_return_checkpoint",
                    "waiting_requires": "evidence_of_active_process",
                    "idle_state": "NEXT_ACTION_REQUIRED",
                    "recovery_tool": "execution_state",
                },
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
                    "runtime-use": "get_runtime_use",
                    "git-use": "get_git_use",
                },
                "guide_resources": {
                    "file-use": FILE_USE_URI,
                    "process-use": PROCESS_USE_URI,
                    "runtime-use": RUNTIME_USE_URI,
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
        name="execution_state",
        title="Execution State",
        annotations=READ_ONLY_TOOL,
        description=(
            "Return a bounded server-side execution-loop snapshot for durable jobs and tmux sessions scoped to "
            "the effective current working directory. Global activity is reported separately and does not justify "
            "waiting for the current workflow. "
            "Use it whenever deciding whether it is valid to wait. state=NEXT_ACTION_REQUIRED means "
            "there is no managed process to wait for: immediately invoke the next concrete tool or "
            "return a checkpoint/final/blocker response. A persistent Codex runtime by itself is not "
            "evidence that task work is still running."
        ),
    )
    def execution_state(
        cwd: Annotated[
            str | None,
            Field(
                description=(
                    "Workflow working directory to scope activity to. Defaults to the session cwd, then the "
                    "workspace root. Jobs and tmux sessions from other cwd values are diagnostic only."
                )
            ),
        ] = None,
    ) -> dict[str, object]:
        effective_cwd = resolve_cwd(cwd, ctx.workspace_root).resolve(strict=False)
        session_cwd = session.get_default_cwd()
        jobs = ctx.job_registry.list_jobs(
            state_dir=ctx.state_dir,
            status="running",
            offset=0,
            limit=20,
            max_tokens=ctx.tool_output_token_budget,
        )
        tmux = TmuxClient(
            binary=ctx.tmux_binary,
            socket_name=ctx.tmux_socket_name,
            timeout=ctx.tmux_control_timeout,
        ).list_sessions(include_panes=False)

        jobs_ok = bool(jobs.get("success"))
        tmux_ok = bool(tmux.get("success"))
        all_running_jobs = jobs.get("jobs", []) if jobs_ok else []
        all_tmux_sessions = tmux.get("sessions", []) if tmux_ok else []

        def same_cwd(raw_path: object) -> bool:
            if not isinstance(raw_path, str) or not raw_path:
                return False
            return Path(raw_path).expanduser().resolve(strict=False) == effective_cwd

        running_jobs = [job for job in all_running_jobs if same_cwd(job.get("cwd"))]
        tmux_sessions = [
            item
            for item in all_tmux_sessions
            if same_cwd((item.get("primary_pane") or {}).get("current_path"))
        ]

        if running_jobs:
            state = "ACTIVE_PROCESS"
            waiting_justified = True
            required_action = "POLL_OR_INSPECT_ACTIVE_PROCESS"
        elif tmux_sessions:
            state = "INTERACTIVE_SESSION_REQUIRES_INSPECTION"
            waiting_justified = False
            required_action = "INSPECT_TMUX_STATUS_AND_CAPTURE"
        elif not jobs_ok or not tmux_ok:
            state = "ACTIVITY_UNKNOWN"
            waiting_justified = False
            required_action = "RESOLVE_ACTIVITY_UNCERTAINTY"
        else:
            state = "NEXT_ACTION_REQUIRED"
            waiting_justified = False
            required_action = "INVOKE_NEXT_TOOL_OR_RETURN_CHECKPOINT"

        return {
            "success": jobs_ok and tmux_ok,
            "state": state,
            "waiting_justified": waiting_justified,
            "required_action": required_action,
            "scope_cwd": str(effective_cwd),
            "scope_source": "explicit" if cwd else ("session" if session_cwd is not None else "workspace_root"),
            "running_jobs": running_jobs,
            "running_job_count": len(running_jobs),
            "tmux_sessions": tmux_sessions,
            "tmux_session_count": len(tmux_sessions),
            "global_running_job_count": len(all_running_jobs),
            "global_tmux_session_count": len(all_tmux_sessions),
            "observation_errors": {
                "jobs": jobs.get("error") if not jobs_ok else None,
                "tmux": tmux.get("error") if not tmux_ok else None,
            },
            "contract": (
                "Do not stop in reasoning after a tool result. Waiting requires evidence of active work. "
                "When state is NEXT_ACTION_REQUIRED, continue with a concrete tool call or return a "
                "checkpoint/final/blocker response in the current turn."
            ),
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
        "execution_state": execution_state,
        "set_default_cwd": set_default_cwd,
        "get_default_cwd": get_default_cwd,
        "env_snapshot": env_snapshot,
        "env_diff": env_diff,
    }
