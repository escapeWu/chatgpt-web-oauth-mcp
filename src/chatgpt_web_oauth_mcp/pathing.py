from __future__ import annotations

from pathlib import Path

from . import session


def resolve_path(path: str, workspace_root: Path) -> Path:
    raw = Path(path).expanduser()
    if raw.is_absolute():
        return raw.resolve(strict=False)
    return (workspace_root / raw).resolve(strict=False)


def resolve_cwd(cwd: str | None, workspace_root: Path) -> Path:
    """Resolve a tool ``cwd`` argument.

    Fallback order when ``cwd`` is not provided:
    1. session.get_default_cwd() (set via the set_default_cwd tool)
    2. workspace_root
    """
    if cwd:
        return resolve_path(cwd, workspace_root)
    session_cwd = session.get_default_cwd()
    if session_cwd is not None:
        return session_cwd
    return workspace_root
