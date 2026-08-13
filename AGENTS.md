# AGENTS.md — chatgpt-web-oauth-mcp

## What is this?

A local MCP (Model Context Protocol) server that lets ChatGPT Web call local filesystem, shell, persistent tmux, git, and delegated coding tools through an HTTPS MCP endpoint with OAuth. Built with Python 3.11+ and FastMCP. Local endpoint: `http://127.0.0.1:8766/mcp`.

## Architecture

```text
ChatGPT Web ──OAuth + HTTPS──▶ FastMCP Server (uvicorn)
                                  │
                  ┌───────────────┼───────────────┐
                  ▼               ▼               ▼
            Direct Tools     Shell Tool     CLI Harness Delegate
           (inspect/edit)   (short local)  (project-scoped readers/writer)
```

ChatGPT Web is the architect/manager/reviewer. It should inspect, plan, and
review through direct MCP tools first. `delegate_task` submits one bounded
read-only exploration reader or coding writer; `delegate_batch` fans out only
read-only exploration. Neither is a broad opaque planning or research loop.

## Source layout

```text
src/chatgpt_web_oauth_mcp/
├── server.py      # FastMCP composition, HTTP app integration, uvicorn entrypoint / fd-aware child
├── tool_context.py # Shared runtime lookup context and MCP tool annotations
├── tools_core.py  # server_info, cwd, and env_snapshot/env_diff tools
├── tools_files.py # list_files, search, read_text, code_map_*, write_file, apply_patch registration
├── tools_git_shell.py # git_*, synchronous shell, and delegate tool registration
├── tools_tmux.py # tmux list/start/status/capture/send/kill MCP registration
├── config.py      # Env-var driven settings
├── oauth.py       # OAuth dynamic registration, PKCE, token store, metadata
├── http_compat.py # ChatGPT-compatible HTTP/OAuth/MCP compatibility layer
├── pathing.py     # Path resolution: relative -> absolute under WORKSPACE_ROOT
├── response_budget.py # Shared o200k response measurement and common pagination metadata
├── content_io.py  # Strict text encoding/BOM/newline detection and lossless re-encoding
├── reader.py      # Unified text/image/PDF/hex reader
├── replacing.py   # Locked CAS mechanical batch replacement and atomic writes
├── envtools.py    # Read-only environment snapshots and inline snapshot diffs
├── code_map.py    # Lightweight symbol/reference/import mapping
├── files.py       # list_files, read_text, write_file
├── search.py      # glob/regex/text search implementations
├── shell.py       # run_command subprocess helper
├── tmux_ops.py    # bounded persistent tmux session wrapper
├── delegate_models.py # Delegate task/group/project domain models
├── delegate_harnesses.py # Pluggable Codex, Pi, and generic CLI harness adapters
├── delegate_guidance.py # Authoritative progressive-disclosure delegate operating guide
├── delegate_project.py # Worktree-aware project identity resolution
├── delegate_scheduler.py # Project-scoped fair reader/writer scheduling
├── delegate_process.py # Harness process, logs, hard timeout, and cancellation
├── executors.py   # Backward-compatible delegate facade
├── tools_skills.py # Skill-index/use tools and matching MCP resources
└── supervisor.py  # rolling-reload supervisor for tunnels / launchd
```

## Tools exposed

