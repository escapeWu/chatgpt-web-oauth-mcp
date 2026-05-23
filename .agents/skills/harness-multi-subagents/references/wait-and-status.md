# Wait and Status

Use board-level status for multi-subtask work. Do not wait on individual delegated task IDs unless debugging a single worker.

## Immediate status

Use:

```text
taskboard_status({
  board_id,
  refresh_runs: true,
  include_tasks: true
})
```

This should return immediately with compact counts and task summaries.

## Blocking board wait

Use:

```text
taskboard_wait({
  board_id,
  timeout: 30,
  poll_interval: 0.5,
  return_on: "change",
  include_tasks: true
})
```

`taskboard_wait` should block the MCP tool call for at most 30 seconds, refresh all subtask run states, and return a TaskBoard summary.

## Timeout semantics

A wait timeout means the tool call stopped waiting. It does not cancel running workers.

When `timed_out=true` and the board still has running tasks:

- call `taskboard_wait` again if the user asked to wait for results;
- otherwise report that work is still running and include the board ID.

## Return reasons

Prefer these return reasons:

- `status_changed`: at least one task changed status;
- `task_completed`: at least one task reached a terminal state;
- `all_done`: all delegated tasks reached terminal states;
- `timeout`: no requested condition happened before timeout.

## Compact status shape

A useful status response includes:

```json
{
  "board_id": "tb_xxx",
  "status": "running",
  "timed_out": false,
  "return_reason": "status_changed",
  "counts": {
    "total": 4,
    "pending": 1,
    "running": 2,
    "succeeded": 1,
    "failed": 0,
    "cancelled": 0
  },
  "changed_tasks": [
    {
      "task_id": "t01",
      "title": "Implement TaskBoardStore",
      "status": "succeeded",
      "summary": "Store implemented and tests passed"
    }
  ]
}
```

## Suggested manager behavior

- If a task failed, inspect it with `taskboard_get_task` and summarize the failure.
- If a task completed, collect results only for that task or the board.
- If all tasks completed, call `taskboard_collect_results` and produce a final compact report.
- If nothing changed after 30 seconds, avoid inventing progress. Say that the board is still running.
