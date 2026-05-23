from __future__ import annotations

import argparse
import os
import re

from fastmcp import FastMCP
import uvicorn

from .http_compat import build_http_compat_app

from .config import (
    APP_NAME,
    AUTH_MODE,
    AUTH_TOKEN,
    CLAUDE_COMMAND,
    CODEX_COMMAND,
    COMMAND_TIMEOUT,
    DEBUG_MCP_LOGGING,
    DELEGATE_TIMEOUT,
    GRACEFUL_SHUTDOWN_SECONDS,
    HOST,
    OAUTH_LOGIN_TOKEN,
    OAUTH_SCOPES,
    OAUTH_TOKEN_TTL_SECONDS,
    OBSIDIAN_API_KEY,
    ENABLE_OBSIDIAN,
    OBSIDIAN_HOST,
    OBSIDIAN_MCP_URL,
    OBSIDIAN_PORT,
    OBSIDIAN_PROTOCOL,
    OBSIDIAN_TIMEOUT_SECONDS,
    OBSIDIAN_VERIFY_SSL,
    PORT,
    PUBLIC_BASE_URL,
    STATE_DIR,
    WORKSPACE_ROOT,
    ensure_runtime_directories,
)
from .executors import ExecutorRegistry
from .files import list_files as list_files_impl
from .files import read_file as read_file_impl
from .files import read_files as read_files_impl
from .files import write_file as write_file_impl
from .gitops import git_blame as git_blame_impl
from .gitops import git_commit as git_commit_impl
from .gitops import git_diff as git_diff_impl
from .gitops import git_log as git_log_impl
from .gitops import git_show as git_show_impl
from .gitops import git_status as git_status_impl
from .oauth import OAuthRuntimeConfig
from .obsidian import ObsidianMCPConfig, call_native_tool as obsidian_call_native_tool, list_native_tools as obsidian_list_native_tools, proxy_error as obsidian_proxy_error
from .patching import apply_patch as apply_patch_impl
from . import session
from .pathing import resolve_cwd, resolve_path
from .search import glob_files as glob_files_impl
from .search import grep_files as grep_files_impl
from .shell import run_command as run_command_impl
from .skills import list_skills as list_skills_impl
from .taskboard import TaskBoardError, TaskBoardStore
from .tasks import TaskStore


# Bearer auth lives exclusively in the HTTP layer (http_compat.HTTPBearerAuthMiddleware)
# so unauthenticated clients can't even open an SSE session. The FastMCP
# protocol-layer middleware was redundant and has been removed.

store = TaskStore(STATE_DIR)
taskboard_store = TaskBoardStore(STATE_DIR)
registry = ExecutorRegistry(
    store=store,
    codex_command=CODEX_COMMAND,
    claude_command=CLAUDE_COMMAND,
)

MCP_INSTRUCTIONS = (
    "Use direct tools first for normal tasks. Prioritize apply_patch/write_file for edits and "
    "run_command_stream/wait_task for long-running shell work. "
    "Use search/read_text for focused repo discovery and reading, not as a substitute for every shell step. "
    "Use git_* only when the current cwd is actually inside a git repository. "
    "Use delegate_task only when direct tools are insufficient for a complex, long-running, or multi-file task. "
    "Use taskboard_* for board-level tracking of user-decomposed subtasks; MCP does not decompose tasks automatically."
)

mcp = FastMCP(
    APP_NAME,
    instructions=MCP_INSTRUCTIONS,
)


def _current_auth_token() -> str:
    # Resolved via module globals so tests that monkeypatch ``AUTH_TOKEN`` on
    # this module (and runtime overrides) are honored per-request.
    return globals().get("AUTH_TOKEN", "") or ""


def _current_oauth_config() -> OAuthRuntimeConfig:
    return OAuthRuntimeConfig(
        auth_mode=globals().get("AUTH_MODE", "") or "",
        auth_token=_current_auth_token(),
        public_base_url=globals().get("PUBLIC_BASE_URL", "") or "",
        state_dir=globals().get("STATE_DIR", STATE_DIR),
        oauth_login_token=globals().get("OAUTH_LOGIN_TOKEN", "") or "",
        oauth_scopes=tuple(globals().get("OAUTH_SCOPES", ("local-ops",)) or ("local-ops",)),
        oauth_token_ttl_seconds=int(globals().get("OAUTH_TOKEN_TTL_SECONDS", 86400) or 86400),
    )


def _current_debug_mcp_logging() -> bool:
    return bool(globals().get("DEBUG_MCP_LOGGING", False))


def _current_obsidian_config() -> ObsidianMCPConfig:
    return ObsidianMCPConfig(
        api_key=globals().get("OBSIDIAN_API_KEY", "") or "",
        host=globals().get("OBSIDIAN_HOST", "127.0.0.1") or "127.0.0.1",
        port=int(globals().get("OBSIDIAN_PORT", 27124) or 27124),
        protocol=globals().get("OBSIDIAN_PROTOCOL", "https") or "https",
        url=globals().get("OBSIDIAN_MCP_URL", "") or "",
        verify_ssl=bool(globals().get("OBSIDIAN_VERIFY_SSL", False)),
        timeout_seconds=int(globals().get("OBSIDIAN_TIMEOUT_SECONDS", 10) or 10),
    )