| Tool | Purpose |
|---|---|
| `server_info` | Inspect runtime config and available MCP tools |
| `get_skill_index` / `get_*_use` | Discover and load file, process, Git, or delegate operating guides before matching workflows |
| `set_default_cwd` / `get_default_cwd` | Manage session default working directory |
| `env_snapshot` / `env_diff` | Read-only runtime diagnostics and inline snapshot comparison |
| `list_files` | Ignore-aware directory listing with sort/type filters, stable pagination, and token budgets |
| `search` | Glob, regex, literal text search, or batch search with `mode="sequential"` / `mode="parallel"`; parallel batches cap `max_concurrency` at 3 |
| `read_text` | Backward-compatible single/batch text reader with line pagination and a shared batch budget |
| `read` | Unified text/encoding, image, PDF-page, and binary-hex reader |
| `code_map_symbols` / `code_map_references` / `code_map_imports` | Tiny read-only code map for definitions, textual references, and imports |
| `write_file` | Create or overwrite a file, with dry-run support |
| `replace` | CAS-protected mechanical batch replacement with locking, atomic writes, and format preservation |
| `apply_patch` | Structured patch editing for existing files |
| `git_status` / `git_diff` / `git_commit` / `git_log` / `git_show` / `git_blame` | Structured git workflows |
| `git_worktree_create` / `git_worktree_list` / `git_worktree_status` / `git_worktree_remove` | Tiny generic git worktree lifecycle |
| `run_command` | Execute one shell command, or multiple commands with `mode="sequential"` or `mode="parallel"`; timeout is capped at 300s unless `force=true` is used after explicit user approval; parallel batches cap `max_concurrency` at 3 |
| `job_start` / `job_list` / `job_status` / `job_output` / `job_tail` / `job_kill` | Durable generic background jobs with disk-registry discovery, per-stream byte-cursor output, and backward-compatible last-N-lines tailing; no scheduler, restart, dependencies, or artifact tracking |
| `tmux_list` / `tmux_start` / `tmux_status` / `tmux_capture` / `tmux_send` / `tmux_kill` | Tiny persistent interactive TTY lifecycle; one primary-pane workflow, bounded capture, stdin-buffer text paste, and no attach or server-wide kill tool |
| `delegate_task` | Submit one read-only explore reader or exclusive code writer in a project lane |
| `delegate_batch` | Fan out read-only explores and fan in only after all group children terminate |
| `delegate_status` | Inspect delegate/group/project/global state with lifecycle long-polling up to 300s |
| `delegate_cancel` | Cancel one delegate or all children of one exploration group |

## Guidance resources

- `skill://chatgpt-web-oauth-mcp/index` is the machine-readable skill index.
- `skill://chatgpt-web-oauth-mcp/delegate-use` is the authoritative Markdown guide.
- `skill://chatgpt-web-oauth-mcp/file-use` covers discovery, reading, code maps, and safe file mutation.
- `skill://chatgpt-web-oauth-mcp/process-use` covers synchronous commands, durable jobs, and tmux sessions.
- `skill://chatgpt-web-oauth-mcp/git-use` covers repository inspection, commits, history, and worktrees.
- The matching tools exist for clients and gateways that expose tools more reliably than MCP resources.
- Keep guidance in `delegate_guidance.py`; do not maintain a second handwritten filesystem-skill copy.

## Key concepts

- `WORKSPACE_ROOT` is a default cwd / relative-path anchor, not a sandbox boundary. Set it with `CHATGPT_MCP_WORKSPACE_ROOT`.
- OAuth mode is enabled with `CHATGPT_MCP_AUTH_MODE=oauth`.
- `CHATGPT_MCP_PUBLIC_BASE_URL` must be set in OAuth mode so issuer and resource URLs are stable and not Host-header-derived.
- Prefer separate `CHATGPT_MCP_AUTH_TOKEN` and `CHATGPT_MCP_OAUTH_LOGIN_TOKEN` values.
- `tmux_*` defaults to the normal `default` tmux socket so sessions remain manually attachable. Use a separate `CHATGPT_MCP_TMUX_SOCKET_NAME` when isolation is preferred. `tmux_capture` is a terminal snapshot, not a lossless stdout/stderr log.
- Delegate scheduling uses a harness-neutral, project-scoped fair reader/writer model. Load `get_delegate_use` before the first delegate call in a task. `harness=codex` retains `gpt-5.6-luna + low` / `gpt-5.6-sol + xhigh` defaults; Codex explore uses `--sandbox read-only --ephemeral`. `harness=pi` inherits Pi's configured model by default; Pi explore disables extensions, Pi-local skills, project context, and sessions and restricts tools to `read,grep,find,ls`. This does not disable MCP skill-guidance tools/resources. Every explore forces `commit_mode=forbidden` and receives a defensive before/after Git status audit. Code is the single exclusive writer per project. A queued writer prevents later readers from overtaking it. Git common-dir is the project key, so linked worktrees share one writer lane. Different projects schedule independently within global resource limits. Wait windows do not kill processes; per-kind execution timeouts do, using TERM then KILL on the process group. Every delegate keeps private prompt/stdout/stderr/metadata logs. Use `delegate_status(delegate_id=...)` or `delegate_status(group_id=...)` after queued/running responses, and `delegate_cancel` for explicit termination. No TaskBoard tools are exposed.

## Development rules

- Keep this project ChatGPT Web / generic MCP focused; do not add product-specific workflow docs or screenshots.
- Prefer `apply_patch` for existing-file edits and `write_file` for new or fully rewritten short files.
- After code changes, run `pytest -q` and `python -m compileall src tests` when feasible.
- Do not commit secrets, local tunnel configs, `.env`, venvs, logs, or task state.

## Quick start

```bash
cp .env.example .env
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
./scripts/dev-tunnel.sh
```
