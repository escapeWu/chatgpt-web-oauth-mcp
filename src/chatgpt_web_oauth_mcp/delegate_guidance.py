from __future__ import annotations

import json


SKILL_GUIDANCE_VERSION = "1.0"
SKILL_NAMESPACE = "chatgpt-web-oauth-mcp"
SKILL_INDEX_URI = f"skill://{SKILL_NAMESPACE}/index"
DELEGATE_USE_URI = f"skill://{SKILL_NAMESPACE}/delegate-use"


DELEGATE_USE_GUIDE = """---
name: delegate-use
description: Use the local delegate_task, delegate_batch, delegate_status, and delegate_cancel tools safely and effectively with Codex, Pi, or another configured CLI harness. Load before the first delegate call in a task, when choosing a harness or task kind, or when recovering from queued, failed, timed-out, or cancelled work.
---

# Delegate Use

## Critical rules

1. Use direct MCP tools first for inspection, planning, small deterministic edits, Git checks, and short verification commands.
2. Delegate exactly one bounded execution slice. Do not use a delegate as an opaque planning loop or unlimited autonomous worker.
3. Use `kind=explore` for read-only discovery and `kind=code` for implementation. Never ask an explore task to write.
4. Read the returned status and logs, then independently review and verify the result before declaring completion.
5. Treat `wait_seconds` as an MCP response window, not a process lifetime. A queued or running response means the delegate is still alive.

## Choose the tool

| Need | Tool |
| --- | --- |
| One bounded read-only or coding task | `delegate_task` |
| Several independent read-only investigations in one project | `delegate_batch` |
| Resume monitoring by delegate, group, project, or global state | `delegate_status` |
| Stop one delegate or every child in an exploration group | `delegate_cancel` |

Use `run_command` for a short non-interactive command, `job_*` for a durable non-interactive process, and `tmux_*` for an interactive terminal. Do not delegate work that direct tools can perform more clearly and cheaply.

## Choose a harness and task kind

- Set `harness=codex` or `harness=pi`; omit it to use the server default reported by `server_info`.
- Codex explore uses a native read-only sandbox and an ephemeral session.
- Pi explore disables sessions, project trust/context, extensions, and Pi-local skills, and allows only `read,grep,find,ls`. This does not disable the managing agent's MCP skill-guidance endpoints.
- Pi code runs non-interactively with project trust enabled and the normal Pi tool set.
- A custom harness may accept explore work only when its adapter explicitly provides a read-only command. A prompt saying “read only” is not a sandbox.
- Every explore task forces `commit_mode=forbidden` and receives a before/after Git-status audit when it runs in a repository.

Choose explore for search, mapping, evidence collection, code review, or independent verification. Choose code only when file or repository changes are required.

## Build a bounded request

Provide `task` or `goal` and keep `cwd` narrow. Add only the fields that improve the execution contract:

- `files_in_scope`: paths the delegate may inspect or change.
- `out_of_scope`: paths, actions, or topics to avoid.
- `context_files`: important files to read first.
- `acceptance_criteria`: observable conditions that must hold.
- `done_means`: required artifacts or evidence in the final manifest.
- `verification_commands`: relevant checks for code work.
- `commit_mode`: `allowed`, `required`, or `forbidden`; explore always becomes forbidden.
- `model` and `reasoning_effort`: omit/default to inherit the harness defaults unless a task needs an explicit override.
- `output_schema`: expected JSON shape metadata. It guides the delegate but is not server-side schema validation.

Prefer one cohesive module or concern per code task. Split unrelated work into separate calls.

Choose `commit_mode` deliberately for code work. The tool default is `allowed`; use `forbidden` when the task should leave reviewable working-tree changes and create no commit.

## Understand scheduling

- Explore tasks are project-scoped readers and may overlap within configured limits.
- Code tasks are project-scoped writers, exclusive and FIFO.
- Once a writer is queued, later readers cannot overtake it.
- Linked Git worktrees sharing one common Git directory share the same writer lane.
- Different projects schedule independently within global limits.
- `delegate_batch` creates a read-only group barrier. Every child is implicitly explore with commits forbidden; do not put `kind` or `commit_mode` in child specifications. `max_concurrency` caps that group within server limits.
- `depends_on_group_ids` waits for groups to become terminal, not necessarily successful. Inspect the group result and require `status=succeeded` before relying on its findings.
- Dependencies control scheduling only. They do not inject one harness's output into another harness's prompt. Read the completed child results/logs, then explicitly summarize the necessary evidence in the follow-on `task`/`goal` or pass existing result paths through `context_files`.

## Wait, monitor, and cancel

- `wait_seconds` controls only how long the current MCP call waits. On expiry, use the returned `delegate_id` or `group_id` with `delegate_status`.
- `execution_timeout_seconds` is the hard subprocess lifetime. On expiry, the server sends TERM, waits for the cancel grace period, then sends KILL.
- Use `delegate_status(watch_seconds=...)` for lifecycle long polling; the maximum watch window is 300 seconds.
- Query by exactly one of `delegate_id`, `group_id`, or `project_cwd`, or omit all for global active/recent state.
- Use `delegate_cancel` with exactly one delegate or group identifier. Cancellation is not a rollback; inspect Git state after cancelling code work.

## Read results and logs

Completed results intentionally omit raw stdout and stderr. Use the returned `logs` or `log_read_hint` paths:

- `prompt`: exact execution prompt.
- `stdout`: final manifest or requested structured JSON.
- `stderr`: progress and diagnostic output.
- `metadata`: lifecycle, process, harness, byte counts, and result details.

When `parse_structured_output=true`, the server makes a best-effort parse of stdout first and stderr second. It accepts a final fenced JSON block or a whole-stream JSON value. A parse failure returns `structured_output=null`; read the logs. Review files and run direct verification even when the delegate reports success.

## Recover from failures

| Error/status | Response |
| --- | --- |
| `unsupported_delegate_harness` | Call `server_info`, choose a configured harness, and retry. |
| `delegate_harness_unavailable` / `codex_unavailable` | Check the configured command and runtime PATH. |
| `readonly_sandbox_unavailable` | Use a harness that explicitly supports read-only explore tasks. Do not bypass the guard. |
| `delegate_queue_full` | Monitor existing work, cancel obsolete work if authorized, then retry later. |
| `process_start_failed` / `process_failed` | Read stderr and metadata, correct the command/task, and submit a new bounded delegate. |
| `readonly_violation` | Stop trusting the explore result, inspect Git state, and determine what changed. |
| `readonly_audit_unavailable` | Inspect repository health and status before retrying. |
| `timed_out` | Read partial logs and decide whether to split the task or use a larger justified execution timeout. |
| `cancelled` | Inspect partial output and repository state before resubmitting. |

Do not assume retries are idempotent, especially for code tasks.

## Minimal examples

Read-only exploration:

```json
{"task":"Map the scheduler entry points and return file:line evidence.","kind":"explore","harness":"pi","cwd":"/path/to/repo","files_in_scope":["src"],"commit_mode":"forbidden"}
```

Independent exploration batch:

```json
{"tasks":[{"task":"Find invocation construction."},{"task":"Find scheduler fairness tests."}],"harness":"codex","cwd":"/path/to/repo","max_concurrency":2,"wait_seconds":30}
```

Bounded implementation:

```json
{"task":"Implement the approved adapter change.","kind":"code","harness":"pi","cwd":"/path/to/repo","files_in_scope":["src/module.py","tests/test_module.py"],"acceptance_criteria":["Existing behavior remains compatible","Targeted tests pass"],"verification_commands":["pytest -q tests/test_module.py"],"commit_mode":"forbidden","wait_seconds":30}
```
"""


