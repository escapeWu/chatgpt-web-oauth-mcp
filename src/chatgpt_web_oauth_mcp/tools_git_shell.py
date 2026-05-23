from __future__ import annotations

from typing import Any

from .gitops import git_blame as git_blame_impl
from .gitops import git_commit as git_commit_impl
from .gitops import git_diff as git_diff_impl
from .gitops import git_log as git_log_impl
from .gitops import git_show as git_show_impl
from .gitops import git_status as git_status_impl
from .pathing import resolve_cwd
from .shell import run_command as run_command_impl
from .tool_context import LOCAL_WRITE_TOOL, OPEN_WORLD_WRITE_TOOL, READ_ONLY_TOOL, ToolContext


def register_git_shell_tools(mcp: Any, ctx: ToolContext) -> dict[str, object]:
    """Register git, shell, delegate, and task lifecycle tools."""

    @mcp.tool(
        name="git_status",
        title="Git Status",
        annotations=READ_ONLY_TOOL,
        description="Return structured git status for the repository at cwd or the current workspace root.",
    )
    def git_status(cwd: str | None = None) -> dict[str, object]:
        resolved_cwd = resolve_cwd(cwd, ctx.workspace_root)
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
        resolved_cwd = resolve_cwd(cwd, ctx.workspace_root)
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
        resolved_cwd = resolve_cwd(cwd, ctx.workspace_root)
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
        resolved_cwd = resolve_cwd(cwd, ctx.workspace_root)
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
        resolved_cwd = resolve_cwd(cwd, ctx.workspace_root)
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
        resolved_cwd = resolve_cwd(cwd, ctx.workspace_root)
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
        resolved_cwd = resolve_cwd(cwd, ctx.workspace_root)
        effective_timeout = timeout if timeout is not None else ctx.command_timeout
        if run_in_background:
            return ctx.registry.submit_command(
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
        resolved_cwd = resolve_cwd(cwd, ctx.workspace_root)
        effective_timeout = timeout if timeout is not None else ctx.command_timeout
        queued = ctx.registry.submit_command(
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
        resolved_cwd = resolve_cwd(cwd, ctx.workspace_root)
        return ctx.registry.submit(
            task=task,
            goal=goal,
            executor=executor,
            cwd=resolved_cwd,
            timeout=timeout if timeout is not None else ctx.delegate_timeout,
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
        return ctx.registry.get(task_id)

    @mcp.tool(
        name="wait_task",
        title="Wait Task",
        annotations=READ_ONLY_TOOL,
        description="Wait for a delegated or background shell task to finish or until timeout, then return its latest status and output tail.",
    )
    def wait_task(task_id: str, timeout: float = 30, poll_interval: float = 0.5) -> dict[str, object]:
        return ctx.registry.wait(task_id, timeout=timeout, poll_interval=poll_interval)

    @mcp.tool(
        name="cancel_task",
        title="Cancel Task",
        annotations=LOCAL_WRITE_TOOL,
        description="Cancel a delegated or background shell task if it is still running.",
    )
    def cancel_task(task_id: str) -> dict[str, object]:
        return ctx.registry.cancel(task_id)

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
        return ctx.store.purge_tasks(
            older_than_seconds=max(float(older_than_hours), 0.0) * 3600.0,
            dry_run=dry_run,
        )

    return {
        "git_status": git_status,
        "git_diff": git_diff,
        "git_commit": git_commit,
        "git_log": git_log,
        "git_show": git_show,
        "git_blame": git_blame,
        "run_command": run_command,
        "run_command_stream": run_command_stream,
        "delegate_task": delegate_task,
        "get_task": get_task,
        "wait_task": wait_task,
        "cancel_task": cancel_task,
        "purge_tasks": purge_tasks,
    }
