from __future__ import annotations

import json


SKILL_GUIDANCE_VERSION = "1.5"
SKILL_NAMESPACE = "chatgpt-web-oauth-mcp"
SKILL_INDEX_URI = f"skill://{SKILL_NAMESPACE}/index"
DELEGATE_USE_URI = f"skill://{SKILL_NAMESPACE}/delegate-use"
FILE_USE_URI = f"skill://{SKILL_NAMESPACE}/file-use"
PROCESS_USE_URI = f"skill://{SKILL_NAMESPACE}/process-use"
RUNTIME_USE_URI = f"skill://{SKILL_NAMESPACE}/runtime-use"
GIT_USE_URI = f"skill://{SKILL_NAMESPACE}/git-use"


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


FILE_USE_GUIDE = """---
name: file-use
description: Use the local file discovery, search, reading, code-map, and editing tools safely. Load before the first nontrivial file workflow or file mutation, and when handling pagination, token budgets, encodings, revisions, PDFs, images, or binary data.
---

# File Use

## Critical rules

1. Treat the workspace root as a relative-path anchor, not a sandbox boundary. Keep every path intentionally scoped.
2. Inspect before mutating: discover, search, read the relevant region, assess references/imports when useful, then edit and verify.
3. Follow `next_offset` or the relevant line/page/byte cursor whenever `partial=true`; never assume the first page is complete.
4. Prefer `apply_patch` for semantic edits, `replace` for bounded mechanical replacements, and `write_file` only for deliberate whole-file creation or overwrite.
5. Use dry-run/validation and revision checks for risky or multi-file edits. Preserve user changes and re-read after a revision conflict.

## Choose the tool

| Need | Tool |
| --- | --- |
| Browse a directory with ignore-aware filtering | `list_files` |
| Find paths, literal text, or regex matches | `search` |
| Read line-oriented text, singly or in a batch | `read_text` |
| Read text with encoding metadata, an image, PDF pages, or binary hex | `read` |
| Locate definitions, textual references, or imports | `code_map_symbols`, `code_map_references`, `code_map_imports` |
| Create or intentionally replace a whole file | `write_file` |
| Apply literal/regex replacements with revision protection | `replace` |
| Apply a structured semantic patch | `apply_patch` |

Use `list_files` for shape, `search` for candidates, and `read_text`/`read` for evidence. Code-map results are lightweight navigation aids: references are identifier-boundary text matches, not a language-server proof of semantic usage.

## Discover and paginate

- `list_files` hides hidden paths, common junk, and Git-ignored paths by default. Use `filter=all` only when hidden/ignored content is intentionally in scope.
- `search` supports `glob`, literal `text`, and `regex`, plus sequential or parallel batches. A batch has at most 20 queries and parallel concurrency is capped at 3.
- Search output modes are `content`, `files_with_matches`, `count`, and `summary`. Choose the smallest representation that answers the question.
- Results are token-budgeted. Inspect `complete`, `partial`, `truncated`, `stop_reason`, `estimated_tokens`, and `effective_budget`.
- Continue with `next_offset`; for text reads use `start_line`, for PDFs use page ranges/offsets, and for hex use `byte_offset`. Do not invent a cursor from the number of visible items.

## Read the right representation

- `read_text` uses 1-based lines and is the compact choice for source evidence. It may return one oversized line intact while still advancing `next_offset`.
- `read(mode=auto)` selects text/image/PDF/hex handling. Text results include encoding, BOM, and newline metadata.
- Text decoding is conservative: BOMs are recognized and UTF-8 is strict. On `encoding_error`, use the reported candidates or an explicit encoding; when NUL bytes indicate binary data, retry with `mode=hex`.
- PDF page numbers are 1-based. The default reads only an initial bounded set and one call is capped, so continue explicitly when more pages matter.
- Hex reads are byte-based and bounded; use them for inspection, not as a substitute for an appropriate binary parser.

## Edit safely

- `write_file(dry_run=true)` previews a whole-file write without touching disk.
- `apply_patch(dry_run=true)` or `validate_only=true` checks a patch without writing. Use `return_diff=true` when review needs the resulting diff.
- `replace` requires nonempty operations and rules. Start with `replace(dry_run=true)` and capture each returned `before_revision`; ordinary read tools do not return this revision. Before the real write, put that value into the matching operation's `expected_revision` for compare-and-swap protection.
- `revision_conflict` means another writer changed a target: re-read, re-plan, dry-run again, and use the new `before_revision`. Never remove the check or blindly retry.
- A replace batch is planned and revision-checked before its first write. Replacement limits are batch-wide; exceeding them rejects the batch. Writes are atomic per file, and a later failure triggers best-effort rollback of already attempted files.
- Replacement writes preserve detected encoding, BOM, newline convention, and permissions, and report `rolled_back`/`rollback_errors` when a write fails.
- After any mutation, re-read the changed region and use Git diff or the relevant verification command.

## Recover from errors

| Error | Response |
| --- | --- |
| `path_not_found` / `not_a_directory` | Recheck the resolved path and current default cwd. |
| `encoding_error` | Retry with a reported encoding candidate or inspect as hex. |
| `revision_conflict` | Re-read the current content/revision, reassess user changes, and rebuild the edit. |
| `budget_exceeded` | Narrow scope or lower result density; do not assume omitted data is absent. |
| `write_failed` | Inspect `rolled_back` and `rollback_errors`, then verify every target before retrying. |
| Invalid search pattern/backend error | Correct the mode/pattern or confirm the required local backend is installed. |

## Minimal examples

```json
{"mode":"glob","path":"src","pattern":"*.py","limit":20,"offset":0}
```

```json
{"path":"src/app.py","start_line":120,"line_limit":40,"include_line_numbers":true}
```

```json
{"operations":[{"path":"src/app.py","rules":[{"pattern":"old","replacement":"new","literal":true}],"expected_revision":"<sha256>"}],"dry_run":true}
```
"""


