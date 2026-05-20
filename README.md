# chatgpt-web-oauth-mcp

> [感谢 LINIXDO 社区](https://linux.do/)

A local FastMCP server that exposes filesystem, shell, git, and delegated coding tools to **ChatGPT Web** through an HTTPS MCP endpoint with OAuth.

This project is a stripped-down ChatGPT Web OAuth MCP server. It keeps the useful local-ops MCP tools and the OAuth compatibility layer, while removing the original Notion-specific workflow, docs, assets, and prompts.

## What it provides

- Streamable HTTP MCP endpoint at `/mcp`
- ChatGPT Web-compatible OAuth discovery and authorization
- Dynamic client registration for OAuth clients
- PKCE authorization-code flow
- Protected-resource metadata and `WWW-Authenticate` challenges
- Bearer access-token validation
- Local tools for files, search, patching, shell commands, git, and delegated Codex/Claude tasks
- Optional `cloudflared` tunnel and macOS `launchd` helpers

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
| `CHATGPT_MCP_CLAUDE_COMMAND` | no | `claude` |
| `CHATGPT_MCP_COMMAND_TIMEOUT` | no | `120` |
| `CHATGPT_MCP_DELEGATE_TIMEOUT` | no | `1800` |
| `CHATGPT_MCP_DEBUG_MCP_LOGGING` | no | `0` |
| `CHATGPT_MCP_GRACEFUL_SHUTDOWN_SECONDS` | no | `30` |

## Tools exposed to MCP clients

| Tool | Purpose |
| --- | --- |
| `server_info` | Inspect runtime config and registered tools |
| `set_default_cwd` / `get_default_cwd` | Manage session default working directory |
| `list_files` | List files and directories |
| `search` | Glob, regex, or literal workspace search |
| `read_text` | Read one or more text files with pagination |
| `write_file` | Write full file contents, with dry-run support |
| `apply_patch` | Apply structured patches to existing files |
| `git_status` / `git_diff` / `git_commit` / `git_log` / `git_show` / `git_blame` | Structured git operations |
| `run_command` / `run_command_stream` | Run short or long shell commands |
| `delegate_task` | Delegate complex work to local Codex or Claude Code |
| `get_task` / `wait_task` / `cancel_task` | Manage background tasks |
| `purge_tasks` | Clean stale task logs |

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
