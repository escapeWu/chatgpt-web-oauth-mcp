from __future__ import annotations

from typing import Any

from .delegate_guidance import (
    FILE_USE_GUIDE,
    FILE_USE_URI,
    GIT_USE_GUIDE,
    GIT_USE_URI,
    PROCESS_USE_GUIDE,
    PROCESS_USE_URI,
    SKILL_INDEX_URI,
    file_use_payload,
    git_use_payload,
    process_use_payload,
    skill_index_json,
    skill_index_payload,
)
from .tool_context import READ_ONLY_TOOL


def register_skill_tools(mcp: Any) -> dict[str, object]:
    """Register progressive-disclosure guidance as tools and MCP resources."""

    @mcp.resource(
        SKILL_INDEX_URI,
        name="skill-index",
        title="Skill Index",
        description=(
            "Discover task-specific operating guides exposed by this MCP server and "
            "which guide should be loaded before each tool family is used."
        ),
        mime_type="application/json",
    )
    def skill_index_resource() -> str:
        return skill_index_json()

    @mcp.resource(
        FILE_USE_URI,
        name="file-use",
        title="File Use Guide",
        description=(
            "Safe operating guide for file discovery, search, reading, code maps, "
            "and mutation tools."
        ),
        mime_type="text/markdown",
    )
    def file_use_resource() -> str:
        return FILE_USE_GUIDE

    @mcp.resource(
        PROCESS_USE_URI,
        name="process-use",
        title="Process Use Guide",
        description=(
            "Lifecycle guide for synchronous commands, durable background jobs, "
            "and persistent interactive tmux sessions."
        ),
        mime_type="text/markdown",
    )
    def process_use_resource() -> str:
        return PROCESS_USE_GUIDE

    @mcp.resource(
        GIT_USE_URI,
        name="git-use",
        title="Git Use Guide",
        description=(
            "Safe operating guide for repository inspection, commits, history, "
            "and Git worktrees."
        ),
        mime_type="text/markdown",
    )
    def git_use_resource() -> str:
        return GIT_USE_GUIDE

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
        name="get_file_use",
        title="Get File Use Guide",
        annotations=READ_ONLY_TOOL,
        description=(
            "Load the file operating guide. Call before the first nontrivial file workflow "
            "or mutation, and when handling pagination, encodings, revisions, PDFs, images, "
            "or binary data."
        ),
    )
    def get_file_use() -> dict[str, object]:
        return file_use_payload()

    @mcp.tool(
        name="get_process_use",
        title="Get Process Use Guide",
        annotations=READ_ONLY_TOOL,
        description=(
            "Load the process operating guide. Call before choosing or operating run_command, "
            "durable jobs, or interactive tmux sessions."
        ),
    )
    def get_process_use() -> dict[str, object]:
        return process_use_payload()

    @mcp.tool(
        name="get_git_use",
        title="Get Git Use Guide",
        annotations=READ_ONLY_TOOL,
        description=(
            "Load the Git operating guide. Call before repository workflows, especially "
            "staging, committing, amending, and worktree creation or removal."
        ),
    )
    def get_git_use() -> dict[str, object]:
        return git_use_payload()

    return {
        "get_skill_index": get_skill_index,
        "get_file_use": get_file_use,
        "get_process_use": get_process_use,
        "get_git_use": get_git_use,
    }