PROCESS_USE_GUIDE = """---
name: process-use
description: Choose and operate synchronous commands, durable background jobs, and persistent tmux sessions. Load before the first run_command, job, or tmux workflow, especially for timeouts, long-running work, streaming logs, interactive input, or termination.
---

# Process Use

## Critical rules

1. Choose the lifecycle first: short synchronous command, durable background process, or interactive terminal session.
2. Use the narrowest cwd and command that satisfies the request. Shell access is not a filesystem sandbox.
3. A timeout or accepted input is not proof of completion. Inspect the returned status, exit code, logs, or terminal capture.
4. Kill only the exact registered job or tmux session requested. Termination does not roll back filesystem or external side effects.
5. Never use `force=true` to exceed the normal command timeout unless the user explicitly approved the longer synchronous execution.

## Choose the execution model

| Need | Tool family |
| --- | --- |
| One short non-interactive command whose result should return now | `run_command` |
| A long-running/non-interactive process with durable stdout/stderr logs | `job_start`, `job_status`, `job_output`, `job_tail`, `job_kill` |
| An interactive TTY, persistent shell/application, or key input | `tmux_start`, `tmux_status`, `tmux_capture`, `tmux_send`, `tmux_kill` |

Do not use a delegate for a deterministic command, a job for an interactive prompt, or tmux when lossless stdout/stderr logs are required.

## Run synchronous commands

- Provide exactly one of `command` or `commands`. Batch mode is `sequential` or `parallel`, has at most 20 commands, and parallel concurrency is capped at 3.
- Each command has its own timeout and batch results preserve input order. Inspect `completed`, `failed`, and `timed_out` rather than only the batch envelope.
- The normal timeout ceiling is 300 seconds. Above it, `force=true` is required and is reserved for explicit user-approved long synchronous work; otherwise use a job.
- On timeout, the server terminates the command process tree. Treat partial output and side effects as real.

## Operate durable jobs

- `job_start` launches a registered background subprocess with private stdout/stderr logs and a durable state record discoverable after MCP/server restarts. It has no execution-time limit; the process runs until it exits or an authorized `job_kill` stops it.
- Jobs do not provide scheduling, automatic restart, dependencies, or artifact tracking. Build those semantics explicitly outside the job API.
- Save the returned `job_id`. If client state is lost, recover it with the newest-first, paginated `job_list`, then confirm identity and lifecycle with `job_status`.
- Use `job_status` for lifecycle/PID/exit information. Use `job_output` for lossless incremental reads with a raw-byte `cursor` and returned `next_cursor`.
- Stdout and stderr have independent cursors and no merged cross-stream ordering. `wait_ms` long-polls one stream for up to 30 seconds.
- Consume stdout and stderr separately: reuse each stream's `next_cursor`, continue while `has_more=true`, and treat `eof=true` as that stream being caught up after the job is terminal. Poll status as well; a quiet stream is not proof the job finished.
- `job_tail` is a quick bounded last-lines view, not a replacement for cursor-based consumption.
- `job_kill` targets only a server-registered job process group. Default to TERM; use KILL only when graceful termination has failed or is inappropriate.

## Operate tmux sessions

- `tmux_start` creates one detached primary-pane workflow. Session names are restricted identifiers; choose a unique stable name. Width and height are bounded.
- `remain_on_exit=true` preserves the pane after its command exits so status/capture can report the exit result.
- `tmux_status` reports panes, PIDs, current commands/paths, dimensions, and dead/exit state.
- `tmux_capture` returns a bounded visible terminal snapshot. It is not a complete or lossless stdout/stderr log and wrapped lines may need `join_wrapped`.
- `tmux_send` passes UTF-8 text through tmux's input buffer and supports only an allowlist of keys. `accepted_by_tmux=true` does not mean the application consumed the input; capture/status must verify progress.
- `tmux_kill` is idempotent and targets one exact session. No tool kills the entire tmux server.

## Recover from failures

| Error/status | Response |
| --- | --- |
| `cwd_not_found` / `cwd_not_directory` | Correct the resolved working directory before retrying. |
| `timed_out` | Inspect partial output/side effects; move long work to a job or use an approved larger limit. |
| Job `failed` / `interrupted` | Read both streams and status before deciding whether a restart is safe. |
| `session_exists` | Reuse/inspect the intended session or choose a new exact name. |
| `session_not_found` | List sessions on the configured socket and check the name. |
| Command/process start failure | Verify executable availability, PATH, cwd, and arguments. |

## Minimal examples

```json
{"command":"pytest -q tests/test_module.py","timeout":120}
```

```json
{"command":"python train.py","name":"training","cwd":"/path/to/project"}
```

Then consume one stream with `{"job_id":"job_...","stream":"stdout","cursor":0,"wait_ms":1000}`.

```json
{"session":"debug-api","cwd":"/path/to/project","command":"python -m app","remain_on_exit":true}
```
"""


