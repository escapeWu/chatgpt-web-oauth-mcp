from __future__ import annotations

import asyncio

from chatgpt_web_oauth_mcp.delegate_guidance import (
    DELEGATE_USE_GUIDE,
    DELEGATE_USE_URI,
    SKILL_INDEX_URI,
    delegate_use_payload,
    skill_index_payload,
)


def test_skill_index_routes_agents_to_delegate_guide() -> None:
    payload = skill_index_payload()

    assert payload["namespace"] == "chatgpt-web-oauth-mcp"
    assert payload["resource_uri"] == SKILL_INDEX_URI
    assert payload["discovery_tool"] == "get_skill_index"
    assert payload["skills"] == [
        {
            "name": "delegate-use",
            "description": (
                "Safe, bounded use of delegate_task, delegate_batch, delegate_status, "
                "and delegate_cancel across Codex, Pi, and custom CLI harnesses."
            ),
            "triggers": [
                "Before the first delegate tool call in a task",
                "When choosing a harness or explore/code kind",
                "When monitoring, cancelling, or recovering delegate work",
            ],
            "required_before_tools": [
                "delegate_task",
                "delegate_batch",
                "delegate_status",
                "delegate_cancel",
            ],
            "guide_tool": "get_delegate_use",
            "resource_uri": DELEGATE_USE_URI,
        }
    ]


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


def test_skill_resources_share_the_same_authoritative_content() -> None:
    from chatgpt_web_oauth_mcp.server import mcp

    async def scenario() -> None:
        resources = await mcp.list_resources()
        resource_uris = {str(resource.uri) for resource in resources}
        assert {SKILL_INDEX_URI, DELEGATE_USE_URI} <= resource_uris

        guide = await mcp.read_resource(DELEGATE_USE_URI)
        assert guide.contents[0].content == DELEGATE_USE_GUIDE

    asyncio.run(scenario())
