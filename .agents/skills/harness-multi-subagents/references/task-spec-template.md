# Task Spec Template

Use this template when creating subtasks for `taskboard_create` or `taskboard_add_task`.

## Minimal shape

```json
{
  "title": "Implement TaskBoard SQLite store",
  "task": "Create a small SQLite-backed TaskBoardStore with board, task, and event CRUD. Keep the implementation focused and covered by tests.",
  "cwd": "/path/to/repo",
  "context_files": [
    "src/chatgpt_web_oauth_mcp/server.py",
    "src/chatgpt_web_oauth_mcp/tasks.py"
  ],
  "acceptance_criteria": [
    "Can create and retrieve a taskboard",
    "Can add and update tasks",
    "Can append task events",
    "Unit tests cover the store happy path and status updates"
  ],
  "verification_commands": [
    "pytest tests/test_taskboard_store.py -v"
  ]
}
```

## Rules

- One Task Spec should be independently executable by one worker.
- Keep the scope narrow enough for a single Codex or Claude worker to finish without needing more planning.
- Prefer 2-6 subtasks for a single board.
- Do not include vague tasks like "improve the system". Convert them into concrete implementation, test, or documentation tasks.
- Include specific `context_files` so the worker does not waste time rediscovering the repository.
- Include at least one validation command for implementation tasks.
- Avoid assigning multiple workers to edit the same high-conflict central file when possible.

## Good subtask examples

```json
{
  "title": "Add per-task git worktree helper",
  "task": "Implement helpers to create an isolated git worktree and branch for a TaskBoard task, detect existing worktrees, capture base/head commit SHAs, and summarize changed files.",
  "context_files": [
    "src/chatgpt_web_oauth_mcp/tasks.py",
    "src/chatgpt_web_oauth_mcp/config.py"
  ],
  "acceptance_criteria": [
    "Creates a worktree for a given board_id and task_id",
    "Uses stable branch names",
    "Returns worktree_path, branch_name, base_sha, and head_sha",
    "Tests use a temporary git repository"
  ],
  "verification_commands": [
    "pytest tests/test_taskboard_worktree.py -v"
  ]
}
```

```json
{
  "title": "Register TaskBoard MCP tools",
  "task": "Register taskboard_create, taskboard_status, taskboard_get_task, and taskboard_list in the MCP server using the TaskBoardStore API. Keep return payloads compact.",
  "context_files": [
    "src/chatgpt_web_oauth_mcp/server.py",
    "src/chatgpt_web_oauth_mcp/skills.py"
  ],
  "acceptance_criteria": [
    "Tools are visible in server_info tool list",
    "taskboard_status returns board-level counts",
    "Tool handlers have focused tests or callable helper tests"
  ],
  "verification_commands": [
    "pytest tests/test_server_taskboard_tools.py -v"
  ]
}
```