async def _proxy_obsidian_tool(tool_name: str, arguments: dict[str, object] | None = None) -> dict[str, object]:
    try:
        return await obsidian_call_native_tool(_current_obsidian_config(), tool_name, arguments or {})
    except Exception as exc:
        return obsidian_proxy_error(exc)


READ_ONLY_TOOL = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}

LOCAL_STATE_TOOL = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}

LOCAL_WRITE_TOOL = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": False,
    "openWorldHint": False,
}

OPEN_WORLD_WRITE_TOOL = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": False,
    "openWorldHint": True,
}


@mcp.tool(
    name="list_skills",
    title="List Skills",
    annotations=READ_ONLY_TOOL,
    description=(
        "List project and global agent skills as lightweight summaries. "
        "Returns skill name, description, preferred path, and source locations. "
        "Use namespace ('agents' | 'codex' | 'claude') to scope, name_pattern "
        "(fnmatch, e.g. 'git-*') to filter by skill name, and "
        "description_max_length to cap long descriptions for index-style scans."
    ),
)
def list_skills(
    include_project: bool = True,
    include_global: bool = True,
    namespace: str | None = None,
    name_pattern: str | None = None,
    description_max_length: int | None = None,
) -> dict[str, object]:
    return list_skills_impl(
        workspace_root=WORKSPACE_ROOT,
        include_project=include_project,
        include_global=include_global,
        namespace=namespace,
        name_pattern=name_pattern,
        description_max_length=description_max_length,
    )


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
    target = resolve_path(path or ".", WORKSPACE_ROOT)
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
    target = resolve_path(path or ".", WORKSPACE_ROOT)

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
        target = resolve_path(path, WORKSPACE_ROOT)
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

    targets = [resolve_path(item, WORKSPACE_ROOT) for item in (paths or [])]
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
    target = resolve_path(path, WORKSPACE_ROOT)
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
        workspace_root=WORKSPACE_ROOT,
        dry_run=dry_run,
        validate_only=validate_only,
        return_diff=return_diff,
    )


@mcp.tool(
    name="server_info",
    title="Server Info",
    annotations=READ_ONLY_TOOL,
    description=(
        "Return server metadata: app name, host/port, workspace root, state dir, "
        "timeouts, auth mode, and the list of registered tools. Useful as a first "
        "call to confirm which bridge you are connected to and what it can do."
    ),
)
async def server_info() -> dict[str, object]:
    list_tools = getattr(mcp, "_list_tools")
    try:
        registered = await list_tools()
    except TypeError:
        # fastmcp 2.14 requires a context arg; None works for server-side listing.
        registered = await list_tools(None)
    tools = sorted(tool.name for tool in registered)
    session_cwd = session.get_default_cwd()
    return {
        "success": True,
        "app_name": APP_NAME,
        "host": HOST,
        "port": PORT,
        "workspace_root": str(WORKSPACE_ROOT),
        "session_cwd": str(session_cwd) if session_cwd else None,
        "state_dir": str(STATE_DIR),
        "command_timeout_seconds": COMMAND_TIMEOUT,
        "delegate_timeout_seconds": DELEGATE_TIMEOUT,
        "auth": _current_oauth_config().normalized_auth_mode,
        "debug_mcp_logging": bool(DEBUG_MCP_LOGGING),
        "codex_command": CODEX_COMMAND,
        "claude_command": CLAUDE_COMMAND,
        "obsidian_proxy": {
            "enabled": bool(globals().get("ENABLE_OBSIDIAN", False)),
            "configured": bool((globals().get("OBSIDIAN_API_KEY", "") or "").strip()),
            "mcp_url": _current_obsidian_config().mcp_url,
            "mode": "native_mcp_proxy",
            "tool_prefix": "obsidian_",
        },
        "tools": tools,
        "tool_count": len(tools),
    }


@mcp.tool(
    name="set_default_cwd",
    title="Set Default CWD",
    annotations=LOCAL_STATE_TOOL,
    description=(
        "Set the session-wide default working directory used whenever a tool call "
        "omits `cwd`. Pass null (or omit path) to clear the override and fall back to "
        "the server's workspace root. Useful when running many commands in the same "
        "repo: set it once instead of passing `cwd` on every call."
    ),
)
def set_default_cwd(path: str | None = None) -> dict[str, object]:
    if not path:
        session.set_default_cwd(None)
        return {
            "success": True,
            "session_cwd": None,
            "workspace_root": str(WORKSPACE_ROOT),
            "cleared": True,
        }
    target = resolve_path(path, WORKSPACE_ROOT)
    if not target.exists():
        return {
            "success": False,
            "error": {
                "code": "cwd_not_found",
                "message": f"Path does not exist: {target}",
            },
            "path": str(target),
        }
    if not target.is_dir():
        return {
            "success": False,
            "error": {
                "code": "cwd_not_directory",
                "message": f"Path is not a directory: {target}",
            },
            "path": str(target),
        }
    session.set_default_cwd(target)
    return {
        "success": True,
        "session_cwd": str(target),
        "workspace_root": str(WORKSPACE_ROOT),
        "cleared": False,
    }


