from __future__ import annotations

import re
from typing import Any

from .files import list_files as list_files_impl
from .files import read_file as read_file_impl
from .files import read_files as read_files_impl
from .files import write_file as write_file_impl
from .patching import apply_patch as apply_patch_impl
from .pathing import resolve_path
from .search import glob_files as glob_files_impl
from .search import grep_files as grep_files_impl
from .tool_context import LOCAL_WRITE_TOOL, READ_ONLY_TOOL, ToolContext


def register_file_tools(mcp: Any, ctx: ToolContext) -> dict[str, object]:
    """Register local file, search, read, write, and patch tools."""

    @mcp.tool(
        name="list_files",
        title="List Files",
        annotations=READ_ONLY_TOOL,
        description=(
            "List files and directories. Hidden entries, common junk dirs "
            "(.git / .venv / node_modules / __pycache__ / ...) and .gitignore'd "
            "paths are excluded by default. Set include_hidden=True or "
            "respect_gitignore=False to see them; add exclude_patterns for "
            "fnmatch-style patterns (matched against both name and relative path)."
        ),
    )
    def list_files(
        path: str | None = None,
        recursive: bool = False,
        limit: int = 200,
        offset: int = 0,
        include_hidden: bool = False,
        respect_gitignore: bool = True,
        exclude_patterns: list[str] | None = None,
    ) -> dict[str, object]:
        target = resolve_path(path or ".", ctx.workspace_root)
        return list_files_impl(
            target,
            recursive=recursive,
            limit=limit,
            offset=offset,
            include_hidden=include_hidden,
            respect_gitignore=respect_gitignore,
            exclude_patterns=exclude_patterns,
        )

    @mcp.tool(
        name="search",
        title="Search Workspace",
        annotations=READ_ONLY_TOOL,
        description=(
            "Canonical search tool that unifies glob, regex grep, and plain-text search. "
            "Use mode='glob' for path discovery, mode='regex' for code/text regex, and "
            "mode='text' for literal substring search. Hidden entries and .gitignore'd "
            "paths are excluded by default; regex/text search also accept a single file path."
        ),
    )
    def search(
        mode: str = "regex",
        path: str | None = None,
        pattern: str | None = None,
        query: str | None = None,
        glob: str | None = None,
        output_mode: str = "content",
        before: int = 0,
        after: int = 0,
        ignore_case: bool = False,
        limit: int = 200,
        offset: int = 0,
        multiline: bool = False,
        include_hidden: bool = False,
        respect_gitignore: bool = True,
        exclude_patterns: list[str] | None = None,
    ) -> dict[str, object]:
        target = resolve_path(path or ".", ctx.workspace_root)

        if mode == "glob":
            if not pattern:
                return {
                    "success": False,
                    "error": {"code": "missing_pattern", "message": "mode=glob requires `pattern`."},
                }
            result = glob_files_impl(
                target,
                pattern=pattern,
                limit=limit,
                offset=offset,
                include_hidden=include_hidden,
                respect_gitignore=respect_gitignore,
                exclude_patterns=exclude_patterns,
            )
            if isinstance(result, dict):
                result["mode"] = mode
            return result

        if mode in {"regex", "text"}:
            effective_pattern = pattern
            if mode == "text":
                if query is None:
                    return {
                        "success": False,
                        "error": {
                            "code": "missing_query",
                            "message": "mode=text requires `query`.",
                        },
                    }
                effective_pattern = re.escape(query)
            elif effective_pattern is None:
                return {
                    "success": False,
                    "error": {"code": "missing_pattern", "message": "mode=regex requires `pattern`."},
                }

            result = grep_files_impl(
                target,
                pattern=effective_pattern,
                glob_pattern=glob,
                output_mode=output_mode,
                before=before,
                after=after,
                ignore_case=ignore_case,
                head_limit=limit,
                offset=offset,
                multiline=multiline,
                include_hidden=include_hidden,
                respect_gitignore=respect_gitignore,
                exclude_patterns=exclude_patterns,
            )
            if isinstance(result, dict):
                result["mode"] = mode
                if mode == "text" and query is not None:
                    result["query"] = query
            return result

        return {
            "success": False,
            "error": {
                "code": "invalid_mode",
                "message": "mode must be one of: glob, regex, text.",
            },
        }

    @mcp.tool(
        name="read_text",
        title="Read Text",
        annotations=READ_ONLY_TOOL,
        description=(
            "Canonical text-reader tool. Pass either `path` for single-file reads or "
            "`paths` for batch reads. Pagination is line-based via start_line/line_limit. "
            "Set include_line_numbers=true when evidence or code-review output needs numbered lines."
        ),
    )
    def read_text(
        path: str | None = None,
        paths: list[str] | None = None,
        start_line: int | None = None,
        line_limit: int | None = None,
        include_line_numbers: bool = False,
    ) -> dict[str, object]:
        has_path = bool(path)
        has_paths = bool(paths)
        if has_path == has_paths:
            return {
                "success": False,
                "error": {
                    "code": "invalid_arguments",
                    "message": "Provide exactly one of `path` or `paths`.",
                },
            }

        effective_offset = start_line
        effective_limit = line_limit
        if path:
            target = resolve_path(path, ctx.workspace_root)
            result = read_file_impl(
                target,
                offset=effective_offset,
                limit=effective_limit,
                max_lines=200,
                max_bytes=32768,
                include_line_numbers=include_line_numbers,
            )
            if isinstance(result, dict):
                result["mode"] = "single"
            return result

        targets = [resolve_path(item, ctx.workspace_root) for item in (paths or [])]
        result = read_files_impl(
            targets,
            offset=effective_offset,
            limit=effective_limit,
            max_lines=200,
            max_bytes=32768,
            include_line_numbers=include_line_numbers,
        )
        if isinstance(result, dict):
            result["mode"] = "batch"
        return result

    @mcp.tool(
        name="write_file",
        title="Write File",
        annotations=LOCAL_WRITE_TOOL,
        description="Write full content to a file (supports dry_run preview without touching disk).",
    )
    def write_file(path: str, content: str, dry_run: bool = False) -> dict[str, object]:
        target = resolve_path(path, ctx.workspace_root)
        return write_file_impl(target, content=content, dry_run=dry_run)

    @mcp.tool(
        name="apply_patch",
        title="Apply Patch",
        annotations=LOCAL_WRITE_TOOL,
        description=(
            "Apply a structured patch using *** Begin Patch / *** Update File blocks. "
            "Each @@ hunk must contain at least one '+' or '-' line and must "
            "match exactly one location in the target file; pure-context hunks are rejected."
        ),
    )
    def apply_patch(
        patch: str,
        dry_run: bool = False,
        validate_only: bool = False,
        return_diff: bool = False,
    ) -> dict[str, object]:
        return apply_patch_impl(
            patch=patch,
            workspace_root=ctx.workspace_root,
            dry_run=dry_run,
            validate_only=validate_only,
            return_diff=return_diff,
        )

    return {
        "list_files": list_files,
        "search": search,
        "read_text": read_text,
        "write_file": write_file,
        "apply_patch": apply_patch,
    }
