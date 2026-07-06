# chatgpt-web-oauth-mcp

> [感谢 LINIXDO 社区](https://linux.do/)

一个给 **ChatGPT Web** 使用的本地 FastMCP 服务器：通过公网 HTTPS + OAuth 暴露 `/mcp`，让 ChatGPT Web 可以调用你本机的文件、搜索、补丁编辑、单条或批量 Shell、Git，以及单任务串行、每次最多阻塞 180 秒的 Codex 执行委托能力。

这个版本已经剥离原项目中的 Notion 专用工作流、说明文档、截图资源和提示词，只保留 ChatGPT Web OAuth MCP 适配与本地操作工具。

## 上游项目来源

本项目基于 [`catoncat/notion-local-ops-mcp`](https://github.com/catoncat/notion-local-ops-mcp) 剥离和改造而来。

原项目探索了让 MCP Agent 调用本地文件、Shell、Git 和本地委托编码的能力。本仓库保留其中可复用的 local-ops MCP server 与 ChatGPT Web 兼容的 OAuth 层，移除了产品专用工作流文档、截图资源、提示词、后台任务轮询、TaskBoard、skills 和品牌命名，使其成为一个更纯粹的 ChatGPT Web OAuth MCP 项目。

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

## 运行模型

ChatGPT Web 是 architect / manager / reviewer：优先通过直接 MCP 工具做仓库检查、计划、局部编辑和验证。`delegate_task` 只作为单任务 Codex executor 使用，每次传入一个边界清楚的 Codex Execution Prompt，例如 `task_id`、`files_in_scope`、`out_of_scope`、`acceptance_criteria`、`done_means` 和验证命令。不要把一个大而泛的长期分析塞进 Codex 委托里变成黑盒。

每次 Codex 委托都会在系统临时缓存目录下写入私有审计日志，路径形如 `chatgpt-web-oauth-mcp/codex-delegates/<timestamp>-<delegate_id>/`。`delegate_task` 返回值里的 `logs` 会指向 `prompt.txt`、`stdout.log`、`stderr.log` 和 `metadata.json`。平台支持时这些文件使用仅当前用户可读写的权限。Prompt 会通过 `codex exec -` 的 stdin 传入，不放在进程命令行里。