RUNTIME_USE_GUIDE = """---
name: runtime-use
description: Manage persistent Codex runtimes with stable logical identities, reuse, bounded concurrency, and automatic detached-binding garbage collection. Load before the first codex_runtime_* or codex_mcp_* workflow.
---

# Runtime Use

## Critical rules

1. For recurring logical workers, use `codex_runtime_acquire` with a stable name such as `manager`, `plm-worker-01`, or `research-worker-02`. Do not create timestamp-suffixed names for every run unless a truly separate runtime is required.
2. Use `codex_runtime_list` to discover existing bindings and `codex_runtime_resume` when a specific known runtime identity must be continued. Use `codex_runtime_open` only for intentionally new isolated runtimes.
3. `codex_runtime_close` detaches the local binding; it does not archive or delete the upstream Codex thread. Any binding with no runtime use beyond the configured idle TTL is eligible for automatic GC, while capacity-driven LRU eviction is detached-only.
4. Capacity LRU never evicts a `ready` runtime. Idle-TTL GC can collect a `ready` binding only after it has had no runtime operation for the full TTL. Check `server_info.codex_runtime` for `max_concurrency`, `max_runtimes`, `idle_ttl_seconds`, and GC counters instead of assuming limits.
5. Runtime GC removes the local resumable binding only. If a binding has been collected, acquire the stable logical name again instead of assuming its old `runtime_id` remains valid.

## Choose the tool

| Need | Tool |
| --- | --- |
| Reuse or create a recurring named runtime | `codex_runtime_acquire` |
| Discover existing bindings | `codex_runtime_list` |
| Create a deliberately distinct runtime | `codex_runtime_open` |
| Resume one known binding/thread | `codex_runtime_resume` |
| Inspect one runtime | `codex_runtime_status` |
| Detach a runtime for later reuse | `codex_runtime_close` |
| Inspect or call MCPs connected through Codex | `codex_mcp_inventory`, `codex_mcp_call` |

## Lifecycle

Preferred recurring-worker flow:

```text
stable worker name
      ↓
codex_runtime_acquire
      ↓
reuse / resume existing binding, or open once
      ↓
work through codex_mcp_* as needed
      ↓
codex_runtime_close when the worker is idle
      ↓
reuse before TTL, otherwise acquire recreates it later
```

The server defaults to an 8-hour idle TTL for every binding. Capacity pressure additionally evicts the least-recently-used detached bindings first. Both policies are local binding lifecycle controls; neither performs destructive upstream thread deletion.

## Stable naming

- Names represent logical workers, not invocations. Prefer `manager` over `manager-20260917-1908`.
- Keep `cwd` and sandbox policy stable for the same logical name. `codex_runtime_acquire` matches all three fields exactly.
- Separate workers that may operate concurrently should have separate stable names, for example `plm-worker-01`, `plm-worker-02`, and `plm-worker-03`.
- If a workflow intentionally needs a fresh isolated context, use `codex_runtime_open` and give it a distinct descriptive name.

## Recover from errors

| Error/status | Response |
| --- | --- |
| `runtime_not_found` | The binding may have been GC/LRU collected; acquire the stable logical name again. |
| `runtime_not_active` | Resume the known runtime or acquire its stable logical name. |
| `runtime_limit_reached` | Inspect runtime counts; capacity LRU does not evict ready runtimes, so release obsolete active work before retrying. |
| `runtime_concurrency_limit` | Wait for one configured concurrent runtime operation to finish; do not create more runtimes to bypass the limit. |
| metadata mismatch | Keep the original cwd/sandbox for that binding or intentionally acquire/open a distinct runtime. |

## Minimal examples

Recurring worker:

```json
{"cwd":"/home/user/workspace","name":"plm-worker-01","sandbox":"full-access"}
```

Discover it later:

```json
{"name":"plm-worker-01","status":"detached","limit":10}
```
"""


