from __future__ import annotations

from typing import Any

from .pathing import resolve_cwd
from .taskboard import TaskBoardError
from .tool_context import LOCAL_WRITE_TOOL, OPEN_WORLD_WRITE_TOOL, READ_ONLY_TOOL, ToolContext


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


def register_taskboard_tools(mcp: Any, ctx: ToolContext) -> dict[str, object]:
    """Register board-level task tracking and delegation tools."""

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
            resolved_cwd = resolve_cwd(cwd, ctx.workspace_root)
            return ctx.taskboard_store.create(
                tasks=tasks or [],
                cwd=str(resolved_cwd),
                workspace_root=ctx.workspace_root,
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
            return ctx.taskboard_store.add_tasks(
                board_id=board_id,
                tasks=specs,
                workspace_root=ctx.workspace_root,
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
            return ctx.taskboard_store.delegate(
                board_id=board_id,
                registry=ctx.registry,
                timeout=timeout if timeout is not None else ctx.delegate_timeout,
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
            return ctx.taskboard_store.status(
                board_id=board_id,
                registry=ctx.registry,
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
            return ctx.taskboard_store.wait(
                board_id=board_id,
                registry=ctx.registry,
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
            return ctx.taskboard_store.get_task(
                board_id=board_id,
                task_id=task_id,
                registry=ctx.registry,
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
            return ctx.taskboard_store.collect_results(
                board_id=board_id,
                registry=ctx.registry,
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
            return ctx.taskboard_store.cancel(
                board_id=board_id,
                registry=ctx.registry,
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
            resolved_cwd = str(resolve_cwd(cwd, ctx.workspace_root)) if cwd else None
            return ctx.taskboard_store.list_boards(
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
        return ctx.taskboard_store.purge(
            older_than_seconds=max(float(older_than_hours), 0.0) * 3600.0,
            dry_run=dry_run,
        )

    return {
        "_taskboard_error": _taskboard_error,
        "_taskboard_specs": _taskboard_specs,
        "taskboard_create": taskboard_create,
        "taskboard_add_task": taskboard_add_task,
        "taskboard_delegate": taskboard_delegate,
        "taskboard_status": taskboard_status,
        "taskboard_wait": taskboard_wait,
        "taskboard_get_task": taskboard_get_task,
        "taskboard_collect_results": taskboard_collect_results,
        "taskboard_cancel": taskboard_cancel,
        "taskboard_list": taskboard_list,
        "taskboard_purge": taskboard_purge,
    }
