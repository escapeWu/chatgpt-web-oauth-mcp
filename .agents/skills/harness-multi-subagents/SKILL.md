---
name: harness-multi-subagents
description: orchestrate complex local engineering tasks in chatgpt web through chatgpt-web-oauth-mcp by decomposing a user goal into multiple flat subtasks, creating a local taskboard, delegating subtasks to local codex or claude workers, using per-task git worktree isolation, waiting on board-level progress in 30-second tool-call windows, collecting worktree results, and reporting compact status. use when the user asks for multi-file implementation, refactoring, debugging, testing, repository maintenance, or harness-style multi-subagent work with local mcp tools.
---

# Harness Multi-Subagents

Use this skill to act as the manager for a local multi-subagent engineering task. The MCP server provides local atomic capabilities; you provide planning, task decomposition, coordination, and reporting.

## Operating boundary

- Treat MCP tools as the local execution substrate, not the planner.
- Keep the plan in ChatGPT, but persist execution state in the local TaskBoard.
- Use TaskBoard tools for multi-subtask work. Do not manage multi-subtask state only in conversation memory.
- Prefer compact board-level status over long logs. Expand only failed, blocked, or completed tasks when needed.

## Standard workflow

1. Understand the user's requested engineering goal and the intended workspace.
2. Use `server_info` and/or `get_default_cwd` when the workspace is unclear.
3. Inspect only enough files to produce useful subtasks.
4. Decompose into 2-6 flat Task Specs. Keep Phase 1 simple: no waves, no complex dependency graph.
5. Create a local TaskBoard with `taskboard_create`.
6. Delegate with `taskboard_delegate`, defaulting to `max_parallel=2` and `worktree_mode="per_task"` for implementation tasks.
7. Wait on the board, not individual worker runs: call `taskboard_wait(board_id, timeout=30, return_on="change")`.
8. If the wait times out and tasks are still running, either wait again or report that work is still running, depending on the user request and interaction flow.
9. Use `taskboard_status` for immediate snapshots and `taskboard_get_task` for single-task detail.
10. Use `taskboard_collect_results` to summarize branches, commits, changed files, validation output, and done reports.
11. Report board-level counts first, then notable task outcomes.

## Required Task Spec quality

Every delegated subtask must include:

- `title`: short and action-oriented.
- `task`: concrete implementation instructions scoped to one worker.
- `cwd`: repository root or relevant subdirectory when known.
- `context_files`: specific files the worker should read first.
- `acceptance_criteria`: observable completion criteria.
- `verification_commands`: commands the worker should run before claiming done.

Keep subtasks independent where possible. Avoid assigning two workers to the same central file unless the user explicitly accepts conflict risk.

## Worktree rule

For implementation, refactor, or test-writing subtasks, request per-task git worktree isolation through the MCP tool. The MCP server should create and pass the task worktree as the worker `cwd`; Codex should not be relied on to create isolation by prompt alone.

Tell each worker:

- stay in the assigned worktree;
- do not switch branches;
- do not edit the parent workspace;
- commit task changes on the assigned branch;
- write a concise done report.

## Waiting rule

After `taskboard_delegate`, do not call `wait_task` for each subtask unless debugging one specific worker. Use board-level waiting:

```text
taskboard_wait(board_id, timeout=30, return_on="change")
```

A timeout means the tool-call wait expired, not that the workers were cancelled. Running tasks continue locally and can be checked later with `taskboard_status`.

## Reporting rule

Default progress report format:

```text
TaskBoard <board_id>: <title>
Status: <running/done/failed/cancelled>
Counts: total N, running N, succeeded N, failed N, pending N
Updates:
- <task_id> <title>: <status> - <one-line summary>
Next: <recommended next action>
```

Never paste full stdout, stderr, or diffs unless the user asks or a failure requires a short excerpt.

## Fallbacks

If TaskBoard tools are not available yet, explain that the local MCP server lacks the TaskBoard primitives and fall back to existing atomic tools:

- use `delegate_task` for one subtask at a time;
- use `get_task` / `wait_task` for individual worker status;
- keep a temporary in-conversation summary, but warn that it is not durable across long conversations.

## References

Load these only when needed:

- `references/taskboard-workflow.md` for the full orchestration sequence.
- `references/task-spec-template.md` for Task Spec shape and examples.
- `references/worktree-rules.md` for per-task git worktree requirements.
- `references/wait-and-status.md` for board-level 30-second waiting behavior.
- `references/reporting.md` for concise user-facing status and completion summaries.
