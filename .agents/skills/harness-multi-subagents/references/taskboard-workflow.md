# TaskBoard Workflow

Use this flow when the user asks ChatGPT Web to manage a complex local engineering task with multiple local subagents.

## 1. Prepare

1. Confirm or infer the workspace.
2. Call `server_info` or `get_default_cwd` when uncertain.
3. Inspect only the files needed to create useful Task Specs.
4. Keep the first board flat. Do not introduce waves, milestones, or a dependency graph in Phase 1.

## 2. Decompose

Create 2-6 Task Specs. Prefer fewer, better-scoped tasks over many tiny tasks.

Good categories:

- storage/model helper
- worktree helper
- MCP tool registration
- delegate integration
- tests/docs

For the first implementation pass, avoid running two subtasks that must edit the same central file at the same time. If unavoidable, make one task own the interface and the other own tests or documentation.

## 3. Create the board

Call:

```text
taskboard_create({
  title,
  original_request,
  cwd,
  executor: "codex",
  max_parallel: 2,
  tasks
})
```

Store and mention the returned `board_id`. Future recovery should use this ID rather than conversation memory.

## 4. Delegate

Call:

```text
taskboard_delegate({
  board_id,
  max_parallel: 2,
  worktree_mode: "per_task",
  base_ref: "HEAD"
})
```

Use `max_parallel=2` by default. Use 3 only when tasks are clearly independent or the user explicitly asks for more parallelism.

## 5. Wait and refresh

After delegation, wait on the board:

```text
taskboard_wait({
  board_id,
  timeout: 30,
  return_on: "change",
  include_tasks: true
})
```

If `timed_out=true`, tasks continue locally. Either call `taskboard_wait` again or report that the board is still running.

Use `taskboard_status({board_id, refresh_runs: true})` for immediate status snapshots.

## 6. Inspect details only when needed

Use `taskboard_get_task` when:

- a task failed;
- a task is blocked;
- a completed task needs review;
- the user asks for details.

Ask for log tails, done report, or prompt content only when needed. Do not load all task logs by default.

## 7. Collect results

When one or more tasks complete, call:

```text
taskboard_collect_results({
  board_id,
  include_diff: false
})
```

Default result collection should summarize:

- branch name
- worktree path
- commit SHA
- changed files count/list
- validation summary
- done report summary

Do not include full diffs unless the user asks or a review requires it.

## 8. Report

Always lead with board-level counts. Then list only changed, failed, or newly completed tasks.

If all tasks are done, summarize outputs and propose the next integration or review step. If some tasks failed, summarize the failure and propose a repair task.
