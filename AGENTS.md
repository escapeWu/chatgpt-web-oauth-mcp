# AGENTS.md — chatgpt-web-oauth-mcp

## What is this?

A local MCP (Model Context Protocol) server that lets ChatGPT Web call local filesystem, shell, git, and delegated coding tools through an HTTPS MCP endpoint with OAuth. Built with Python 3.11+ and FastMCP. Local endpoint: `http://127.0.0.1:8766/mcp`.

## Architecture

```text
ChatGPT Web ──OAuth + HTTPS──▶ FastMCP Server (uvicorn)
                                  │
                  ┌───────────────┼───────────────┐
                  ▼               ▼               ▼
            Direct Tools     Shell Tool     Delegate Tasks
           (files/search)   (run_command)  (codex/claude-code)
```

## Source layout

```text
src/chatgpt_web_oauth_mcp/
├── server.py      # FastMCP app, tool registration, uvicorn entrypoint / fd-aware child
├── config.py      # Env-var driven settings
├── oauth.py       # OAuth dynamic registration, PKCE, token store, metadata
├── http_compat.py # ChatGPT-compatible HTTP/OAuth/MCP compatibility layer
├── pathing.py     # Path resolution: relative -> absolute under WORKSPACE_ROOT
├── files.py       # list_files, read_text, write_file
├── search.py      # glob/regex/text search implementations
├── shell.py       # run_command subprocess helper
├── tasks.py       # Persistent task metadata and logs
├── executors.py   # async delegate_task via codex / claude-code
└── supervisor.py  # rolling-reload supervisor for tunnels / launchd
```

## Tools exposed

| Tool | Purpose |
|---|---|
| `server_info` | Inspect runtime config and available MCP tools |
| `set_default_cwd` / `get_default_cwd` | Manage session default working directory |
| `list_files` | List directory contents |
| `search` | Glob, regex, or literal text search |
| `read_text` | Single/batch text reader with pagination |
| `write_file` | Create or overwrite a file, with dry-run support |
| `apply_patch` | Structured patch editing for existing files |
| `git_status` / `git_diff` / `git_commit` / `git_log` / `git_show` / `git_blame` | Structured git workflows |
| `run_command` / `run_command_stream` | Execute shell commands |
| `delegate_task` | Submit long-running tasks to codex/claude-code |
| `get_task` / `wait_task` / `cancel_task` | Manage delegated/background tasks |
| `purge_tasks` | GC old task logs under `STATE_DIR/tasks` |
| `obsidian_*` tools | Optional Obsidian native MCP proxy tools; only registered when `CHATGPT_MCP_ENABLE_OBSIDIAN=1` |

## Key concepts

- `WORKSPACE_ROOT` is a default cwd / relative-path anchor, not a sandbox boundary. Set it with `CHATGPT_MCP_WORKSPACE_ROOT`.
- OAuth mode is enabled with `CHATGPT_MCP_AUTH_MODE=oauth`.
- `CHATGPT_MCP_PUBLIC_BASE_URL` must be set in OAuth mode so issuer and resource URLs are stable and not Host-header-derived.
- Prefer separate `CHATGPT_MCP_AUTH_TOKEN` and `CHATGPT_MCP_OAUTH_LOGIN_TOKEN` values.
- Task state is persisted under `CHATGPT_MCP_STATE_DIR`.

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
