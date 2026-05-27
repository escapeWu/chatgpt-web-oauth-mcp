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
    CLAUDE_COMMAND,
    CODEX_COMMAND,
    COMMAND_TIMEOUT,
    DEBUG_MCP_LOGGING,
    DELEGATE_TIMEOUT,
    ENABLE_OBSIDIAN,
    ENABLE_NOTEBOOKLM,
    GRACEFUL_SHUTDOWN_SECONDS,
    HOST,
    NOTEBOOKLM_DEFAULT_NOTEBOOK_ID,
    NOTEBOOKLM_PROFILE,
    NOTEBOOKLM_STORAGE_PATH,
    NOTEBOOKLM_TIMEOUT_SECONDS,
    OAUTH_LOGIN_TOKEN,
    OAUTH_SCOPES,
    OAUTH_TOKEN_TTL_SECONDS,
    OBSIDIAN_API_KEY,
    OBSIDIAN_HOST,
    OBSIDIAN_MCP_URL,
    OBSIDIAN_PORT,
    OBSIDIAN_PROTOCOL,
    OBSIDIAN_TIMEOUT_SECONDS,
    OBSIDIAN_VERIFY_SSL,
    PORT,
    PUBLIC_BASE_URL,
    STATE_DIR,
    TG_BOT_TOKEN,
    TG_NOTIFY_TIMEOUT_SECONDS,
    TG_RECEIVER_ID,
    WORKSPACE_ROOT,
    ensure_runtime_directories,
)
from .executors import ExecutorRegistry
from .http_compat import build_http_compat_app
from .notebooklm import (
    NotebookLMConfig,
    NotebookLMConfigError,
    create_client as create_notebooklm_client,
    proxy_error as notebooklm_proxy_error,
)
from .notifiers import build_telegram_notifier
from .oauth import OAuthRuntimeConfig
from .obsidian import (
    ObsidianMCPConfig,
    call_native_tool as obsidian_call_native_tool,
    list_native_tools as obsidian_list_native_tools,
    proxy_error as obsidian_proxy_error,
)
from .skills import list_skills as list_skills_impl
from .taskboard import TaskBoardError, TaskBoardStore
from .tasks import TaskStore
from .tool_context import ToolContext
from .tools_core import register_core_tools
from .tools_files import register_file_tools
from .tools_git_shell import register_git_shell_tools
from .tools_notebooklm import register_notebooklm_tools
from .tools_obsidian import register_obsidian_tools
from .tools_taskboard import register_taskboard_tools


# Bearer auth lives exclusively in the HTTP layer (http_compat.HTTPBearerAuthMiddleware)
# so unauthenticated clients can't even open an SSE session. The FastMCP
# protocol-layer middleware was redundant and has been removed.

store = TaskStore(STATE_DIR)
taskboard_store = TaskBoardStore(
    STATE_DIR,
    notifier=build_telegram_notifier(
        bot_token=TG_BOT_TOKEN,
        receiver_id=TG_RECEIVER_ID,
        timeout_seconds=TG_NOTIFY_TIMEOUT_SECONDS,
    ),
)
registry = ExecutorRegistry(
    store=store,
    codex_command=CODEX_COMMAND,
    claude_command=CLAUDE_COMMAND,
)

MCP_INSTRUCTIONS = (
    "Use direct tools first for normal tasks. Prioritize apply_patch/write_file for edits and "
    "run_command_stream/wait_task for long-running shell work. "
    "Use search/read_text for focused repo discovery and reading, not as a substitute for every shell step. "
    "Use git_* only when the current cwd is actually inside a git repository. "
    "Use delegate_task only when direct tools are insufficient for a complex, long-running, or multi-file task. "
    "Use taskboard_* for board-level tracking of user-decomposed subtasks; MCP does not decompose tasks automatically."
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


def _current_obsidian_config() -> ObsidianMCPConfig:
    return ObsidianMCPConfig(
        api_key=globals().get("OBSIDIAN_API_KEY", "") or "",
        host=globals().get("OBSIDIAN_HOST", "127.0.0.1") or "127.0.0.1",
        port=int(globals().get("OBSIDIAN_PORT", 27124) or 27124),
        protocol=globals().get("OBSIDIAN_PROTOCOL", "https") or "https",
        url=globals().get("OBSIDIAN_MCP_URL", "") or "",
        verify_ssl=bool(globals().get("OBSIDIAN_VERIFY_SSL", False)),
        timeout_seconds=int(globals().get("OBSIDIAN_TIMEOUT_SECONDS", 10) or 10),
    )


def _current_notebooklm_config() -> NotebookLMConfig:
    return NotebookLMConfig(
        enabled=bool(globals().get("ENABLE_NOTEBOOKLM", False)),
        storage_path=globals().get("NOTEBOOKLM_STORAGE_PATH", "") or "",
        profile=globals().get("NOTEBOOKLM_PROFILE", "") or "",
        default_notebook_id=globals().get("NOTEBOOKLM_DEFAULT_NOTEBOOK_ID", "") or "",
        timeout_seconds=int(globals().get("NOTEBOOKLM_TIMEOUT_SECONDS", 30) or 30),
    )


def _global_value(name: str, default: Any = None) -> Any:
    return globals().get(name, default)


_tool_context = ToolContext(
    global_value=_global_value,
    current_oauth_config=_current_oauth_config,
    current_obsidian_config=_current_obsidian_config,
    current_notebooklm_config=_current_notebooklm_config,
)

_tool_exports: dict[str, object] = {}
_tool_exports.update(register_core_tools(mcp, _tool_context))
_tool_exports.update(register_file_tools(mcp, _tool_context))
_tool_exports.update(register_git_shell_tools(mcp, _tool_context))
_tool_exports.update(register_taskboard_tools(mcp, _tool_context))
_tool_exports.update(register_obsidian_tools(mcp, _tool_context))
_tool_exports.update(register_notebooklm_tools(mcp, _tool_context))
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
