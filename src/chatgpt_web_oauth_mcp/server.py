from __future__ import annotations

import argparse
import os
from typing import Any

from fastmcp import FastMCP
import uvicorn

from .config import (
    APP_NAME,
    AUTH_MODE,
    AUTH_TOKEN,
    CODEX_COMMAND,
    COMMAND_TIMEOUT,
    DEBUG_MCP_LOGGING,
    DELEGATE_TIMEOUT,
    GRACEFUL_SHUTDOWN_SECONDS,
    HOST,
    OAUTH_LOGIN_TOKEN,
    OAUTH_SCOPES,
    OAUTH_TOKEN_TTL_SECONDS,
    PORT,
    PUBLIC_BASE_URL,
    STATE_DIR,
    WORKSPACE_ROOT,
    ensure_runtime_directories,
)
from .executors import ExecutorRegistry
from .http_compat import build_http_compat_app
from .oauth import OAuthRuntimeConfig
from .shell import JobRegistry
from .tool_context import ToolContext
from .tools_core import register_core_tools
from .tools_files import register_file_tools
from .tools_git_shell import register_git_shell_tools


# Bearer auth lives exclusively in the HTTP layer (http_compat.HTTPBearerAuthMiddleware)
# so unauthenticated clients can't even open an SSE session. The FastMCP
# protocol-layer middleware was redundant and has been removed.

registry = ExecutorRegistry(codex_command=CODEX_COMMAND)
job_registry = JobRegistry()

MCP_INSTRUCTIONS = (
    "Architecture: ChatGPT Web is the architect/manager/reviewer; this local MCP server exposes "
    "scoped local tools; delegate_task is only a single-task Codex executor. Use direct tools first "
    "for repo inspection, planning, patching, short commands, git checks, and verification. "
    "Use search/read_text for focused or batched discovery and reading, apply_patch/write_file for edits, "
    "env_snapshot/env_diff for read-only runtime diagnostics. Before edits or reviews, use "
    "code_map_symbols to find definitions, code_map_references to estimate impact, and "
    "code_map_imports to inspect module boundaries; use those results to narrow "
    "delegate_task files_in_scope when delegating. code_map_* is lightweight and not for "
    "precise rename, type inference, or call graph analysis. "
    "run_command for short single or batched shell work, job_start/job_status/job_tail/job_kill for "
    "generic background local jobs, and git_* only inside a git repository. "
    "Use delegate_task only for one bounded Codex Execution Prompt when direct tools are insufficient; "
    "it runs one serialized Codex delegate and blocks up to 300 seconds by default. If it returns "
    "status=running, call delegate_task again to continue waiting and use read_text on returned "
    "stdout/stderr/metadata log paths for live progress. Each delegate writes private "
    "audit logs under the system temporary cache directory and returns their paths in logs. Completed "
    "delegate responses do not inline raw stdout/stderr; use read_text on logs for output. Do not "
    "use delegate_task as a large opaque planning/research loop; split broad work into small "
    "verified execution prompts. "
    "Use delegate_status when the browser context is stateless and needs the active or recent "
    "server-generated delegate_id values; pass watch_seconds=300 for a five-minute status-change "
    "monitor. No taskboard or skill-discovery tools are exposed."
)

mcp = FastMCP(
    APP_NAME,
    instructions=MCP_INSTRUCTIONS,
)


def _current_auth_token() -> str:
    # Resolved via module globals so tests that monkeypatch ``AUTH_TOKEN`` on
    # this module (and runtime overrides) are honored per-request.
    return globals().get("AUTH_TOKEN", "") or ""


def _current_oauth_config() -> OAuthRuntimeConfig:
    return OAuthRuntimeConfig(
        auth_mode=globals().get("AUTH_MODE", "") or "",
        auth_token=_current_auth_token(),
        public_base_url=globals().get("PUBLIC_BASE_URL", "") or "",
        state_dir=globals().get("STATE_DIR", STATE_DIR),
        oauth_login_token=globals().get("OAUTH_LOGIN_TOKEN", "") or "",
        oauth_scopes=tuple(globals().get("OAUTH_SCOPES", ("local-ops",)) or ("local-ops",)),
        oauth_token_ttl_seconds=int(globals().get("OAUTH_TOKEN_TTL_SECONDS", 86400) or 86400),
    )