@mcp.tool(
    name="get_default_cwd",
    title="Get Default CWD",
    annotations=READ_ONLY_TOOL,
    description=(
        "Return the currently active default working directory and whether it comes "
        "from the session override (set_default_cwd) or from the server's workspace root."
    ),
)
def get_default_cwd() -> dict[str, object]:
    session_cwd = session.get_default_cwd()
    effective = session_cwd if session_cwd is not None else WORKSPACE_ROOT
    return {
        "success": True,
        "session_cwd": str(session_cwd) if session_cwd else None,
        "workspace_root": str(WORKSPACE_ROOT),
        "effective_cwd": str(effective),
        "source": "session" if session_cwd else "workspace_root",
    }


@mcp.tool(
    name="git_status",
    title="Git Status",
    annotations=READ_ONLY_TOOL,
    description="Return structured git status for the repository at cwd or the current workspace root.",
)
def git_status(cwd: str | None = None) -> dict[str, object]:
    resolved_cwd = resolve_cwd(cwd, WORKSPACE_ROOT)
    return git_status_impl(cwd=resolved_cwd)


@mcp.tool(
    name="git_diff",
    title="Git Diff",
    annotations=READ_ONLY_TOOL,
    description=(
        "Return git diff output plus per-file diffs with added/removed counts. "
        "Each file is truncated independently to per_file_max_bytes so a single huge "
        "file does not hide changes in other files."
    ),
)
def git_diff(
    cwd: str | None = None,
    staged: bool = False,
    paths: list[str] | None = None,
    max_bytes: int = 65536,
    per_file_max_bytes: int = 16384,
) -> dict[str, object]:
    resolved_cwd = resolve_cwd(cwd, WORKSPACE_ROOT)
    return git_diff_impl(
        cwd=resolved_cwd,
        staged=staged,
        paths=paths,
        max_bytes=max_bytes,
        per_file_max_bytes=per_file_max_bytes,
    )


@mcp.tool(
    name="git_commit",
    title="Git Commit",
    annotations=LOCAL_WRITE_TOOL,
    description=(
        "Create a git commit for staged changes, selected paths, or all current changes. "
        "Supports amend (rewrite HEAD), allow_empty (commit without changes), custom author, "
        "sign_off (append Signed-off-by trailer), and dry_run preview."
    ),
)
def git_commit(
    message: str,
    cwd: str | None = None,
    paths: list[str] | None = None,
    stage_all: bool = False,
    amend: bool = False,
    allow_empty: bool = False,
    author: str | None = None,
    sign_off: bool = False,
    dry_run: bool = False,
) -> dict[str, object]:
    resolved_cwd = resolve_cwd(cwd, WORKSPACE_ROOT)
    return git_commit_impl(
        cwd=resolved_cwd,
        message=message,
        paths=paths,
        stage_all=stage_all,
        amend=amend,
        allow_empty=allow_empty,
        author=author,
        sign_off=sign_off,
        dry_run=dry_run,
    )


@mcp.tool(
    name="git_log",
    title="Git Log",
    annotations=READ_ONLY_TOOL,
    description="Return recent git commits for the repository at cwd.",
)
def git_log(cwd: str | None = None, limit: int = 10) -> dict[str, object]:
    resolved_cwd = resolve_cwd(cwd, WORKSPACE_ROOT)
    return git_log_impl(cwd=resolved_cwd, limit=limit)


@mcp.tool(
    name="git_show",
    title="Git Show",
    annotations=READ_ONLY_TOOL,
    description=(
        "Show metadata + per-file diff for a commit or any git ref (defaults to HEAD). "
        "Useful for inspecting a specific commit without shelling out."
    ),
)
def git_show(
    ref: str = "HEAD",
    cwd: str | None = None,
    max_bytes: int = 65536,
    per_file_max_bytes: int = 16384,
) -> dict[str, object]:
    resolved_cwd = resolve_cwd(cwd, WORKSPACE_ROOT)
    return git_show_impl(
        cwd=resolved_cwd,
        ref=ref,
        max_bytes=max_bytes,
        per_file_max_bytes=per_file_max_bytes,
    )


@mcp.tool(
    name="git_blame",
    title="Git Blame",
    annotations=READ_ONLY_TOOL,
    description=(
        "Return per-line blame info (commit, author, summary, content) for a file. "
        "Restrict to a line range via start_line / end_line."
    ),
)
def git_blame(
    path: str,
    cwd: str | None = None,
    ref: str | None = None,
    start_line: int | None = None,
    end_line: int | None = None,
) -> dict[str, object]:
    resolved_cwd = resolve_cwd(cwd, WORKSPACE_ROOT)
    return git_blame_impl(
        cwd=resolved_cwd,
        path=path,
        ref=ref,
        start_line=start_line,
        end_line=end_line,
    )


@mcp.tool(
    name="run_command",
    title="Run Command",
    annotations=OPEN_WORLD_WRITE_TOOL,
    description="Run a local shell command now or queue it as a background task for wait_task/get_task polling.",
)
def run_command(
    command: str,
    cwd: str | None = None,
    timeout: int | None = None,
    run_in_background: bool = False,
) -> dict[str, object]:
    resolved_cwd = resolve_cwd(cwd, WORKSPACE_ROOT)
    effective_timeout = timeout if timeout is not None else COMMAND_TIMEOUT
    if run_in_background:
        return registry.submit_command(
            command=command,
            cwd=resolved_cwd,
            timeout=effective_timeout,
        )
    return run_command_impl(
        command=command,
        cwd=resolved_cwd,
        timeout=effective_timeout,
    )


