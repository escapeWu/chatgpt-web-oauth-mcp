# Reporting

Use compact, board-first reporting. The user should see progress without reading raw worker logs.

## Progress update format

```text
TaskBoard <board_id>: <title>
Status: <running|done|failed|cancelled>
Counts: total <n>, running <n>, succeeded <n>, failed <n>, pending <n>
Updates:
- <task_id> <title>: <status> — <one-line summary>
Next: <recommended next action>
```

## Completion report format

```text
TaskBoard <board_id> completed.

Results:
- <task_id> <title>: <branch>, <commit>, <changed files summary>, <validation summary>
- ...

Notes:
- <integration caveat or follow-up>
```

## Failure report format

```text
TaskBoard <board_id> has failed or blocked subtasks.

Failed:
- <task_id> <title>: <short reason>

Still running:
- <task_id> <title>

Suggested next step:
- inspect logs / create repair task / cancel remaining tasks / continue waiting
```

## Rules

- Lead with counts, not logs.
- Mention the `board_id` so the user can resume later.
- Include branch and commit only after results are collected.
- Do not paste full stdout/stderr by default.
- Do not claim completion without evidence from TaskBoard status or collected results.
- Be explicit when a wait timed out but local workers are still running.
