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
    OBSIDIAN_HOST,
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
from .obsidian import ObsidianClient, ObsidianConfig, fail as obsidian_fail, ok as obsidian_ok
from .patching import apply_patch as apply_patch_impl
from . import session
from .pathing import resolve_cwd, resolve_path
from .search import glob_files as glob_files_impl
from .search import grep_files as grep_files_impl
from .shell import run_command as run_command_impl
from .skills import list_skills as list_skills_impl
from .tasks import TaskStore


# Bearer auth lives exclusively in the HTTP layer (http_compat.HTTPBearerAuthMiddleware)
# so unauthenticated clients can't even open an SSE session. The FastMCP
# protocol-layer middleware was redundant and has been removed.

store = TaskStore(STATE_DIR)
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
    "Use delegate_task only when direct tools are insufficient for a complex, long-running, or multi-file task."
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


def _current_obsidian_config() -> ObsidianConfig:
    return ObsidianConfig(
        api_key=globals().get("OBSIDIAN_API_KEY", "") or "",
        host=globals().get("OBSIDIAN_HOST", "127.0.0.1") or "127.0.0.1",
        port=int(globals().get("OBSIDIAN_PORT", 27124) or 27124),
        protocol=globals().get("OBSIDIAN_PROTOCOL", "https") or "https",
        verify_ssl=bool(globals().get("OBSIDIAN_VERIFY_SSL", False)),
        timeout_seconds=int(globals().get("OBSIDIAN_TIMEOUT_SECONDS", 10) or 10),
    )


