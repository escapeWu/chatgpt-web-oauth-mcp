from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Annotated, Any, Literal

from pydantic import Field

from .code_map import code_map_imports as code_map_imports_impl
from .code_map import code_map_references as code_map_references_impl
from .code_map import code_map_symbols as code_map_symbols_impl
from .files import list_files as list_files_impl
from .files import read_file as read_file_impl
from .files import read_files as read_files_impl
from .files import write_file as write_file_impl
from .patching import apply_patch as apply_patch_impl
from .pathing import resolve_path
from .reader import read_path as read_path_impl
from .replacing import replace_files as replace_files_impl
from .response_budget import ResponseBudget, with_budget_metadata
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

    def _finalize_search_result(
        result: dict[str, object],
        *,
        search_mode: str,
        query: str | None,
        offset: int,
        max_tokens: int,
    ) -> dict[str, object]:
        result["mode"] = search_mode
        if search_mode == "text" and query is not None:
            result["query"] = query
        if result.get("success") is not True:
            return result
        budget = ResponseBudget(max_tokens=max_tokens)
        rendered, measurement = with_budget_metadata(
            result,
            budget=budget,
            truncated=bool(result.get("truncated")),
            stop_reason=str(result.get("stop_reason", "end_of_results")),
        )
        result_key = next(
            (
                key
                for key in ("matches", "files", "counts")
                if isinstance(rendered.get(key), list)
            ),
            None,
        )
        items = rendered.get(result_key) if result_key is not None else None
        while not measurement.fits and isinstance(items, list) and items:
            items.pop()
            rendered["next_offset"] = max(offset, 0) + len(items)
            rendered, measurement = with_budget_metadata(
                rendered,
                budget=budget,
                truncated=True,
                stop_reason="token_budget",
            )
            items = rendered.get(result_key) if result_key is not None else None
        return rendered

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
        file_type: str | None,
        only_matching: bool,
        max_tokens: int,
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
                max_tokens=max_tokens,
            )
            return _finalize_search_result(
                result,
                search_mode=search_mode,
                query=query,
                offset=offset,
                max_tokens=max_tokens,
            )

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
                effective_pattern = query
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
                file_type=file_type,
                only_matching=only_matching,
                fixed_strings=search_mode == "text",
                regex_engine="default",
                rg_binary=ctx.ripgrep_binary,
                max_tokens=max_tokens,
            )
            return _finalize_search_result(
                result,
                search_mode=search_mode,
                query=query,
                offset=offset,
                max_tokens=max_tokens,
            )

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
        file_type: str | None,
        only_matching: bool,
        max_tokens: int,
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
                file_type=item.get("file_type", file_type),
                only_matching=bool(item.get("only_matching", only_matching)),
                max_tokens=max_tokens,
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
        sort: Annotated[
            Literal["path", "modified", "size"],
            Field(description="Stable ordering: path, newest modified first, or largest size first."),
        ] = "path",
        files_only: Annotated[bool, Field(description="Return files only.")] = False,
        dirs_only: Annotated[bool, Field(description="Return directories only.")] = False,
        filter: Annotated[
            Literal["project", "all"],
            Field(description="project applies hidden/junk/.gitignore filters; all disables built-in filtering."),
        ] = "project",
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
            sort=sort,
            files_only=files_only,
            dirs_only=dirs_only,
            filter_mode=filter,
            max_tokens=ctx.tool_output_token_budget,
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
                    "as a single call, including mode/path/pattern/query/glob/output_mode, "
                    "file_type, and only_matching."
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
            Field(description="For regex/text search: content, files_with_matches, count, or summary."),
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
        file_type: Annotated[
            str | None,
            Field(description="Optional ripgrep file type filter, such as py, rust, or js."),
        ] = None,
        only_matching: Annotated[
            bool,
            Field(description="For content output, return each matched substring instead of the full matching line."),
        ] = False,
        max_concurrency: Annotated[
            int,
            Field(description="Maximum concurrent searches in parallel batch mode. Hard limit: 3.", ge=1, le=3),
        ] = MAX_SEARCH_BATCH_CONCURRENCY,
        batch_offset: Annotated[
            int,
            Field(description="For queries batches, number of queries to skip for response pagination.", ge=0),
        ] = 0,
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
                file_type=file_type,
                only_matching=only_matching,
                max_tokens=ctx.tool_output_token_budget,
            )

        if not queries:
            return _invalid_search_arguments("Provide at least one search query.")
        if len(queries) > MAX_SEARCH_BATCH_SIZE:
            return _invalid_search_arguments(
                f"At most {MAX_SEARCH_BATCH_SIZE} searches may be executed in one batch."
            )
        effective_batch_offset = max(batch_offset, 0)
        selected_queries = queries[effective_batch_offset:]
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
                file_type=file_type,
                only_matching=only_matching,
                max_tokens=ctx.tool_output_token_budget,
            )

        if not selected_queries:
            results = []
            effective_concurrency = 0
        elif execution_mode == "sequential":
            results = [
                run_item(index, item)
                for index, item in enumerate(selected_queries, start=effective_batch_offset)
            ]
            effective_concurrency = 1
        else:
            effective_concurrency = min(max_concurrency, len(selected_queries))
            ordered_results: list[dict[str, object] | None] = [None] * len(selected_queries)
            with ThreadPoolExecutor(max_workers=effective_concurrency) as executor:
                futures = {
                    executor.submit(run_item, index, item): local_index
                    for local_index, (index, item) in enumerate(
                        enumerate(selected_queries, start=effective_batch_offset)
                    )
                }
                for future in as_completed(futures):
                    local_index = futures[future]
                    ordered_results[local_index] = future.result()
            results = [item for item in ordered_results if item is not None]

        failed_count = sum(1 for item in results if not item.get("success"))
        payload: dict[str, object] = {
            "success": failed_count == 0,
            "mode": "batch",
            "execution_mode": execution_mode,
            "query_count": len(queries),
            "completed_count": len(results),
            "failed_count": failed_count,
            "max_concurrency": effective_concurrency,
            "results": results,
            "next_offset": None,
        }
        budget = ResponseBudget(max_tokens=ctx.tool_output_token_budget)
        rendered, measurement = with_budget_metadata(
            payload,
            budget=budget,
            truncated=False,
            stop_reason="end_of_results",
        )
        rendered_results = rendered.get("results")
        while not measurement.fits and isinstance(rendered_results, list) and rendered_results:
            rendered_results.pop()
            rendered["next_offset"] = effective_batch_offset + len(rendered_results)
            rendered, measurement = with_budget_metadata(
                rendered,
                budget=budget,
                truncated=True,
                stop_reason="token_budget",
            )
            rendered_results = rendered.get("results")
        return rendered

    @mcp.tool(
        name="read_text",
        title="Read Text",
        annotations=READ_ONLY_TOOL,
        description=(
            "Canonical text-reader tool. Pass either `path` for single-file reads or "
            "`paths` for batch reads. Pagination is line-based via start_line/line_limit. "
            "Byte- or token-limited pages stop between lines; a single line larger than either "
            "budget is returned whole with oversized_line=true so pagination can advance. "
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
        batch_offset: Annotated[
            int,
            Field(description="For paths batch reads, number of paths to skip for response pagination.", ge=0),
        ] = 0,
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
                max_tokens=ctx.read_token_budget,
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
            max_tokens=ctx.read_token_budget,
            batch_offset=batch_offset,
        )
        if isinstance(result, dict):
            result["mode"] = "batch"
        return result

    @mcp.tool(
        name="read",
        title="Read File",
        annotations=READ_ONLY_TOOL,
        description=(
            "Unified file reader. mode=auto conservatively selects text, image metadata, "
            "PDF text, or a binary hex preview. Text reads support explicit encodings and "
            "report ambiguous encoding instead of silently replacing bytes. All modes use "
            "the shared o200k token budget and return common pagination metadata."
        ),
    )
    def read(
        path: Annotated[str, Field(description="File to read. Relative paths resolve from workspace root.")],
        mode: Annotated[
            Literal["auto", "text", "image", "pdf", "hex"],
            Field(description="Read mode. auto detects from MIME, suffix, BOM, and a bounded prefix."),
        ] = "auto",
        encoding: Annotated[
            str | None,
            Field(description="Explicit text encoding such as utf-8, utf-16le, gbk, or windows-1252."),
        ] = None,
        start_line: Annotated[int, Field(description="1-based first text line to return.", ge=1)] = 1,
        line_limit: Annotated[int, Field(description="Maximum text lines to return.", ge=1)] = 200,
        include_line_numbers: Annotated[
            bool,
            Field(description="Prefix text lines with 1-based line numbers."),
        ] = False,
        pages: Annotated[
            str | None,
            Field(description="PDF pages using 1-based ranges such as '1-5,8'. At most 20 pages."),
        ] = None,
        byte_offset: Annotated[int, Field(description="0-based byte offset for hex mode.", ge=0)] = 0,
        byte_limit: Annotated[
            int,
            Field(description="Maximum bytes to inspect in hex mode (hard cap 262144).", ge=1),
        ] = 4096,
    ) -> dict[str, object]:
        target = resolve_path(path, ctx.workspace_root)
        return read_path_impl(
            target,
            mode=mode,
            encoding=encoding,
            start_line=start_line,
            line_limit=line_limit,
            include_line_numbers=include_line_numbers,
            pages=pages,
            byte_offset=byte_offset,
            byte_limit=byte_limit,
            max_tokens=ctx.read_token_budget,
        )

    @mcp.tool(
        name="code_map_symbols",
        title="Code Map Symbols",
        annotations=READ_ONLY_TOOL,
        description=(
            "Scan a path for lightweight symbol definitions. Python uses ast for classes, "
            "functions, methods, and async functions; TypeScript/JavaScript use small regex "
            "patterns for classes, functions, and const definitions. Use before edits or "
            "reviews to find implementation entry points and candidate files_in_scope. "
            "This is not an LSP."
        ),
    )
    def code_map_symbols(
        path: Annotated[
            str,
            Field(description="File or directory to scan. Relative paths resolve from workspace root."),
        ] = ".",
        language: Annotated[
            Literal["python", "typescript", "javascript"],
            Field(description="Source language to scan: python, typescript, or javascript."),
        ] = "python",
        limit: Annotated[
            int,
            Field(description="Maximum number of symbols to return.", ge=1),
        ] = 500,
        offset: Annotated[int, Field(description="Number of symbols to skip for pagination.", ge=0)] = 0,
    ) -> dict[str, object]:
        target = resolve_path(path, ctx.workspace_root)
        return code_map_symbols_impl(
            path=target,
            language=language,
            limit=limit,
            offset=offset,
            max_tokens=ctx.tool_output_token_budget,
        )

    @mcp.tool(
        name="code_map_references",
        title="Code Map References",
        annotations=READ_ONLY_TOOL,
        description=(
            "Find bounded text references for a symbol under a path using identifier word "
            "boundaries. Results include file, line, and text snippets; matches are textual, "
            "not AST-precise references. Use before changing a function/class to estimate "
            "impact; definition lines may also appear."
        ),
    )
    def code_map_references(
        symbol: Annotated[
            str,
            Field(description="Identifier or symbol text to search for using word-boundary matching."),
        ],
        path: Annotated[
            str,
            Field(description="File or directory to search. Relative paths resolve from workspace root."),
        ] = ".",
        glob: Annotated[
            str | None,
            Field(description="Optional file glob filter such as '*.py' or '*.ts'."),
        ] = "*.py",
        limit: Annotated[
            int,
            Field(description="Maximum number of references to return.", ge=1),
        ] = 200,
        offset: Annotated[int, Field(description="Number of references to skip for pagination.", ge=0)] = 0,
    ) -> dict[str, object]:
        target = resolve_path(path, ctx.workspace_root)
        return code_map_references_impl(
            path=target,
            symbol=symbol,
            glob_pattern=glob,
            limit=limit,
            offset=offset,
            max_tokens=ctx.tool_output_token_budget,
        )

    @mcp.tool(
        name="code_map_imports",
        title="Code Map Imports",
        annotations=READ_ONLY_TOOL,
        description=(
            "Extract lightweight import relationships from a file or directory. Python uses ast; "
            "TypeScript/JavaScript use regex for import-from, side-effect imports, and require(). "
            "Use during review or refactors to inspect module boundaries and dependency direction."
        ),
    )
    def code_map_imports(
        path: Annotated[
            str,
            Field(description="File or directory to scan. Relative paths resolve from workspace root."),
        ] = ".",
        language: Annotated[
            Literal["python", "typescript", "javascript"],
            Field(description="Source language to scan: python, typescript, or javascript."),
        ] = "python",
        limit: Annotated[
            int,
            Field(description="Maximum number of per-file import entries to return.", ge=1),
        ] = 500,
        offset: Annotated[int, Field(description="Number of import-bearing files to skip.", ge=0)] = 0,
    ) -> dict[str, object]:
        target = resolve_path(path, ctx.workspace_root)
        return code_map_imports_impl(
            path=target,
            language=language,
            limit=limit,
            offset=offset,
            max_tokens=ctx.tool_output_token_budget,
        )

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
        name="replace",
        title="Batch Replace",
        annotations=LOCAL_WRITE_TOOL,
        description=(
            "Deterministic mechanical replacement across one or more files. Each operation "
            "contains path, rules, optional expected_revision, and optional encoding. Supports "
            "literal or regex rules, dry_run, a batch-wide max_replacements guard, cross-process "
            "locks, CAS checks, and same-directory atomic writes while preserving BOM, newline "
            "style, encoding, and permissions. Use apply_patch for semantic code edits."
        ),
    )
    def replace(
        operations: Annotated[
            list[dict[str, Any]],
            Field(
                description=(
                    "Replacement operations. Each object requires path and rules=[{pattern, "
                    "replacement, literal?, count?, ignore_case?, multiline?, dot_all?}]; "
                    "expected_revision and encoding are optional."
                )
            ),
        ],
        dry_run: Annotated[
            bool,
            Field(description="Plan and validate replacements without modifying files."),
        ] = False,
        max_replacements: Annotated[
            int,
            Field(description="Batch-wide replacement ceiling; the whole batch is rejected if exceeded.", ge=1),
        ] = 10000,
    ) -> dict[str, object]:
        resolved: list[dict[str, Any]] = []
        for operation in operations:
            if not isinstance(operation, dict) or not isinstance(operation.get("path"), str):
                return {
                    "success": False,
                    "error": {
                        "code": "invalid_arguments",
                        "message": "Each operation requires a string path.",
                    },
                }
            item = dict(operation)
            item["path"] = resolve_path(str(operation["path"]), ctx.workspace_root)
            resolved.append(item)
        return replace_files_impl(
            resolved,
            dry_run=dry_run,
            max_replacements=max_replacements,
        )

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
        "read": read,
        "code_map_symbols": code_map_symbols,
        "code_map_references": code_map_references,
        "code_map_imports": code_map_imports,
        "write_file": write_file,
        "replace": replace,
        "apply_patch": apply_patch,
    }
