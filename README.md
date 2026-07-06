# chatgpt-web-oauth-mcp

> [感谢 LINIXDO 社区](https://linux.do/)

[![中文文档](https://img.shields.io/badge/docs-中文-blue.svg)](README-zh.md)

A local FastMCP server that exposes filesystem, shell, git, and delegated coding tools to **ChatGPT Web** through an HTTPS MCP endpoint with OAuth.

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
- Local tools for files, search, patching, synchronous shell commands, git, and one serialized long-polling Codex execution delegate
- Optional `cloudflared` tunnel and macOS `launchd` helpers

## Operating model

ChatGPT Web is expected to act as the architect, manager, and reviewer. It should
use direct MCP tools to inspect the workspace, form the plan, make small edits
when appropriate, and verify results. `delegate_task` is reserved for one
bounded Codex Execution Prompt at a time: a narrow task id, files in scope, out
of scope, acceptance criteria, done-means, and verification commands. This keeps
Codex delegation observable and avoids turning long work into one opaque
black-box subprocess.

Every delegate run writes a private audit trail under the system temporary cache
directory, in `chatgpt-web-oauth-mcp/codex-delegates/<timestamp>-<delegate_id>/`.
The returned `logs` object points to `prompt.txt`, `stdout.log`, `stderr.log`,
and `metadata.json`. Files are created with owner-only permissions where the
platform supports it. The prompt is passed to `codex exec -` over stdin rather
than placed on the process command line.

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

## Obsidian native MCP proxy

This project can expose the Obsidian Local REST API plugin's **built-in MCP server** to ChatGPT Web through the same public OAuth MCP endpoint. ChatGPT still connects only to this project; this project connects locally to Obsidian's native MCP endpoint.

Obsidian is opt-in. Enable the Obsidian Local REST API plugin, copy its API key, then add these values to `.env` and reinstall the MCP service:

```bash
CHATGPT_MCP_ENABLE_OBSIDIAN=1
CHATGPT_MCP_OAUTH_SCOPES="local-ops obsidian"
OBSIDIAN_API_KEY="<your-obsidian-local-rest-api-key>"
OBSIDIAN_MCP_URL=https://127.0.0.1:27124/mcp
OBSIDIAN_VERIFY_SSL=0
```

When enabled, the bridge exposes prefixed tools such as `obsidian_vault_list`, `obsidian_vault_read`, `obsidian_vault_patch`, `obsidian_search_simple`, `obsidian_command_execute`, and `obsidian_open_file`. Use `obsidian_mcp_list_tools` to inspect the tools currently advertised by the local Obsidian plugin. If `CHATGPT_MCP_ENABLE_OBSIDIAN=0` or is unset, no `obsidian_*` tools are registered.

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
| `CHATGPT_MCP_DELEGATE_TIMEOUT` | no | `1800` |
| `CHATGPT_MCP_DEBUG_MCP_LOGGING` | no | `0` |
| `CHATGPT_MCP_GRACEFUL_SHUTDOWN_SECONDS` | no | `30` |
| `CHATGPT_MCP_ENABLE_OBSIDIAN` | no | `0` |
| `OBSIDIAN_API_KEY` | required only when Obsidian proxy is enabled | empty |
| `OBSIDIAN_MCP_URL` | no | `https://127.0.0.1:27124/mcp/` |
| `OBSIDIAN_VERIFY_SSL` | no | `0` |

## Tools exposed to MCP clients

| Tool | Purpose |
| --- | --- |
| `server_info` | Inspect runtime config and registered tools |
| `set_default_cwd` / `get_default_cwd` | Manage session default working directory |
| `list_files` | List files and directories |
| `search` | Glob, regex, literal workspace search, or batch search with `mode="sequential"` / `mode="parallel"`; parallel batches cap `max_concurrency` at 3 |
| `read_text` | Read one or more text files with pagination |
| `write_file` | Write full file contents, with dry-run support |
| `apply_patch` | Apply structured patches to existing files |
| `git_status` / `git_diff` / `git_commit` / `git_log` / `git_show` / `git_blame` | Structured git operations |
| `run_command` | Run one shell command, or run multiple commands with `mode="sequential"` or `mode="parallel"`; parallel batches cap `max_concurrency` at 3 |
| `delegate_task` | Run one serialized, bounded Codex execution slice; wait up to 180s, then return stdout/stderr/status or `status=running` |
| `obsidian_*` tools | Optional Obsidian native MCP proxy tools; only registered when `CHATGPT_MCP_ENABLE_OBSIDIAN=1` |

## Security notes

This server can expose powerful local capabilities. Treat the public URL and tokens as sensitive.

Recommended defaults:

- Point `CHATGPT_MCP_WORKSPACE_ROOT` at a dedicated workspace, not your entire home directory.
- Set `CHATGPT_MCP_PUBLIC_BASE_URL`; do not rely on request host fallback.
- Use a separate `CHATGPT_MCP_OAUTH_LOGIN_TOKEN` instead of reusing `CHATGPT_MCP_AUTH_TOKEN`.
- Prefer a named Cloudflare tunnel for stable URLs.
- Rotate tokens and clear `~/.chatgpt-web-oauth-mcp/oauth.json` if credentials leak.
- Remove or disable high-risk tools before exposing this to untrusted clients.

## Development

```bash
source .venv/bin/activate
pytest -q
python -m compileall src tests
```

## License

MIT