def _current_debug_mcp_logging() -> bool:
    return bool(globals().get("DEBUG_MCP_LOGGING", False))


def _global_value(name: str, default: Any = None) -> Any:
    return globals().get(name, default)


_tool_context = ToolContext(
    global_value=_global_value,
    current_oauth_config=_current_oauth_config,
)

_tool_exports: dict[str, object] = {}
_tool_exports.update(register_core_tools(mcp, _tool_context))
_tool_exports.update(register_file_tools(mcp, _tool_context))
_tool_exports.update(register_git_shell_tools(mcp, _tool_context))
globals().update(_tool_exports)


def build_http_app():
    streamable_app = mcp.http_app(
        path="/mcp",
        transport="streamable-http",
    )
    legacy_sse_app = mcp.http_app(
        path="/mcp",
        transport="sse",
    )
    return build_http_compat_app(
        streamable_app=streamable_app,
        legacy_sse_app=legacy_sse_app,
        app_name=APP_NAME,
        mcp_path="/mcp",
        get_auth_token=_current_auth_token,
        get_oauth_config=_current_oauth_config,
        get_debug_enabled=_current_debug_mcp_logging,
        instructions=MCP_INSTRUCTIONS,
    )


app = build_http_app()


class _ReadySignalServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, *, ready_fd: int | None) -> None:
        super().__init__(config)
        self._ready_fd = ready_fd

    def _emit_ready(self) -> None:
        if self._ready_fd is None:
            return
        os.write(self._ready_fd, b"ready\n")
        os.close(self._ready_fd)
        self._ready_fd = None

    def _close_ready_fd(self) -> None:
        if self._ready_fd is None:
            return
        os.close(self._ready_fd)
        self._ready_fd = None

    async def startup(self, sockets=None) -> None:
        await super().startup(sockets=sockets)
        if not self.should_exit:
            self._emit_ready()

    async def serve(self, sockets=None) -> None:
        try:
            await super().serve(sockets=sockets)
        finally:
            self._close_ready_fd()


def _consume_ready_fd() -> int | None:
    raw_value = os.environ.pop("CHATGPT_MCP_READY_FD", "").strip()
    if not raw_value:
        return None
    return int(raw_value)


def build_uvicorn_server(*, fd: int | None = None, ready_fd: int | None = None) -> uvicorn.Server:
    http_app = build_http_app()
    config = uvicorn.Config(
        http_app,
        host=HOST,
        port=PORT,
        fd=fd,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )
    return _ReadySignalServer(config, ready_fd=ready_fd)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the chatgpt-web-oauth-mcp MCP server.")
    parser.add_argument("--fd", type=int, default=None, help="Inherited listening socket fd.")
    args = parser.parse_args(argv)

    ensure_runtime_directories()
    print(f"Starting {APP_NAME} on {HOST}:{PORT}")
    print(f"workspace_root={WORKSPACE_ROOT}")
    print(f"state_dir={STATE_DIR}")
    print("transport=streamable-http")
    print("mcp_path=/mcp")
    print(f"debug_mcp_logging={DEBUG_MCP_LOGGING}")
    print(f"graceful_shutdown_seconds={GRACEFUL_SHUTDOWN_SECONDS}")

    oauth_config = _current_oauth_config()
    if oauth_config.normalized_auth_mode == "oauth":
        if not oauth_config.public_base_url:
            print(
                "WARNING: CHATGPT_MCP_PUBLIC_BASE_URL is not set; OAuth "
                "metadata will fall back to the request Host header. Set it to "
                "your public tunnel URL (e.g. https://mcp.example.com) so issuer "
                "URLs cannot be spoofed."
            )
        if not oauth_config.oauth_login_token and oauth_config.auth_token:
            print(
                "WARNING: CHATGPT_MCP_OAUTH_LOGIN_TOKEN is not set; "
                "AUTH_TOKEN is being reused as the OAuth login token. Anyone "
                "with AUTH_TOKEN can mint long-TTL OAuth access tokens. After "
                "rotating AUTH_TOKEN, also clear oauth.json[\"tokens\"] under "
                f"{STATE_DIR}/oauth.json."
            )

    server = build_uvicorn_server(fd=args.fd, ready_fd=_consume_ready_fd())
    server.run()


if __name__ == "__main__":
    main()
