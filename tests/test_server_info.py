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
        "default_wait_seconds": 180,
        "continuation": "call delegate_task again when status is running",
        "audit_logs": "system temp / chatgpt-web-oauth-mcp / codex-delegates",
    }
    assert payload["routing_contract"]["chatgpt_web_role"] == "architect_manager_reviewer"
    assert payload["routing_contract"]["codex_delegate_role"] == "single_bounded_execution_slice"
    tools = payload["tools"]
    assert isinstance(tools, list)
    # Spot-check a handful of must-have tools from each module.
    for name in [
        "server_info",
        "search",
        "read_text",
        "run_command",
        "apply_patch",
        "git_status",
        "git_show",
        "git_blame",
        "delegate_task",
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
    assert payload["tool_count"] == len(tools)
