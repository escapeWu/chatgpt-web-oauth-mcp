# chatgpt-web-oauth-mcp

> [感谢 LINIXDO 社区](https://linux.do/)

一个给 **ChatGPT Web** 使用的本地 FastMCP 服务器：通过公网 HTTPS + OAuth 暴露 `/mcp`，让 ChatGPT Web 可以调用你本机的文件、搜索、补丁编辑、Shell、Git 和本地 Codex/Claude 委托任务能力。

这个版本已经剥离原项目中的 Notion 专用工作流、说明文档、截图资源和提示词，只保留 ChatGPT Web OAuth MCP 适配与本地操作工具。

## 上游项目来源

本项目基于 [`catoncat/notion-local-ops-mcp`](https://github.com/catoncat/notion-local-ops-mcp) 剥离和改造而来。

原项目探索了让 MCP Agent 调用本地文件、Shell、Git 和本地 Codex/Claude 委托任务的能力。本仓库保留其中可复用的 local-ops MCP server 与 ChatGPT Web 兼容的 OAuth 层，移除了产品专用工作流文档、截图资源、提示词和品牌命名，使其成为一个更纯粹的 ChatGPT Web OAuth MCP 项目。

相对上游的主要改动：

- 将 Python 包名、CLI 命令、launchd label、环境变量前缀统一改为 `chatgpt-web-oauth-mcp` / `CHATGPT_MCP_*`。
- 删除产品专用文档、截图资源和 agent prompt。
- 围绕 ChatGPT Web OAuth MCP 使用方式重写 README。
- 保留本地工具、OAuth dynamic client registration、PKCE flow、protected-resource metadata 和 Cloudflare Tunnel 辅助脚本。

## 能力

- `/mcp` Streamable HTTP MCP endpoint
- ChatGPT Web 兼容的 OAuth discovery / authorization
- Dynamic client registration
- PKCE authorization-code flow
- Protected resource metadata
- `WWW-Authenticate` 中返回 `resource_metadata`
- Bearer access token 校验
- 文件、搜索、patch、shell、git、本地 Codex/Claude delegate 工具
- 可选 `cloudflared` tunnel 与 macOS `launchd` 常驻脚本

## 连接链路

```text
ChatGPT Web
  -> 公网 HTTPS URL
  -> OAuth discovery / register / authorize
  -> /mcp
  -> 本地 FastMCP tools
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

## 快速开始

```bash
git clone https://github.com/<your-account>/chatgpt-web-oauth-mcp.git
cd chatgpt-web-oauth-mcp
cp .env.example .env
```

编辑 `.env`，至少设置：

```bash
CHATGPT_MCP_WORKSPACE_ROOT="/absolute/path/to/workspace"
CHATGPT_MCP_AUTH_MODE=oauth
CHATGPT_MCP_PUBLIC_BASE_URL="https://<your-domain-or-tunnel>"
CHATGPT_MCP_AUTH_TOKEN="replace-me"
CHATGPT_MCP_OAUTH_LOGIN_TOKEN="replace-me-too"
```

启动本地服务和 tunnel：

```bash
./scripts/dev-tunnel.sh
```

在 ChatGPT Web 里添加 MCP：

```text
MCP server URL: https://<your-domain-or-tunnel>/mcp
Authentication: OAuth
Client registration: Dynamic registration
```

授权页弹出后，输入 `CHATGPT_MCP_OAUTH_LOGIN_TOKEN`。

## 冒烟测试

```bash
curl -sS https://<your-domain-or-tunnel>/.well-known/oauth-protected-resource/mcp
curl -sS https://<your-domain-or-tunnel>/.well-known/oauth-authorization-server
curl -i https://<your-domain-or-tunnel>/mcp
```

期望结果：

- 前两个接口返回 JSON metadata。
- 未带认证访问 `/mcp` 返回 `401`。
- `WWW-Authenticate` header 里包含 `resource_metadata`。

## 本地安装

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
chatgpt-web-oauth-mcp
```

本地 endpoint：

```text
http://127.0.0.1:8766/mcp
```

## macOS launchd 常驻

```bash
./scripts/install-launchd.sh
```

常用命令：

```bash
./scripts/launchd-status.sh
./scripts/launchd-doctor.sh
./scripts/launchd-doctor.sh --fix
./scripts/launchd-reload.sh
./scripts/launchd-restart.sh mcp
./scripts/launchd-restart.sh all
./scripts/uninstall-launchd.sh
```

## 环境变量

| 变量 | 是否必需 | 默认值 |
| --- | --- | --- |
| `CHATGPT_MCP_HOST` | 否 | `127.0.0.1` |
| `CHATGPT_MCP_PORT` | 否 | `8766` |
| `CHATGPT_MCP_WORKSPACE_ROOT` | 是 | `$HOME` |
| `CHATGPT_MCP_STATE_DIR` | 否 | `~/.chatgpt-web-oauth-mcp` |
| `CHATGPT_MCP_AUTH_MODE` | 建议 | 有 `AUTH_TOKEN` 时为 `shared_token`，否则 `none` |
| `CHATGPT_MCP_AUTH_TOKEN` | 建议 | 空 |
| `CHATGPT_MCP_PUBLIC_BASE_URL` | OAuth 必需 | 空 |
| `CHATGPT_MCP_OAUTH_LOGIN_TOKEN` | 建议 | fallback 到 `AUTH_TOKEN` |
| `CHATGPT_MCP_OAUTH_SCOPES` | 否 | `local-ops` |
| `CHATGPT_MCP_OAUTH_TOKEN_TTL_SECONDS` | 否 | `86400` |
| `CHATGPT_MCP_CLOUDFLARED_CONFIG` | 否 | 空 |
| `CHATGPT_MCP_TUNNEL_NAME` | 否 | 空 |
| `CHATGPT_MCP_CODEX_COMMAND` | 否 | `codex` |
| `CHATGPT_MCP_CLAUDE_COMMAND` | 否 | `claude` |
| `CHATGPT_MCP_COMMAND_TIMEOUT` | 否 | `120` |
| `CHATGPT_MCP_DELEGATE_TIMEOUT` | 否 | `1800` |
| `CHATGPT_MCP_DEBUG_MCP_LOGGING` | 否 | `0` |
| `CHATGPT_MCP_GRACEFUL_SHUTDOWN_SECONDS` | 否 | `30` |

## 暴露的 MCP 工具

| 工具 | 用途 |
| --- | --- |
| `server_info` | 查看运行配置和工具列表 |
| `set_default_cwd` / `get_default_cwd` | 管理 session 默认工作目录 |
| `list_files` | 列文件和目录 |
| `search` | glob / regex / literal 搜索 |
| `read_text` | 分页读取文本文件 |
| `write_file` | 写入完整文件，支持 dry-run |
| `apply_patch` | 对已有文件应用结构化 patch |
| `git_status` / `git_diff` / `git_commit` / `git_log` / `git_show` / `git_blame` | Git 操作 |
| `run_command` / `run_command_stream` | 执行短命令或长任务 |
| `delegate_task` | 委托本地 Codex 或 Claude Code 做复杂任务 |
| `get_task` / `wait_task` / `cancel_task` | 管理后台任务 |
| `purge_tasks` | 清理过期任务日志 |

## 安全提醒

这个服务会暴露很强的本地能力，公网 URL 和 token 都要按敏感凭据处理。

建议：

- `CHATGPT_MCP_WORKSPACE_ROOT` 指向专门 workspace，不要直接暴露整个 home。
- 一定设置 `CHATGPT_MCP_PUBLIC_BASE_URL`，不要依赖 Host header fallback。
- `CHATGPT_MCP_OAUTH_LOGIN_TOKEN` 和 `CHATGPT_MCP_AUTH_TOKEN` 分开。
- 长期使用时优先 Cloudflare named tunnel。
- 凭据泄露后，除了换 token，还要清理 `~/.chatgpt-web-oauth-mcp/oauth.json`。
- 暴露给不完全可信的客户端前，先裁剪高风险工具。

## 开发

```bash
source .venv/bin/activate
pytest -q
python -m compileall src tests
```

## License

MIT
