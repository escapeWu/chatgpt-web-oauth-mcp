"""Process-wide session state for the local ops MCP bridge.

Today there is a single mutable “default working directory” shared by every
client of this server process. That is deliberately simple: the project runs
as a single-user local bridge, so per-connection sessions are overkill.

The default cwd is used by :func:`chatgpt_web_oauth_mcp.pathing.resolve_cwd`
as the fallback whenever a tool call omits ``cwd``. Resolution order is:

1. explicit ``cwd`` argument on the tool call
2. the session default (if one has been set via ``set_default_cwd``)
3. ``WORKSPACE_ROOT`` from config
"""

from __future__ import annotations

import threading
from pathlib import Path

_lock = threading.RLock()
_default_cwd: Path | None = None


def get_default_cwd() -> Path | None:
    """Return the current session-wide default cwd, or ``None`` if unset."""
    with _lock:
        return _default_cwd


def set_default_cwd(cwd: Path | None) -> Path | None:
    """Set (or clear with ``None``) the session-wide default cwd.

    Returns the newly-active value (``None`` when cleared). Validation of the
    path (must exist and be a directory) is left to the caller so this module
    has no filesystem side effects.
    """
    global _default_cwd
    with _lock:
        _default_cwd = cwd
        return _default_cwd
