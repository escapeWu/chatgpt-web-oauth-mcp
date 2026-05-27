from __future__ import annotations

import asyncio
import sys
from typing import Any


def _call(tool, *args, **kwargs):
    fn = tool.fn if hasattr(tool, "fn") else tool
    result = fn(*args, **kwargs)
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result


async def _tool_map(server) -> dict[str, Any]:
    list_tools = getattr(server.mcp, "_list_tools")
    try:
        tools = await list_tools()
    except TypeError:
        tools = await list_tools(None)
    return {tool.name: tool for tool in tools}


def _annotations(tool: Any) -> dict[str, object]:
    annotations = getattr(tool, "annotations", None)
    if hasattr(annotations, "model_dump"):
        return annotations.model_dump(mode="json")
    return dict(annotations or {})


def _drop_server_modules() -> None:
    for name in [
        "chatgpt_web_oauth_mcp.server",
        "chatgpt_web_oauth_mcp.config",
    ]:
        sys.modules.pop(name, None)
    parent = sys.modules.get("chatgpt_web_oauth_mcp")
    if parent is not None and hasattr(parent, "server"):
        delattr(parent, "server")


def _load_server_with_notebooklm_enabled(monkeypatch):
    monkeypatch.setenv("CHATGPT_MCP_ENABLE_NOTEBOOKLM", "1")
    _drop_server_modules()
    import chatgpt_web_oauth_mcp.server as server

    return server


def test_notebooklm_tools_are_not_registered_by_default() -> None:
    _drop_server_modules()
    from chatgpt_web_oauth_mcp import server

    result = _call(server.server_info)

    assert result["notebooklm"]["enabled"] is False
    assert not [name for name in result["tools"] if name.startswith("notebooklm_")]


def test_notebooklm_tools_register_with_annotations_when_enabled(monkeypatch) -> None:
    server = _load_server_with_notebooklm_enabled(monkeypatch)

    result = _call(server.server_info)
    tools = result["tools"]
    assert result["notebooklm"]["enabled"] is True
    for name in [
        "notebooklm_auth_check",
        "notebooklm_reauth",
        "notebooklm_notebook_list",
        "notebooklm_notebook_create",
        "notebooklm_source_add_text",
        "notebooklm_source_delete",
        "notebooklm_source_list",
        "notebooklm_ask",
    ]:
        assert name in tools

    registered = asyncio.run(_tool_map(server))
    assert _annotations(registered["notebooklm_auth_check"])["readOnlyHint"] is True
    assert _annotations(registered["notebooklm_reauth"])["openWorldHint"] is True
    assert _annotations(registered["notebooklm_notebook_list"])["readOnlyHint"] is True
    assert _annotations(registered["notebooklm_source_list"])["readOnlyHint"] is True
    assert _annotations(registered["notebooklm_notebook_create"])["openWorldHint"] is True
    assert _annotations(registered["notebooklm_source_add_text"])["openWorldHint"] is True
    assert _annotations(registered["notebooklm_source_delete"])["openWorldHint"] is True
    assert _annotations(registered["notebooklm_ask"])["openWorldHint"] is True

    assert "notebook_id" not in registered["notebooklm_source_add_text"].parameters["required"]
    assert registered["notebooklm_source_add_text"].parameters["required"] == ["title", "text"]
    assert "notebook_id" not in registered["notebooklm_source_delete"].parameters["required"]
    assert registered["notebooklm_source_delete"].parameters["required"] == ["source_id"]
    assert "notebook_id" not in registered["notebooklm_source_list"].parameters.get("required", [])
    assert "notebook_id" not in registered["notebooklm_ask"].parameters["required"]
    assert registered["notebooklm_ask"].parameters["required"] == ["question"]


