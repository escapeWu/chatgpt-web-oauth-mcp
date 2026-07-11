# chatgpt-web-oauth-mcp

> [感谢 LINIXDO 社区](https://linux.do/)

[![中文文档](https://img.shields.io/badge/docs-中文-blue.svg)](README-zh.md)

A local FastMCP server that exposes filesystem, shell, git, persistent tmux sessions, and delegated coding tools to **ChatGPT Web** through an HTTPS MCP endpoint with OAuth.

This project is a stripped-down ChatGPT Web OAuth MCP server. It keeps the useful local-ops MCP tools and the OAuth compatibility layer, while removing the original Notion-specific workflow, docs, assets, and prompts.

## Upstream project

This project was extracted from [`catoncat/notion-local-ops-mcp`](https://github.com/catoncat/notion-local-ops-mcp).

The upstream project explored using an MCP Agent with local files, shell, git, and delegated coding. This repository keeps the reusable local-ops MCP server and the ChatGPT Web-compatible OAuth layer, then removes the product-specific workflow docs, screenshots, prompts, background task polling, task boards, skills, and branding so the result is a focused ChatGPT Web OAuth MCP server.

Major changes from upstream:

- Renamed the Python package, CLI commands, launchd labels, and environment variable prefix to `chatgpt-web-oauth-mcp` / `CHATGPT_MCP_*`.
- Removed product-specific docs, screenshots, and agent prompts.
- Rewrote the README around ChatGPT Web OAuth MCP usage.
- Kept the local tools, OAuth dynamic client registration, PKCE flow, protected-resource metadata, and Cloudflare Tunnel helpers.

## What it provides

- Streamable HTTP MCP endpoint at `/mcp`
- ChatGPT Web-compatible OAuth discovery and authorization
- Dynamic client registration for OAuth clients
- PKCE authorization-code flow
- Protected-resource metadata and `WWW-Authenticate` challenges
- Bearer access-token validation
- Local tools for files, search, patching, synchronous shell commands, background jobs, persistent interactive tmux sessions, git, and one serialized long-polling Codex execution delegate
- Optional `cloudflared` tunnel and macOS `launchd` helpers

## Operating model

ChatGPT Web is expected to act as the architect, manager, and reviewer. It should
use direct MCP tools to inspect the workspace, form the plan, make small edits
when appropriate, and verify results. `delegate_task` is reserved for one
bounded Codex Execution Prompt at a time: a narrow task id, files in scope, out
of scope, acceptance criteria, done-means, verification commands, and optional
per-call model/reasoning overrides. This keeps Codex delegation observable and
avoids turning long work into one opaque black-box subprocess.

Every delegate run writes a private audit trail under the system temporary cache
directory, in `chatgpt-web-oauth-mcp/codex-delegates/<timestamp>-<delegate_id>/`.
The returned `logs` object points to `prompt.txt`, `stdout.log`, `stderr.log`,
and `metadata.json`. Files are created with owner-only permissions where the
platform supports it. The prompt is passed to `codex exec -` over stdin rather
than placed on the process command line. While a delegate is still running,
clients can call `read_text` on the returned stdout/stderr/metadata paths to
inspect live progress. Completed `delegate_task` responses do not inline raw
stdout/stderr; read the returned log files when raw output is needed.
Stateless clients can call `delegate_status` to recover the active and recent
server-generated `delegate_id` values plus their log paths. For long-polling,
call `delegate_status` with `watch_seconds=300` and the default `poll_seconds=5`;
it returns early when task status changes, otherwise it returns the last snapshot
when the watch window expires.

## How ChatGPT Web connects

```text
ChatGPT Web
  -> HTTPS public URL
  -> OAuth discovery / registration / authorization
  -> /mcp
  -> local FastMCP tools
```

OAuth endpoints:

```text
/.well-known/oauth-protected-resource/mcp
/.well-known/oauth-authorization-server
/.well-known/openid-configuration
/oauth/register
/oauth/authorize
/oauth/token
```

MCP endpoint:

```text
/mcp
```

## Quick start

```bash
git clone https://github.com/<your-account>/chatgpt-web-oauth-mcp.git
cd chatgpt-web-oauth-mcp
cp .env.example .env
```

Edit `.env` and set at least:

```bash
CHATGPT_MCP_WORKSPACE_ROOT="/absolute/path/to/workspace"
CHATGPT_MCP_AUTH_MODE=oauth
CHATGPT_MCP_PUBLIC_BASE_URL="https://<your-domain-or-tunnel>"
CHATGPT_MCP_AUTH_TOKEN="replace-me"
CHATGPT_MCP_OAUTH_LOGIN_TOKEN="replace-me-too"
```

Start a local server with a tunnel:

```bash
./scripts/dev-tunnel.sh
```

Use the public URL plus `/mcp` in ChatGPT Web:

```text
MCP server URL: https://<your-domain-or-tunnel>/mcp
Authentication: OAuth
Client registration: Dynamic registration
```

When ChatGPT opens the authorization page, enter `CHATGPT_MCP_OAUTH_LOGIN_TOKEN`.

## Smoke test

```bash
curl -sS https://<your-domain-or-tunnel>/.well-known/oauth-protected-resource/mcp
curl -sS https://<your-domain-or-tunnel>/.well-known/oauth-authorization-server
curl -i https://<your-domain-or-tunnel>/mcp
```

Expected behavior:

- The first two commands return JSON metadata.
- `/mcp` without credentials returns `401`.
- The `WWW-Authenticate` header includes `resource_metadata`.

## Local install

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
chatgpt-web-oauth-mcp
```

Local endpoint:

```text
http://127.0.0.1:8766/mcp
```

## Existing Cloudflare Tunnel

If you already run `cloudflared` yourself and your public hostname already routes to `http://127.0.0.1:8766`, do not let this project start a second tunnel. Configure OAuth normally, set the public URL, and install only the MCP service:

```bash
CHATGPT_MCP_PUBLIC_BASE_URL="https://<your-existing-host>"
CHATGPT_MCP_EXTERNAL_CLOUDFLARED=1
./scripts/install-launchd.sh --mcp-only
```

In `--mcp-only` mode, this project installs and watches the local MCP server, but it does not create, bootstrap, restart, or monitor a `cloudflared` launchd service. Your existing Cloudflare Tunnel remains responsible for the public HTTPS route.

## Persistent macOS launchd install

```bash
./scripts/install-launchd.sh
```

Useful commands:

```bash
./scripts/launchd-status.sh
./scripts/launchd-doctor.sh
./scripts/launchd-doctor.sh --fix
./scripts/launchd-reload.sh
./scripts/launchd-restart.sh mcp
./scripts/launchd-restart.sh all
./scripts/uninstall-launchd.sh
```

## Environment variables

| Variable | Required | Default |
| --- | --- | --- |
| `CHATGPT_MCP_HOST` | no | `127.0.0.1` |
| `CHATGPT_MCP_PORT` | no | `8766` |
| `CHATGPT_MCP_WORKSPACE_ROOT` | yes | `$HOME` |
| `CHATGPT_MCP_STATE_DIR` | no | `~/.chatgpt-web-oauth-mcp` |
| `CHATGPT_MCP_AUTH_MODE` | recommended | `shared_token` when `AUTH_TOKEN` is set, otherwise `none` |
| `CHATGPT_MCP_AUTH_TOKEN` | recommended | empty |
| `CHATGPT_MCP_PUBLIC_BASE_URL` | required for OAuth | empty |
| `CHATGPT_MCP_OAUTH_LOGIN_TOKEN` | recommended | falls back to `AUTH_TOKEN` |
| `CHATGPT_MCP_OAUTH_SCOPES` | no | `local-ops` |
| `CHATGPT_MCP_OAUTH_TOKEN_TTL_SECONDS` | no | `86400` |
| `CHATGPT_MCP_CLOUDFLARED_CONFIG` | no | empty |
| `CHATGPT_MCP_TUNNEL_NAME` | no | empty |
| `CHATGPT_MCP_CODEX_COMMAND` | no | `codex` |
| `CHATGPT_MCP_COMMAND_TIMEOUT` | no | `120` |
| `CHATGPT_MCP_DELEGATE_TIMEOUT` | no | `300` |
| `CHATGPT_MCP_TMUX_BINARY` | no | `tmux` |
| `CHATGPT_MCP_TMUX_SOCKET_NAME` | no | `default` |
| `CHATGPT_MCP_TMUX_CONTROL_TIMEOUT` | no | `10` |
| `CHATGPT_MCP_DEBUG_MCP_LOGGING` | no | `0` |
| `CHATGPT_MCP_GRACEFUL_SHUTDOWN_SECONDS` | no | `30` |

## Tools exposed to MCP clients

| Tool | Purpose |
| --- | --- |
| `server_info` | Inspect runtime config and registered tools |
| `set_default_cwd` / `get_default_cwd` | Manage session default working directory |
| `env_snapshot` / `env_diff` | Read-only runtime diagnostics and inline environment snapshot comparison |
| `list_files` | List files and directories |
| `search` | Glob, regex, literal workspace search, or batch search with `mode="sequential"` / `mode="parallel"`; parallel batches cap `max_concurrency` at 3 |
| `read_text` | Read one or more text files with pagination |
| `code_map_symbols` / `code_map_references` / `code_map_imports` | Lightweight definitions, textual references, and import mapping |
| `write_file` | Write full file contents, with dry-run support |
| `apply_patch` | Apply structured patches to existing files |
| `git_status` / `git_diff` / `git_commit` / `git_log` / `git_show` / `git_blame` | Structured git operations |
| `git_worktree_create` / `git_worktree_list` / `git_worktree_status` / `git_worktree_remove` | Tiny generic git worktree lifecycle |
| `run_command` | Run one shell command, or run multiple commands with `mode="sequential"` or `mode="parallel"`; timeout is capped at 300s unless `force=true` is used after explicit user approval; parallel batches cap `max_concurrency` at 3 |
| `job_start` / `job_status` / `job_tail` / `job_kill` | Tiny in-process background job runner with stdout/stderr logs under the state directory |
| `tmux_list` / `tmux_start` / `tmux_status` / `tmux_capture` / `tmux_send` / `tmux_kill` | Persistent interactive TTY sessions using the configured tmux socket; text input uses stdin-backed tmux buffers and capture output is a bounded terminal snapshot |
| `delegate_task` | Run one serialized, bounded Codex execution slice; optionally override `model` and `reasoning_effort` for that call; wait up to 300s by default, then return status/log paths or `status=running` with readable log paths while Codex continues; raw stdout/stderr stay in log files |
| `delegate_status` | Read-only active/recent delegate status list with server-generated `delegate_id` values and log paths; supports `watch_seconds` long-polling up to 300s |

## Security notes

This server can expose powerful local capabilities. Treat the public URL and tokens as sensitive.

Recommended defaults:

- Point `CHATGPT_MCP_WORKSPACE_ROOT` at a dedicated workspace, not your entire home directory.
- Set `CHATGPT_MCP_PUBLIC_BASE_URL`; do not rely on request host fallback.
- Use a separate `CHATGPT_MCP_OAUTH_LOGIN_TOKEN` instead of reusing `CHATGPT_MCP_AUTH_TOKEN`.
- Prefer a named Cloudflare tunnel for stable URLs.
- Rotate tokens and clear `~/.chatgpt-web-oauth-mcp/oauth.json` if credentials leak.
- Remove or disable high-risk tools before exposing this to untrusted clients.
- The default tmux socket name is `default`, so MCP-created sessions can be attached manually with `tmux attach -t <session>`. Set `CHATGPT_MCP_TMUX_SOCKET_NAME` to isolate them when needed, then attach with `tmux -L <socket> attach -t <session>`.

## Tmux usage model

Use `run_command` for short commands, `job_*` for non-interactive background processes with separate stdout/stderr logs, and `tmux_*` for interactive programs that need a persistent pseudo-terminal. The tmux tools deliberately expose one primary-pane workflow: create a detached session, list or inspect it, capture recent terminal history, paste text or send allowlisted keys, and remove the session.

`tmux_capture` is not a complete application log. It returns the current pane screen and retained tmux history, so full-screen TUIs, progress bars, carriage-return updates, and history limits affect the result. For tools such as Codex CLI, prefer modes such as `--no-alt-screen` when the terminal history must remain observable.

## Development

```bash
source .venv/bin/activate
pytest -q
python -m compileall src tests
```

## License

MIT
