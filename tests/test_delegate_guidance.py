from __future__ import annotations

import asyncio

from chatgpt_web_oauth_mcp.delegate_guidance import (
    DELEGATE_USE_GUIDE,
    DELEGATE_USE_URI,
    FILE_USE_GUIDE,
    FILE_USE_URI,
    GIT_USE_GUIDE,
    GIT_USE_URI,
    PROCESS_USE_GUIDE,
    PROCESS_USE_URI,
    SKILL_INDEX_URI,
    delegate_use_payload,
    file_use_payload,
    git_use_payload,
    process_use_payload,
    skill_index_payload,
)


def test_skill_index_routes_agents_to_delegate_guide() -> None:
    payload = skill_index_payload()

    assert payload["namespace"] == "chatgpt-web-oauth-mcp"
    assert payload["resource_uri"] == SKILL_INDEX_URI
    assert payload["discovery_tool"] == "get_skill_index"
    skills = {item["name"]: item for item in payload["skills"]}
    assert set(skills) == {"delegate-use", "file-use", "process-use", "git-use"}
    assert skills["delegate-use"]["guide_tool"] == "get_delegate_use"
    assert skills["delegate-use"]["resource_uri"] == DELEGATE_USE_URI
    assert skills["file-use"]["guide_tool"] == "get_file_use"
    assert skills["file-use"]["resource_uri"] == FILE_USE_URI
    assert skills["process-use"]["guide_tool"] == "get_process_use"
    assert skills["process-use"]["resource_uri"] == PROCESS_USE_URI
    assert skills["git-use"]["guide_tool"] == "get_git_use"
    assert skills["git-use"]["resource_uri"] == GIT_USE_URI

    required_tools = {
        tool
        for skill in skills.values()
        for tool in skill["required_before_tools"]
    }
    assert {
        "delegate_task",
        "list_files",
        "apply_patch",
        "run_command",
        "job_start",
        "tmux_start",
        "git_status",
        "git_worktree_remove",
    } <= required_tools


def test_delegate_use_guide_contains_critical_operating_contracts() -> None:
    payload = delegate_use_payload()

    assert payload["success"] is True
    assert payload["resource_uri"] == DELEGATE_USE_URI
    assert payload["content"] == DELEGATE_USE_GUIDE
    for required in [
        "Use direct MCP tools first",
        "kind=explore",
        "kind=code",
        "harness=codex",
        "harness=pi",
        "depends_on_group_ids",
        "Dependencies control scheduling only",
        "do not put `kind` or `commit_mode` in child specifications",
        "The tool default is `allowed`",
        "wait_seconds",
        "execution_timeout_seconds",
        "delegate_status",
        "delegate_cancel",
        "readonly_violation",
        "Do not assume retries are idempotent",
    ]:
        assert required in DELEGATE_USE_GUIDE


def test_file_use_guide_contains_critical_operating_contracts() -> None:
    payload = file_use_payload()

    assert payload["success"] is True
    assert payload["resource_uri"] == FILE_USE_URI
    assert payload["content"] == FILE_USE_GUIDE
    for required in [
        "list_files",
        "next_offset",
        "encoding_error",
        "mode=hex",
        "apply_patch",
        "expected_revision",
        "revision_conflict",
        "dry_run=true",
        "rolled_back",
    ]:
        assert required in FILE_USE_GUIDE


def test_process_use_guide_contains_critical_operating_contracts() -> None:
    payload = process_use_payload()

    assert payload["success"] is True
    assert payload["resource_uri"] == PROCESS_USE_URI
    assert payload["content"] == PROCESS_USE_GUIDE
    for required in [
        "run_command",
        "job_start",
        "job_output",
        "next_cursor",
        "tmux_start",
        "accepted_by_tmux=true",
        "force=true",
        "explicit user-approved",
    ]:
        assert required in PROCESS_USE_GUIDE


def test_git_use_guide_contains_critical_operating_contracts() -> None:
    payload = git_use_payload()

    assert payload["success"] is True
    assert payload["resource_uri"] == GIT_USE_URI
    assert payload["content"] == GIT_USE_GUIDE
    for required in [
        "git_status",
        "git_diff(staged=true",
        "stage_all=true",
        "amend=true",
        "git_worktree_create",
        "git_worktree_remove(force=true)",
        "worktree_dirty",
    ]:
        assert required in GIT_USE_GUIDE


def test_skill_resources_share_the_same_authoritative_content() -> None:
    from chatgpt_web_oauth_mcp.server import mcp

    async def scenario() -> None:
        resources = await mcp.list_resources()
        resource_uris = {str(resource.uri) for resource in resources}
        assert {
            SKILL_INDEX_URI,
            DELEGATE_USE_URI,
            FILE_USE_URI,
            PROCESS_USE_URI,
            GIT_USE_URI,
        } <= resource_uris

        guide = await mcp.read_resource(DELEGATE_USE_URI)
        assert guide.contents[0].content == DELEGATE_USE_GUIDE
        file_guide = await mcp.read_resource(FILE_USE_URI)
        assert file_guide.contents[0].content == FILE_USE_GUIDE
        process_guide = await mcp.read_resource(PROCESS_USE_URI)
        assert process_guide.contents[0].content == PROCESS_USE_GUIDE
        git_guide = await mcp.read_resource(GIT_USE_URI)
        assert git_guide.contents[0].content == GIT_USE_GUIDE

    asyncio.run(scenario())