def test_notebooklm_reauth_requires_confirmation_and_redacts_env(monkeypatch) -> None:
    monkeypatch.setenv("NOTEBOOKLM_LOGIN_BROWSER_PROFILE", "Profile 2")
    monkeypatch.setenv("NOTEBOOKLM_LOGIN_ACCOUNT", "person@example.test")
    monkeypatch.setenv("NOTEBOOKLM_LOGIN_PROFILE_NAME", "example-profile")
    server = _load_server_with_notebooklm_enabled(monkeypatch)

    denied = _call(server.notebooklm_reauth)
    assert denied["success"] is False
    assert denied["error"]["code"] == "confirmation_required"
    assert denied["reauth"] == {
        "command": "notebooklm",
        "browser": "chrome",
        "browser_profile": "Profile 2",
        "account": "p***@example.test",
        "profile_name": "example-profile",
    }

    dry_run = _call(server.notebooklm_reauth, dry_run=True)
    assert dry_run == {"success": True, "dry_run": True, "reauth": denied["reauth"]}


def test_server_wires_taskboard_telegram_notifier_when_configured(monkeypatch) -> None:
    monkeypatch.setenv("TG_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("TG_RECEIVER_ID", "5955543529")
    monkeypatch.setenv("TG_NOTIFY_TIMEOUT_SECONDS", "7")
    _drop_server_modules()
    import chatgpt_web_oauth_mcp.server as server

    assert server.taskboard_store.notifier is not None
    assert server.taskboard_store.notifier.__class__.__name__ == "TelegramTaskBoardNotifier"


def test_notebooklm_tools_call_client_wrapper(monkeypatch) -> None:
    server = _load_server_with_notebooklm_enabled(monkeypatch)

    class FakeNotebookLMClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

        async def auth_check(self) -> dict[str, object]:
            self.calls.append(("auth_check", (), {}))
            return {"authenticated": True, "notebook_count": 1}

        async def list_notebooks(self) -> list[dict[str, object]]:
            self.calls.append(("list_notebooks", (), {}))
            return [{"id": "nb1", "title": "Research", "created_at": "2026-05-24", "sources_count": 1}]

        async def create_notebook(self, title: str) -> dict[str, object]:
            self.calls.append(("create_notebook", (title,), {}))
            return {"id": "nb2", "title": title, "sources_count": 0}

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
            return {"id": "src1", "title": title, "status": "ready"}

        async def delete_source(self, notebook_id: str, source_id: str) -> bool:
            self.calls.append(("delete_source", (notebook_id, source_id), {}))
            return True

        async def list_sources(self, notebook_id: str) -> list[dict[str, object]]:
            self.calls.append(("list_sources", (notebook_id,), {}))
            return [{"id": "src1", "title": "Brief", "status": "ready"}]

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
            return {"answer": "Use the cited source.", "conversation_id": "conv1", "turn_number": 1}

    fake = FakeNotebookLMClient()
    monkeypatch.setattr(server, "create_notebooklm_client", lambda config: fake)

    assert _call(server.notebooklm_auth_check)["authenticated"] is True
    assert _call(server.notebooklm_notebook_list)["notebooks"] == [
        {"id": "nb1", "title": "Research", "created_at": "2026-05-24", "sources_count": 1}
    ]
    assert _call(server.notebooklm_notebook_create, "New notebook")["notebook"]["id"] == "nb2"
    assert _call(server.notebooklm_source_add_text, "Brief", "body", "nb1", True, 10)["source"]["id"] == "src1"
    assert _call(server.notebooklm_source_delete, "src1", "nb1", confirm=False)["success"] is False
    assert _call(server.notebooklm_source_delete, "src1", "nb1", confirm=True)["deleted"] is True
    assert _call(server.notebooklm_source_list, "nb1")["sources"] == [{"id": "src1", "title": "Brief", "status": "ready"}]
    assert _call(server.notebooklm_ask, "What matters?", "nb1", ["src1"], "conv0")["answer"]["answer"] == "Use the cited source."

    assert ("delete_source", ("nb1", "src1"), {}) in fake.calls
    assert ("ask", ("nb1", "What matters?"), {"source_ids": ["src1"], "conversation_id": "conv0"}) in fake.calls


def test_notebooklm_missing_package_error_is_serialized(monkeypatch) -> None:
    server = _load_server_with_notebooklm_enabled(monkeypatch)

    def broken_factory(config):
        raise server.NotebookLMConfigError("notebooklm-py is not installed")

    monkeypatch.setattr(server, "create_notebooklm_client", broken_factory)

    result = _call(server.notebooklm_auth_check)

    assert result["success"] is False
    assert result["error"]["code"] == "notebooklm_not_configured"
