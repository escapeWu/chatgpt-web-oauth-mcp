from __future__ import annotations

import asyncio

from chatgpt_web_oauth_mcp import config, server
from chatgpt_web_oauth_mcp.server import server_info


def _call() -> dict:
    fn = server_info.fn if hasattr(server_info, "fn") else server_info
    return asyncio.run(fn())


def test_server_info_reports_metadata_and_tools() -> None:
    assert server._tool_context.codex_runtime_default_sandbox == config.CODEX_RUNTIME_DEFAULT_SANDBOX
    payload = _call()
    assert payload["success"] is True
    assert payload["app_name"] == "chatgpt-web-oauth-mcp"
    assert isinstance(payload["port"], int)
    assert isinstance(payload["workspace_root"], str)
    assert payload["auth"] in {"none", "shared_token", "oauth"}
    assert payload["command_timeout_seconds"] >= 1
    assert payload["delegate_timeout_seconds"] >= 1
    assert payload["delegate_wait_timeout_seconds"] >= 1
    runtime_info = payload["codex_runtime"]
    assert runtime_info["enabled"] is True
    assert runtime_info["process_status"] == "stopped"
    assert runtime_info["runtime_count"] >= 0
    assert runtime_info["binding_store"]["available"] is True
    assert runtime_info["default_sandbox"] == config.CODEX_RUNTIME_DEFAULT_SANDBOX
    delegate_mode = payload["delegate_mode"]
    assert delegate_mode["executor"] == "codex"
    assert delegate_mode["default_harness"] == "codex"
    assert set(delegate_mode["harnesses"]) == {"codex", "pi"}
    assert delegate_mode["harnesses"]["pi"]["read_only_supported"] is True
    assert delegate_mode["scheduler"] == "project_scoped_fair_reader_writer"
    assert delegate_mode["serial"] is False
    assert delegate_mode["background_tasks"] is True
    assert delegate_mode["explore"] == {
        "lane": "reader",
        "model": "gpt-5.6-luna",
        "reasoning_effort": "low",
        "sandbox_mode": "read-only",
        "execution_timeout_seconds": 900,
    }
    assert delegate_mode["code"] == {
        "lane": "writer",
        "model": "gpt-5.6-sol",
        "reasoning_effort": "xhigh",
        "sandbox_mode": "danger-full-access",
        "execution_timeout_seconds": 3600,
    }
    assert payload["routing_contract"]["chatgpt_web_role"] == "architect_manager_reviewer"
    assert payload["routing_contract"]["codex_delegate_role"] == "project_scoped_reader_writer_execution"
    assert payload["skill_guidance"] == {
        "discovery_tool": "get_skill_index",
        "delegate_guide_tool": "get_delegate_use",
        "index_resource": "skill://chatgpt-web-oauth-mcp/index",
        "delegate_resource": "skill://chatgpt-web-oauth-mcp/delegate-use",
        "guide_tools": {
            "delegate-use": "get_delegate_use",
            "file-use": "get_file_use",
            "process-use": "get_process_use",
            "git-use": "get_git_use",
        },
        "guide_resources": {
            "delegate-use": "skill://chatgpt-web-oauth-mcp/delegate-use",
            "file-use": "skill://chatgpt-web-oauth-mcp/file-use",
            "process-use": "skill://chatgpt-web-oauth-mcp/process-use",
            "git-use": "skill://chatgpt-web-oauth-mcp/git-use",
        },
        "progressive_disclosure": True,
    }
    assert payload["resources"] == [
        "skill://chatgpt-web-oauth-mcp/delegate-use",
        "skill://chatgpt-web-oauth-mcp/file-use",
        "skill://chatgpt-web-oauth-mcp/git-use",
        "skill://chatgpt-web-oauth-mcp/index",
        "skill://chatgpt-web-oauth-mcp/process-use",
    ]
    assert payload["resource_count"] == len(payload["resources"])
    tools = payload["tools"]
    assert isinstance(tools, list)
    assert "obsidian_proxy" not in payload
    # Spot-check a handful of must-have tools from each module.
    for name in [
        "server_info",
        "codex_runtime_open",
        "codex_runtime_resume",
        "codex_runtime_status",
        "codex_runtime_close",
        "codex_exec",
        "codex_mcp_inventory",
        "codex_mcp_call",
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
        "tmux_list",
        "tmux_start",
        "tmux_status",
        "tmux_capture",
        "tmux_send",
        "tmux_kill",
        "delegate_task",
        "delegate_batch",
        "delegate_status",
        "delegate_cancel",
        "get_skill_index",
        "get_delegate_use",
        "get_file_use",
        "get_process_use",
        "get_git_use",
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
