---
name: agents-deployment
description: Use when deploying chatgpt-web-oauth-mcp as a ChatGPT Web OAuth MCP server, especially with an existing Cloudflare Tunnel and macOS launchd.
version: 1.0.0
author: Yuzu
license: MIT
metadata:
  hermes:
    tags: [chatgpt-web, oauth, mcp, cloudflare-tunnel, launchd, deployment]
    related_skills: []
---

# Agents Deployment

## Overview

This skill captures the deployment workflow for `chatgpt-web-oauth-mcp`: expose a local FastMCP server on `127.0.0.1:8766`, publish it through a public HTTPS hostname, and register it in ChatGPT Web as an OAuth MCP server.

The preferred production-like setup is to let an existing Cloudflare Tunnel own the public route, while this project only manages the local MCP server and its watchdog via macOS `launchd`. This avoids two `cloudflared` processes fighting over the same hostname, tunnel, or ingress route.

## When to Use

Use this skill when:

- Setting up ChatGPT Web to call this local MCP server.
- A Cloudflare Tunnel already maps a hostname to `http://127.0.0.1:8766`.
- Updating the public hostname used by OAuth metadata.
- Installing or refreshing the launchd services.
- Diagnosing whether a failure is local MCP, OAuth metadata, Cloudflare DNS/TLS, or ChatGPT Web registration.

Do not use it for generic MCP client configuration unrelated to this repository.

## Deployment Model

```text
ChatGPT Web
  -> https://<public-host>/mcp
  -> Cloudflare Tunnel
  -> http://127.0.0.1:8766/mcp
  -> chatgpt-web-oauth-mcp FastMCP server
```

In external Cloudflare mode:

- `cloudflared` is installed and run outside this project.
- Cloudflare ingress points `<public-host>` to `http://127.0.0.1:8766`.
- This project installs only:
  - `com.chatgpt-web-oauth-mcp.mcp`
  - `com.chatgpt-web-oauth-mcp.watchdog`
- This project must not install or restart `com.chatgpt-web-oauth-mcp.cloudflared`.

## Prerequisites

From the repository root:

```bash
cd /Users/shancw/workspace/chatgpt-web-oauth-mcp
```

Check local project state:

```bash
git status --short
command -v cloudflared || true
lsof -nP -iTCP:8766 -sTCP:LISTEN || true
```

If the Cloudflare Tunnel is external, it is acceptable for `cloudflared` to already be running, but port `8766` should be free before installing this MCP launchd service.


## Human-in-the-loop Installation Flow

When a user asks to install a repository as a local ChatGPT Web MCP server, do not assume every optional integration should be enabled. First sync/clone the repo, read `AGENTS.md` and this skill, then ask the human to fill only the missing decisions.

Required decisions before writing final `.env`:

- Local workspace root, e.g. `/Users/<user>/workspace`.
- Public access strategy: existing Cloudflare Tunnel, nginx/Caddy reverse proxy, or not created yet.
- Public base URL, e.g. `https://mcp.example.com`; if missing, install the local MCP first and tell the user to create a public HTTPS route to `http://127.0.0.1:8766`.
- Optional integrations. Obsidian must be explicit opt-in. If the user says they do not need Obsidian, set `CHATGPT_MCP_ENABLE_OBSIDIAN=0` and do not configure `OBSIDIAN_*`.

After local MCP is ready, explain the next human step clearly: create a Cloudflare Tunnel connector or nginx/Caddy HTTPS reverse proxy whose upstream is `http://127.0.0.1:8766`, then give the final domain back to the agent. Only after the domain is known should `CHATGPT_MCP_PUBLIC_BASE_URL` be finalized and launchd reinstalled.

After public verification passes, guide the user to ChatGPT Web: create a custom app/connector, use `https://<public-host>/mcp`, choose OAuth with dynamic client registration, and enter `CHATGPT_MCP_OAUTH_LOGIN_TOKEN` on the authorization page.

## Configure `.env`

The local secret file lives at:

```text
/Users/shancw/workspace/chatgpt-web-oauth-mcp/.env
```

Use this shape, replacing `<public-host>` and token values. Never print or commit real token values.

```bash
CHATGPT_MCP_HOST=127.0.0.1
CHATGPT_MCP_PORT=8766
CHATGPT_MCP_WORKSPACE_ROOT=/Users/shancw/workspace
CHATGPT_MCP_STATE_DIR=/Users/shancw/.chatgpt-web-oauth-mcp
CHATGPT_MCP_AUTH_MODE=oauth
CHATGPT_MCP_PUBLIC_BASE_URL=https://<public-host>
CHATGPT_MCP_AUTH_TOKEN=<long-random-secret>
CHATGPT_MCP_OAUTH_LOGIN_TOKEN=<separate-long-random-secret>
CHATGPT_MCP_OAUTH_SCOPES=local-ops
CHATGPT_MCP_OAUTH_TOKEN_TTL_SECONDS=86400
CHATGPT_MCP_ENABLE_OBSIDIAN=0
CHATGPT_MCP_EXTERNAL_CLOUDFLARED=1
CHATGPT_MCP_CODEX_COMMAND=codex
CHATGPT_MCP_CLAUDE_COMMAND=claude
CHATGPT_MCP_COMMAND_TIMEOUT=120
CHATGPT_MCP_DELEGATE_TIMEOUT=1800
CHATGPT_MCP_DEBUG_MCP_LOGGING=0
CHATGPT_MCP_GRACEFUL_SHUTDOWN_SECONDS=30
CHATGPT_MCP_WATCHDOG_INTERVAL_SECONDS=60
CHATGPT_MCP_DOCTOR_FAILURE_THRESHOLD=3
CHATGPT_MCP_DOCTOR_BASE_BACKOFF_SECONDS=300
CHATGPT_MCP_DOCTOR_MAX_BACKOFF_SECONDS=3600
CHATGPT_MCP_LAUNCHD_LABEL_PREFIX=com.chatgpt-web-oauth-mcp
```