@mcp.tool(
    name="run_command_stream",
    title="Run Command Stream",
    annotations=OPEN_WORLD_WRITE_TOOL,
    description=(
        "Start a shell command in background and return task_id immediately for "
        "stream-like polling via get_task/wait_task."
    ),
)
def run_command_stream(
    command: str,
    cwd: str | None = None,
    timeout: int | None = None,
) -> dict[str, object]:
    resolved_cwd = resolve_cwd(cwd, WORKSPACE_ROOT)
    effective_timeout = timeout if timeout is not None else COMMAND_TIMEOUT
    queued = registry.submit_command(
        command=command,
        cwd=resolved_cwd,
        timeout=effective_timeout,
    )
    queued["stream_mode"] = "task-polling"
    queued["next"] = "call get_task(task_id) or wait_task(task_id)"
    return queued


@mcp.tool(
    name="delegate_task",
    title="Delegate Task",
    annotations=OPEN_WORLD_WRITE_TOOL,
    description=(
        "Fallback only. Use this when direct tools are insufficient for a complex, long-running, or "
        "multi-file task. Supported executors: auto, codex, claude-code. "
        "Optionally provide output_schema and parse_structured_output=true to "
        "capture JSON output as structured_output."
    ),
)
def delegate_task(
    task: str | None = None,
    goal: str | None = None,
    executor: str = "auto",
    cwd: str | None = None,
    context_files: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
    verification_commands: list[str] | None = None,
    commit_mode: str = "allowed",
    timeout: int | None = None,
    output_schema: dict[str, object] | None = None,
    parse_structured_output: bool = True,
) -> dict[str, object]:
    resolved_cwd = resolve_cwd(cwd, WORKSPACE_ROOT)
    return registry.submit(
        task=task,
        goal=goal,
        executor=executor,
        cwd=resolved_cwd,
        timeout=timeout if timeout is not None else DELEGATE_TIMEOUT,
        context_files=context_files,
        acceptance_criteria=acceptance_criteria,
        verification_commands=verification_commands,
        commit_mode=commit_mode,
        output_schema=output_schema,
        parse_structured_output=parse_structured_output,
    )


@mcp.tool(
    name="get_task",
    title="Get Task",
    annotations=READ_ONLY_TOOL,
    description="Get the current status and output tail for a delegated or background shell task.",
)
def get_task(task_id: str) -> dict[str, object]:
    return registry.get(task_id)


@mcp.tool(
    name="wait_task",
    title="Wait Task",
    annotations=READ_ONLY_TOOL,
    description="Wait for a delegated or background shell task to finish or until timeout, then return its latest status and output tail.",
)
def wait_task(task_id: str, timeout: float = 30, poll_interval: float = 0.5) -> dict[str, object]:
    return registry.wait(task_id, timeout=timeout, poll_interval=poll_interval)


@mcp.tool(
    name="cancel_task",
    title="Cancel Task",
    annotations=LOCAL_WRITE_TOOL,
    description="Cancel a delegated or background shell task if it is still running.",
)
def cancel_task(task_id: str) -> dict[str, object]:
    return registry.cancel(task_id)


@mcp.tool(
    name="purge_tasks",
    title="Purge Tasks",
    annotations=LOCAL_WRITE_TOOL,
    description=(
        "Delete old task metadata/log directories under STATE_DIR/tasks. "
        "Defaults to 7 days; supports dry_run preview."
    ),
)
def purge_tasks(older_than_hours: float = 24 * 7, dry_run: bool = False) -> dict[str, object]:
    return store.purge_tasks(
        older_than_seconds=max(float(older_than_hours), 0.0) * 3600.0,
        dry_run=dry_run,
    )


def _taskboard_error(exc: TaskBoardError) -> dict[str, object]:
    return exc.to_payload()


def _taskboard_specs(
    *,
    tasks: list[dict[str, object]] | None = None,
    title: str | None = None,
    task: str | None = None,
    cwd: str | None = None,
    executor: str | None = None,
    context_files: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
    verification_commands: list[str] | None = None,
    notes: str | None = None,
) -> list[dict[str, object]]:
    if tasks is not None:
        return tasks
    if title is None or task is None:
        raise TaskBoardError(
            "invalid_arguments",
            "Provide either tasks=[TaskSpec, ...] or both title and task.",
        )
    spec: dict[str, object] = {
        "title": title,
        "task": task,
    }
    if cwd is not None:
        spec["cwd"] = cwd
    if executor is not None:
        spec["executor"] = executor
    if context_files is not None:
        spec["context_files"] = context_files
    if acceptance_criteria is not None:
        spec["acceptance_criteria"] = acceptance_criteria
    if verification_commands is not None:
        spec["verification_commands"] = verification_commands
    if notes is not None:
        spec["notes"] = notes
    return [spec]


