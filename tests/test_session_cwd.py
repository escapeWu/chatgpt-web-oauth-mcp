from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from chatgpt_web_oauth_mcp import session
from chatgpt_web_oauth_mcp.pathing import resolve_cwd
from chatgpt_web_oauth_mcp.server import (
    get_default_cwd,
    server_info,
    set_default_cwd,
)


def _call(tool_or_fn, *args, **kwargs):
    fn = tool_or_fn.fn if hasattr(tool_or_fn, "fn") else tool_or_fn
    result = fn(*args, **kwargs)
    if asyncio.iscoroutine(result):
        result = asyncio.run(result)
    return result


@pytest.fixture(autouse=True)
def _reset_session_cwd():
    session.set_default_cwd(None)
    yield
    session.set_default_cwd(None)


def test_resolve_cwd_falls_back_to_workspace_root_when_unset(tmp_path: Path) -> None:
    assert resolve_cwd(None, tmp_path) == tmp_path


def test_resolve_cwd_prefers_session_cwd_over_workspace_root(tmp_path: Path) -> None:
    other = tmp_path / "sub"
    other.mkdir()
    session.set_default_cwd(other)
    assert resolve_cwd(None, tmp_path) == other


def test_resolve_cwd_explicit_arg_still_wins(tmp_path: Path) -> None:
    other = tmp_path / "sub"
    other.mkdir()
    session.set_default_cwd(other)
    explicit = tmp_path / "explicit"
    explicit.mkdir()
    assert resolve_cwd(str(explicit), tmp_path) == explicit


def test_set_default_cwd_validates_missing_path(tmp_path: Path) -> None:
    result = _call(set_default_cwd, path=str(tmp_path / "does-not-exist"))
    assert result["success"] is False
    assert result["error"]["code"] == "cwd_not_found"
    assert session.get_default_cwd() is None


def test_set_default_cwd_rejects_file(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("x")
    result = _call(set_default_cwd, path=str(f))
    assert result["success"] is False
    assert result["error"]["code"] == "cwd_not_directory"


def test_set_default_cwd_sets_and_clears(tmp_path: Path) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    result = _call(set_default_cwd, path=str(sub))
    assert result["success"] is True
    assert result["session_cwd"] == str(sub)
    assert result["cleared"] is False
    assert session.get_default_cwd() == sub

    cleared = _call(set_default_cwd, path=None)
    assert cleared["success"] is True
    assert cleared["session_cwd"] is None
    assert cleared["cleared"] is True
    assert session.get_default_cwd() is None


def test_get_default_cwd_reports_source(tmp_path: Path) -> None:
    result = _call(get_default_cwd)
    assert result["session_cwd"] is None
    assert result["source"] == "workspace_root"

    sub = tmp_path / "sub"
    sub.mkdir()
    session.set_default_cwd(sub)
    result = _call(get_default_cwd)
    assert result["session_cwd"] == str(sub)
    assert result["effective_cwd"] == str(sub)
    assert result["source"] == "session"


def test_server_info_surfaces_session_cwd(tmp_path: Path) -> None:
    before = _call(server_info)
    assert before["session_cwd"] is None
    sub = tmp_path / "sub"
    sub.mkdir()
    session.set_default_cwd(sub)
    after = _call(server_info)
    assert after["session_cwd"] == str(sub)
    assert "set_default_cwd" in after["tools"]
    assert "get_default_cwd" in after["tools"]
