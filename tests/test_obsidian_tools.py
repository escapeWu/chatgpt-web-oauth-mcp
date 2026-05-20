from __future__ import annotations

import asyncio


def _call(tool, *args, **kwargs):
    fn = tool.fn if hasattr(tool, "fn") else tool
    result = fn(*args, **kwargs)
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result


class FakeObsidianClient:
    def status(self):
        return {"status_code": 200, "body": {"ok": True}}

    def list_files_in_vault(self):
        return ["Daily", "README.md"]

    def list_files_in_dir(self, dirpath: str):
        return [f"{dirpath}/note.md"]

    def get_file_contents(self, filepath: str):
        return f"# {filepath}\ncontent"

    def batch_get_file_contents(self, filepaths: list[str]):
        return [{"filepath": item, "success": True, "content": item} for item in filepaths]

    def simple_search(self, query: str, context_length: int = 100):
        return [{"filename": "note.md", "matches": [{"context": query}], "context_length": context_length}]

    def complex_search(self, query: dict[str, object]):
        return [{"filename": "tagged.md", "query": query}]

    def search_by_tag(self, tag: str, dirpath: str | None = None):
        return [f"{dirpath or 'root'}/{tag}.md"]

    def get_frontmatter(self, filepath: str):
        return {"filepath": filepath, "tags": ["test"]}

    def append_content(self, filepath: str, content: str):
        return None

    def patch_content(self, filepath: str, operation: str, target_type: str, target: str, content: str):
        return None

    def put_content(self, filepath: str, content: str):
        return None

    def delete_file(self, filepath: str):
        return None

    def get_periodic_note(self, period: str, note_type: str = "content"):
        return f"{period}:{note_type}"

    def get_recent_periodic_notes(self, period: str, limit: int = 5, include_content: bool = False):
        return [{"period": period, "limit": limit, "include_content": include_content}]

    def get_recent_changes(self, limit: int = 10, days: int = 90):
        return [{"limit": limit, "days": days}]


def test_obsidian_tool_group_uses_client(monkeypatch) -> None:
    from chatgpt_web_oauth_mcp import server

    monkeypatch.setattr(server, "OBSIDIAN_API_KEY", "secret")
    monkeypatch.setattr(server, "_obsidian_client", lambda: FakeObsidianClient())

    assert _call(server.obsidian_status)["success"] is True
    assert _call(server.obsidian_list_files_in_vault)["data"] == ["Daily", "README.md"]
    assert _call(server.obsidian_list_files_in_dir, "Daily")["data"] == ["Daily/note.md"]
    assert "content" in _call(server.obsidian_get_file_contents, "README.md")["data"]
    assert _call(server.obsidian_batch_get_file_contents, ["a.md", "b.md"])["data"][1]["filepath"] == "b.md"
    assert _call(server.obsidian_simple_search, "hello", 50)["data"][0]["context_length"] == 50
    assert _call(server.obsidian_complex_search, {"glob": ["*.md", {"var": "path"}]})["data"][0]["filename"] == "tagged.md"
    assert _call(server.obsidian_search_by_tag, "project", "Work")["data"] == ["Work/project.md"]
    assert _call(server.obsidian_get_frontmatter, "README.md")["data"]["tags"] == ["test"]
    assert _call(server.obsidian_append_content, "README.md", "x")["success"] is True
    assert _call(server.obsidian_patch_content, "README.md", "append", "heading", "Todo", "x")["success"] is True
    assert _call(server.obsidian_put_content, "README.md", "x")["success"] is True
    assert _call(server.obsidian_delete_file, "README.md", confirm=False)["success"] is False
    assert _call(server.obsidian_delete_file, "README.md", confirm=True)["success"] is True
    assert _call(server.obsidian_get_periodic_note, "daily")["data"] == "daily:content"
    assert _call(server.obsidian_get_recent_periodic_notes, "weekly", 2, True)["data"][0]["limit"] == 2
    assert _call(server.obsidian_get_recent_changes, 3, 7)["data"][0] == {"limit": 3, "days": 7}


def test_obsidian_tools_report_missing_api_key(monkeypatch) -> None:
    from chatgpt_web_oauth_mcp import server

    monkeypatch.setattr(server, "OBSIDIAN_API_KEY", "")
    result = _call(server.obsidian_status)

    assert result["success"] is False
    assert result["error"]["code"] == "obsidian_not_configured"


def test_server_info_lists_obsidian_tools(monkeypatch) -> None:
    from chatgpt_web_oauth_mcp import server

    monkeypatch.setattr(server, "OBSIDIAN_API_KEY", "secret")
    result = _call(server.server_info)

    assert result["obsidian"]["configured"] is True
    assert result["obsidian"]["base_url"].endswith(":27124")
    assert "obsidian_simple_search" in result["tools"]
    assert "obsidian_put_content" in result["tools"]