@mcp.tool(
    name="taskboard_create",
    title="Create TaskBoard",
    annotations=LOCAL_WRITE_TOOL,
    description=(
        "Create a persistent lightweight TaskBoard with pending TaskSpec items. "
        "This stores local board state only; it never delegates automatically. "
        "Defaults: executor=auto, max_parallel=2, worktree_mode=per_task, base_ref=HEAD, branch_prefix=taskboard."
    ),
)
def taskboard_create(
    tasks: list[dict[str, object]] | None = None,
    title: str | None = None,
    original_request: str | None = None,
    cwd: str | None = None,
    executor: str = "auto",
    max_parallel: int = 2,
    worktree_mode: str = "per_task",
    base_ref: str = "HEAD",
    branch_prefix: str = "taskboard",
    notes: str | None = None,
) -> dict[str, object]:
    try:
        resolved_cwd = resolve_cwd(cwd, WORKSPACE_ROOT)
        return taskboard_store.create(
            tasks=tasks or [],
            cwd=str(resolved_cwd),
            workspace_root=WORKSPACE_ROOT,
            title=title,
            executor=executor,
            max_parallel=max_parallel,
            worktree_mode=worktree_mode,
            base_ref=base_ref,
            branch_prefix=branch_prefix,
            notes=notes,
            original_request=original_request,
        )
    except TaskBoardError as exc:
        return _taskboard_error(exc)


@mcp.tool(
    name="taskboard_add_task",
    title="Add TaskBoard Task",
    annotations=LOCAL_WRITE_TOOL,
    description=(
        "Append pending TaskSpec items to an existing TaskBoard. "
        "Pass tasks=[...] for batch append, or title/task plus optional cwd/executor/context fields for one task."
    ),
)
def taskboard_add_task(
    board_id: str,
    tasks: list[dict[str, object]] | None = None,
    title: str | None = None,
    task: str | None = None,
    cwd: str | None = None,
    executor: str | None = None,
    context_files: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
    verification_commands: list[str] | None = None,
    notes: str | None = None,
) -> dict[str, object]:
    try:
        specs = _taskboard_specs(
            tasks=tasks,
            title=title,
            task=task,
            cwd=cwd,
            executor=executor,
            context_files=context_files,
            acceptance_criteria=acceptance_criteria,
            verification_commands=verification_commands,
            notes=notes,
        )
        return taskboard_store.add_tasks(
            board_id=board_id,
            tasks=specs,
            workspace_root=WORKSPACE_ROOT,
        )
    except TaskBoardError as exc:
        return _taskboard_error(exc)


@mcp.tool(
    name="taskboard_delegate",
    title="Delegate TaskBoard Tasks",
    annotations=OPEN_WORLD_WRITE_TOOL,
    description=(
        "Submit up to max_parallel pending TaskBoard subtasks to delegate_task. "
        "With worktree_mode=per_task, creates and persists a unique git worktree/branch per submitted subtask, "
        "and delegates with cwd set to that worktree. Supports dry_run."
    ),
)
def taskboard_delegate(
    board_id: str,
    task_ids: list[str] | None = None,
    max_parallel: int | None = None,
    dry_run: bool = False,
    timeout: int | None = None,
) -> dict[str, object]:
    try:
        return taskboard_store.delegate(
            board_id=board_id,
            registry=registry,
            timeout=timeout if timeout is not None else DELEGATE_TIMEOUT,
            task_ids=task_ids,
            max_parallel=max_parallel,
            dry_run=dry_run,
        )
    except TaskBoardError as exc:
        return _taskboard_error(exc)


@mcp.tool(
    name="taskboard_status",
    title="TaskBoard Status",
    annotations=READ_ONLY_TOOL,
    description=(
        "Return compact board counts and one-line subtask summaries immediately. "
        "Set refresh=true to update subtask status from underlying delegate task state."
    ),
)
def taskboard_status(
    board_id: str,
    refresh: bool = True,
    event_limit: int = 10,
) -> dict[str, object]:
    try:
        return taskboard_store.status(
            board_id=board_id,
            registry=registry,
            refresh=refresh,
            event_limit=event_limit,
        )
    except TaskBoardError as exc:
        return _taskboard_error(exc)


@mcp.tool(
    name="taskboard_wait",
    title="Wait TaskBoard",
    annotations=READ_ONLY_TOOL,
    description=(
        "Wait for a TaskBoard status change, any completed subtask, or terminal board state without "
        "cancelling on timeout. Default timeout=30 seconds, poll_interval=0.5, "
        "return_on=change. Allowed return_on values: change, any_done, all_done."
    ),
)
def taskboard_wait(
    board_id: str,
    timeout: float = 30,
    poll_interval: float = 0.5,
    return_on: str = "change",
    event_limit: int = 10,
) -> dict[str, object]:
    try:
        return taskboard_store.wait(
            board_id=board_id,
            registry=registry,
            timeout=timeout,
            poll_interval=poll_interval,
            return_on=return_on,
            event_limit=event_limit,
        )
    except TaskBoardError as exc:
        return _taskboard_error(exc)


