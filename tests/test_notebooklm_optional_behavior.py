from __future__ import annotations

import asyncio
import importlib
import importlib.util
import sys

import pytest


def _call(tool, *args, **kwargs):
    fn = tool.fn if hasattr(tool, "fn") else tool
    result = fn(*args, **kwargs)
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result


def _require_notebooklm_modules() -> None:
    if importlib.util.find_spec("chatgpt_web_oauth_mcp.notebooklm") is None:
        pytest.skip("NotebookLM implementation module is not present in this isolated test worktree")


def _drop_runtime_modules() -> None:
    for name in [
        "chatgpt_web_oauth_mcp.server",
        "chatgpt_web_oauth_mcp.config",
        "chatgpt_web_oauth_mcp.tools_core",
        "chatgpt_web_oauth_mcp.tools_notebooklm",
    ]:
        sys.modules.pop(name, None)
    parent = sys.modules.get("chatgpt_web_oauth_mcp")
    if parent is not None and hasattr(parent, "server"):
        delattr(parent, "server")


def _load_server(monkeypatch: pytest.MonkeyPatch, *, enabled: bool):
    _require_notebooklm_modules()
    monkeypatch.setenv("CHATGPT_MCP_ENABLE_NOTEBOOKLM", "1" if enabled else "0")
    _drop_runtime_modules()
    import chatgpt_web_oauth_mcp.server as server

    return server


