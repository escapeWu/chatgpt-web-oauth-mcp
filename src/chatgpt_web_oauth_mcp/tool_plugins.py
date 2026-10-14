from __future__ import annotations

import logging
from importlib.metadata import entry_points
from typing import Any

logger = logging.getLogger(__name__)

TOOL_PLUGIN_GROUP = "chatgpt_web_oauth_mcp.tools"


def load_tool_plugins(mcp: Any) -> dict[str, object]:
    """Register tools from installed packages that declare the ``chatgpt_web_oauth_mcp.tools`` entry-point group.

    Each entry point resolves to ``register(mcp) -> dict[str, object] | None``; the returned mapping is merged into the
    server's tool exports. A plugin that fails to import or register is logged and skipped, so one broken package cannot
    take the server down.
    """
    exports: dict[str, object] = {}
    for entry in entry_points(group=TOOL_PLUGIN_GROUP):
        try:
            result = entry.load()(mcp)
        except Exception:  # noqa: BLE001 - isolate third-party plugin failures
            logger.exception("tool plugin %r failed to load and was skipped", entry.name)
            continue
        if isinstance(result, dict):
            exports.update(result)
        logger.info("tool plugin %r loaded", entry.name)
    return exports