@mcp.tool(
    name="taskboard_get_task",
    title="Get TaskBoard Task",
    annotations=READ_ONLY_TOOL,
    description=(
        "Return detailed single-subtask metadata. Logs, prompt, and done_report are opt-in "
        "via include_prompt/include_done_report/include_stdout_tail/include_stderr_tail."
    ),
)
def taskboard_get_task(
    board_id: str,
    task_id: str,
    refresh: bool = True,
    include_prompt: bool = False,
    include_done_report: bool = False,
    include_stdout_tail: bool = False,
    include_stderr_tail: bool = False,
    tail_chars: int = 4000,
) -> dict[str, object]:
    try:
        return taskboard_store.get_task(
            board_id=board_id,
            task_id=task_id,
            registry=registry,
            refresh=refresh,
            include_prompt=include_prompt,
            include_done_report=include_done_report,
            include_stdout_tail=include_stdout_tail,
            include_stderr_tail=include_stderr_tail,
            tail_chars=tail_chars,
        )
    except TaskBoardError as exc:
        return _taskboard_error(exc)


@mcp.tool(
    name="taskboard_collect_results",
    title="Collect TaskBoard Results",
    annotations=READ_ONLY_TOOL,
    description=(
        "Collect compact worktree/executor result metadata for TaskBoard subtasks. "
        "Returns worktree path, branch, base/head/commit SHA, changed files, and diff summary. "
        "Full diff/log tails are omitted unless explicitly requested; this never merges or deletes worktrees."
    ),
)
def taskboard_collect_results(
    board_id: str,
    task_ids: list[str] | None = None,
    include_diff: bool = False,
    include_log_tail: bool = False,
    diff_max_bytes: int = 20000,
    tail_chars: int = 4000,
    event_limit: int = 10,
) -> dict[str, object]:
    try:
        return taskboard_store.collect_results(
            board_id=board_id,
            registry=registry,
            task_ids=task_ids,
            include_diff=include_diff,
            include_log_tail=include_log_tail,
            diff_max_bytes=diff_max_bytes,
            tail_chars=tail_chars,
            event_limit=event_limit,
        )
    except TaskBoardError as exc:
        return _taskboard_error(exc)


@mcp.tool(
    name="taskboard_cancel",
    title="Cancel TaskBoard",
    annotations=LOCAL_WRITE_TOOL,
    description=(
        "Cancel selected TaskBoard subtasks or, when task_ids is omitted, all non-terminal subtasks on the board. "
        "Running delegated subtasks are cancelled through cancel_task. Worktrees are never deleted."
    ),
)
def taskboard_cancel(
    board_id: str,
    task_ids: list[str] | None = None,
    event_limit: int = 10,
) -> dict[str, object]:
    try:
        return taskboard_store.cancel(
            board_id=board_id,
            registry=registry,
            task_ids=task_ids,
            event_limit=event_limit,
        )
    except TaskBoardError as exc:
        return _taskboard_error(exc)


@mcp.tool(
    name="taskboard_list",
    title="List TaskBoards",
    annotations=READ_ONLY_TOOL,
    description="List recent TaskBoards ordered by updated_at descending, with optional status/cwd filters.",
)
def taskboard_list(
    status: str | None = None,
    cwd: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, object]:
    try:
        resolved_cwd = str(resolve_cwd(cwd, WORKSPACE_ROOT)) if cwd else None
        return taskboard_store.list_boards(
            status=status,
            cwd=resolved_cwd,
            limit=limit,
            offset=offset,
        )
    except TaskBoardError as exc:
        return _taskboard_error(exc)


@mcp.tool(
    name="taskboard_purge",
    title="Purge TaskBoards",
    annotations=LOCAL_WRITE_TOOL,
    description=(
        "Delete stale TaskBoard metadata under STATE_DIR/taskboards/boards. "
        "This does not delete git worktrees; use dry_run to preview."
    ),
)
def taskboard_purge(older_than_hours: float = 24 * 7, dry_run: bool = False) -> dict[str, object]:
    return taskboard_store.purge(
        older_than_seconds=max(float(older_than_hours), 0.0) * 3600.0,
        dry_run=dry_run,
    )



def _obsidian_tool(*args, **kwargs):
    """Register Obsidian proxy tools only when explicitly enabled."""
    if bool(globals().get("ENABLE_OBSIDIAN", False)):
        return mcp.tool(*args, **kwargs)

    def decorator(fn):
        return fn

    return decorator


@_obsidian_tool(
    name="obsidian_vault_list",
    title="Obsidian Vault List",
    annotations=READ_ONLY_TOOL,
    description="Proxy to Obsidian native MCP tool `vault_list`: list files and subdirectories inside a vault directory.",
)
async def obsidian_vault_list(path: str = "") -> dict[str, object]:
    return await _proxy_obsidian_tool("vault_list", {"path": path})


@_obsidian_tool(
    name="obsidian_vault_read",
    title="Obsidian Vault Read",
    annotations=READ_ONLY_TOOL,
    description="Proxy to native Obsidian MCP `vault_read`: read a file's content/metadata, or a targeted heading/block/frontmatter section.",
)
async def obsidian_vault_read(
    path: str,
    targetType: str | None = None,
    target: str | None = None,
    targetDelimiter: str | None = None,
) -> dict[str, object]:
    args: dict[str, object] = {"path": path}
    if targetType is not None:
        args["targetType"] = targetType
    if target is not None:
        args["target"] = target
    if targetDelimiter is not None:
        args["targetDelimiter"] = targetDelimiter
    return await _proxy_obsidian_tool("vault_read", args)


