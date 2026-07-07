# AGENTS.md — chatgpt-web-oauth-mcp

## What is this?

A local MCP (Model Context Protocol) server that lets ChatGPT Web call local filesystem, shell, git, and delegated coding tools through an HTTPS MCP endpoint with OAuth. Built with Python 3.11+ and FastMCP. Local endpoint: `http://127.0.0.1:8766/mcp`.

## Architecture

```text
ChatGPT Web ──OAuth + HTTPS──▶ FastMCP Server (uvicorn)
                                  │
                  ┌───────────────┼───────────────┐
                  ▼               ▼               ▼
            Direct Tools     Shell Tool     Codex Delegate
           (inspect/edit)   (short local)  (one bounded execution slice)
```

ChatGPT Web is the architect/manager/reviewer. It should inspect, plan, and
review through direct MCP tools first. `delegate_task` is only a single-task
Codex executor for a bounded Codex Execution Prompt, not a broad opaque planning
or research loop.

## Source layout

```text
src/chatgpt_web_oauth_mcp/
├── server.py      # FastMCP composition, HTTP app integration, uvicorn entrypoint / fd-aware child
├── tool_context.py # Shared runtime lookup context and MCP tool annotations
├── tools_core.py  # server_info and cwd tools
├── tools_files.py # list_files, search, read_text, write_file, apply_patch registration
├── tools_git_shell.py # git_*, synchronous shell, serial Codex delegate registration
├── tools_obsidian.py # optional obsidian_* proxy tool registration
├── config.py      # Env-var driven settings
├── oauth.py       # OAuth dynamic registration, PKCE, token store, metadata
├── http_compat.py # ChatGPT-compatible HTTP/OAuth/MCP compatibility layer
├── pathing.py     # Path resolution: relative -> absolute under WORKSPACE_ROOT
├── files.py       # list_files, read_text, write_file
├── search.py      # glob/regex/text search implementations
├── shell.py       # run_command subprocess helper
├── executors.py   # synchronous serialized delegate_task via Codex
└── supervisor.py  # rolling-reload supervisor for tunnels / launchd
```

## Tools exposed

| Tool | Purpose |
|---|---|
| `server_info` | Inspect runtime config and available MCP tools |
| `set_default_cwd` / `get_default_cwd` | Manage session default working directory |
| `list_files` | List directory contents |
| `search` | Glob, regex, literal text search, or batch search with `mode="sequential"` / `mode="parallel"`; parallel batches cap `max_concurrency` at 3 |
| `read_text` | Single/batch text reader with pagination |
| `write_file` | Create or overwrite a file, with dry-run support |
| `apply_patch` | Structured patch editing for existing files |
| `git_status` / `git_diff` / `git_commit` / `git_log` / `git_show` / `git_blame` | Structured git workflows |
| `run_command` | Execute one shell command, or multiple commands with `mode="sequential"` or `mode="parallel"`; timeout is capped at 300s unless `force=true` is used after explicit user approval; parallel batches cap `max_concurrency` at 3 |
| `delegate_task` | Run one serialized, bounded Codex execution slice; wait up to 300s by default, then return either the result or `status=running` with readable log paths while Codex continues |
| `delegate_status` | Read-only active/recent delegate status list with server-generated `delegate_id` values and log paths; supports `watch_seconds` long-polling up to 300s |
| `obsidian_*` tools | Optional Obsidian native MCP proxy tools; only registered when `CHATGPT_MCP_ENABLE_OBSIDIAN=1` |

## Key concepts

- `WORKSPACE_ROOT` is a default cwd / relative-path anchor, not a sandbox boundary. Set it with `CHATGPT_MCP_WORKSPACE_ROOT`.
- OAuth mode is enabled with `CHATGPT_MCP_AUTH_MODE=oauth`.
- `CHATGPT_MCP_PUBLIC_BASE_URL` must be set in OAuth mode so issuer and resource URLs are stable and not Host-header-derived.
- Prefer separate `CHATGPT_MCP_AUTH_TOKEN` and `CHATGPT_MCP_OAUTH_LOGIN_TOKEN` values.
- `delegate_task` is intentionally Codex-only and single-flight. It should receive one small execution prompt with `files_in_scope`, `out_of_scope`, `acceptance_criteria`, `done_means`, and verification commands when possible. A caller-provided `task_id` is optional; the server always returns a generated `delegate_id`, and stateless clients can call `delegate_status` to recover active/recent delegate ids. It long-polls the active delegate for up to 300 seconds by default; if Codex is still running, it returns `status=running` and the client should call `delegate_task` again to continue waiting. `delegate_status` can also long-poll with `watch_seconds=300` and returns early only when task status changes. Each delegate writes private audit logs under the system temporary cache directory (`prompt.txt`, `stdout.log`, `stderr.log`, `metadata.json`) and returns their paths in `logs`; callers can use `read_text` on those paths to inspect live progress. Completed delegate responses do not inline raw stdout/stderr; read the returned logs for raw output. No TaskBoard, Claude delegate, or skill-discovery tools are exposed.

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
