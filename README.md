# chatgpt-web-oauth-mcp

> [感谢 LINIXDO 社区](https://linux.do/)

[![中文文档](https://img.shields.io/badge/docs-中文-blue.svg)](README-zh.md)

A local FastMCP server that exposes filesystem, shell, git, and delegated coding tools to **ChatGPT Web** through an HTTPS MCP endpoint with OAuth.

This project is a stripped-down ChatGPT Web OAuth MCP server. It keeps the useful local-ops MCP tools and the OAuth compatibility layer, while removing the original Notion-specific workflow, docs, assets, and prompts.

## AGENT READ: deployment guidance

If you are an AI agent asked to deploy, install, refresh, troubleshoot, or register this project as a ChatGPT Web OAuth MCP server, read and follow the repository skill first:

```text
.agents/skills/agents-deployment/SKILL.md
```

That skill is the deployment runbook for the important choices in this project: OAuth token placement, existing Cloudflare Tunnel vs project-managed tunnel, macOS `launchd` installation, endpoint verification, and ChatGPT Web registration. Do not rely on the README alone for deployment operations.

## Upstream project

This project was extracted from [`catoncat/notion-local-ops-mcp`](https://github.com/catoncat/notion-local-ops-mcp).

The upstream project explored using an MCP Agent with local files, shell, git, and delegated coding tasks. This repository keeps the reusable local-ops MCP server and the ChatGPT Web-compatible OAuth layer, then removes the product-specific workflow docs, screenshots, prompts, and branding so the result is a focused ChatGPT Web OAuth MCP server.

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
