# chatgpt-web-oauth-mcp

> [感谢 LINIXDO 社区](https://linux.do/)

一个给 **ChatGPT Web** 使用的本地 FastMCP 服务器：通过公网 HTTPS + OAuth 暴露 `/mcp`，让 ChatGPT Web 可以调用你本机的文件、搜索、补丁编辑、Shell、Git 和本地 Codex/Claude 委托任务能力。

这个版本已经剥离原项目中的 Notion 专用工作流、说明文档、截图资源和提示词，只保留 ChatGPT Web OAuth MCP 适配与本地操作工具。

## AGENT READ：部署指导

如果你是 AI Agent，并且被要求部署、安装、刷新、排障或注册这个项目作为 ChatGPT Web OAuth MCP server，必须先阅读并遵循仓库内的部署 skill：

```text
.agents/skills/agents-deployment/SKILL.md
```

这个 skill 是本项目部署时的运行手册，覆盖关键决策：OAuth token 应该写入哪个变量、已有 Cloudflare Tunnel 与项目托管 tunnel 的区别、macOS `launchd` 安装、端点验证，以及 ChatGPT Web 注册流程。不要只根据 README 进行部署操作。

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