def _obsidian_client() -> ObsidianClient:
    return ObsidianClient(_current_obsidian_config())


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
        "obsidian": {
            "configured": bool((globals().get("OBSIDIAN_API_KEY", "") or "").strip()),
            "base_url": _current_obsidian_config().base_url,
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



@mcp.tool(
    name="obsidian_status",
    title="Obsidian Status",
    annotations=READ_ONLY_TOOL,
    description="Check whether the Obsidian Local REST API is configured and reachable.",
)
def obsidian_status() -> dict[str, object]:
    config = _current_obsidian_config()
    data: dict[str, object] = {
        "configured": bool(config.api_key.strip()),
        "base_url": config.base_url,
        "host": config.host,
        "port": config.port,
        "protocol": config.protocol,
    }
    if not config.api_key.strip():
        return {"success": False, "data": data, "error": {"code": "obsidian_not_configured", "message": "Set OBSIDIAN_API_KEY in .env after enabling the Obsidian Local REST API plugin."}}
    try:
        data["probe"] = _obsidian_client().status()
        return obsidian_ok(data)
    except Exception as exc:
        return obsidian_fail(exc)


@mcp.tool(
    name="obsidian_list_files_in_vault",
    title="Obsidian List Files In Vault",
    annotations=READ_ONLY_TOOL,
    description="List files and directories in the root of the Obsidian vault via the Local REST API.",
)
def obsidian_list_files_in_vault() -> dict[str, object]:
    try:
        return obsidian_ok(_obsidian_client().list_files_in_vault())
    except Exception as exc:
        return obsidian_fail(exc)


@mcp.tool(
    name="obsidian_list_files_in_dir",
    title="Obsidian List Files In Directory",
    annotations=READ_ONLY_TOOL,
    description="List files and directories in a vault-relative Obsidian directory.",
)
def obsidian_list_files_in_dir(dirpath: str) -> dict[str, object]:
    try:
        return obsidian_ok(_obsidian_client().list_files_in_dir(dirpath))
    except Exception as exc:
        return obsidian_fail(exc)


@mcp.tool(
    name="obsidian_get_file_contents",
    title="Obsidian Get File Contents",
    annotations=READ_ONLY_TOOL,
    description="Read a single Obsidian note/file by vault-relative path.",
)
def obsidian_get_file_contents(filepath: str) -> dict[str, object]:
    try:
        return obsidian_ok(_obsidian_client().get_file_contents(filepath), filepath=filepath)
    except Exception as exc:
        return obsidian_fail(exc)


@mcp.tool(
    name="obsidian_batch_get_file_contents",
    title="Obsidian Batch Get File Contents",
    annotations=READ_ONLY_TOOL,
    description="Read multiple Obsidian files by vault-relative paths; individual failures are returned per item.",
)
def obsidian_batch_get_file_contents(filepaths: list[str]) -> dict[str, object]:
    try:
        return obsidian_ok(_obsidian_client().batch_get_file_contents(filepaths))
    except Exception as exc:
        return obsidian_fail(exc)


@mcp.tool(
    name="obsidian_simple_search",
    title="Obsidian Simple Search",
    annotations=READ_ONLY_TOOL,
    description="Search Obsidian notes for plain text using the Local REST API simple search endpoint.",
)
def obsidian_simple_search(query: str, context_length: int = 100) -> dict[str, object]:
    try:
        return obsidian_ok(_obsidian_client().simple_search(query, context_length))
    except Exception as exc:
        return obsidian_fail(exc)


@mcp.tool(
    name="obsidian_complex_search",
    title="Obsidian Complex Search",
    annotations=READ_ONLY_TOOL,
    description="Run an Obsidian Local REST API JsonLogic search query for tags, paths, regex, or content conditions.",
)
def obsidian_complex_search(query: dict[str, object]) -> dict[str, object]:
    try:
        return obsidian_ok(_obsidian_client().complex_search(query))
    except Exception as exc:
        return obsidian_fail(exc)


@mcp.tool(
    name="obsidian_search_by_tag",
    title="Obsidian Search By Tag",
    annotations=READ_ONLY_TOOL,
    description="Find notes carrying a specific parsed Obsidian tag. Pass tag without '#'; optionally scope to a directory.",
)
def obsidian_search_by_tag(tag: str, dirpath: str | None = None) -> dict[str, object]:
    try:
        return obsidian_ok(_obsidian_client().search_by_tag(tag, dirpath))
    except Exception as exc:
        return obsidian_fail(exc)


@mcp.tool(
    name="obsidian_get_frontmatter",
    title="Obsidian Get Frontmatter",
    annotations=READ_ONLY_TOOL,
    description="Return parsed YAML frontmatter for an Obsidian note as JSON.",
)
def obsidian_get_frontmatter(filepath: str) -> dict[str, object]:
    try:
        return obsidian_ok(_obsidian_client().get_frontmatter(filepath), filepath=filepath)
    except Exception as exc:
        return obsidian_fail(exc)


@mcp.tool(
    name="obsidian_append_content",
    title="Obsidian Append Content",
    annotations=LOCAL_WRITE_TOOL,
    description="Append markdown content to a new or existing Obsidian file.",
)
def obsidian_append_content(filepath: str, content: str) -> dict[str, object]:
    try:
        _obsidian_client().append_content(filepath, content)
        return obsidian_ok(filepath=filepath, message=f"Appended content to {filepath}")
    except Exception as exc:
        return obsidian_fail(exc)


@mcp.tool(
    name="obsidian_patch_content",
    title="Obsidian Patch Content",
    annotations=LOCAL_WRITE_TOOL,
    description="Patch an Obsidian note relative to a heading, block reference, or frontmatter field using append/prepend/replace.",
)
def obsidian_patch_content(filepath: str, operation: str, target_type: str, target: str, content: str) -> dict[str, object]:
    try:
        _obsidian_client().patch_content(filepath, operation, target_type, target, content)
        return obsidian_ok(filepath=filepath, message=f"Patched content in {filepath}")
    except Exception as exc:
        return obsidian_fail(exc)


@mcp.tool(
    name="obsidian_put_content",
    title="Obsidian Put Content",
    annotations=LOCAL_WRITE_TOOL,
    description="Create or completely overwrite an Obsidian file. Prefer append/patch for non-destructive edits.",
)
def obsidian_put_content(filepath: str, content: str) -> dict[str, object]:
    try:
        _obsidian_client().put_content(filepath, content)
        return obsidian_ok(filepath=filepath, message=f"Wrote content to {filepath}")
    except Exception as exc:
        return obsidian_fail(exc)


@mcp.tool(
    name="obsidian_delete_file",
    title="Obsidian Delete File",
    annotations=LOCAL_WRITE_TOOL,
    description="Delete a vault-relative Obsidian file or directory. Requires confirm=true.",
)
def obsidian_delete_file(filepath: str, confirm: bool = False) -> dict[str, object]:
    if not confirm:
        return {"success": False, "error": {"code": "confirmation_required", "message": "Set confirm=true to delete an Obsidian file."}}
    try:
        _obsidian_client().delete_file(filepath)
        return obsidian_ok(filepath=filepath, message=f"Deleted {filepath}")
    except Exception as exc:
        return obsidian_fail(exc)


@mcp.tool(
    name="obsidian_get_periodic_note",
    title="Obsidian Get Periodic Note",
    annotations=READ_ONLY_TOOL,
    description="Get the current daily/weekly/monthly/quarterly/yearly Obsidian periodic note.",
)
def obsidian_get_periodic_note(period: str, note_type: str = "content") -> dict[str, object]:
    if period not in {"daily", "weekly", "monthly", "quarterly", "yearly"}:
        return {"success": False, "error": {"code": "invalid_period", "message": "period must be daily, weekly, monthly, quarterly, or yearly."}}
    if note_type not in {"content", "metadata"}:
        return {"success": False, "error": {"code": "invalid_note_type", "message": "note_type must be content or metadata."}}
    try:
        return obsidian_ok(_obsidian_client().get_periodic_note(period, note_type), period=period, note_type=note_type)
    except Exception as exc:
        return obsidian_fail(exc)


@mcp.tool(
    name="obsidian_get_recent_periodic_notes",
    title="Obsidian Get Recent Periodic Notes",
    annotations=READ_ONLY_TOOL,
    description="Get recent Obsidian periodic notes for a period, optionally including content.",
)
def obsidian_get_recent_periodic_notes(period: str, limit: int = 5, include_content: bool = False) -> dict[str, object]:
    if period not in {"daily", "weekly", "monthly", "quarterly", "yearly"}:
        return {"success": False, "error": {"code": "invalid_period", "message": "period must be daily, weekly, monthly, quarterly, or yearly."}}
    try:
        return obsidian_ok(_obsidian_client().get_recent_periodic_notes(period, limit, include_content))
    except Exception as exc:
        return obsidian_fail(exc)


@mcp.tool(
    name="obsidian_get_recent_changes",
    title="Obsidian Get Recent Changes",
    annotations=READ_ONLY_TOOL,
    description="Get recently modified Obsidian files using a Dataview DQL query through Local REST API.",
)
def obsidian_get_recent_changes(limit: int = 10, days: int = 90) -> dict[str, object]:
    try:
        return obsidian_ok(_obsidian_client().get_recent_changes(limit, days))
    except Exception as exc:
        return obsidian_fail(exc)

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
