from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from .gitops import git_blame as git_blame_impl
from .gitops import git_commit as git_commit_impl
from .gitops import git_diff as git_diff_impl
from .gitops import git_log as git_log_impl
from .gitops import git_show as git_show_impl
from .gitops import git_status as git_status_impl
from .gitops import git_worktree_create as git_worktree_create_impl
from .gitops import git_worktree_list as git_worktree_list_impl
from .gitops import git_worktree_remove as git_worktree_remove_impl
from .gitops import git_worktree_status as git_worktree_status_impl
from .pathing import resolve_cwd
from .shell import (
    MAX_COMMAND_BATCH_CONCURRENCY,
    MAX_COMMAND_TIMEOUT_SECONDS,
    MAX_JOB_LIST_LIMIT,
    MAX_JOB_OUTPUT_BYTES,
    MAX_JOB_TAIL_LINES,
)
from .shell import run_command as run_command_impl
from .shell import run_commands as run_commands_impl
from .tool_context import LOCAL_WRITE_TOOL, OPEN_WORLD_WRITE_TOOL, READ_ONLY_TOOL, ToolContext


DelegateModel = (
    Literal[
        "default",
        "gpt-5.3-codex-spark",
        "gpt-5.4-mini",
        "gpt-5.5",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
    ]
    | str
    | None
)


