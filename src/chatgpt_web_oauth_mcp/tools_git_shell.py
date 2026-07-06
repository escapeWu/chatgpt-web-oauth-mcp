from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from .gitops import git_blame as git_blame_impl
from .gitops import git_commit as git_commit_impl
from .gitops import git_diff as git_diff_impl
from .gitops import git_log as git_log_impl
from .gitops import git_show as git_show_impl
from .gitops import git_status as git_status_impl
from .pathing import resolve_cwd
from .shell import MAX_COMMAND_BATCH_CONCURRENCY
from .shell import run_command as run_command_impl
from .shell import run_commands as run_commands_impl
from .tool_context import LOCAL_WRITE_TOOL, OPEN_WORLD_WRITE_TOOL, READ_ONLY_TOOL, ToolContext


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
        name="run_command",
        title="Run Command",
        annotations=OPEN_WORLD_WRITE_TOOL,
        description=(
            "Run one local shell command, or run a batch of commands with mode=sequential "
            "or mode=parallel. Parallel batches are capped at max_concurrency=3."
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
            Field(description="Maximum runtime in seconds for each command before it is killed."),
        ] = None,
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
                mode=mode,
                max_concurrency=max_concurrency,
            )
        return run_command_impl(
            command=command or "",
            cwd=resolved_cwd,
            timeout=effective_timeout,
        )

    @mcp.tool(
        name="delegate_task",
        title="Delegate Task",
        annotations=OPEN_WORLD_WRITE_TOOL,
        description=(
            "Fallback executor only. Run exactly one bounded Codex Execution Prompt, serialized "
            "behind any other active Codex delegate call. ChatGPT Web should act as the "
            "architect/manager: inspect, plan, and review with direct MCP tools, then call this "
            "only for a small local execution slice. Blocks for up to wait_seconds (default 180) "
            "and returns status=running when Codex is still working, so callers can invoke this "
            "tool again to continue waiting. Each run writes private audit logs under the system "
            "temporary cache directory and returns their paths in logs. "
            "Optionally provide output_schema and parse_structured_output=true to capture JSON output."
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
        timeout: Annotated[
            int | None,
            Field(
                description=(
                    "Maximum total runtime in seconds for the Codex subprocess before it is killed. "
                    "Defaults to CHATGPT_MCP_DELEGATE_TIMEOUT."
                )
            ),
        ] = None,
        wait_seconds: Annotated[
            float,
            Field(
                description=(
                    "Maximum seconds this MCP call should block waiting for Codex. Default 180. "
                    "If Codex is still running after this wait, the tool returns status=running; "
                    "call delegate_task again to continue waiting."
                )
            ),
        ] = 180,
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
        return ctx.registry.run_codex(
            task=task,
            goal=goal,
            task_id=task_id,
            cwd=resolved_cwd,
            timeout=timeout if timeout is not None else ctx.delegate_timeout,
            wait_seconds=wait_seconds,
            files_in_scope=files_in_scope,
            out_of_scope=out_of_scope,
            context_files=context_files,
            acceptance_criteria=acceptance_criteria,
            done_means=done_means,
            verification_commands=verification_commands,
            commit_mode=commit_mode,
            output_schema=output_schema,
            parse_structured_output=parse_structured_output,
        )

    return {
        "git_status": git_status,
        "git_diff": git_diff,
        "git_commit": git_commit,
        "git_log": git_log,
        "git_show": git_show,
        "git_blame": git_blame,
        "run_command": run_command,
        "delegate_task": delegate_task,
    }
