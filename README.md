# chatgpt-web-oauth-mcp

[![中文文档](https://img.shields.io/badge/docs-中文-blue.svg)](README-zh.md)

> Thanks to the [LINUX.DO community](https://linux.do/).

A local [FastMCP](https://github.com/jlowin/fastmcp) server that lets **ChatGPT Web** call trusted tools on your computer through a remote HTTPS MCP endpoint protected by OAuth.

It exposes bounded filesystem and code-search tools, structured Git operations, short shell commands, durable background jobs, persistent tmux sessions, and serialized Codex delegation while keeping ChatGPT Web in the architect / manager / reviewer role.

## Why this project exists

ChatGPT cannot connect directly to a process bound to `127.0.0.1`. A custom MCP app needs a reachable remote endpoint, an authentication flow, and tools whose output is small enough to remain useful inside the model context window.

This project provides that bridge:

- a Streamable HTTP MCP endpoint at `/mcp`;
- ChatGPT-compatible OAuth discovery, dynamic client registration, PKCE, and bearer-token validation;
- optional Cloudflare Tunnel and macOS `launchd` helpers;
- local-operation tools with explicit pagination, token budgets, and bounded output;
- separate execution paths for short commands, durable jobs, interactive terminal sessions, and delegated coding.

## Architecture

```text
ChatGPT Web
    │
    │ HTTPS + OAuth
    ▼
Public MCP endpoint (/mcp)
    │
    ▼
FastMCP server on 127.0.0.1:8766
    │
    ├── Direct tools
    │   ├── files / search / read / patch
    │   ├── code maps / environment inspection
    │   └── Git / worktrees
    │
    ├── Execution tools
    │   ├── run_command     short bounded commands
    │   ├── job_*           durable non-interactive jobs
    │   └── tmux_*          persistent interactive TTY sessions
    │
    └── delegate_task       one serialized Codex execution slice
```

ChatGPT Web should inspect, plan, make small direct edits when appropriate, and verify results through MCP tools. `delegate_task` is deliberately not a general-purpose agent loop: it runs one bounded Codex task at a time and returns auditable status and log paths.

## Core capabilities

| Area | Capabilities |
| --- | --- |
| Remote MCP access | Streamable HTTP `/mcp`, discovery metadata, server card, HTTPS tunnel support |
| Authentication | `none`, shared bearer token, or OAuth authorization-code flow with PKCE and dynamic client registration |
| Bounded context retrieval | Token-aware pagination, common continuation metadata, shared batch budgets, ignore-aware traversal |
| Files and code | Glob/regex/text search, text and multimodal file reads, lightweight symbol/reference/import maps |
| Safe mechanical edits | Full-file writes, structured patches, CAS-protected batch replacement with atomic writes and format preservation |
| Git | Status, diff, commit, log, show, blame, and a small worktree lifecycle |
| Local execution | Bounded commands, durable background jobs, and persistent tmux sessions |
| Codex delegation | Single-flight delegated execution, model/reasoning overrides, long polling, private audit logs |
| macOS operations | Development tunnel, persistent `launchd` install, status, doctor, reload, restart, and uninstall helpers |

## Operating model

Use the narrowest tool that matches the task:

1. Inspect with `list_files`, `search`, `read_text`, `read`, `code_map_*`, `git_status`, or `git_diff`.
2. Make small deterministic changes with `apply_patch`, `replace`, `write_file`, or structured Git tools.
3. Verify directly.
4. Use `delegate_task` only when a bounded implementation task genuinely benefits from Codex.

A good delegation request includes:

- one clear task or goal;
- a narrow `cwd` and `files_in_scope`;
- explicit `out_of_scope` items;
- acceptance criteria and `done_means`;
- verification commands;
- a deliberate `commit_mode`.

Delegates are serialized. If another delegate is active, a new task is not started. Use `delegate_status` to recover the active or recent server-generated `delegate_id` and monitor it.

Each delegate writes a private audit directory under the system temporary cache:

```text
chatgpt-web-oauth-mcp/codex-delegates/<timestamp>-<delegate_id>/
├── prompt.txt
├── stdout.log
├── stderr.log
└── metadata.json
```

Completed `delegate_task` responses do not inline raw stdout or stderr. Read the returned log paths when the original output is required.

## Requirements

- Python 3.11 or newer
- Git
- `ripgrep` for the preferred search backend
- `tmux` for persistent interactive sessions
- Codex CLI for `delegate_task`
- `cloudflared` only when using the included tunnel helpers
- macOS only for the included `launchd` scripts; the Python server itself is not launchd-specific

Only install the optional binaries required by the tools you intend to use.

## Quick start

```bash
git clone https://github.com/escapeWu/chatgpt-web-oauth-mcp.git
cd chatgpt-web-oauth-mcp

python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env
```

Set at least the following values in `.env` for ChatGPT OAuth mode:

```bash
CHATGPT_MCP_WORKSPACE_ROOT="/absolute/path/to/workspace"
CHATGPT_MCP_AUTH_MODE=oauth
CHATGPT_MCP_PUBLIC_BASE_URL="https://your-public-mcp-host.example"
CHATGPT_MCP_AUTH_TOKEN="replace-with-a-long-random-token"
CHATGPT_MCP_OAUTH_LOGIN_TOKEN="replace-with-a-different-long-random-token"
```

`CHATGPT_MCP_AUTH_TOKEN` protects MCP bearer access. `CHATGPT_MCP_OAUTH_LOGIN_TOKEN` is entered on the authorization page and should normally be a different value.

Start the local server and configured tunnel:

```bash
./scripts/dev-tunnel.sh
```

Local MCP endpoint:

```text
http://127.0.0.1:8766/mcp
```

Public MCP endpoint:

```text
https://your-public-mcp-host.example/mcp
```

## Add it to ChatGPT

ChatGPT currently describes custom MCP integrations as **apps**. The exact UI and plan permissions can change; the server-side sequence is:

1. Enable ChatGPT developer mode for an eligible account or workspace.
2. Open **Settings → Apps → Create**, or the corresponding workspace-admin Apps page.
3. Enter the public MCP endpoint, for example `https://your-public-mcp-host.example/mcp`.
4. Select OAuth authentication.
5. Scan the tools.
6. Complete the authorization flow with `CHATGPT_MCP_OAUTH_LOGIN_TOKEN`.
7. Create or publish the app according to the workspace policy.

Official references:

- [Apps in ChatGPT](https://help.openai.com/en/articles/11487775-connectors-in)
- [Developer mode and MCP apps in ChatGPT](https://help.openai.com/en/articles/12584461-developer-mode-and-full-mcp-connectors-in-chatgpt-beta)

ChatGPT plan, workspace, approval, and write-action availability are controlled by ChatGPT, not by this server.

### OAuth lifecycle limitation

The current OAuth implementation supports the authorization-code grant with PKCE and issues expiring access tokens. It does not currently issue refresh tokens or advertise `offline_access`. After `CHATGPT_MCP_OAUTH_TOKEN_TTL_SECONDS` expires, the client may require reauthorization.

## OAuth endpoints

```text
/.well-known/oauth-protected-resource
/.well-known/oauth-protected-resource/mcp
/.well-known/oauth-authorization-server
/.well-known/openid-configuration
/oauth/register
/oauth/authorize
/oauth/token
```

The MCP endpoint is:

```text
/mcp
```

## Smoke test

```bash
curl -sS https://your-public-mcp-host.example/.well-known/oauth-protected-resource/mcp
curl -sS https://your-public-mcp-host.example/.well-known/oauth-authorization-server
curl -i https://your-public-mcp-host.example/mcp
```

Expected behavior:

- the first two commands return OAuth metadata as JSON;
- an unauthenticated request to `/mcp` returns `401`;
- the `WWW-Authenticate` header includes `resource_metadata` in OAuth mode.

## Local-only run

```bash
source .venv/bin/activate
chatgpt-web-oauth-mcp
```

This starts the server without creating a public route. It is useful for local testing, but ChatGPT cannot connect directly to the loopback endpoint.

## Existing Cloudflare Tunnel

If another service already maps your public hostname to `http://127.0.0.1:8766`, do not start a second tunnel. Configure OAuth normally and install only the MCP service:

```bash
CHATGPT_MCP_PUBLIC_BASE_URL="https://your-existing-host.example"
CHATGPT_MCP_EXTERNAL_CLOUDFLARED=1
./scripts/install-launchd.sh --mcp-only
```

In `--mcp-only` mode, this project installs and watches the MCP process but does not create, restart, or monitor a `cloudflared` launchd service.

## Persistent macOS launchd install

A full installation requires a named Cloudflare Tunnel configuration:

```bash
./scripts/install-launchd.sh
```

Operational commands:

```bash
./scripts/launchd-status.sh
./scripts/launchd-doctor.sh
./scripts/launchd-doctor.sh --fix
./scripts/launchd-reload.sh
./scripts/launchd-restart.sh mcp
./scripts/launchd-restart.sh all
./scripts/uninstall-launchd.sh
```

The watchdog checks service health. The doctor script applies targeted restarts with a failure threshold and capped exponential backoff.

## Tool reference

### Runtime and environment

| Tool | Purpose |
| --- | --- |
| `server_info` | Inspect runtime configuration and registered MCP tools |
| `set_default_cwd` / `get_default_cwd` | Set or read the session-wide default working directory |
| `env_snapshot` / `env_diff` | Collect a small read-only environment snapshot and compare two inline snapshots |

### Files, search, and code context

| Tool | Purpose |
| --- | --- |
| `list_files` | Ignore-aware directory listing with filters, sorting, stable pagination, and token budgets |
| `search` | Glob, regex, literal-text, or batch search; parallel batches are capped at three workers |
| `read_text` | Backward-compatible single or batch text reader with line pagination |
| `read` | Unified text/encoding, image metadata/reference, PDF-page text, and binary-hex reader |
| `code_map_symbols` | Lightweight Python, JavaScript, or TypeScript definition discovery |
| `code_map_references` | Bounded textual reference lookup using identifier word boundaries |
| `code_map_imports` | Lightweight import discovery |
| `write_file` | Create or fully overwrite a file, with dry-run support |
| `replace` | Locked CAS batch replacement with dry-run, atomic writes, and encoding/newline/BOM/permission preservation |
| `apply_patch` | Apply structured patches to existing files |

### Git and worktrees

| Tool | Purpose |
| --- | --- |
| `git_status` | Structured repository status |
| `git_diff` | Per-file bounded unstaged or staged diff |
| `git_commit` | Stage selected paths or all changes and create a commit |
| `git_log` | Recent commit history |
| `git_show` | Commit metadata and bounded per-file diff |
| `git_blame` | Per-line commit, author, summary, and content |
| `git_worktree_create` | Create a clean-branch or detached worktree |
| `git_worktree_list` | List registered worktrees |
| `git_worktree_status` | Inspect one or all worktrees |
| `git_worktree_remove` | Remove a registered worktree; dirty worktrees are refused unless forced |

### Commands and durable jobs

| Tool | Purpose |
| --- | --- |
| `run_command` | Run one short command or a sequential/parallel batch; normal timeout is capped at 300 seconds |
| `job_start` | Start a non-interactive background process with persisted metadata and separate logs |
| `job_list` | Discover jobs from the state directory, including after server restart |
| `job_status` | Read process state, exit status, timing, resources, and log paths |
| `job_output` | Read one stdout/stderr stream using an independent raw-byte cursor |
| `job_tail` | Read the latest lines as a compatibility API |
| `job_kill` | Stop a running job |

### Persistent tmux sessions

| Tool | Purpose |
| --- | --- |
| `tmux_list` | List sessions on the configured tmux socket |
| `tmux_start` | Start one detached session with a primary pane |
| `tmux_status` | Inspect pane commands, directories, PIDs, dimensions, and exit state |
| `tmux_capture` | Capture a bounded terminal screen/history snapshot |
| `tmux_send` | Paste UTF-8 text through a tmux buffer and send a small allowlist of keys |
| `tmux_kill` | Remove one exact session |

`tmux_capture` is not a lossless application log. Full-screen TUIs, progress bars, carriage-return updates, and tmux history limits can change what is visible. Prefer application log files, or use modes such as `--no-alt-screen` when terminal history matters.

### Codex delegation

| Tool | Purpose |
| --- | --- |
| `delegate_task` | Run one serialized, bounded Codex execution slice with optional model and reasoning overrides |
| `delegate_status` | Recover and monitor active or recent delegates by server-generated `delegate_id` |

`delegate_task` waits for the configured soft timeout. If Codex is still running, it returns `status=running` and log paths without killing the subprocess. Continue monitoring the same delegate rather than creating another task.

## Choosing an execution tool

| Need | Use | Do not use it for |
| --- | --- | --- |
| A short, bounded, non-interactive command | `run_command` | Long-running daemons or interactive TUIs |
| A durable non-interactive process with inspectable logs | `job_*` | Interactive input |
| A persistent interactive terminal or manually attachable session | `tmux_*` | Lossless stdout/stderr capture |
| A bounded coding task delegated to Codex | `delegate_task` | Broad planning, multiple unrelated tasks, or unlimited autonomous work |

## Output budgets and pagination

Token-aware read-only responses use the `o200k_base` encoding and expose a common result contract including:

- `complete` / `partial`;
- `estimated_tokens` and the effective budget;
- `truncated` and `stop_reason`;
- a continuation offset when more results are available.

Batch `read_text`, `search`, and `run_command` calls use one shared response budget rather than multiplying the configured limit by the number of child requests.

## Environment variables

### Server and authentication

| Variable | Required | Default / behavior |
| --- | --- | --- |
| `CHATGPT_MCP_HOST` | no | `127.0.0.1` |
| `CHATGPT_MCP_PORT` | no | `8766` |
| `CHATGPT_MCP_WORKSPACE_ROOT` | recommended | `$HOME`; relative-path anchor and default cwd, **not a sandbox** |
| `CHATGPT_MCP_STATE_DIR` | no | `~/.chatgpt-web-oauth-mcp` |
| `CHATGPT_MCP_AUTH_MODE` | recommended | Explicit `none`, `shared_token`, or `oauth`; when empty, shared token is selected if `AUTH_TOKEN` exists, otherwise none |
| `CHATGPT_MCP_AUTH_TOKEN` | recommended | Empty; bearer token for shared-token access and an accepted static bearer in OAuth mode |
| `CHATGPT_MCP_PUBLIC_BASE_URL` | required for OAuth | Empty; stable public issuer/resource base URL |
| `CHATGPT_MCP_OAUTH_LOGIN_TOKEN` | recommended for OAuth | Falls back to `CHATGPT_MCP_AUTH_TOKEN` |
| `CHATGPT_MCP_OAUTH_SCOPES` | no | `local-ops` |
| `CHATGPT_MCP_OAUTH_TOKEN_TTL_SECONDS` | no | `86400` |

### Search, output, and execution

| Variable | Required | Default / behavior |
| --- | --- | --- |
| `CHATGPT_MCP_RIPGREP_BINARY` | no | `rg` |
| `CHATGPT_MCP_TOOL_OUTPUT_TOKEN_BUDGET` | no | `8500` |
| `CHATGPT_MCP_READ_TOKEN_BUDGET` | no | Inherits the global tool budget |
| `CHATGPT_MCP_RUN_TOKEN_BUDGET` | no | Inherits the global tool budget |
| `CHATGPT_MCP_JOB_OUTPUT_TOKEN_BUDGET` | no | Inherits the global tool budget |
| `CHATGPT_MCP_RUN_CAPTURE_MAX_BYTES` | no | `1048576` bytes |
| `CHATGPT_MCP_CODEX_COMMAND` | no | `codex` |
| `CHATGPT_MCP_COMMAND_TIMEOUT` | no | `120` seconds |
| `CHATGPT_MCP_DELEGATE_TIMEOUT` | no | `300` seconds |
| `CHATGPT_MCP_DEBUG_MCP_LOGGING` | no | `0` |
| `CHATGPT_MCP_GRACEFUL_SHUTDOWN_SECONDS` | no | `30` seconds |
| `CHATGPT_MCP_RELOAD_READY_TIMEOUT_SECONDS` | no | `15` seconds |

### tmux

| Variable | Required | Default |
| --- | --- | --- |
| `CHATGPT_MCP_TMUX_BINARY` | no | `tmux` |
| `CHATGPT_MCP_TMUX_SOCKET_NAME` | no | `default` |
| `CHATGPT_MCP_TMUX_CONTROL_TIMEOUT` | no | `10` seconds |

### Tunnel and launchd helpers

| Variable | Required | Default / behavior |
| --- | --- | --- |
| `CHATGPT_MCP_CLOUDFLARED_CONFIG` | for full tunnel install | Empty; named tunnel config path |
| `CHATGPT_MCP_TUNNEL_NAME` | no | Empty; optional named-tunnel override |
| `CHATGPT_MCP_EXTERNAL_CLOUDFLARED` | no | `0`; set to `1` when cloudflared is managed externally |
| `CHATGPT_MCP_WATCHDOG_INTERVAL_SECONDS` | no | `60` |
| `CHATGPT_MCP_DOCTOR_FAILURE_THRESHOLD` | no | `3` |
| `CHATGPT_MCP_DOCTOR_BASE_BACKOFF_SECONDS` | no | `300` |
| `CHATGPT_MCP_DOCTOR_MAX_BACKOFF_SECONDS` | no | `3600` |
| `CHATGPT_MCP_LAUNCHD_LABEL_PREFIX` | no | `com.chatgpt-web-oauth-mcp` |
| `CHATGPT_MCP_LAUNCHD_DIR` | no | `~/Library/LaunchAgents` |
| `CHATGPT_MCP_LAUNCHD_LOG_DIR` | no | `~/Library/Logs/chatgpt-web-oauth-mcp` |
| `CHATGPT_MCP_LAUNCHD_PATH` | no | Current shell `PATH` captured by the install scripts |
| `CHATGPT_MCP_DOCTOR_LOCAL_WAIT_SECONDS` | no | `20` |
| `CHATGPT_MCP_DOCTOR_PUBLIC_WAIT_SECONDS` | no | `30` |
| `CHATGPT_MCP_DOCTOR_STATE_FILE` | no | Internal default under the state directory when empty |

`CHATGPT_MCP_READY_FD` is an internal supervisor-to-child handoff and should not be configured manually.

## Security model

This server exposes powerful local capabilities. Treat the public endpoint and all credentials as sensitive.

Important boundaries:

- `CHATGPT_MCP_WORKSPACE_ROOT` is a relative-path anchor and default working directory. It is **not** a filesystem sandbox.
- Absolute paths remain absolute.
- `run_command`, `job_*`, `tmux_*`, write tools, Git writes, and `delegate_task` can modify the local machine with the permissions of the server process.
- Only connect trusted ChatGPT accounts/workspaces and only expose the tools you are prepared to authorize.
- Prefer separate random values for `CHATGPT_MCP_AUTH_TOKEN` and `CHATGPT_MCP_OAUTH_LOGIN_TOKEN`.
- Keep `CHATGPT_MCP_PUBLIC_BASE_URL` stable and do not rely on untrusted Host headers for OAuth issuer metadata.
- Point the default cwd at a dedicated workspace rather than your whole home directory.
- Rotate leaked tokens and clear `~/.chatgpt-web-oauth-mcp/oauth.json` when OAuth state must be invalidated.
- Use an isolated tmux socket when MCP-created sessions should not share the normal local tmux server.

## Development

```bash
source .venv/bin/activate
pytest -q
python -m compileall src tests
```

Project rules and architecture notes are documented in [`AGENTS.md`](AGENTS.md).

## Upstream project

This repository was extracted from [`catoncat/notion-local-ops-mcp`](https://github.com/catoncat/notion-local-ops-mcp).

It keeps the reusable local-operations MCP server concepts and ChatGPT-compatible OAuth layer, while removing the original product-specific workflows, screenshots, prompts, TaskBoard integration, skills, and branding.

Major changes include:

- renamed package, CLI, launchd labels, and environment prefix to `chatgpt-web-oauth-mcp` / `CHATGPT_MCP_*`;
- a focused ChatGPT Web OAuth MCP architecture;
- bounded, token-aware local context tools;
- generic Git, job, tmux, and serialized Codex delegation workflows.

## License

MIT