def register_git_shell_tools(mcp: Any, ctx: ToolContext) -> dict[str, object]:
    """Register git, synchronous shell, and serial Codex delegate tools."""

    @mcp.tool(
        name="git_status",
        title="Git Status",
        annotations=READ_ONLY_TOOL,
        description="Return structured git status for the repository at cwd or the current workspace root.",
    )
    def git_status(
        cwd: Annotated[
            str | None,
            Field(description="Repository working directory. Defaults to the session cwd or workspace root."),
        ] = None
    ) -> dict[str, object]:
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
        cwd: Annotated[
            str | None,
            Field(description="Repository working directory. Defaults to the session cwd or workspace root."),
        ] = None,
        staged: Annotated[bool, Field(description="Show staged diff instead of unstaged working-tree diff.")] = False,
        paths: Annotated[
            list[str] | None,
            Field(description="Optional repository-relative paths to restrict the diff."),
        ] = None,
        max_bytes: Annotated[int, Field(description="Maximum total diff bytes to return.")] = 65536,
        per_file_max_bytes: Annotated[int, Field(description="Maximum diff bytes to return per changed file.")] = 16384,
        offset: Annotated[int, Field(description="Byte offset into the flat diff for lossless pagination.", ge=0)] = 0,
    ) -> dict[str, object]:
        resolved_cwd = resolve_cwd(cwd, ctx.workspace_root)
        return git_diff_impl(
            cwd=resolved_cwd,
            staged=staged,
            paths=paths,
            max_bytes=max_bytes,
            per_file_max_bytes=per_file_max_bytes,
            offset=offset,
            max_tokens=ctx.tool_output_token_budget,
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
        message: Annotated[str, Field(description="Commit message to use.")],
        cwd: Annotated[
            str | None,
            Field(description="Repository working directory. Defaults to the session cwd or workspace root."),
        ] = None,
        paths: Annotated[
            list[str] | None,
            Field(description="Optional repository-relative paths to stage before committing."),
        ] = None,
        stage_all: Annotated[bool, Field(description="Stage all current repository changes before committing.")] = False,
        amend: Annotated[bool, Field(description="Amend the current HEAD commit instead of creating a new commit.")] = False,
        allow_empty: Annotated[bool, Field(description="Allow creating an empty commit when there are no changes.")] = False,
        author: Annotated[str | None, Field(description="Optional git author string, e.g. 'Name <email>'.")] = None,
        sign_off: Annotated[bool, Field(description="Append a Signed-off-by trailer to the commit message.")] = False,
        dry_run: Annotated[bool, Field(description="Preview the commit operation without changing git state.")] = False,
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
    def git_log(
        cwd: Annotated[
            str | None,
            Field(description="Repository working directory. Defaults to the session cwd or workspace root."),
        ] = None,
        limit: Annotated[int, Field(description="Maximum number of commits to return.")] = 10,
    ) -> dict[str, object]:
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
        ref: Annotated[str, Field(description="Git revision, commit, tag, branch, or other ref to show.")] = "HEAD",
        cwd: Annotated[
            str | None,
            Field(description="Repository working directory. Defaults to the session cwd or workspace root."),
        ] = None,
        max_bytes: Annotated[int, Field(description="Maximum total output bytes to return.")] = 65536,
        per_file_max_bytes: Annotated[int, Field(description="Maximum diff bytes to return per file.")] = 16384,
        offset: Annotated[int, Field(description="Byte offset into the flat commit diff.", ge=0)] = 0,
    ) -> dict[str, object]:
        resolved_cwd = resolve_cwd(cwd, ctx.workspace_root)
        return git_show_impl(
            cwd=resolved_cwd,
            ref=ref,
            max_bytes=max_bytes,
            per_file_max_bytes=per_file_max_bytes,
            offset=offset,
            max_tokens=ctx.tool_output_token_budget,
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
        path: Annotated[str, Field(description="Repository-relative file path to blame.")],
        cwd: Annotated[
            str | None,
            Field(description="Repository working directory. Defaults to the session cwd or workspace root."),
        ] = None,
        ref: Annotated[str | None, Field(description="Optional git ref to blame instead of the working tree.")] = None,
        start_line: Annotated[int | None, Field(description="Optional 1-based first line to include.")] = None,
        end_line: Annotated[int | None, Field(description="Optional 1-based last line to include.")] = None,
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
        name="git_worktree_create",
        title="Git Worktree Create",
        annotations=LOCAL_WRITE_TOOL,
        description=(
            "Create a small generic git worktree from base_ref. mode='clean' creates a new branch "
            "for the worktree; mode='detached' creates a detached-HEAD worktree."
        ),
    )
    def git_worktree_create(
        path: Annotated[
            str,
            Field(description="Path for the new worktree. Relative paths resolve from cwd."),
        ],
        cwd: Annotated[
            str | None,
            Field(description="Repository working directory. Defaults to the session cwd or workspace root."),
        ] = None,
        base_ref: Annotated[
            str,
            Field(description="Git ref, branch, tag, or commit to create the worktree from."),
        ] = "HEAD",
        mode: Annotated[
            Literal["clean", "detached"],
            Field(description="Worktree mode: clean creates a new branch; detached checks out base_ref detached."),
        ] = "clean",
        branch: Annotated[
            str | None,
            Field(description="Optional branch name for mode='clean'. Defaults to the worktree directory name."),
        ] = None,
    ) -> dict[str, object]:
        resolved_cwd = resolve_cwd(cwd, ctx.workspace_root)
        return git_worktree_create_impl(
            cwd=resolved_cwd,
            path=path,
            base_ref=base_ref,
            mode=mode,
            branch=branch,
        )

    @mcp.tool(
        name="git_worktree_list",
        title="Git Worktree List",
        annotations=READ_ONLY_TOOL,
        description="Return registered git worktrees for the repository at cwd.",
    )
    def git_worktree_list(
        cwd: Annotated[
            str | None,
            Field(description="Repository working directory. Defaults to the session cwd or workspace root."),
        ] = None
    ) -> dict[str, object]:
        resolved_cwd = resolve_cwd(cwd, ctx.workspace_root)
        return git_worktree_list_impl(cwd=resolved_cwd)

    @mcp.tool(
        name="git_worktree_status",
        title="Git Worktree Status",
        annotations=READ_ONLY_TOOL,
        description=(
            "Return git status for one registered worktree, or for all registered worktrees when path is omitted."
        ),
    )
    def git_worktree_status(
        cwd: Annotated[
            str | None,
            Field(description="Repository working directory. Defaults to the session cwd or workspace root."),
        ] = None,
        path: Annotated[
            str | None,
            Field(description="Optional worktree path to inspect. Relative paths resolve from cwd."),
        ] = None,
    ) -> dict[str, object]:
        resolved_cwd = resolve_cwd(cwd, ctx.workspace_root)
        return git_worktree_status_impl(cwd=resolved_cwd, path=path)

    @mcp.tool(
        name="git_worktree_remove",
        title="Git Worktree Remove",
        annotations=LOCAL_WRITE_TOOL,
        description="Remove a registered git worktree. Dirty worktrees are refused unless force=true.",
    )
    def git_worktree_remove(
        path: Annotated[
            str,
            Field(description="Registered worktree path to remove. Relative paths resolve from cwd."),
        ],
        cwd: Annotated[
            str | None,
            Field(description="Repository working directory. Defaults to the session cwd or workspace root."),
        ] = None,
        force: Annotated[
            bool,
            Field(description="Remove even when the worktree has uncommitted changes."),
        ] = False,
    ) -> dict[str, object]:
        resolved_cwd = resolve_cwd(cwd, ctx.workspace_root)
        return git_worktree_remove_impl(cwd=resolved_cwd, path=path, force=force)

    @mcp.tool(
        name="run_command",
        title="Run Command",
        annotations=OPEN_WORLD_WRITE_TOOL,
        description=(
            "Run one local shell command, or run a batch of commands with mode=sequential "
            f"or mode=parallel. Timeout is capped at {MAX_COMMAND_TIMEOUT_SECONDS}s unless "
            "force=true is set after explicit user approval. Parallel batches are capped at "
            "max_concurrency=3."
        ),
    )
    def run_command(
        command: Annotated[
            str | None,
            Field(description="Single shell command to run. Provide exactly one of command or commands."),
        ] = None,
        commands: Annotated[
            list[str] | None,
            Field(description="Batch of shell commands to run. Provide exactly one of command or commands."),
        ] = None,
        cwd: Annotated[
            str | None,
            Field(description="Working directory for the command. Defaults to the session cwd or workspace root."),
        ] = None,
        timeout: Annotated[
            int | None,
            Field(
                description=(
                    f"Maximum runtime in seconds for each command before it is killed. "
                    f"Values above {MAX_COMMAND_TIMEOUT_SECONDS}s are rejected unless force=true "
                    "has explicit user approval."
                )
            ),
        ] = None,
        force: Annotated[
            bool,
            Field(
                description=(
                    f"Allow run_command timeouts above {MAX_COMMAND_TIMEOUT_SECONDS}s. Set this "
                    "only after explicit user approval; otherwise use delegate_task for complex "
                    "or long-running work."
                )
            ),
        ] = False,
        mode: Annotated[
            Literal["sequential", "parallel"],
            Field(description="Batch execution mode when commands is provided."),
        ] = "sequential",
        max_concurrency: Annotated[
            int,
            Field(
                description=(
                    "Maximum number of commands to run concurrently in parallel mode. "
                    f"Hard limit: {MAX_COMMAND_BATCH_CONCURRENCY}."
                ),
                ge=1,
                le=MAX_COMMAND_BATCH_CONCURRENCY,
            ),
        ] = MAX_COMMAND_BATCH_CONCURRENCY,
    ) -> dict[str, object]:
        has_command = bool(command)
        has_commands = bool(commands)
        if has_command == has_commands:
            return {
                "success": False,
                "error": {
                    "code": "invalid_arguments",
                    "message": "Provide exactly one of command or commands.",
                },
            }

        resolved_cwd = resolve_cwd(cwd, ctx.workspace_root)
        effective_timeout = timeout if timeout is not None else ctx.command_timeout
        if commands is not None:
            return run_commands_impl(
                commands=commands,
                cwd=resolved_cwd,
                timeout=effective_timeout,
                force=force,
                mode=mode,
                max_concurrency=max_concurrency,
                max_tokens=ctx.run_token_budget,
                capture_max_bytes=ctx.run_capture_max_bytes,
            )
        return run_command_impl(
            command=command or "",
            cwd=resolved_cwd,
            timeout=effective_timeout,
            force=force,
            max_tokens=ctx.run_token_budget,
            capture_max_bytes=ctx.run_capture_max_bytes,
        )

    @mcp.tool(
        name="job_start",
        title="Job Start",
        annotations=OPEN_WORLD_WRITE_TOOL,
        description=(
            "Start one generic local subprocess in the background. stdout and stderr are "
            "captured to private per-job log files under the server state directory. A detached "
            "supervisor keeps the job recoverable across MCP server restarts; this does not add "
            "scheduling, automatic restart, dependencies, or artifact tracking."
        ),
    )
    def job_start(
        command: Annotated[str, Field(description="Shell command to start in the background.")],
        cwd: Annotated[
            str | None,
            Field(description="Working directory for the job. Defaults to the session cwd or workspace root."),
        ] = None,
        env: Annotated[
            dict[str, str] | None,
            Field(description="Optional environment variable overrides for the job process."),
        ] = None,
        name: Annotated[
            str | None,
            Field(description="Optional human-readable job name returned by status calls."),
        ] = None,
    ) -> dict[str, object]:
        resolved_cwd = resolve_cwd(cwd, ctx.workspace_root)
        return ctx.job_registry.start_job(
            command=command,
            cwd=resolved_cwd,
            state_dir=ctx.state_dir,
            env=env,
            name=name,
        )

    @mcp.tool(
        name="job_list",
        title="Job List",
        annotations=READ_ONLY_TOOL,
        description=(
            "Discover durable background jobs from the current server state directory without "
            "depending on in-memory registry state. Reconciles stale nonterminal records, filters "
            "by durable status, and returns newest-first cursor-safe offset pagination with bounded "
            "command summaries and warnings for skipped corrupt or unsafe records."
        ),
    )
    def job_list(
        status: Annotated[
            Literal["all", "running", "succeeded", "failed", "killed", "interrupted"],
            Field(description="Durable job status to include, or all for every valid job record."),
        ] = "all",
        offset: Annotated[
            int,
            Field(description="Zero-based offset into the filtered newest-first job list.", ge=0),
        ] = 0,
        limit: Annotated[
            int,
            Field(
                description=f"Maximum number of job summaries to return. Hard limit: {MAX_JOB_LIST_LIMIT}.",
                ge=1,
                le=MAX_JOB_LIST_LIMIT,
            ),
        ] = 50,
    ) -> dict[str, object]:
        return ctx.job_registry.list_jobs(
            state_dir=ctx.state_dir,
            status=status,
            offset=offset,
            limit=limit,
            max_tokens=ctx.tool_output_token_budget,
        )

    @mcp.tool(
        name="job_status",
        title="Job Status",
        annotations=READ_ONLY_TOOL,
        description=(
            "Return durable status for a background job in the current server state directory, including "
            "pid, elapsed time, exit code, resource usage when available, and stdout/stderr log paths."
        ),
    )
    def job_status(
        job_id: Annotated[str, Field(description="Server-generated job_id returned by job_start.")]
    ) -> dict[str, object]:
        return ctx.job_registry.job_status(job_id=job_id, state_dir=ctx.state_dir)

    @mcp.tool(
        name="job_output",
        title="Job Output",
        annotations=READ_ONLY_TOOL,
        description=(
            "Read one durable job log incrementally using a per-stream raw-byte cursor. Pass the "
            "returned next_cursor back with the same stdout or stderr stream; stdout/stderr are not "
            "merged and no cross-stream ordering is inferred. Reads are bounded by max_bytes and the "
            "configured o200k token budget, with optional long-polling while a job is nonterminal."
        ),
    )
    def job_output(
        job_id: Annotated[str, Field(description="Server-generated job_id returned by job_start.")],
        stream: Annotated[
            Literal["stdout", "stderr"],
            Field(description="Single job log stream whose independent raw-byte cursor is being read."),
        ] = "stdout",
        cursor: Annotated[
            int,
            Field(description="Raw-byte offset in the selected stream; reuse next_cursor to continue.", ge=0),
        ] = 0,
        max_bytes: Annotated[
            int,
            Field(
                description=f"Maximum raw log bytes to read in this page. Hard limit: {MAX_JOB_OUTPUT_BYTES}.",
                ge=1,
                le=MAX_JOB_OUTPUT_BYTES,
            ),
        ] = 65536,
        wait_ms: Annotated[
            int,
            Field(
                description=(
                    "Long-poll window in milliseconds when cursor is caught up and the job is nonterminal. "
                    "Returns early when bytes arrive or the job becomes terminal. Hard limit: 30000."
                ),
                ge=0,
                le=30000,
            ),
        ] = 0,
    ) -> dict[str, object]:
        return ctx.job_registry.output_job(
            job_id=job_id,
            state_dir=ctx.state_dir,
            stream=stream,
            cursor=cursor,
            max_bytes=max_bytes,
            wait_ms=wait_ms,
            max_tokens=ctx.job_output_token_budget,
        )

    @mcp.tool(
        name="job_tail",
        title="Job Tail",
        annotations=READ_ONLY_TOOL,
        description=(
            "Return the last N lines from a background job's stdout or stderr log. "
            f"Line count is bounded to {MAX_JOB_TAIL_LINES}."
        ),
    )
    def job_tail(
        job_id: Annotated[str, Field(description="Server-generated job_id returned by job_start.")],
        stream: Annotated[
            Literal["stdout", "stderr"],
            Field(description="Which job log stream to tail."),
        ] = "stdout",
        lines: Annotated[
            int,
            Field(description=f"Number of log lines to return, capped at {MAX_JOB_TAIL_LINES}.", ge=1),
        ] = 50,
    ) -> dict[str, object]:
        return ctx.job_registry.tail_job(job_id=job_id, state_dir=ctx.state_dir, stream=stream, lines=lines)

    @mcp.tool(
        name="job_kill",
        title="Job Kill",
        annotations=OPEN_WORLD_WRITE_TOOL,
        description=(
            "Signal a background job that was started by job_start. Uses TERM by default, "
            "or KILL when explicitly requested. It only signals the registered subprocess "
            "for the supplied job_id, never an arbitrary caller-provided pid."
        ),
    )
    def job_kill(
        job_id: Annotated[str, Field(description="Server-generated job_id returned by job_start.")],
        signal: Annotated[
            Literal["TERM", "KILL"],
            Field(description="Signal to send to the registered job process group."),
        ] = "TERM",
    ) -> dict[str, object]:
        return ctx.job_registry.kill_job(job_id=job_id, state_dir=ctx.state_dir, signal_name=signal)

    @mcp.tool(
        name="delegate_task",
        title="Delegate Task",
        annotations=OPEN_WORLD_WRITE_TOOL,
        description=(
            "Fallback executor only. Run exactly one bounded Codex Execution Prompt, serialized "
            "behind any other active Codex delegate call. ChatGPT Web should act as the "
            "architect/manager: inspect, plan, and review with direct MCP tools, then call this "
            "only for a small local execution slice. Blocks for up to timeout/wait_seconds "
            "(default 300s) and returns status=running when Codex is still working; Codex "
            "continues running and callers can invoke this tool again to continue waiting. "
            "Each run writes private audit logs under the system temporary cache directory and "
            "returns their paths in logs; callers can use read_text on stdout/stderr/metadata "
            "to inspect live progress. Completed responses do not inline stdout/stderr; use logs "
            "for raw output. If another non-matching delegate is active, the requested new task is "
            "not started and the response includes request_conflict/new_task_started=false. "
            "Optionally provide output_schema and parse_structured_output=true to capture JSON output. "
            "Model routing: use gpt-5.6-sol for the hardest architecture, quantitative-model "
            "RCA, trading-training design, research, and complex code review; gpt-5.6-terra "
            "for regular feature development, single-module implementation, test repair, and "
            "data analysis; gpt-5.6-luna for code search, format conversion, simple scripts, "
            "and batch mechanical work. For fast context-gathering tasks, "
            "gpt-5.3-codex-spark remains available with reasoning_effort unset/default. For "
            "general delegation, omit or pass empty/default for both model and reasoning_effort."
        ),
    )
    def delegate_task(
        task: Annotated[
            str | None,
            Field(
                description=(
                    "Concrete work instruction for Codex. Required when starting a new delegate "
                    "unless goal is provided. Omit task and goal on a later call to continue "
                    "waiting for the currently running delegate."
                )
            ),
        ] = None,
        goal: Annotated[
            str | None,
            Field(
                description=(
                    "High-level objective or context for this one Codex execution slice. Required "
                    "when starting a new delegate unless task is provided. Can be combined with task."
                )
            ),
        ] = None,
        task_id: Annotated[
            str | None,
            Field(
                description=(
                    "Optional caller-defined id for this single execution slice, e.g. T3 or "
                    "audit-step4-label-alignment. Used only in the Codex prompt and result context."
                )
            ),
        ] = None,
        cwd: Annotated[
            str | None,
            Field(
                description=(
                    "Working directory for the Codex delegate. Relative paths resolve from the "
                    "server default cwd; absolute paths are used as-is."
                )
            ),
        ] = None,
        files_in_scope: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Optional paths Codex is allowed or expected to inspect/change for this single "
                    "execution slice. Keep this narrow to avoid opaque long-running analysis."
                )
            ),
        ] = None,
        out_of_scope: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Optional paths, actions, or topics Codex must avoid while executing this slice."
                )
            ),
        ] = None,
        context_files: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Optional file paths to mention in the Codex prompt as relevant context. "
                    "The server does not read or attach them automatically."
                )
            ),
        ] = None,
        acceptance_criteria: Annotated[
            list[str] | None,
            Field(description="Optional checklist of conditions Codex should satisfy before finishing."),
        ] = None,
        done_means: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Optional explicit completion contract for this slice, such as changed files, "
                    "expected report sections, or verification evidence required before returning."
                )
            ),
        ] = None,
        verification_commands: Annotated[
            list[str] | None,
            Field(description="Optional shell commands Codex should consider running to verify the work."),
        ] = None,
        commit_mode: Annotated[
            Literal["allowed", "required", "forbidden"],
            Field(
                description=(
                    "Whether Codex may create commits: allowed permits commits, required asks Codex "
                    "to commit if it changes files, forbidden tells Codex not to commit."
                )
            ),
        ] = "allowed",
        model: Annotated[
            DelegateModel,
            Field(
                description=(
                    "Optional Codex model override. Recommended choices: gpt-5.6-sol is the "
                    "flagship for complex reasoning, long tasks, coding, research, and strong "
                    "tool collaboration (system architecture, quantitative-model RCA, "
                    "trading-training design, complex code review); gpt-5.6-terra is the "
                    "cost-effective default for regular development, single-module work, test "
                    "repair, and data analysis; gpt-5.6-luna is the fastest, lowest-cost option "
                    "for code search, format conversion, simple scripts, and batch mechanical "
                    "tasks. gpt-5.3-codex-spark remains suitable for fast context-gathering tasks with "
                    "reasoning_effort unset/default. Omit or pass default to inherit the Codex "
                    "CLI/user config. Other model names remain accepted for forward compatibility; "
                    "non-default values are passed as --model <value> to codex exec."
                )
            ),
        ] = None,
        reasoning_effort: Annotated[
            Literal["default", "none", "minimal", "low", "medium", "high", "xhigh", "max"],
            Field(
                description=(
                    "Optional Codex reasoning effort override for this delegate call. Use default "
                    "to inherit the Codex CLI/user config; otherwise the server passes "
                    "-c model_reasoning_effort=<value> to codex exec. Common combinations: leave "
                    "unset/default with model=gpt-5.3-codex-spark for fast context-gathering tasks; "
                    "omit or pass empty/default for general delegation."
                )
            ),
        ] = "default",
        timeout: Annotated[
            int | None,
            Field(
                description=(
                    "Soft MCP wait timeout in seconds. Defaults to CHATGPT_MCP_DELEGATE_TIMEOUT "
                    "(normally 300). When exceeded, the tool returns status=running with log paths; "
                    "the Codex subprocess is not killed."
                )
            ),
        ] = None,
        wait_seconds: Annotated[
            float | None,
            Field(
                description=(
                    "Optional override for how long this MCP call should block waiting for Codex. "
                    "Defaults to timeout. If Codex is still running after this wait, the tool "
                    "returns status=running and log paths; call delegate_task again to continue waiting."
                )
            ),
        ] = None,
        output_schema: Annotated[
            dict[str, object] | None,
            Field(
                description=(
                    "Optional JSON schema describing the structured result expected from Codex. "
                    "Returned in output_schema and used only as prompt/context metadata."
                )
            ),
        ] = None,
        parse_structured_output: Annotated[
            bool,
            Field(
                description=(
                    "When true, the server tries to parse JSON from Codex stdout/stderr and returns "
                    "it as structured_output."
                )
            ),
        ] = True,
    ) -> dict[str, object]:
        resolved_cwd = resolve_cwd(cwd, ctx.workspace_root)
        effective_timeout = timeout if timeout is not None else ctx.delegate_timeout
        effective_wait_seconds = wait_seconds if wait_seconds is not None else effective_timeout
        return ctx.registry.run_codex(
            task=task,
            goal=goal,
            task_id=task_id,
            cwd=resolved_cwd,
            timeout=effective_timeout,
            wait_seconds=effective_wait_seconds,
            files_in_scope=files_in_scope,
            out_of_scope=out_of_scope,
            context_files=context_files,
            acceptance_criteria=acceptance_criteria,
            done_means=done_means,
            verification_commands=verification_commands,
            commit_mode=commit_mode,
            model=model,
            reasoning_effort=reasoning_effort,
            output_schema=output_schema,
            parse_structured_output=parse_structured_output,
        )

    @mcp.tool(
        name="delegate_status",
        title="Delegate Status",
        annotations=READ_ONLY_TOOL,
        description=(
            "Inspect the current and recent Codex delegates without relying on ChatGPT Web "
            "to remember a caller-generated task_id. Returns the active delegate, latest "
            "delegate, and a recent list with server-generated delegate_id values plus log "
            "paths. Pass delegate_id to fetch one known delegate. Set watch_seconds up to "
            "300 to long-poll every poll_seconds seconds and return early only when task "
            "status changes; if no status change occurs, the last snapshot is returned."
        ),
    )
    def delegate_status(
        delegate_id: Annotated[
            str | None,
            Field(
                description=(
                    "Optional server-generated delegate_id to inspect. Omit to list the active "
                    "delegate and recent completed delegates."
                )
            ),
        ] = None,
        limit: Annotated[
            int,
            Field(description="Maximum recent delegates to return. Hard limit: 20.", ge=1, le=20),
        ] = 10,
        offset: Annotated[
            int,
            Field(description="Number of recent delegates to skip for pagination.", ge=0),
        ] = 0,
        watch_seconds: Annotated[
            float,
            Field(
                description=(
                    "Optional long-poll window in seconds. Use 300 for a five-minute monitor. "
                    "When positive, delegate_status polls until task status changes or the "
                    "watch window expires. Hard limit: 300."
                ),
                ge=0,
                le=300,
            ),
        ] = 0,
        poll_seconds: Annotated[
            float,
            Field(
                description=(
                    "Polling interval in seconds while watch_seconds is positive. Defaults to 5."
                ),
                ge=0.1,
                le=60,
            ),
        ] = 5,
    ) -> dict[str, object]:
        return ctx.registry.delegate_status(
            delegate_id=delegate_id,
            limit=limit,
            offset=offset,
            watch_seconds=watch_seconds,
            poll_seconds=poll_seconds,
            max_tokens=ctx.tool_output_token_budget,
        )

    return {
        "git_status": git_status,
        "git_diff": git_diff,
        "git_commit": git_commit,
        "git_log": git_log,
        "git_show": git_show,
        "git_blame": git_blame,
        "git_worktree_create": git_worktree_create,
        "git_worktree_list": git_worktree_list,
        "git_worktree_status": git_worktree_status,
        "git_worktree_remove": git_worktree_remove,
        "run_command": run_command,
        "job_start": job_start,
        "job_list": job_list,
        "job_status": job_status,
        "job_output": job_output,
        "job_tail": job_tail,
        "job_kill": job_kill,
        "delegate_task": delegate_task,
        "delegate_status": delegate_status,
    }