@_obsidian_tool(
    name="obsidian_vault_write",
    title="Obsidian Vault Write",
    annotations=LOCAL_WRITE_TOOL,
    description="Proxy to native Obsidian MCP `vault_write`: create or overwrite a vault file.",
)
async def obsidian_vault_write(path: str, content: str) -> dict[str, object]:
    return await _proxy_obsidian_tool("vault_write", {"path": path, "content": content})


@_obsidian_tool(
    name="obsidian_vault_append",
    title="Obsidian Vault Append",
    annotations=LOCAL_WRITE_TOOL,
    description="Proxy to native Obsidian MCP `vault_append`: append content to a vault file, creating it if missing.",
)
async def obsidian_vault_append(path: str, content: str) -> dict[str, object]:
    return await _proxy_obsidian_tool("vault_append", {"path": path, "content": content})


@_obsidian_tool(
    name="obsidian_vault_patch",
    title="Obsidian Vault Patch",
    annotations=LOCAL_WRITE_TOOL,
    description="Proxy to native Obsidian MCP `vault_patch`: patch a heading, block reference, or frontmatter field.",
)
async def obsidian_vault_patch(
    path: str,
    targetType: str,
    target: str,
    operation: str,
    content: object,
    contentType: str | None = None,
    createTargetIfMissing: bool | None = None,
    trimTargetWhitespace: bool | None = None,
    rejectIfContentPreexists: bool | None = None,
    targetDelimiter: str | None = None,
    targetScope: str | None = None,
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
    return await _proxy_obsidian_tool("vault_patch", args)


@_obsidian_tool(
    name="obsidian_vault_delete",
    title="Obsidian Vault Delete",
    annotations=LOCAL_WRITE_TOOL,
    description="Proxy to native Obsidian MCP `vault_delete`: delete a vault file. Requires confirm=true at this bridge layer.",
)
async def obsidian_vault_delete(path: str, confirm: bool = False) -> dict[str, object]:
    if not confirm:
        return {"success": False, "error": {"code": "confirmation_required", "message": "Set confirm=true to delete an Obsidian file."}}
    return await _proxy_obsidian_tool("vault_delete", {"path": path})


@_obsidian_tool(
    name="obsidian_vault_get_document_map",
    title="Obsidian Vault Get Document Map",
    annotations=READ_ONLY_TOOL,
    description="Proxy to native Obsidian MCP `vault_get_document_map`: list headings, block references, and frontmatter fields in a file.",
)
async def obsidian_vault_get_document_map(path: str) -> dict[str, object]:
    return await _proxy_obsidian_tool("vault_get_document_map", {"path": path})


@_obsidian_tool(
    name="obsidian_active_file_get_path",
    title="Obsidian Active File Get Path",
    annotations=READ_ONLY_TOOL,
    description="Proxy to native Obsidian MCP `active_file_get_path`: return the vault path of the currently active file.",
)
async def obsidian_active_file_get_path() -> dict[str, object]:
    return await _proxy_obsidian_tool("active_file_get_path", {})


@_obsidian_tool(
    name="obsidian_periodic_note_get_path",
    title="Obsidian Periodic Note Get Path",
    annotations=LOCAL_WRITE_TOOL,
    description="Proxy to native Obsidian MCP `periodic_note_get_path`: get or create the current periodic note path.",
)
async def obsidian_periodic_note_get_path(period: str) -> dict[str, object]:
    return await _proxy_obsidian_tool("periodic_note_get_path", {"period": period})


@_obsidian_tool(
    name="obsidian_search_query",
    title="Obsidian Search Query",
    annotations=READ_ONLY_TOOL,
    description="Proxy to native Obsidian MCP `search_query`: run a JsonLogic query against note metadata.",
)
async def obsidian_search_query(query: dict[str, object]) -> dict[str, object]:
    return await _proxy_obsidian_tool("search_query", {"query": query})


@_obsidian_tool(
    name="obsidian_search_simple",
    title="Obsidian Search Simple",
    annotations=READ_ONLY_TOOL,
    description="Proxy to native Obsidian MCP `search_simple`: full-text search using Obsidian's built-in search.",
)
async def obsidian_search_simple(query: str, contextLength: float | None = None) -> dict[str, object]:
    args: dict[str, object] = {"query": query}
    if contextLength is not None:
        args["contextLength"] = contextLength
    return await _proxy_obsidian_tool("search_simple", args)


@_obsidian_tool(
    name="obsidian_tag_list",
    title="Obsidian Tag List",
    annotations=READ_ONLY_TOOL,
    description="Proxy to native Obsidian MCP `tag_list`: list all tags across the vault with usage counts.",
)
async def obsidian_tag_list() -> dict[str, object]:
    return await _proxy_obsidian_tool("tag_list", {})


@_obsidian_tool(
    name="obsidian_command_list",
    title="Obsidian Command List",
    annotations=READ_ONLY_TOOL,
    description="Proxy to native Obsidian MCP `command_list`: list registered Obsidian commands.",
)
async def obsidian_command_list() -> dict[str, object]:
    return await _proxy_obsidian_tool("command_list", {})


@_obsidian_tool(
    name="obsidian_command_execute",
    title="Obsidian Command Execute",
    annotations=LOCAL_WRITE_TOOL,
    description="Proxy to native Obsidian MCP `command_execute`: execute an Obsidian command by ID.",
)
async def obsidian_command_execute(commandId: str) -> dict[str, object]:
    return await _proxy_obsidian_tool("command_execute", {"commandId": commandId})


@_obsidian_tool(
    name="obsidian_open_file",
    title="Obsidian Open File",
    annotations=LOCAL_STATE_TOOL,
    description="Proxy to native Obsidian MCP `open_file`: open a vault file in the Obsidian UI.",
)
async def obsidian_open_file(path: str, newLeaf: bool | None = None) -> dict[str, object]:
    args: dict[str, object] = {"path": path}
    if newLeaf is not None:
        args["newLeaf"] = newLeaf
    return await _proxy_obsidian_tool("open_file", args)


@_obsidian_tool(
    name="obsidian_mcp_list_tools",
    title="Obsidian Native MCP List Tools",
    annotations=READ_ONLY_TOOL,
    description="List tools advertised by the Obsidian Local REST API plugin's native MCP server.",
)
async def obsidian_mcp_list_tools() -> dict[str, object]:
    try:
        return await obsidian_list_native_tools(_current_obsidian_config())
    except Exception as exc:
        return obsidian_proxy_error(exc)

def build_http_app():
    streamable_app = mcp.http_app(
        path="/mcp",
        transport="streamable-http",
    )
    legacy_sse_app = mcp.http_app(
        path="/mcp",
        transport="sse",
    )
    return build_http_compat_app(
        streamable_app=streamable_app,
        legacy_sse_app=legacy_sse_app,
        app_name=APP_NAME,
        mcp_path="/mcp",
        get_auth_token=_current_auth_token,
        get_oauth_config=_current_oauth_config,
        get_debug_enabled=_current_debug_mcp_logging,
        instructions=MCP_INSTRUCTIONS,
    )


class _ReadySignalServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, *, ready_fd: int | None) -> None:
        super().__init__(config)
        self._ready_fd = ready_fd

    def _emit_ready(self) -> None:
        if self._ready_fd is None:
            return
        os.write(self._ready_fd, b"ready\n")
        os.close(self._ready_fd)
        self._ready_fd = None

    def _close_ready_fd(self) -> None:
        if self._ready_fd is None:
            return
        os.close(self._ready_fd)
        self._ready_fd = None

    async def startup(self, sockets=None) -> None:
        await super().startup(sockets=sockets)
        if not self.should_exit:
            self._emit_ready()

    async def serve(self, sockets=None) -> None:
        try:
            await super().serve(sockets=sockets)
        finally:
            self._close_ready_fd()


