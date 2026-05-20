from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


class ObsidianMCPConfigError(RuntimeError):
    pass


class ObsidianMCPRequestError(RuntimeError):
    pass


@dataclass(frozen=True)
class ObsidianMCPConfig:
    api_key: str
    host: str = "127.0.0.1"
    port: int = 27124
    protocol: str = "https"
    url: str = ""
    verify_ssl: bool = False
    timeout_seconds: int = 10

    @property
    def base_url(self) -> str:
        protocol = "http" if self.protocol.lower() == "http" else "https"
        return f"{protocol}://{self.host}:{self.port}"

    @property
    def mcp_url(self) -> str:
        if self.url.strip():
            return self.url.strip()
        return f"{self.base_url}/mcp/"

    @property
    def configured(self) -> bool:
        return bool(self.api_key.strip())


def _client_factory_for(config: ObsidianMCPConfig):
    def factory(
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {
            "follow_redirects": True,
            "verify": config.verify_ssl,
        }
        kwargs["timeout"] = timeout or httpx.Timeout(config.timeout_seconds, read=max(config.timeout_seconds, 60))
        if headers is not None:
            kwargs["headers"] = headers
        if auth is not None:
            kwargs["auth"] = auth
        return httpx.AsyncClient(**kwargs)

    return factory


def _serialize_model(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_serialize_model(item) for item in value]
    if isinstance(value, tuple):
        return [_serialize_model(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _serialize_model(item) for key, item in value.items()}
    return value


async def list_native_tools(config: ObsidianMCPConfig) -> dict[str, object]:
    if not config.configured:
        raise ObsidianMCPConfigError(
            "OBSIDIAN_API_KEY is not configured. Enable the Obsidian Local REST API plugin, "
            "copy its API key, and set OBSIDIAN_API_KEY in .env."
        )
    headers = {"Authorization": f"Bearer {config.api_key}"}
    try:
        async with streamablehttp_client(
            config.mcp_url,
            headers=headers,
            timeout=config.timeout_seconds,
            sse_read_timeout=max(config.timeout_seconds, 60),
            httpx_client_factory=_client_factory_for(config),
        ) as (read_stream, write_stream, _get_session_id):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.list_tools()
                tools = [_serialize_model(tool) for tool in result.tools]
                return {"success": True, "url": config.mcp_url, "tool_count": len(tools), "tools": tools}
    except ObsidianMCPConfigError:
        raise
    except Exception as exc:
        raise ObsidianMCPRequestError(str(exc)) from exc


async def call_native_tool(
    config: ObsidianMCPConfig,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
) -> dict[str, object]:
    if not config.configured:
        raise ObsidianMCPConfigError(
            "OBSIDIAN_API_KEY is not configured. Enable the Obsidian Local REST API plugin, "
            "copy its API key, and set OBSIDIAN_API_KEY in .env."
        )
    headers = {"Authorization": f"Bearer {config.api_key}"}
    try:
        async with streamablehttp_client(
            config.mcp_url,
            headers=headers,
            timeout=config.timeout_seconds,
            sse_read_timeout=max(config.timeout_seconds, 60),
            httpx_client_factory=_client_factory_for(config),
        ) as (read_stream, write_stream, _get_session_id):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments or {})
                return {
                    "success": not bool(result.isError),
                    "proxied_tool": tool_name,
                    "url": config.mcp_url,
                    "result": _serialize_model(result),
                }
    except ObsidianMCPConfigError:
        raise
    except Exception as exc:
        raise ObsidianMCPRequestError(str(exc)) from exc


def proxy_error(exc: Exception) -> dict[str, object]:
    code = "obsidian_mcp_not_configured" if isinstance(exc, ObsidianMCPConfigError) else "obsidian_mcp_proxy_failed"
    return {"success": False, "error": {"code": code, "message": str(exc)}}
