from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import re
from typing import Annotated, Any

from pydantic import Field

from .files import list_files as list_files_impl
from .files import read_file as read_file_impl
from .files import read_files as read_files_impl
from .files import write_file as write_file_impl
from .patching import apply_patch as apply_patch_impl
from .pathing import resolve_path
from .search import glob_files as glob_files_impl
from .search import grep_files as grep_files_impl
from .tool_context import LOCAL_WRITE_TOOL, READ_ONLY_TOOL, ToolContext


MAX_SEARCH_BATCH_CONCURRENCY = 3
MAX_SEARCH_BATCH_SIZE = 20
SEARCH_MODES = {"glob", "regex", "text"}
SEARCH_BATCH_EXECUTION_MODES = {"sequential", "parallel"}


def _invalid_search_arguments(message: str) -> dict[str, object]:
    return {
        "success": False,
        "mode": "batch",
        "error": {
            "code": "invalid_arguments",
            "message": message,
        },
        "results": [],
    }


def _with_batch_index(index: int, result: dict[str, object]) -> dict[str, object]:
    payload = dict(result)
    payload["index"] = index
    return payload


def register_file_tools(mcp: Any, ctx: ToolContext) -> dict[str, object]:
    """Register local file, search, read, write, and patch tools."""

    def _search_once(
        *,
        search_mode: str,
        path: str | None,
        pattern: str | None,
        query: str | None,
        glob: str | None,
        output_mode: str,
        before: int,
        after: int,
        ignore_case: bool,
        limit: int,
        offset: int,
        multiline: bool,
        include_hidden: bool,
        respect_gitignore: bool,
        exclude_patterns: list[str] | None,
    ) -> dict[str, object]:
        target = resolve_path(path or ".", ctx.workspace_root)

        if search_mode == "glob":
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
                result["mode"] = search_mode
            return result

        if search_mode in {"regex", "text"}:
            effective_pattern = pattern
            if search_mode == "text":
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
                result["mode"] = search_mode
                if search_mode == "text" and query is not None:
                    result["query"] = query
            return result

        return {
            "success": False,
            "error": {
                "code": "invalid_mode",
                "message": "mode must be one of: glob, regex, text.",
            },
        }

    def _search_batch_item(
        *,
        index: int,
        item: dict[str, Any],
        shared_search_mode: str,
        path: str | None,
        pattern: str | None,
        query: str | None,
        glob: str | None,
        output_mode: str,
        before: int,
        after: int,
        ignore_case: bool,
        limit: int,
        offset: int,
        multiline: bool,
        include_hidden: bool,
        respect_gitignore: bool,
        exclude_patterns: list[str] | None,
    ) -> dict[str, object]:
        if not isinstance(item, dict):
            return _with_batch_index(
                index,
                {
                    "success": False,
                    "error": {
                        "code": "invalid_arguments",
                        "message": "Each search query must be an object.",
                    },
                },
            )
        try:
            result = _search_once(
                search_mode=str(item.get("mode", shared_search_mode) or shared_search_mode),
                path=item.get("path", path),
                pattern=item.get("pattern", pattern),
                query=item.get("query", query),
                glob=item.get("glob", glob),
                output_mode=str(item.get("output_mode", output_mode) or output_mode),
                before=int(item.get("before", before)),
                after=int(item.get("after", after)),
                ignore_case=bool(item.get("ignore_case", ignore_case)),
                limit=int(item.get("limit", limit)),
                offset=int(item.get("offset", offset)),
                multiline=bool(item.get("multiline", multiline)),
                include_hidden=bool(item.get("include_hidden", include_hidden)),
                respect_gitignore=bool(item.get("respect_gitignore", respect_gitignore)),
                exclude_patterns=item.get("exclude_patterns", exclude_patterns),
            )
        except (TypeError, ValueError) as exc:
            result = {
                "success": False,
                "error": {
                    "code": "invalid_arguments",
                    "message": f"Invalid batch search query: {exc}",
                },
            }
        return _with_batch_index(index, result)

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
        path: Annotated[
            str | None,
            Field(description="Directory to list. Relative paths resolve from the server workspace root."),
        ] = None,
        recursive: Annotated[bool, Field(description="When true, recursively list nested files and directories.")] = False,
        limit: Annotated[int, Field(description="Maximum number of entries to return after offset.")] = 200,
        offset: Annotated[int, Field(description="Number of matching entries to skip for pagination.")] = 0,
        include_hidden: Annotated[bool, Field(description="Include dotfiles and other hidden entries when true.")] = False,
        respect_gitignore: Annotated[
            bool,
            Field(description="Exclude paths ignored by .gitignore when true."),
        ] = True,
        exclude_patterns: Annotated[
            list[str] | None,
            Field(description="Optional fnmatch-style patterns to exclude, matched against entry name and relative path."),
        ] = None,
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
            "mode='text' for literal substring search. For batch search, pass queries=[...] "
            "and set top-level mode='sequential' or mode='parallel'; each query object can "
            "set its own mode='glob'/'regex'/'text'. Parallel batches are capped at "
            "max_concurrency=3. Hidden entries and .gitignore'd paths are excluded by "
            "default; regex/text search also accept a single file path."
        ),
    )
    def search(
        mode: Annotated[
            str,
            Field(
                description=(
                    "Single-search mode: glob, regex, or text. Batch execution mode when "
                    "queries is provided: sequential or parallel."
                )
            ),
        ] = "regex",
        queries: Annotated[
            list[dict[str, Any]] | None,
            Field(
                description=(
                    "Batch search requests. Each object accepts the same search fields "
                    "as a single call, including mode/path/pattern/query/glob/output_mode."
                )
            ),
        ] = None,
        path: Annotated[
            str | None,
            Field(description="Directory or single file to search. Relative paths resolve from workspace root."),
        ] = None,
        pattern: Annotated[
            str | None,
            Field(description="Glob pattern for mode=glob, or regular expression for mode=regex."),
        ] = None,
        query: Annotated[str | None, Field(description="Literal text query required for mode=text.")] = None,
        glob: Annotated[
            str | None,
            Field(description="Optional file glob filter for regex/text search, such as '*.py'."),
        ] = None,
        output_mode: Annotated[
            str,
            Field(description="For regex/text search: content, files_with_matches, or count."),
        ] = "content",
        before: Annotated[int, Field(description="Number of context lines before each regex/text match.")] = 0,
        after: Annotated[int, Field(description="Number of context lines after each regex/text match.")] = 0,
        ignore_case: Annotated[bool, Field(description="Perform case-insensitive regex/text matching when true.")] = False,
        limit: Annotated[int, Field(description="Maximum number of matches or paths to return after offset.")] = 200,
        offset: Annotated[int, Field(description="Number of matches or paths to skip for pagination.")] = 0,
        multiline: Annotated[bool, Field(description="Enable multiline regular-expression matching for mode=regex.")] = False,
        include_hidden: Annotated[bool, Field(description="Include hidden files and directories when true.")] = False,
        respect_gitignore: Annotated[bool, Field(description="Exclude .gitignore'd paths when true.")] = True,
        exclude_patterns: Annotated[
            list[str] | None,
            Field(description="Optional fnmatch-style patterns to exclude from search."),
        ] = None,
        max_concurrency: Annotated[
            int,
            Field(description="Maximum concurrent searches in parallel batch mode. Hard limit: 3.", ge=1, le=3),
        ] = MAX_SEARCH_BATCH_CONCURRENCY,
    ) -> dict[str, object]:
        if queries is None:
            return _search_once(
                search_mode=mode,
                path=path,
                pattern=pattern,
                query=query,
                glob=glob,
                output_mode=output_mode,
                before=before,
                after=after,
                ignore_case=ignore_case,
                limit=limit,
                offset=offset,
                multiline=multiline,
                include_hidden=include_hidden,
                respect_gitignore=respect_gitignore,
                exclude_patterns=exclude_patterns,
            )

        if not queries:
            return _invalid_search_arguments("Provide at least one search query.")
        if len(queries) > MAX_SEARCH_BATCH_SIZE:
            return _invalid_search_arguments(
                f"At most {MAX_SEARCH_BATCH_SIZE} searches may be executed in one batch."
            )
        if max_concurrency < 1 or max_concurrency > MAX_SEARCH_BATCH_CONCURRENCY:
            return _invalid_search_arguments(
                f"max_concurrency must be between 1 and {MAX_SEARCH_BATCH_CONCURRENCY}."
            )

        if mode in SEARCH_BATCH_EXECUTION_MODES:
            execution_mode = mode
            shared_search_mode = "regex"
        else:
            execution_mode = "sequential"
            shared_search_mode = mode
        if shared_search_mode not in SEARCH_MODES:
            return {
                "success": False,
                "mode": "batch",
                "error": {
                    "code": "invalid_mode",
                    "message": "mode must be one of: glob, regex, text, sequential, parallel.",
                },
                "results": [],
            }

        def run_item(index: int, item: dict[str, Any]) -> dict[str, object]:
            return _search_batch_item(
                index=index,
                item=item,
                shared_search_mode=shared_search_mode,
                path=path,
                pattern=pattern,
                query=query,
                glob=glob,
                output_mode=output_mode,
                before=before,
                after=after,
                ignore_case=ignore_case,
                limit=limit,
                offset=offset,
                multiline=multiline,
                include_hidden=include_hidden,
                respect_gitignore=respect_gitignore,
                exclude_patterns=exclude_patterns,
            )

        if execution_mode == "sequential":
            results = [run_item(index, item) for index, item in enumerate(queries)]
            effective_concurrency = 1
        else:
            effective_concurrency = min(max_concurrency, len(queries))
            ordered_results: list[dict[str, object] | None] = [None] * len(queries)
            with ThreadPoolExecutor(max_workers=effective_concurrency) as executor:
                futures = {
                    executor.submit(run_item, index, item): index
                    for index, item in enumerate(queries)
                }
                for future in as_completed(futures):
                    index = futures[future]
                    ordered_results[index] = future.result()
            results = [item for item in ordered_results if item is not None]

        failed_count = sum(1 for item in results if not item.get("success"))
        return {
            "success": failed_count == 0,
            "mode": "batch",
            "execution_mode": execution_mode,
            "query_count": len(queries),
            "completed_count": len(results),
            "failed_count": failed_count,
            "max_concurrency": effective_concurrency,
            "results": results,
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
        path: Annotated[
            str | None,
            Field(description="Single text file to read. Provide exactly one of path or paths."),
        ] = None,
        paths: Annotated[
            list[str] | None,
            Field(description="Multiple text files to read in batch. Provide exactly one of path or paths."),
        ] = None,
        start_line: Annotated[
            int | None,
            Field(description="1-based first line to include. Omit to start at the beginning."),
        ] = None,
        line_limit: Annotated[
            int | None,
            Field(description="Maximum number of lines to return per file."),
        ] = None,
        include_line_numbers: Annotated[
            bool,
            Field(description="Prefix returned lines with 1-based line numbers when true."),
        ] = False,
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
    def write_file(
        path: Annotated[str, Field(description="Target file path to create or overwrite.")],
        content: Annotated[str, Field(description="Full file contents to write.")],
        dry_run: Annotated[bool, Field(description="Preview the write without changing the filesystem.")] = False,
    ) -> dict[str, object]:
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
        patch: Annotated[
            str,
            Field(description="Structured patch text using *** Begin Patch and update/add/delete file blocks."),
        ],
        dry_run: Annotated[bool, Field(description="Preview patch application without modifying files.")] = False,
        validate_only: Annotated[
            bool,
            Field(description="Only validate patch syntax and matching; do not modify files."),
        ] = False,
        return_diff: Annotated[
            bool,
            Field(description="Include a diff of the changes in the response when true."),
        ] = False,
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
