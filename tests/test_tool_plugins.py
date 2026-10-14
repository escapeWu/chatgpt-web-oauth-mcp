from __future__ import annotations

import logging

import pytest

from chatgpt_web_oauth_mcp import tool_plugins


class _Entry:
    def __init__(self, name: str, loader):
        self.name = name
        self._loader = loader

    def load(self):
        return self._loader()


def _patch(monkeypatch: pytest.MonkeyPatch, entries: list[_Entry]) -> list[str]:
    groups: list[str] = []

    def fake_entry_points(*, group: str):
        groups.append(group)
        return entries

    monkeypatch.setattr(tool_plugins, "entry_points", fake_entry_points)
    return groups


def test_plugin_register_is_called_with_mcp_and_exports_are_merged(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[object] = []

    def register(mcp):
        seen.append(mcp)
        return {"my_tool": object()}

    groups = _patch(monkeypatch, [_Entry("one", lambda: register)])
    sentinel = object()

    exports = tool_plugins.load_tool_plugins(sentinel)

    assert groups == ["chatgpt_web_oauth_mcp.tools"]
    assert seen == [sentinel]
    assert set(exports) == {"my_tool"}


def test_plugin_may_return_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, [_Entry("quiet", lambda: (lambda mcp: None))])
    assert tool_plugins.load_tool_plugins(object()) == {}


def test_broken_plugins_are_skipped_and_logged(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    def explode_on_import():
        raise ImportError("boom")

    def explode_on_register(mcp):
        raise RuntimeError("bad register")

    _patch(
        monkeypatch,
        [
            _Entry("bad-import", explode_on_import),
            _Entry("bad-register", lambda: explode_on_register),
            _Entry("good", lambda: (lambda mcp: {"ok_tool": object()})),
        ],
    )

    with caplog.at_level(logging.ERROR):
        exports = tool_plugins.load_tool_plugins(object())

    assert set(exports) == {"ok_tool"}
    assert "bad-import" in caplog.text and "bad-register" in caplog.text


def test_no_plugins_installed_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, [])
    assert tool_plugins.load_tool_plugins(object()) == {}