SKILL_INDEX = {
    "version": SKILL_GUIDANCE_VERSION,
    "namespace": SKILL_NAMESPACE,
    "resource_uri": SKILL_INDEX_URI,
    "discovery_tool": "get_skill_index",
    "usage": (
        "Call the listed guide tool before the first matching workflow, then follow "
        "its critical rules. Refresh after a server upgrade or when tool behavior differs."
    ),
    "skills": [
        {
            "name": "delegate-use",
            "description": (
                "Safe, bounded use of delegate_task, delegate_batch, delegate_status, "
                "and delegate_cancel across Codex, Pi, and custom CLI harnesses."
            ),
            "triggers": [
                "Before the first delegate tool call in a task",
                "When choosing a harness or explore/code kind",
                "When monitoring, cancelling, or recovering delegate work",
            ],
            "required_before_tools": [
                "delegate_task",
                "delegate_batch",
                "delegate_status",
                "delegate_cancel",
            ],
            "guide_tool": "get_delegate_use",
            "resource_uri": DELEGATE_USE_URI,
        }
    ],
}


def skill_index_payload() -> dict[str, object]:
    return json.loads(json.dumps(SKILL_INDEX))


def skill_index_json() -> str:
    return json.dumps(SKILL_INDEX, ensure_ascii=False, indent=2) + "\n"


def delegate_use_payload() -> dict[str, object]:
    return {
        "success": True,
        "name": "delegate-use",
        "version": SKILL_GUIDANCE_VERSION,
        "resource_uri": DELEGATE_USE_URI,
        "content_type": "text/markdown",
        "content": DELEGATE_USE_GUIDE,
    }