GIT_USE_GUIDE = """---
name: git-use
description: Inspect repositories, review and create scoped commits, examine history, and manage Git worktrees safely. Load before the first Git workflow, especially before staging, committing, amending, creating/removing worktrees, or using force.
---

# Git Use

## Critical rules

1. Start with `git_status`; preserve unrelated staged, unstaged, and untracked user changes.
2. Review the relevant unstaged and staged diffs before committing. A clean-looking path filter does not prove the whole index is clean.
3. Stage narrowly with `paths` by default. Use `stage_all=true`, `amend=true`, `allow_empty=true`, or worktree `force=true` only when the requested scope clearly requires it.
4. Repository paths are resolved for convenience but the workspace root is not a sandbox boundary. Verify `repo_root` and every worktree target.
5. Removing a worktree with force can destroy uncommitted and untracked data. Inspect, preserve, or commit it first.

## Choose the tool

| Need | Tool |
| --- | --- |
| Inspect branch and staged/unstaged/untracked state | `git_status` |
| Review bounded unstaged or staged changes | `git_diff` |
| Stage selected paths and create a commit | `git_commit` |
| Browse recent commits | `git_log` |
| Inspect one commit/ref and its bounded diff | `git_show` |
| Attribute a file or line range | `git_blame` |
| Create/list/inspect/remove linked worktrees | `git_worktree_create`, `git_worktree_list`, `git_worktree_status`, `git_worktree_remove` |

## Review and commit

1. Call `git_status(cwd=...)` and confirm `repo_root`, branch/detached state, and all change categories.
2. Call `git_diff(staged=false, paths=[...])` for working-tree changes and inspect the complete index with `git_diff(staged=true)` before a commit. Diffs are byte-bounded and paginated; follow `next_offset` when partial.
3. If the index contains any unrelated staged path, do not call `git_commit`: this structured API has no index-isolation operation. Ask the user to preserve/unstage those entries manually or explicitly authorize a reviewed `run_command` workflow that isolates and restores the index.
4. Once the index is free of unrelated entries, use `git_commit(paths=[...], dry_run=true)` to preview a narrow stage/commit plan when scope is nontrivial.
5. Commit with explicit `paths` and a meaningful message. Re-run status and inspect the created commit afterward.

`git_commit(paths=...)` stages those paths. `stage_all=true` runs the broad equivalent of `git add -A`. Existing staged changes remain staged and may enter the commit, so always inspect the staged diff. A failed commit may also leave paths staged.

`amend=true` rewrites HEAD and may affect already-shared history. `allow_empty=true`, author overrides, and sign-off are specialized options; use them only when requested or required by repository policy.

## Inspect history

- Use `git_log` to select a commit, then `git_show(ref=...)` for metadata, body, parents, and a bounded per-file diff.
- Use `git_blame(path=..., start_line=..., end_line=...)` for targeted attribution, then inspect the referenced commit with `git_show` before drawing conclusions.
- Unknown refs and invalid line/path selections should be corrected, not silently replaced with HEAD or a broader range.

## Manage worktrees

- List existing worktrees before creation. Use an explicit, reviewed target path and base ref.
- `mode=clean` creates a new branch worktree; specify `branch` explicitly when naming matters. `mode=detached` creates no branch and cannot be combined with `branch`.
- Linked worktrees share repository state and, in this server, the same delegate writer lane.
- Inspect `git_worktree_status(path=...)` before removal. Normal removal refuses dirty worktrees.
- `git_worktree_remove(force=true)` may discard modified and untracked files. Use it only after explicit confirmation that the data can be lost or has been preserved elsewhere.

## Recover from failures

| Error | Response |
| --- | --- |
| `not_a_git_repo` | Correct cwd and confirm the intended repository root. |
| `git_add_failed` / `git_commit_failed` | Re-run status and staged diff; do not assume the index was restored. |
| `nothing_to_commit` | Recheck scope and staging; do not use `allow_empty` merely to suppress the error. |
| `git_show_failed` / `git_blame_failed` | Validate the ref, path, and requested line range. |
| `worktree_not_found` | List registered worktrees and use the exact registered path. |
| `worktree_dirty` | Inspect and preserve changes; avoid force unless loss is explicitly acceptable. |

## Minimal examples

```json
{"cwd":"/path/to/repo"}
```

```json
{"cwd":"/path/to/repo","staged":true,"paths":["src/app.py"]}
```

```json
{"cwd":"/path/to/repo","message":"feat: add API","paths":["src/app.py"],"dry_run":true}
```

```json
{"cwd":"/path/to/repo","path":"../worktrees/api","base_ref":"HEAD","mode":"clean","branch":"feature/api"}
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
            "name": "file-use",
            "description": (
                "Safe discovery, search, reading, code navigation, and file mutation "
                "with pagination, encoding, dry-run, and revision protection."
            ),
            "triggers": [
                "Before the first nontrivial file workflow or file mutation",
                "When handling pagination, token budgets, encodings, PDFs, images, or binary data",
                "When recovering from revision conflicts or multi-file write failures",
            ],
            "required_before_tools": [
                "list_files",
                "search",
                "read_text",
                "read",
                "code_map_symbols",
                "code_map_references",
                "code_map_imports",
                "write_file",
                "replace",
                "apply_patch",
            ],
            "guide_tool": "get_file_use",
            "resource_uri": FILE_USE_URI,
        },
        {
            "name": "process-use",
            "description": (
                "Selection and lifecycle management for synchronous commands, durable "
                "background jobs, and persistent interactive tmux sessions."
            ),
            "triggers": [
                "Before the first run_command, job, or tmux workflow",
                "When choosing between synchronous, background, and interactive execution",
                "When monitoring output, handling timeouts, or terminating a process/session",
            ],
            "required_before_tools": [
                "run_command",
                "job_start",
                "job_list",
                "job_status",
                "job_output",
                "job_tail",
                "job_kill",
                "tmux_list",
                "tmux_start",
                "tmux_status",
                "tmux_capture",
                "tmux_send",
                "tmux_kill",
            ],
            "guide_tool": "get_process_use",
            "resource_uri": PROCESS_USE_URI,
        },
        {
            "name": "runtime-use",
            "description": (
                "Stable Codex runtime acquisition/reuse with idle-TTL GC and "
                "capacity-driven LRU eviction."
            ),
            "triggers": [
                "Before the first codex_runtime_* or codex_mcp_* workflow",
                "When recurring workers need persistent runtime identities",
                "When diagnosing runtime capacity, GC, LRU, or concurrency behavior",
            ],
            "required_before_tools": [
                "codex_runtime_acquire",
                "codex_runtime_list",
                "codex_runtime_open",
                "codex_runtime_resume",
                "codex_runtime_status",
                "codex_runtime_close",
                "codex_mcp_inventory",
                "codex_mcp_call",
            ],
            "guide_tool": "get_runtime_use",
            "resource_uri": RUNTIME_USE_URI,
        },
        {
            "name": "git-use",
            "description": (
                "Safe repository inspection, scoped staging and commits, history analysis, "
                "and Git worktree lifecycle management."
            ),
            "triggers": [
                "Before the first Git workflow",
                "Before staging, committing, amending, or creating/removing worktrees",
                "When reviewing history, blame, staged state, or destructive worktree options",
            ],
            "required_before_tools": [
                "git_status",
                "git_diff",
                "git_commit",
                "git_log",
                "git_show",
                "git_blame",
                "git_worktree_create",
                "git_worktree_list",
                "git_worktree_status",
                "git_worktree_remove",
            ],
            "guide_tool": "get_git_use",
            "resource_uri": GIT_USE_URI,
        },
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


def file_use_payload() -> dict[str, object]:
    return _guide_payload("file-use", FILE_USE_URI, FILE_USE_GUIDE)


def process_use_payload() -> dict[str, object]:
    return _guide_payload("process-use", PROCESS_USE_URI, PROCESS_USE_GUIDE)


def runtime_use_payload() -> dict[str, object]:
    return _guide_payload("runtime-use", RUNTIME_USE_URI, RUNTIME_USE_GUIDE)


def git_use_payload() -> dict[str, object]:
    return _guide_payload("git-use", GIT_USE_URI, GIT_USE_GUIDE)


def _guide_payload(name: str, resource_uri: str, content: str) -> dict[str, object]:
    return {
        "success": True,
        "name": name,
        "version": SKILL_GUIDANCE_VERSION,
        "resource_uri": resource_uri,
        "content_type": "text/markdown",
        "content": content,
    }