Generate missing secrets without displaying them:

```bash
python3 - <<'PY'
from pathlib import Path
import secrets
p = Path('.env')
text = p.read_text() if p.exists() else ''
keys = {line.split('=', 1)[0] for line in text.splitlines() if '=' in line and not line.startswith('#')}
append = []
if 'CHATGPT_MCP_AUTH_TOKEN' not in keys:
    append.append('CHATGPT_MCP_AUTH_TOKEN=mcp_auth_' + secrets.token_urlsafe(32))
if 'CHATGPT_MCP_OAUTH_LOGIN_TOKEN' not in keys:
    append.append('CHATGPT_MCP_OAUTH_LOGIN_TOKEN=mcp_login_' + secrets.token_urlsafe(32))
if append:
    p.write_text(text.rstrip() + '\n' + '\n'.join(append) + '\n')
    p.chmod(0o600)
print('secrets ensured; values not printed')
PY
```

## Install with Existing Cloudflare Tunnel

If Cloudflare already routes the public hostname to the local MCP service, install only MCP + watchdog:

```bash
./scripts/install-launchd.sh --mcp-only
```

Expected output includes:

```text
- MCP:         gui/<uid>/com.chatgpt-web-oauth-mcp.mcp
- cloudflared: external / not managed by this project
- watchdog:    gui/<uid>/com.chatgpt-web-oauth-mcp.watchdog
```

If changing `CHATGPT_MCP_PUBLIC_BASE_URL`, rerun the installer instead of only sending a HUP reload. The public URL is baked into the launchd environment, so `./scripts/launchd-reload.sh` is not enough for environment changes.

## Verify Local Endpoints

Check launchd status:

```bash
./scripts/launchd-status.sh | sed -n '1,160p'
```

Check local MCP and OAuth metadata:

```bash
curl -sSI --max-time 5 http://127.0.0.1:8766/mcp | sed -n '1,30p'
curl -sS --max-time 10 http://127.0.0.1:8766/.well-known/oauth-protected-resource/mcp | python -m json.tool
curl -sS --max-time 10 http://127.0.0.1:8766/.well-known/oauth-authorization-server | python -m json.tool
```

Expected:

- `/mcp` returns an HTTP response from uvicorn, usually `204 No Content` for `HEAD`.
- `resource` is `https://<public-host>/mcp`.
- `issuer`, `authorization_endpoint`, `token_endpoint`, and `registration_endpoint` all use `https://<public-host>`.

## Verify Public Endpoints

```bash
PUBLIC_BASE_URL=https://<public-host>

curl -sS --max-time 20 "$PUBLIC_BASE_URL/.well-known/oauth-protected-resource/mcp" | python -m json.tool
curl -sS --max-time 20 "$PUBLIC_BASE_URL/.well-known/oauth-authorization-server" | python -m json.tool
curl -sSI --max-time 20 "$PUBLIC_BASE_URL/mcp" | sed -n '1,30p'
```

Expected:

- Public metadata returns JSON.
- Public `/mcp` reaches the local MCP server through Cloudflare.
- Returned OAuth URLs match exactly the same public hostname ChatGPT Web will use.

If public HTTPS fails before reaching the MCP server, inspect DNS/TLS first:

```bash
dig +short <public-host> A
dig +short <public-host> CNAME
curl -vI --max-time 20 https://<public-host>/mcp 2>&1 | sed -n '1,160p'
pgrep -af cloudflared || true
```

A TLS handshake failure at Cloudflare edge means the hostname certificate/SSL configuration is not ready or not covering the subdomain. It is not a local MCP bug.

## Enable Obsidian Native MCP Proxy

Obsidian is optional and must be explicitly confirmed by the human. If they decline it, leave `CHATGPT_MCP_ENABLE_OBSIDIAN=0`; no `obsidian_*` tools will be registered in MCP `tools/list`.

If the human opts in, install and enable the Obsidian Local REST API plugin. Use the plugin's built-in Streamable HTTP MCP server locally; do not install third-party `mcp-obsidian` unless explicitly testing alternatives. Add the plugin API key and endpoint to `.env`:

