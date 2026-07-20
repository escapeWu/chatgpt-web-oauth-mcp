from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


READ_ONLY_TOOL = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}

LOCAL_STATE_TOOL = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}

LOCAL_WRITE_TOOL = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": False,
    "openWorldHint": False,
}

OPEN_WORLD_WRITE_TOOL = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": False,
    "openWorldHint": True,
}


@dataclass(frozen=True)
class ToolContext:
    """Runtime lookups shared by tool registration modules.

    Values are resolved through ``global_value`` on every tool call so tests and
    local runtime overrides that patch ``server.py`` globals keep their existing
    behavior after tool implementations move out of ``server.py``.
    """

    global_value: Callable[[str, Any], Any]
    current_oauth_config: Callable[[], Any]

    def _get(self, name: str, default: Any = None) -> Any:
        return self.global_value(name, default)

    @property
    def app_name(self) -> str:
        return str(self._get("APP_NAME", ""))

    @property
    def host(self) -> str:
        return str(self._get("HOST", ""))

    @property
    def port(self) -> int:
        return int(self._get("PORT", 0))

    @property
    def workspace_root(self) -> Path:
        return self._get("WORKSPACE_ROOT")

    @property
    def state_dir(self) -> Path:
        return self._get("STATE_DIR")

    @property
    def command_timeout(self) -> int:
        return int(self._get("COMMAND_TIMEOUT", 120))

    @property
    def delegate_timeout(self) -> int:
        return int(self._get("DELEGATE_TIMEOUT", 300))

    @property
    def tool_output_token_budget(self) -> int:
        return int(self._get("TOOL_OUTPUT_TOKEN_BUDGET", 8500))

    @property
    def read_token_budget(self) -> int:
        return int(self._get("READ_TOKEN_BUDGET", self.tool_output_token_budget))

    @property
    def run_token_budget(self) -> int:
        return int(self._get("RUN_TOKEN_BUDGET", self.tool_output_token_budget))

    @property
    def job_output_token_budget(self) -> int:
        return int(self._get("JOB_OUTPUT_TOKEN_BUDGET", self.tool_output_token_budget))

    @property
    def run_capture_max_bytes(self) -> int:
        return int(self._get("RUN_CAPTURE_MAX_BYTES", 1024 * 1024))

    @property
    def ripgrep_binary(self) -> str:
        return str(self._get("RIPGREP_BINARY", "rg"))

    @property
    def debug_mcp_logging(self) -> bool:
        return bool(self._get("DEBUG_MCP_LOGGING", False))

    @property
    def codex_command(self) -> str | None:
        return self._get("CODEX_COMMAND")

    @property
    def tmux_binary(self) -> str:
        return str(self._get("TMUX_BINARY", "tmux"))

    @property
    def tmux_socket_name(self) -> str:
        return str(self._get("TMUX_SOCKET_NAME", "default"))

    @property
    def tmux_control_timeout(self) -> int:
        return int(self._get("TMUX_CONTROL_TIMEOUT", 10))

    @property
    def registry(self) -> Any:
        return self._get("registry")

    @property
    def job_registry(self) -> Any:
        return self._get("job_registry")