def _consume_ready_fd() -> int | None:
    raw_value = os.environ.pop("CHATGPT_MCP_READY_FD", "").strip()
    if not raw_value:
        return None
    return int(raw_value)


def build_uvicorn_server(*, fd: int | None = None, ready_fd: int | None = None) -> uvicorn.Server:
    app = build_http_app()
    config = uvicorn.Config(
        app,
        host=HOST,
        port=PORT,
        fd=fd,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )
    return _ReadySignalServer(config, ready_fd=ready_fd)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the chatgpt-web-oauth-mcp MCP server.")
    parser.add_argument("--fd", type=int, default=None, help="Inherited listening socket fd.")
    args = parser.parse_args(argv)

    ensure_runtime_directories()
    print(f"Starting {APP_NAME} on {HOST}:{PORT}")
    print(f"workspace_root={WORKSPACE_ROOT}")
    print(f"state_dir={STATE_DIR}")
    print("transport=streamable-http")
    print("mcp_path=/mcp")
    print(f"debug_mcp_logging={DEBUG_MCP_LOGGING}")
    print(f"graceful_shutdown_seconds={GRACEFUL_SHUTDOWN_SECONDS}")

    oauth_config = _current_oauth_config()
    if oauth_config.normalized_auth_mode == "oauth":
        if not oauth_config.public_base_url:
            print(
                "WARNING: CHATGPT_MCP_PUBLIC_BASE_URL is not set; OAuth "
                "metadata will fall back to the request Host header. Set it to "
                "your public tunnel URL (e.g. https://mcp.example.com) so issuer "
                "URLs cannot be spoofed."
            )
        if not oauth_config.oauth_login_token and oauth_config.auth_token:
            print(
                "WARNING: CHATGPT_MCP_OAUTH_LOGIN_TOKEN is not set; "
                "AUTH_TOKEN is being reused as the OAuth login token. Anyone "
                "with AUTH_TOKEN can mint long-TTL OAuth access tokens. After "
                "rotating AUTH_TOKEN, also clear oauth.json[\"tokens\"] under "
                f"{STATE_DIR}/oauth.json."
            )

    server = build_uvicorn_server(fd=args.fd, ready_fd=_consume_ready_fd())
    server.run()


if __name__ == "__main__":
    main()