```bash
CHATGPT_MCP_ENABLE_OBSIDIAN=1
CHATGPT_MCP_OAUTH_SCOPES="local-ops obsidian"
OBSIDIAN_API_KEY=<obsidian-local-rest-api-key>
OBSIDIAN_MCP_URL=https://127.0.0.1:27124/mcp
OBSIDIAN_VERIFY_SSL=0
```

The bridge exposes prefixed tools such as `obsidian_vault_list`, `obsidian_vault_read`, `obsidian_vault_patch`, `obsidian_search_simple`, `obsidian_command_execute`, and `obsidian_open_file`, while still proxying to upstream native tool names like `vault_list` and `search_simple`. If the self-signed HTTPS endpoint causes client issues, enable the plugin's HTTP server and use `OBSIDIAN_MCP_URL=http://127.0.0.1:27123/mcp/`. Rerun `./scripts/install-launchd.sh --mcp-only` after changing these values so launchd receives the new environment. Verify with `obsidian_mcp_list_tools` from ChatGPT or via local MCP smoke tests.

## Register in ChatGPT Web

Use:

```text
MCP server URL: https://<public-host>/mcp
Authentication: OAuth
Client registration: Dynamic registration
```

When the authorization page asks for a login token, enter `CHATGPT_MCP_OAUTH_LOGIN_TOKEN` from `.env`. To print it locally without showing other secrets:

```bash
python3 - <<'PY'
from pathlib import Path
for line in Path(".env").read_text().splitlines():
    if line.startswith("CHATGPT_MCP_OAUTH_LOGIN_TOKEN="):
        print(line.split("=", 1)[1])
PY
```

For local curl smoke tests only, use `CHATGPT_MCP_AUTH_TOKEN`, not the OAuth login token.

Do not expose `CHATGPT_MCP_AUTH_TOKEN` or `CHATGPT_MCP_OAUTH_LOGIN_TOKEN` in chat, logs, README, screenshots, or commits.

## Updating the Public Hostname

When moving from one hostname to another:

```bash
python3 - <<'PY'
from pathlib import Path
p = Path('.env')
s = p.read_text()
s = s.replace('CHATGPT_MCP_PUBLIC_BASE_URL=https://old-host.example', 'CHATGPT_MCP_PUBLIC_BASE_URL=https://new-host.example')
p.write_text(s)
PY

./scripts/install-launchd.sh --mcp-only
```

Then verify local and public metadata again. The metadata must show the new host everywhere before ChatGPT Web registration.

## Common Pitfalls

1. **Using `launchd-reload.sh` after changing `.env`.** HUP reloads code but does not rewrite launchd environment variables. Rerun `install-launchd.sh --mcp-only`.

2. **Starting two cloudflared managers.** If an external tunnel already exists, do not run the full launchd installer. Use `--mcp-only`.

3. **Old hostname in metadata.** This means the running launchd plist still has the old `CHATGPT_MCP_PUBLIC_BASE_URL`. Reinstall MCP launchd services.

4. **Public HTTPS handshake failure.** If local endpoints are OK but public `curl -vI https://<host>/mcp` fails in TLS handshake, fix Cloudflare SSL/Universal SSL/custom certificate coverage first.

5. **Testing only `/mcp`.** OAuth registration also requires both well-known metadata endpoints to return the same public host.

6. **ChatGPT posts to `/` after OAuth.** If logs show `POST /` returning `404 Not Found` right after `POST /oauth/token 200 OK`, add or verify the root compatibility alias that rewrites `/` to `/mcp`.

7. **Obsidian native MCP tools missing.** This is expected when `CHATGPT_MCP_ENABLE_OBSIDIAN=0`; the tools are opt-in and use the `obsidian_*` prefix when enabled. If enabled but missing, rerun `install-launchd.sh --mcp-only` so launchd receives the new environment.

8. **Obsidian native MCP tools visible but failing.** They require the Obsidian Local REST API plugin to be running, `OBSIDIAN_MCP_URL` to point at its `/mcp/` endpoint, and `OBSIDIAN_API_KEY` to be present in the launchd environment. After editing `.env`, rerun `install-launchd.sh --mcp-only`.

9. **Leaking secrets while debugging.** Redact token values when printing `.env`, process args, or logs.

## Verification Checklist

- [ ] `.env` exists and is mode `600` or otherwise private.
- [ ] `CHATGPT_MCP_PUBLIC_BASE_URL` is the exact HTTPS host used in ChatGPT Web.
- [ ] `CHATGPT_MCP_EXTERNAL_CLOUDFLARED=1` when using an existing tunnel.
- [ ] Obsidian opt-in decision is recorded; if disabled, `CHATGPT_MCP_ENABLE_OBSIDIAN=0` and no `obsidian_*` tools appear.
- [ ] `./scripts/install-launchd.sh --mcp-only` completed successfully.
- [ ] `launchd-status.sh` says cloudflared is external / not managed.
- [ ] Local `/mcp` responds on `127.0.0.1:8766`.
- [ ] Local OAuth metadata shows the public hostname.
- [ ] Public OAuth metadata shows the same hostname.
- [ ] Public `/mcp` reaches the MCP server over HTTPS.
- [ ] ChatGPT Web is configured with `https://<public-host>/mcp` and OAuth dynamic registration.
