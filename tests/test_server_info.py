from __future__ import annotations

import asyncio

from chatgpt_web_oauth_mcp.server import server_info


def _call() -> dict:
    fn = server_info.fn if hasattr(server_info, "fn") else server_info
    return asyncio.run(fn())


def test_server_info_reports_metadata_and_tools() -> None:
    payload = _call()
    assert payload["success"] is True
    assert payload["app_name"] == "chatgpt-web-oauth-mcp"
    assert isinstance(payload["port"], int)
    assert isinstance(payload["workspace_root"], str)
    assert payload["auth"] in {"none", "shared_token", "oauth"}
    assert payload["command_timeout_seconds"] >= 1
    assert payload["delegate_timeout_seconds"] >= 1
    assert payload["delegate_mode"] == {
        "executor": "codex",
        "serial": True,
        "background_tasks": False,
        "default_wait_seconds": 300,
        "continuation": "call delegate_task again when status is running",
        "audit_logs": "system temp / chatgpt-web-oauth-mcp / codex-delegates",
        "log_progress": "use read_text on returned stdout/stderr/metadata paths",
        "raw_output": "stdout/stderr are stored in logs and not inlined in completed responses",
        "status_recovery": "use delegate_status to list active/recent server-generated delegate_id values",
        "status_monitor": "delegate_status supports watch_seconds up to 300 and polls every 5s by default",
    }
    assert payload["routing_contract"]["chatgpt_web_role"] == "architect_manager_reviewer"
    assert payload["routing_contract"]["codex_delegate_role"] == "single_bounded_execution_slice"
    tools = payload["tools"]
    assert isinstance(tools, list)
    assert "obsidian_proxy" not in payload
    # Spot-check a handful of must-have tools from each module.
    for name in [
        "server_info",
        "env_snapshot",
        "env_diff",
        "search",
        "read_text",
        "code_map_symbols",
        "code_map_references",
        "code_map_imports",
        "run_command",
        "apply_patch",
        "git_status",
        "git_show",
        "git_blame",
        "git_worktree_create",
        "git_worktree_list",
        "git_worktree_status",
        "git_worktree_remove",
        "delegate_task",
        "delegate_status",
    ]:
        assert name in tools, f"expected {name} in tools list"
    for name in [
        "run_command_stream",
        "get_task",
        "wait_task",
        "cancel_task",
        "purge_tasks",
        "taskboard_create",
        "taskboard_delegate",
        "taskboard_status",
        "taskboard_collect_results",
        "list_skills",
    ]:
        assert name not in tools, f"did not expect removed tool {name}"
    for removed in ["search_files", "glob_files", "grep_files", "read_file", "read_files", "replace_in_file"]:
        assert removed not in tools, f"did not expect legacy alias tool {removed}"
    assert not [name for name in tools if name.startswith("obsidian_")]
    assert payload["tool_count"] == len(tools)
