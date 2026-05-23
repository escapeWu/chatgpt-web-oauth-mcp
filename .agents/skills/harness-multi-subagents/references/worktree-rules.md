# Worktree Rules

Use per-task git worktree isolation for implementation, refactor, and test-writing subtasks.

## Core rule

The MCP server should create the worktree before starting the worker and pass that worktree path as the worker `cwd`.

Do not rely on Codex or Claude to create isolation from prompt instructions alone.

## Expected layout

A typical local layout may look like:

```text
<state_dir>/taskboards/<board_id>/worktrees/<task_id>/
```

A typical branch name may look like:

```text
taskboard/<board_id>/<task_id>
```

The exact paths and branch names are owned by the MCP server. ChatGPT should not invent paths after the server returns real values.

## Prompt rules for each worker

Include these rules in the delegated task prompt:

```text
You are already running inside an isolated git worktree for this TaskBoard task.

Rules:
1. Stay in the assigned working directory.
2. Do not switch branches.
3. Do not edit the parent workspace.
4. Do not remove or prune worktrees.
5. Keep changes scoped to this task.
6. Commit your changes on the assigned branch before declaring done.
7. Write a concise done report with files changed, validation commands run, and blockers.
```

## Result expectations

A completed implementation task should provide enough evidence for the manager to collect results without reading long logs:

- task status: complete, partial, blocked, or failed
- commit SHA on the task branch
- files changed
- validation commands run
- validation outcome
- known limitations or follow-up needs

## Conflict caution

Per-task worktrees prevent concurrent workers from corrupting the same checkout. They do not automatically merge results. If multiple task branches touch the same file, report the conflict risk and defer integration or merge decisions to the user or a later integration workflow.
