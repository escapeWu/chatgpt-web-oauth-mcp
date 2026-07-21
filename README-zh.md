# chatgpt-web-oauth-mcp

[English](README.md)

> 感谢 [LINUX.DO 社区](https://linux.do/)。

一个本地 [FastMCP](https://github.com/jlowin/fastmcp) 服务器，让 **ChatGPT Web** 通过受 OAuth 保护的远程 HTTPS MCP endpoint，调用你电脑上的可信工具。

它提供有界的文件与代码搜索、结构化 Git 操作、短命令执行、持久后台任务、tmux 交互会话和串行 Codex 委派，同时让 ChatGPT Web 保持 architect / manager / reviewer 的角色。

## 为什么需要这个项目

ChatGPT 无法直接连接只监听 `127.0.0.1` 的本地进程。一个自定义 MCP 应用需要可访问的远程 endpoint、认证流程，以及不会无边界占满模型上下文的工具输出。

本项目提供这层桥接：

- `/mcp` Streamable HTTP MCP endpoint；
- 与 ChatGPT 兼容的 OAuth discovery、dynamic client registration、PKCE 和 bearer token 校验；
- 可选的 Cloudflare Tunnel 与 macOS `launchd` 辅助脚本；
- 带明确分页、token budget 和输出边界的本地操作工具；
- 将短命令、持久任务、交互终端和委派编码拆成不同执行通道。

## 架构

```text
ChatGPT Web
    │
    │ HTTPS + OAuth
    ▼
公网 MCP endpoint (/mcp)
    │
    ▼
本地 FastMCP server 127.0.0.1:8766
    │
    ├── Direct tools
    │   ├── files / search / read / patch
    │   ├── code maps / environment inspection
    │   └── Git / worktrees
    │
    ├── Execution tools
    │   ├── run_command     短时、有界命令
    │   ├── job_*           持久、非交互后台任务
    │   └── tmux_*          持久交互式 TTY 会话
    │
    └── delegate_task       单个串行 Codex 执行切片
```

ChatGPT Web 应先通过直接工具检查上下文、形成计划、在合适时完成小范围编辑，并自行验证结果。`delegate_task` 刻意不被设计成通用 Agent Loop：它每次只执行一个边界清楚的 Codex 任务，并返回可审计的状态和日志路径。

## 核心能力

| 领域 | 能力 |
| --- | --- |
| 远程 MCP 接入 | Streamable HTTP `/mcp`、discovery metadata、server card、HTTPS tunnel 支持 |
| 认证 | `none`、共享 bearer token，或支持 PKCE 和动态客户端注册的 OAuth authorization-code flow |
| 有界上下文获取 | token-aware 分页、统一 continuation metadata、共享 batch budget、ignore-aware 遍历 |
| 文件与代码 | Glob/regex/文本搜索、文本与多模态文件读取、轻量 symbol/reference/import map |
| 机械式安全编辑 | 完整文件写入、结构化 patch、带 CAS 保护和原子写入的批量替换 |
| Git | status、diff、commit、log、show、blame，以及精简 worktree 生命周期 |
| 本地执行 | 有界命令、持久后台 job、持久 tmux 会话 |
| Codex 委派 | single-flight 串行执行、模型与 reasoning 覆盖、长轮询、私有审计日志 |
| macOS 运维 | 开发隧道、持久 launchd 安装、状态、doctor、reload、restart 和卸载脚本 |

## 运行模型

始终选择最窄、最匹配当前任务的工具：

1. 使用 `list_files`、`search`、`read_text`、`read`、`code_map_*`、`git_status` 或 `git_diff` 检查上下文。
2. 使用 `apply_patch`、`replace`、`write_file` 或结构化 Git 工具完成确定性小改动。
3. 直接验证结果。
4. 只有当一个边界明确的实现任务确实适合 Codex 时，才调用 `delegate_task`。

一个好的委派请求应包含：

- 单一且明确的 task 或 goal；
- 收窄后的 `cwd` 和 `files_in_scope`；
- 明确的 `out_of_scope`；
- acceptance criteria 与 `done_means`；
- verification commands；
- 明确选择的 `commit_mode`。

Codex 委派是串行的。如果已有 delegate 正在运行，新任务不会启动。使用 `delegate_status` 找回当前或最近的服务端 `delegate_id` 并持续监控。

每次委派都会在系统临时缓存目录中生成私有审计目录：

```text
chatgpt-web-oauth-mcp/codex-delegates/<timestamp>-<delegate_id>/
├── prompt.txt
├── stdout.log
├── stderr.log
└── metadata.json
```

已完成的 `delegate_task` 响应不会内联原始 stdout/stderr。需要查看原始输出时，应读取返回的日志路径。

## 依赖要求

- Python 3.11 或更高版本
- Git
- `ripgrep`，作为首选搜索后端
- `tmux`，用于持久交互式会话
- Codex CLI，用于 `delegate_task`
- 只有使用内置 tunnel helper 时才需要 `cloudflared`
- 只有内置 `launchd` 脚本依赖 macOS；Python server 本身并不绑定 launchd

只需安装你计划使用的工具所依赖的可选二进制程序。

## 快速开始

```bash
git clone https://github.com/escapeWu/chatgpt-web-oauth-mcp.git
cd chatgpt-web-oauth-mcp

python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env
```

使用 ChatGPT OAuth 模式时，至少在 `.env` 中配置：

```bash
CHATGPT_MCP_WORKSPACE_ROOT="/absolute/path/to/workspace"
CHATGPT_MCP_AUTH_MODE=oauth
CHATGPT_MCP_PUBLIC_BASE_URL="https://your-public-mcp-host.example"
CHATGPT_MCP_AUTH_TOKEN="replace-with-a-long-random-token"
CHATGPT_MCP_OAUTH_LOGIN_TOKEN="replace-with-a-different-long-random-token"
```

`CHATGPT_MCP_AUTH_TOKEN` 用于保护 MCP bearer access。`CHATGPT_MCP_OAUTH_LOGIN_TOKEN` 需要在授权页面中输入，通常应使用不同的随机值。

启动本地 server 与已配置的 tunnel：

```bash
./scripts/dev-tunnel.sh
```

本地 MCP endpoint：

```text
http://127.0.0.1:8766/mcp
```

公网 MCP endpoint：

```text
https://your-public-mcp-host.example/mcp
```

## 添加到 ChatGPT

ChatGPT 当前将自定义 MCP 集成称为 **应用（Apps）**。具体界面和套餐权限可能变化，但服务端接入流程是：

1. 为符合条件的账号或工作空间启用 ChatGPT developer mode。
2. 打开 **Settings → Apps → Create**，或工作空间管理员对应的 Apps 页面。
3. 填入公网 MCP endpoint，例如 `https://your-public-mcp-host.example/mcp`。
4. 选择 OAuth 认证。
5. 扫描工具。
6. 使用 `CHATGPT_MCP_OAUTH_LOGIN_TOKEN` 完成授权流程。
7. 按工作空间策略创建或发布应用。

OpenAI 官方参考：

- [ChatGPT 中的应用](https://help.openai.com/zh-hans-cn/articles/11487775-connectors-in-chatgpt)
- [ChatGPT 中的开发者模式和 MCP 应用](https://help.openai.com/zh-hans-cn/articles/12584461-developer-mode-and-full-mcp-connectors-in-chatgpt-beta)

ChatGPT 套餐、工作空间、审批和写操作可用性由 ChatGPT 控制，而不是由本 server 决定。

### OAuth 生命周期限制

当前 OAuth 实现支持带 PKCE 的 authorization-code grant，并签发有过期时间的 access token。当前不签发 refresh token，也不声明 `offline_access`。当 `CHATGPT_MCP_OAUTH_TOKEN_TTL_SECONDS` 到期后，客户端可能需要重新授权。

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

MCP endpoint：

```text
/mcp
```

## Smoke test

```bash
curl -sS https://your-public-mcp-host.example/.well-known/oauth-protected-resource/mcp
curl -sS https://your-public-mcp-host.example/.well-known/oauth-authorization-server
curl -i https://your-public-mcp-host.example/mcp
```

预期行为：

- 前两个请求返回 JSON 格式的 OAuth metadata；
- 未认证访问 `/mcp` 返回 `401`；
- OAuth 模式下，`WWW-Authenticate` header 包含 `resource_metadata`。

## 仅本地运行

```bash
source .venv/bin/activate
chatgpt-web-oauth-mcp
```

这会启动 server，但不会创建公网路由，适合本地测试。ChatGPT 无法直接连接 loopback endpoint。

## 使用已有 Cloudflare Tunnel

如果已有服务将公网域名映射到 `http://127.0.0.1:8766`，不要再启动第二个 tunnel。正常配置 OAuth，并只安装 MCP service：

```bash
CHATGPT_MCP_PUBLIC_BASE_URL="https://your-existing-host.example"
CHATGPT_MCP_EXTERNAL_CLOUDFLARED=1
./scripts/install-launchd.sh --mcp-only
```

在 `--mcp-only` 模式下，本项目会安装并监控 MCP 进程，但不会创建、重启或监控 `cloudflared` launchd service。

## 持久化 macOS launchd 安装

完整安装需要 named Cloudflare Tunnel 配置：

```bash
./scripts/install-launchd.sh
```

运维命令：

```bash
./scripts/launchd-status.sh
./scripts/launchd-doctor.sh
./scripts/launchd-doctor.sh --fix
./scripts/launchd-reload.sh
./scripts/launchd-restart.sh mcp
./scripts/launchd-restart.sh all
./scripts/uninstall-launchd.sh
```

watchdog 负责检查服务健康状态。doctor 脚本会按照失败阈值和有上限的指数退避执行定向重启。

## Tool 参考

### Runtime 与环境

| Tool | 用途 |
| --- | --- |
| `server_info` | 检查运行时配置和已注册 MCP tools |
| `set_default_cwd` / `get_default_cwd` | 设置或读取 session 级默认工作目录 |
| `env_snapshot` / `env_diff` | 收集小型只读环境快照，并比较两个 inline snapshot |

### 文件、搜索与代码上下文

| Tool | 用途 |
| --- | --- |
| `list_files` | 支持 ignore、过滤、排序、稳定分页和 token budget 的目录列表 |
| `search` | Glob、regex、文本或 batch 搜索；并行 batch 最多三个 worker |
| `read_text` | 向后兼容的单文件或批量文本读取，支持行分页 |
| `read` | 统一读取文本/指定编码、图片 metadata/reference、PDF 页文本和 binary hex |
| `code_map_symbols` | 轻量 Python、JavaScript 或 TypeScript 定义发现 |
| `code_map_references` | 使用 identifier word boundary 的有界文本引用查找 |
| `code_map_imports` | 轻量 import 发现 |
| `write_file` | 创建或完整覆盖文件，支持 dry-run |
| `replace` | 带锁与 CAS 的批量替换，支持 dry-run、原子写入和编码/换行/BOM/权限保持 |
| `apply_patch` | 对已有文件应用结构化 patch |

### Git 与 worktree

| Tool | 用途 |
| --- | --- |
| `git_status` | 结构化仓库状态 |
| `git_diff` | 按文件限制大小的 staged 或 unstaged diff |
| `git_commit` | stage 指定路径或全部改动并创建 commit |
| `git_log` | 最近提交历史 |
| `git_show` | commit metadata 与按文件限制大小的 diff |
| `git_blame` | 每行 commit、author、summary 和内容 |
| `git_worktree_create` | 创建 clean branch 或 detached worktree |
| `git_worktree_list` | 列出已注册 worktree |
| `git_worktree_status` | 检查一个或全部 worktree |
| `git_worktree_remove` | 移除已注册 worktree；默认拒绝 dirty worktree |

### 命令与持久后台任务

| Tool | 用途 |
| --- | --- |
| `run_command` | 执行一个短命令或顺序/并行 batch；常规 timeout 上限为 300 秒 |
| `job_start` | 启动非交互后台进程，持久化 metadata，并保存独立日志 |
| `job_list` | 从 state directory 发现任务，包括 server 重启后的任务 |
| `job_status` | 读取进程状态、exit status、耗时、资源和日志路径 |
| `job_output` | 使用独立 raw-byte cursor 读取一个 stdout/stderr stream |
| `job_tail` | 以兼容 API 读取最后若干行 |
| `job_kill` | 停止正在运行的任务 |

### 持久 tmux 会话

| Tool | 用途 |
| --- | --- |
| `tmux_list` | 列出配置 socket 上的 session |
| `tmux_start` | 启动一个带 primary pane 的 detached session |
| `tmux_status` | 检查 pane command、cwd、PID、尺寸和退出状态 |
| `tmux_capture` | 捕获有界终端 screen/history 快照 |
| `tmux_send` | 通过 tmux buffer 粘贴 UTF-8 文本，并发送小范围 allowlist key |
| `tmux_kill` | 删除一个精确匹配的 session |

`tmux_capture` 不是无损应用日志。全屏 TUI、进度条、回车覆盖更新和 tmux history limit 都会影响可见内容。需要保留终端历史时，优先使用应用日志，或使用类似 `--no-alt-screen` 的模式。

### Codex 委派

| Tool | 用途 |
| --- | --- |
| `delegate_task` | 执行一个串行、有界的 Codex execution slice，可覆盖 model 和 reasoning |
| `delegate_status` | 通过服务端 `delegate_id` 找回并监控当前或最近委派 |

`delegate_task` 会等待配置的 soft timeout。如果 Codex 仍在运行，它会返回 `status=running` 和日志路径，而不会终止 subprocess。应继续监控同一个 delegate，不要另起新任务。

## 如何选择执行工具

| 需求 | 使用 | 不适合 |
| --- | --- | --- |
| 短时、有界、非交互命令 | `run_command` | 长期 daemon 或交互式 TUI |
| 带可检查日志的持久非交互进程 | `job_*` | 需要交互输入的程序 |
| 持久交互终端或可人工 attach 的 session | `tmux_*` | 无损 stdout/stderr 采集 |
| 委派给 Codex 的边界明确编码任务 | `delegate_task` | 泛化规划、多个无关任务或无限自主执行 |

## 输出 budget 与分页

Token-aware 只读响应使用 `o200k_base` 编码，并提供统一结果协议，包括：

- `complete` / `partial`；
- `estimated_tokens` 和实际生效的 budget；
- `truncated` 和 `stop_reason`；
- 存在后续结果时的 continuation offset。

批量 `read_text`、`search` 和 `run_command` 使用一个共享响应 budget，不会按照子请求数量重复放大上限。

## 环境变量

### Server 与认证

| 变量 | 必需 | 默认值 / 行为 |
| --- | --- | --- |
| `CHATGPT_MCP_HOST` | 否 | `127.0.0.1` |
| `CHATGPT_MCP_PORT` | 否 | `8766` |
| `CHATGPT_MCP_WORKSPACE_ROOT` | 建议 | `$HOME`；相对路径锚点和默认 cwd，**不是 sandbox** |
| `CHATGPT_MCP_STATE_DIR` | 否 | `~/.chatgpt-web-oauth-mcp` |
| `CHATGPT_MCP_AUTH_MODE` | 建议 | 显式设置 `none`、`shared_token` 或 `oauth`；为空时，有 `AUTH_TOKEN` 则选 shared token，否则为 none |
| `CHATGPT_MCP_AUTH_TOKEN` | 建议 | 空；用于 shared-token access，也可在 OAuth 模式作为静态 bearer |
| `CHATGPT_MCP_PUBLIC_BASE_URL` | OAuth 必需 | 空；稳定的公网 issuer/resource base URL |
| `CHATGPT_MCP_OAUTH_LOGIN_TOKEN` | OAuth 建议 | 回退到 `CHATGPT_MCP_AUTH_TOKEN` |
| `CHATGPT_MCP_OAUTH_SCOPES` | 否 | `local-ops` |
| `CHATGPT_MCP_OAUTH_TOKEN_TTL_SECONDS` | 否 | `86400` |

### 搜索、输出与执行

| 变量 | 必需 | 默认值 / 行为 |
| --- | --- | --- |
| `CHATGPT_MCP_RIPGREP_BINARY` | 否 | `rg` |
| `CHATGPT_MCP_TOOL_OUTPUT_TOKEN_BUDGET` | 否 | `8500` |
| `CHATGPT_MCP_READ_TOKEN_BUDGET` | 否 | 继承全局 tool budget |
| `CHATGPT_MCP_RUN_TOKEN_BUDGET` | 否 | 继承全局 tool budget |
| `CHATGPT_MCP_JOB_OUTPUT_TOKEN_BUDGET` | 否 | 继承全局 tool budget |
| `CHATGPT_MCP_RUN_CAPTURE_MAX_BYTES` | 否 | `1048576` bytes |
| `CHATGPT_MCP_CODEX_COMMAND` | 否 | `codex` |
| `CHATGPT_MCP_COMMAND_TIMEOUT` | 否 | `120` 秒 |
| `CHATGPT_MCP_DELEGATE_TIMEOUT` | 否 | `300` 秒 |
| `CHATGPT_MCP_DEBUG_MCP_LOGGING` | 否 | `0` |
| `CHATGPT_MCP_GRACEFUL_SHUTDOWN_SECONDS` | 否 | `30` 秒 |
| `CHATGPT_MCP_RELOAD_READY_TIMEOUT_SECONDS` | 否 | `15` 秒 |

### tmux

| 变量 | 必需 | 默认值 |
| --- | --- | --- |
| `CHATGPT_MCP_TMUX_BINARY` | 否 | `tmux` |
| `CHATGPT_MCP_TMUX_SOCKET_NAME` | 否 | `default` |
| `CHATGPT_MCP_TMUX_CONTROL_TIMEOUT` | 否 | `10` 秒 |

### Tunnel 与 launchd helper

| 变量 | 必需 | 默认值 / 行为 |
| --- | --- | --- |
| `CHATGPT_MCP_CLOUDFLARED_CONFIG` | 完整 tunnel 安装需要 | 空；named tunnel config path |
| `CHATGPT_MCP_TUNNEL_NAME` | 否 | 空；可选 named-tunnel override |
| `CHATGPT_MCP_EXTERNAL_CLOUDFLARED` | 否 | `0`；cloudflared 由外部管理时设为 `1` |
| `CHATGPT_MCP_WATCHDOG_INTERVAL_SECONDS` | 否 | `60` |
| `CHATGPT_MCP_DOCTOR_FAILURE_THRESHOLD` | 否 | `3` |
| `CHATGPT_MCP_DOCTOR_BASE_BACKOFF_SECONDS` | 否 | `300` |
| `CHATGPT_MCP_DOCTOR_MAX_BACKOFF_SECONDS` | 否 | `3600` |
| `CHATGPT_MCP_LAUNCHD_LABEL_PREFIX` | 否 | `com.chatgpt-web-oauth-mcp` |
| `CHATGPT_MCP_LAUNCHD_DIR` | 否 | `~/Library/LaunchAgents` |
| `CHATGPT_MCP_LAUNCHD_LOG_DIR` | 否 | `~/Library/Logs/chatgpt-web-oauth-mcp` |
| `CHATGPT_MCP_LAUNCHD_PATH` | 否 | 安装脚本捕获的当前 shell `PATH` |
| `CHATGPT_MCP_DOCTOR_LOCAL_WAIT_SECONDS` | 否 | `20` |
| `CHATGPT_MCP_DOCTOR_PUBLIC_WAIT_SECONDS` | 否 | `30` |
| `CHATGPT_MCP_DOCTOR_STATE_FILE` | 否 | 为空时使用 state directory 下的内部默认值 |

`CHATGPT_MCP_READY_FD` 是 supervisor 向 child process 传递的内部参数，不应手工配置。

## 安全模型

这个 server 会暴露强大的本地能力。必须将公网 endpoint 和所有认证信息视为敏感数据。

重要边界：

- `CHATGPT_MCP_WORKSPACE_ROOT` 只是相对路径锚点和默认工作目录，**不是文件系统 sandbox**。
- 绝对路径仍会按绝对路径处理。
- `run_command`、`job_*`、`tmux_*`、写入工具、Git 写操作和 `delegate_task` 都能以 server process 的权限修改本机。
- 只连接可信 ChatGPT 账号/工作空间，只暴露你愿意授权的工具。
- `CHATGPT_MCP_AUTH_TOKEN` 与 `CHATGPT_MCP_OAUTH_LOGIN_TOKEN` 应使用两个不同的随机值。
- 保持 `CHATGPT_MCP_PUBLIC_BASE_URL` 稳定，不要依赖不可信 Host header 生成 OAuth issuer metadata。
- 默认 cwd 建议指向独立 workspace，而不是整个 home directory。
- token 泄露后应立即轮换；需要使 OAuth state 失效时，清理 `~/.chatgpt-web-oauth-mcp/oauth.json`。
- 如果不希望 MCP 创建的 session 与普通本地 tmux server 共用 socket，应配置隔离 socket。

## 开发

```bash
source .venv/bin/activate
pytest -q
python -m compileall src tests
```

项目规则与架构说明见 [`AGENTS.md`](AGENTS.md)。

## 上游项目

本仓库从 [`catoncat/notion-local-ops-mcp`](https://github.com/catoncat/notion-local-ops-mcp) 剥离而来。

它保留了可复用的本地操作 MCP server 思路和 ChatGPT 兼容 OAuth 层，同时移除了原项目中的产品专用工作流、截图、prompt、TaskBoard 集成、skills 和品牌命名。

主要变化包括：

- 将 package、CLI、launchd label 和环境变量前缀统一为 `chatgpt-web-oauth-mcp` / `CHATGPT_MCP_*`；
- 形成聚焦 ChatGPT Web OAuth MCP 的架构；
- 增加有界、token-aware 的本地上下文工具；
- 提供通用 Git、job、tmux 和串行 Codex 委派流程。

## License

MIT
