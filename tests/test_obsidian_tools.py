from __future__ import annotations

import asyncio
from typing import Any


def _call(tool, *args, **kwargs):
    fn = tool.fn if hasattr(tool, "fn") else tool
    result = fn(*args, **kwargs)
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result


async def fake_call_native_tool(config, tool_name: str, arguments: dict[str, Any] | None = None):
    return {
        "success": True,
        "proxied_tool": tool_name,
        "url": config.mcp_url,
        "arguments": arguments or {},
        "result": {"content": [{"type": "text", "text": "ok"}], "isError": False},
    }


async def fake_list_native_tools(config):
    return {
        "success": True,
        "url": config.mcp_url,
        "tool_count": 2,
        "tools": [{"name": "vault_list"}, {"name": "search_simple"}],
    }


def test_native_obsidian_mcp_proxy_tools(monkeypatch) -> None:
    from chatgpt_web_oauth_mcp import server

    monkeypatch.setattr(server, "OBSIDIAN_API_KEY", "secret")
    monkeypatch.setattr(server, "OBSIDIAN_MCP_URL", "https://127.0.0.1:27124/mcp")
    monkeypatch.setattr(server, "obsidian_call_native_tool", fake_call_native_tool)
    monkeypatch.setattr(server, "obsidian_list_native_tools", fake_list_native_tools)

    assert _call(server.vault_list, "Projects")["proxied_tool"] == "vault_list"
    assert _call(server.vault_read, "Projects/a.md")["arguments"] == {"path": "Projects/a.md"}
    assert _call(server.vault_write, "Projects/a.md", "hello")["proxied_tool"] == "vault_write"
    assert _call(server.vault_append, "Projects/a.md", "hello")["proxied_tool"] == "vault_append"
    assert _call(server.vault_patch, "Projects/a.md", "heading", "Todo", "append", "x")["arguments"]["operation"] == "append"
    assert _call(server.vault_delete, "Projects/a.md", confirm=False)["success"] is False
    assert _call(server.vault_delete, "Projects/a.md", confirm=True)["proxied_tool"] == "vault_delete"
    assert _call(server.vault_get_document_map, "Projects/a.md")["proxied_tool"] == "vault_get_document_map"
    assert _call(server.active_file_get_path)["proxied_tool"] == "active_file_get_path"
    assert _call(server.periodic_note_get_path, "daily")["arguments"] == {"period": "daily"}
    assert _call(server.search_query, {"glob": ["*.md", {"var": "path"}]})["proxied_tool"] == "search_query"
    assert _call(server.search_simple, "hello", 50)["arguments"] == {"query": "hello", "contextLength": 50}
    assert _call(server.tag_list)["proxied_tool"] == "tag_list"
    assert _call(server.command_list)["proxied_tool"] == "command_list"
    assert _call(server.command_execute, "editor:toggle-bold")["arguments"] == {"commandId": "editor:toggle-bold"}
    assert _call(server.open_file, "Projects/a.md", True)["arguments"] == {"path": "Projects/a.md", "newLeaf": True}
    assert _call(server.vault_mcp_list_tools)["tool_count"] == 2


def test_server_info_lists_native_obsidian_proxy_tools(monkeypatch) -> None:
    from chatgpt_web_oauth_mcp import server

    monkeypatch.setattr(server, "OBSIDIAN_API_KEY", "secret")
    monkeypatch.setattr(server, "OBSIDIAN_MCP_URL", "https://127.0.0.1:27124/mcp")
    result = _call(server.server_info)

    assert result["obsidian_proxy"]["configured"] is True
    assert result["obsidian_proxy"]["mode"] == "native_mcp_proxy"
    assert result["obsidian_proxy"]["mcp_url"] == "https://127.0.0.1:27124/mcp"
    assert "vault_list" in result["tools"]
    assert "search_simple" in result["tools"]
    assert "obsidian_simple_search" not in result["tools"]
    assert "obsidian_list_files_in_vault" not in result["tools"]


def test_missing_obsidian_key_returns_proxy_error(monkeypatch) -> None:
    from chatgpt_web_oauth_mcp import server

    monkeypatch.setattr(server, "OBSIDIAN_API_KEY", "")
    result = _call(server.vault_list)

    assert result["success"] is False
    assert result["error"]["code"] == "obsidian_mcp_not_configured"