def test_notebooklm_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _require_notebooklm_modules()
    monkeypatch.delenv("CHATGPT_MCP_ENABLE_NOTEBOOKLM", raising=False)
    monkeypatch.delenv("NOTEBOOKLM_STORAGE_PATH", raising=False)
    monkeypatch.delenv("NOTEBOOKLM_PROFILE", raising=False)
    monkeypatch.delenv("NOTEBOOKLM_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("CHATGPT_MCP_NOTEBOOKLM_DEFAULT_NOTEBOOK_ID", raising=False)
    monkeypatch.delenv("NOTEBOOKLM_NOTEBOOK", raising=False)

    sys.modules.pop("chatgpt_web_oauth_mcp.config", None)
    import chatgpt_web_oauth_mcp.config as config

    assert config.ENABLE_NOTEBOOKLM is False
    assert config.NOTEBOOKLM_STORAGE_PATH == ""
    assert config.NOTEBOOKLM_PROFILE == ""
    assert config.NOTEBOOKLM_TIMEOUT_SECONDS == 30
    if hasattr(config, "NOTEBOOKLM_DEFAULT_NOTEBOOK_ID"):
        assert config.NOTEBOOKLM_DEFAULT_NOTEBOOK_ID == ""


def test_notebooklm_disabled_does_not_register_or_import_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    imported: list[str] = []
    real_import_module = importlib.import_module

    def tracked_import(name: str, package: str | None = None):
        if name == "notebooklm":
            imported.append(name)
            raise AssertionError("NotebookLM SDK import should be lazy while tools are disabled")
        return real_import_module(name, package)

    monkeypatch.setattr(importlib, "import_module", tracked_import)
    server = _load_server(monkeypatch, enabled=False)

    result = _call(server.server_info)

    assert result["notebooklm"]["enabled"] is False
    assert not [name for name in result["tools"] if name.startswith("notebooklm_")]
    assert imported == []


def test_notebooklm_enabled_registers_without_obsidian_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _load_server(monkeypatch, enabled=True)

    result = _call(server.server_info)

    assert result["notebooklm"]["enabled"] is True
    assert "notebooklm_auth_check" in result["tools"]
    assert "notebooklm_notebook_list" in result["tools"]
    assert "notebooklm_notebook_create" in result["tools"]
    assert "notebooklm_source_add_text" in result["tools"]
    assert "notebooklm_source_delete" in result["tools"]
    assert "notebooklm_source_list" in result["tools"]
    assert "notebooklm_ask" in result["tools"]
    assert result["obsidian_proxy"]["enabled"] is False
    assert not [name for name in result["tools"] if name.startswith("obsidian_")]


def test_notebooklm_missing_dependency_error_is_structured(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _load_server(monkeypatch, enabled=True)

    def broken_factory(config):
        raise server.NotebookLMConfigError("notebooklm-py is not installed")

    monkeypatch.setattr(server, "create_notebooklm_client", broken_factory)

    result = _call(server.notebooklm_auth_check)

    assert result["success"] is False
    assert result["error"]["code"] == "notebooklm_not_configured"
    assert "notebooklm-py" in result["error"]["message"]


def test_notebooklm_low_level_tools_use_mocked_client_and_compact_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _load_server(monkeypatch, enabled=True)

    class FakeNotebookLMClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

        async def auth_check(self) -> dict[str, object]:
            self.calls.append(("auth_check", (), {}))
            return {"authenticated": True, "notebook_count": 2}

        async def list_notebooks(self) -> list[dict[str, object]]:
            self.calls.append(("list_notebooks", (), {}))
            return [
                {
                    "id": "nb-1",
                    "title": "Research",
                    "created_at": "2026-05-24",
                    "sources_count": 3,
                    "private_url": "https://example.invalid/notebook",
                }
            ]

        async def create_notebook(self, title: str) -> dict[str, object]:
            self.calls.append(("create_notebook", (title,), {}))
            return {"id": "nb-2", "title": title, "sources_count": 0, "owner_email": "hidden@example.invalid"}

        async def add_text_source(
            self,
            notebook_id: str,
            title: str,
            text: str,
            *,
            wait: bool = False,
            wait_timeout: float | None = None,
        ) -> dict[str, object]:
            self.calls.append(
                (
                    "add_text_source",
                    (notebook_id, title, text),
                    {"wait": wait, "wait_timeout": wait_timeout},
                )
            )
            return {"id": "src-1", "title": title, "status": "ready", "raw_text": text}

        async def delete_source(self, notebook_id: str, source_id: str) -> bool:
            self.calls.append(("delete_source", (notebook_id, source_id), {}))
            return True

        async def list_sources(self, notebook_id: str) -> list[dict[str, object]]:
            self.calls.append(("list_sources", (notebook_id,), {}))
            return [{"id": "src-1", "title": "Brief", "status": "ready", "raw_text": "hidden"}]

        async def ask(
            self,
            notebook_id: str,
            question: str,
            *,
            source_ids: list[str] | None = None,
            conversation_id: str | None = None,
        ) -> dict[str, object]:
            self.calls.append(
                (
                    "ask",
                    (notebook_id, question),
                    {"source_ids": source_ids, "conversation_id": conversation_id},
                )
            )
            return {
                "answer": "Use the cited source.",
                "conversation_id": "conv-1",
                "turn_number": 4,
                "debug_trace": "hidden",
            }

    fake = FakeNotebookLMClient()
    monkeypatch.setattr(server, "create_notebooklm_client", lambda config: fake)

    assert _call(server.notebooklm_auth_check) == {
        "success": True,
        "authenticated": True,
        "notebook_count": 2,
    }

    listed = _call(server.notebooklm_notebook_list)
    assert listed["notebooks"] == [
        {"id": "nb-1", "title": "Research", "created_at": "2026-05-24", "sources_count": 3}
    ]
    assert "private_url" not in listed["notebooks"][0]

    created = _call(server.notebooklm_notebook_create, "New notebook")
    assert created["notebook"] == {"id": "nb-2", "title": "New notebook", "sources_count": 0}

    source = _call(server.notebooklm_source_add_text, "Brief", "body", "nb-1", True, 12.5)
    assert source["source"] == {"id": "src-1", "title": "Brief", "status": "ready"}

    denied_delete = _call(server.notebooklm_source_delete, "src-1")
    assert denied_delete["error"]["code"] == "confirmation_required"

    confirmed_delete = _call(server.notebooklm_source_delete, "src-1", "nb-1", confirm=True)
    assert confirmed_delete == {"success": True, "deleted": True, "notebook_id": "nb-1", "source_id": "src-1"}

    sources = _call(server.notebooklm_source_list, "nb-1")
    assert sources["sources"] == [{"id": "src-1", "title": "Brief", "status": "ready"}]

    answer = _call(server.notebooklm_ask, "What matters?", "nb-1", ["src-1"], "conv-0")
    assert answer["answer"] == {"answer": "Use the cited source.", "conversation_id": "conv-1", "turn_number": 4}

    assert ("add_text_source", ("nb-1", "Brief", "body"), {"wait": True, "wait_timeout": 12.5}) in fake.calls
    assert ("delete_source", ("nb-1", "src-1"), {}) in fake.calls
    assert ("ask", ("nb-1", "What matters?"), {"source_ids": ["src-1"], "conversation_id": "conv-0"}) in fake.calls
