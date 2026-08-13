from __future__ import annotations

from typing import Any

from .delegate_guidance import (
    DELEGATE_USE_GUIDE,
    DELEGATE_USE_URI,
    SKILL_INDEX_URI,
    delegate_use_payload,
    skill_index_json,
    skill_index_payload,
)
from .tool_context import READ_ONLY_TOOL


def register_skill_tools(mcp: Any) -> dict[str, object]:
    """Register progressive-disclosure guidance as tools and MCP resources."""

    @mcp.resource(
        SKILL_INDEX_URI,
        name="delegate-skill-index",
        title="Delegate Skill Index",
        description=(
            "Discover task-specific operating guides exposed by this MCP server and "
            "which guide must be loaded before delegate tools are used."
        ),
        mime_type="application/json",
    )
    def delegate_skill_index_resource() -> str:
        return skill_index_json()

    @mcp.resource(
        DELEGATE_USE_URI,
        name="delegate-use",
        title="Delegate Use Guide",
        description=(
            "Complete operating contract for delegate_task, delegate_batch, "
            "delegate_status, and delegate_cancel."
        ),
        mime_type="text/markdown",
    )
    def delegate_use_resource() -> str:
        return DELEGATE_USE_GUIDE

    @mcp.tool(
        name="get_skill_index",
        title="Get Skill Index",
        annotations=READ_ONLY_TOOL,
        description=(
            "Discover progressive-disclosure operating guides exposed by this MCP server. "
            "Call this when first learning the server or before using an unfamiliar tool family; "
            "then load the guide named by guide_tool or resource_uri."
        ),
    )
    def get_skill_index() -> dict[str, object]:
        return {
            "success": True,
            **skill_index_payload(),
        }

    @mcp.tool(
        name="get_delegate_use",
        title="Get Delegate Use Guide",
        annotations=READ_ONLY_TOOL,
        description=(
            "Load the complete delegate operating guide. Call before the first use of "
            "delegate_task, delegate_batch, delegate_status, or delegate_cancel in a task, "
            "and reload it after server upgrades or when recovering from delegate failures."
        ),
    )
    def get_delegate_use() -> dict[str, object]:
        return delegate_use_payload()

    return {
        "get_skill_index": get_skill_index,
        "get_delegate_use": get_delegate_use,
    }
