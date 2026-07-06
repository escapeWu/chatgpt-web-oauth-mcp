from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from .tool_context import LOCAL_STATE_TOOL, LOCAL_WRITE_TOOL, READ_ONLY_TOOL, ToolContext


async def _proxy_obsidian_tool(ctx: ToolContext, tool_name: str, arguments: dict[str, object] | None = None) -> dict[str, object]:
    try:
        return await ctx.obsidian_call_native_tool(ctx.current_obsidian_config(), tool_name, arguments or {})
    except Exception as exc:
        return ctx.obsidian_proxy_error(exc)


def _obsidian_tool(mcp: Any, ctx: ToolContext, *args, **kwargs):
    """Register Obsidian proxy tools only when explicitly enabled."""
    if ctx.enable_obsidian:
        return mcp.tool(*args, **kwargs)

    def decorator(fn):
        return fn

    return decorator


def register_obsidian_tools(mcp: Any, ctx: ToolContext) -> dict[str, object]:
    """Register optional Obsidian native MCP proxy tools."""

    def obsidian_tool(*args, **kwargs):
        return _obsidian_tool(mcp, ctx, *args, **kwargs)

    async def proxy_obsidian_tool(tool_name: str, arguments: dict[str, object] | None = None) -> dict[str, object]:
        return await _proxy_obsidian_tool(ctx, tool_name, arguments)

    @_obsidian_tool(
        mcp,
        ctx,
        name="obsidian_vault_list",
        title="Obsidian Vault List",
        annotations=READ_ONLY_TOOL,
        description="Proxy to Obsidian native MCP tool `vault_list`: list files and subdirectories inside a vault directory.",
    )
    async def obsidian_vault_list(
        path: Annotated[str, Field(description="Vault-relative directory path to list. Empty string lists the vault root.")] = ""
    ) -> dict[str, object]:
        return await _proxy_obsidian_tool(ctx, "vault_list", {"path": path})

    @_obsidian_tool(
        mcp,
        ctx,
        name="obsidian_vault_read",
        title="Obsidian Vault Read",
        annotations=READ_ONLY_TOOL,
        description="Proxy to native Obsidian MCP `vault_read`: read a file's content/metadata, or a targeted heading/block/frontmatter section.",
    )
    async def obsidian_vault_read(
        path: Annotated[str, Field(description="Vault-relative file path to read.")],
        targetType: Annotated[
            str | None,
            Field(description="Optional target type such as heading, block, or frontmatter, as supported by Obsidian MCP."),
        ] = None,
        target: Annotated[
            str | None,
            Field(description="Optional target identifier, such as heading text, block id, or frontmatter key."),
        ] = None,
        targetDelimiter: Annotated[
            str | None,
            Field(description="Optional delimiter used by the native Obsidian MCP target selection."),
        ] = None,
    ) -> dict[str, object]:
        args: dict[str, object] = {"path": path}
        if targetType is not None:
            args["targetType"] = targetType
        if target is not None:
            args["target"] = target
        if targetDelimiter is not None:
            args["targetDelimiter"] = targetDelimiter
        return await _proxy_obsidian_tool(ctx, "vault_read", args)

    @_obsidian_tool(
        mcp,
        ctx,
        name="obsidian_vault_write",
        title="Obsidian Vault Write",
        annotations=LOCAL_WRITE_TOOL,
        description="Proxy to native Obsidian MCP `vault_write`: create or overwrite a vault file.",
    )
    async def obsidian_vault_write(
        path: Annotated[str, Field(description="Vault-relative file path to create or overwrite.")],
        content: Annotated[str, Field(description="Full markdown/text content to write.")],
    ) -> dict[str, object]:
        return await _proxy_obsidian_tool(ctx, "vault_write", {"path": path, "content": content})

    @_obsidian_tool(
        mcp,
        ctx,
        name="obsidian_vault_append",
        title="Obsidian Vault Append",
        annotations=LOCAL_WRITE_TOOL,
        description="Proxy to native Obsidian MCP `vault_append`: append content to a vault file, creating it if missing.",
    )
    async def obsidian_vault_append(
        path: Annotated[str, Field(description="Vault-relative file path to append to.")],
        content: Annotated[str, Field(description="Content to append to the file.")],
    ) -> dict[str, object]:
        return await _proxy_obsidian_tool(ctx, "vault_append", {"path": path, "content": content})

    @_obsidian_tool(
        mcp,
        ctx,
        name="obsidian_vault_patch",
        title="Obsidian Vault Patch",
        annotations=LOCAL_WRITE_TOOL,
        description="Proxy to native Obsidian MCP `vault_patch`: patch a heading, block reference, or frontmatter field.",
    )
    async def obsidian_vault_patch(
        path: Annotated[str, Field(description="Vault-relative file path to patch.")],
        targetType: Annotated[
            str,
            Field(description="Target type for the patch, such as heading, block, or frontmatter."),
        ],
        target: Annotated[str, Field(description="Target identifier to patch, such as heading text or field name.")],
        operation: Annotated[str, Field(description="Patch operation supported by the native Obsidian MCP tool.")],
        content: Annotated[object, Field(description="Replacement or inserted content for the patch operation.")],
        contentType: Annotated[
            str | None,
            Field(description="Optional content type hint for the native Obsidian MCP patch operation."),
        ] = None,
        createTargetIfMissing: Annotated[
            bool | None,
            Field(description="Create the target section or field when it is missing, if supported."),
        ] = None,
        trimTargetWhitespace: Annotated[
            bool | None,
            Field(description="Trim whitespace around the matched target before patching, if supported."),
        ] = None,
        rejectIfContentPreexists: Annotated[
            bool | None,
            Field(description="Reject the patch if the content already exists, if supported."),
        ] = None,
        targetDelimiter: Annotated[
            str | None,
            Field(description="Optional delimiter used for target matching by the native tool."),
        ] = None,
        targetScope: Annotated[
            str | None,
            Field(description="Optional scope constraint for target matching by the native tool."),
        ] = None,
    ) -> dict[str, object]:
        args: dict[str, object] = {
            "path": path,
            "targetType": targetType,
            "target": target,
            "operation": operation,
            "content": content,
        }
        for key, value in {
            "contentType": contentType,
            "createTargetIfMissing": createTargetIfMissing,
            "trimTargetWhitespace": trimTargetWhitespace,
            "rejectIfContentPreexists": rejectIfContentPreexists,
            "targetDelimiter": targetDelimiter,
            "targetScope": targetScope,
        }.items():
            if value is not None:
                args[key] = value
        return await _proxy_obsidian_tool(ctx, "vault_patch", args)

    @_obsidian_tool(
        mcp,
        ctx,
        name="obsidian_vault_delete",
        title="Obsidian Vault Delete",
        annotations=LOCAL_WRITE_TOOL,
        description="Proxy to native Obsidian MCP `vault_delete`: delete a vault file. Requires confirm=true at this bridge layer.",
    )
    async def obsidian_vault_delete(
        path: Annotated[str, Field(description="Vault-relative file path to delete.")],
        confirm: Annotated[bool, Field(description="Must be true to delete the file.")] = False,
    ) -> dict[str, object]:
        if not confirm:
            return {"success": False, "error": {"code": "confirmation_required", "message": "Set confirm=true to delete an Obsidian file."}}
        return await _proxy_obsidian_tool(ctx, "vault_delete", {"path": path})

    @_obsidian_tool(
        mcp,
        ctx,
        name="obsidian_vault_get_document_map",
        title="Obsidian Vault Get Document Map",
        annotations=READ_ONLY_TOOL,
        description="Proxy to native Obsidian MCP `vault_get_document_map`: list headings, block references, and frontmatter fields in a file.",
    )
    async def obsidian_vault_get_document_map(
        path: Annotated[str, Field(description="Vault-relative file path to inspect.")]
    ) -> dict[str, object]:
        return await _proxy_obsidian_tool(ctx, "vault_get_document_map", {"path": path})

    @_obsidian_tool(
        mcp,
        ctx,
        name="obsidian_active_file_get_path",
        title="Obsidian Active File Get Path",
        annotations=READ_ONLY_TOOL,
        description="Proxy to native Obsidian MCP `active_file_get_path`: return the vault path of the currently active file.",
    )
    async def obsidian_active_file_get_path() -> dict[str, object]:
        return await _proxy_obsidian_tool(ctx, "active_file_get_path", {})

    @_obsidian_tool(
        mcp,
        ctx,
        name="obsidian_periodic_note_get_path",
        title="Obsidian Periodic Note Get Path",
        annotations=LOCAL_WRITE_TOOL,
        description="Proxy to native Obsidian MCP `periodic_note_get_path`: get or create the current periodic note path.",
    )
    async def obsidian_periodic_note_get_path(
        period: Annotated[str, Field(description="Periodic note period, such as daily, weekly, monthly, or yearly.")]
    ) -> dict[str, object]:
        return await _proxy_obsidian_tool(ctx, "periodic_note_get_path", {"period": period})

    @_obsidian_tool(
        mcp,
        ctx,
        name="obsidian_search_query",
        title="Obsidian Search Query",
        annotations=READ_ONLY_TOOL,
        description="Proxy to native Obsidian MCP `search_query`: run a JsonLogic query against note metadata.",
    )
    async def obsidian_search_query(
        query: Annotated[dict[str, object], Field(description="JsonLogic query object for Obsidian metadata search.")]
    ) -> dict[str, object]:
        return await _proxy_obsidian_tool(ctx, "search_query", {"query": query})

    @_obsidian_tool(
        mcp,
        ctx,
        name="obsidian_search_simple",
        title="Obsidian Search Simple",
        annotations=READ_ONLY_TOOL,
        description="Proxy to native Obsidian MCP `search_simple`: full-text search using Obsidian's built-in search.",
    )
    async def obsidian_search_simple(
        query: Annotated[str, Field(description="Obsidian full-text search query.")],
        contextLength: Annotated[
            float | None,
            Field(description="Optional number of context characters to include around each match."),
        ] = None,
    ) -> dict[str, object]:
        args: dict[str, object] = {"query": query}
        if contextLength is not None:
            args["contextLength"] = contextLength
        return await _proxy_obsidian_tool(ctx, "search_simple", args)

    @_obsidian_tool(
        mcp,
        ctx,
        name="obsidian_tag_list",
        title="Obsidian Tag List",
        annotations=READ_ONLY_TOOL,
        description="Proxy to native Obsidian MCP `tag_list`: list all tags across the vault with usage counts.",
    )
    async def obsidian_tag_list() -> dict[str, object]:
        return await _proxy_obsidian_tool(ctx, "tag_list", {})

    @_obsidian_tool(
        mcp,
        ctx,
        name="obsidian_command_list",
        title="Obsidian Command List",
        annotations=READ_ONLY_TOOL,
        description="Proxy to native Obsidian MCP `command_list`: list registered Obsidian commands.",
    )
    async def obsidian_command_list() -> dict[str, object]:
        return await _proxy_obsidian_tool(ctx, "command_list", {})

    @_obsidian_tool(
        mcp,
        ctx,
        name="obsidian_command_execute",
        title="Obsidian Command Execute",
        annotations=LOCAL_WRITE_TOOL,
        description="Proxy to native Obsidian MCP `command_execute`: execute an Obsidian command by ID.",
    )
    async def obsidian_command_execute(
        commandId: Annotated[str, Field(description="Obsidian command id to execute.")]
    ) -> dict[str, object]:
        return await _proxy_obsidian_tool(ctx, "command_execute", {"commandId": commandId})

    @_obsidian_tool(
        mcp,
        ctx,
        name="obsidian_open_file",
        title="Obsidian Open File",
        annotations=LOCAL_STATE_TOOL,
        description="Proxy to native Obsidian MCP `open_file`: open a vault file in the Obsidian UI.",
    )
    async def obsidian_open_file(
        path: Annotated[str, Field(description="Vault-relative file path to open in Obsidian.")],
        newLeaf: Annotated[
            bool | None,
            Field(description="Open in a new leaf/tab when true, if supported by Obsidian."),
        ] = None,
    ) -> dict[str, object]:
        args: dict[str, object] = {"path": path}
        if newLeaf is not None:
            args["newLeaf"] = newLeaf
        return await _proxy_obsidian_tool(ctx, "open_file", args)

    @_obsidian_tool(
        mcp,
        ctx,
        name="obsidian_mcp_list_tools",
        title="Obsidian Native MCP List Tools",
        annotations=READ_ONLY_TOOL,
        description="List tools advertised by the Obsidian Local REST API plugin's native MCP server.",
    )
    async def obsidian_mcp_list_tools() -> dict[str, object]:
        try:
            return await ctx.obsidian_list_native_tools(ctx.current_obsidian_config())
        except Exception as exc:
            return ctx.obsidian_proxy_error(exc)

    return {
        "_obsidian_tool": obsidian_tool,
        "_proxy_obsidian_tool": proxy_obsidian_tool,
        "obsidian_vault_list": obsidian_vault_list,
        "obsidian_vault_read": obsidian_vault_read,
        "obsidian_vault_write": obsidian_vault_write,
        "obsidian_vault_append": obsidian_vault_append,
        "obsidian_vault_patch": obsidian_vault_patch,
        "obsidian_vault_delete": obsidian_vault_delete,
        "obsidian_vault_get_document_map": obsidian_vault_get_document_map,
        "obsidian_active_file_get_path": obsidian_active_file_get_path,
        "obsidian_periodic_note_get_path": obsidian_periodic_note_get_path,
        "obsidian_search_query": obsidian_search_query,
        "obsidian_search_simple": obsidian_search_simple,
        "obsidian_tag_list": obsidian_tag_list,
        "obsidian_command_list": obsidian_command_list,
        "obsidian_command_execute": obsidian_command_execute,
        "obsidian_open_file": obsidian_open_file,
        "obsidian_mcp_list_tools": obsidian_mcp_list_tools,
    }
