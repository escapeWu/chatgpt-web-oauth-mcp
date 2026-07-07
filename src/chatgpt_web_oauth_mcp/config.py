"""Process-wide configuration loaded from environment variables.

Semantic note on ``WORKSPACE_ROOT`` / ``DEFAULT_CWD``
-----------------------------------------------------
Despite the name "root", this value is **not a sandbox boundary**. The project
is designed to give an MCP client (ChatGPT Web / Codex) arbitrary
local-shell capability; once a client passes the bearer token it has full shell
and full-filesystem access.

``WORKSPACE_ROOT`` is only used for two things:

1. **Relative-path anchor.** :func:`chatgpt_web_oauth_mcp.pathing.resolve_path`
   joins relative inputs onto it; absolute paths are returned as-is.
2. **Default ``cwd``.** :func:`chatgpt_web_oauth_mcp.pathing.resolve_cwd`
   falls back to it when neither the tool call nor the session-level override
   (``set_default_cwd``) provides a directory.

It therefore behaves like a *default working directory*, not a root. The
``DEFAULT_CWD`` alias below reflects that; ``WORKSPACE_ROOT`` is kept for
API compatibility. The environment variable name ``CHATGPT_MCP_WORKSPACE_ROOT``
is the canonical setting for this project.
"""

from __future__ import annotations

import os
from pathlib import Path


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


APP_NAME = "chatgpt-web-oauth-mcp"
HOST = os.environ.get("CHATGPT_MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("CHATGPT_MCP_PORT", "8766"))

# Default cwd for tool calls (see module docstring). Kept as WORKSPACE_ROOT
# for API compatibility; DEFAULT_CWD is the preferred name going forward.
WORKSPACE_ROOT = Path(
    os.environ.get("CHATGPT_MCP_WORKSPACE_ROOT", str(Path.home()))
).expanduser().resolve()
DEFAULT_CWD = WORKSPACE_ROOT

STATE_DIR = Path(
    os.environ.get("CHATGPT_MCP_STATE_DIR", str(Path.home() / ".chatgpt-web-oauth-mcp"))
).expanduser().resolve()
AUTH_TOKEN = os.environ.get("CHATGPT_MCP_AUTH_TOKEN", "").strip()
AUTH_MODE = os.environ.get("CHATGPT_MCP_AUTH_MODE", "").strip().lower()
PUBLIC_BASE_URL = os.environ.get("CHATGPT_MCP_PUBLIC_BASE_URL", "").strip().rstrip("/")
OAUTH_LOGIN_TOKEN = os.environ.get("CHATGPT_MCP_OAUTH_LOGIN_TOKEN", "").strip()
OAUTH_SCOPES = tuple(
    scope
    for scope in os.environ.get("CHATGPT_MCP_OAUTH_SCOPES", "local-ops").split()
    if scope
)
OAUTH_TOKEN_TTL_SECONDS = int(os.environ.get("CHATGPT_MCP_OAUTH_TOKEN_TTL_SECONDS", "86400"))
CODEX_COMMAND = os.environ.get("CHATGPT_MCP_CODEX_COMMAND", "codex").strip()
COMMAND_TIMEOUT = int(os.environ.get("CHATGPT_MCP_COMMAND_TIMEOUT", "120"))
DELEGATE_TIMEOUT = int(os.environ.get("CHATGPT_MCP_DELEGATE_TIMEOUT", "300"))
DEBUG_MCP_LOGGING = _env_flag("CHATGPT_MCP_DEBUG_MCP_LOGGING", default=False)
GRACEFUL_SHUTDOWN_SECONDS = int(
    os.environ.get("CHATGPT_MCP_GRACEFUL_SHUTDOWN_SECONDS", "30")
)

ENABLE_OBSIDIAN = _env_flag("CHATGPT_MCP_ENABLE_OBSIDIAN", default=False)
OBSIDIAN_API_KEY = os.environ.get("OBSIDIAN_API_KEY", "").strip()
OBSIDIAN_HOST = os.environ.get("OBSIDIAN_HOST", "127.0.0.1").strip() or "127.0.0.1"
OBSIDIAN_PORT = int(os.environ.get("OBSIDIAN_PORT", "27124"))
OBSIDIAN_PROTOCOL = os.environ.get("OBSIDIAN_PROTOCOL", "https").strip().lower() or "https"
OBSIDIAN_MCP_URL = os.environ.get("OBSIDIAN_MCP_URL", "").strip()
OBSIDIAN_VERIFY_SSL = _env_flag("OBSIDIAN_VERIFY_SSL", default=False)
OBSIDIAN_TIMEOUT_SECONDS = int(os.environ.get("OBSIDIAN_TIMEOUT_SECONDS", "10"))


def ensure_runtime_directories() -> None:
    if not WORKSPACE_ROOT.exists():
        raise FileNotFoundError(f"Default cwd does not exist: {WORKSPACE_ROOT}")
    if not WORKSPACE_ROOT.is_dir():
        raise NotADirectoryError(f"Default cwd is not a directory: {WORKSPACE_ROOT}")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        STATE_DIR.chmod(0o700)
    except OSError:
        pass
