from __future__ import annotations

import contextlib
import shlex
import socket
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import httpx
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from chatgpt_web_oauth_mcp.executors import ExecutorRegistry


def _python_cmd(code: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.contextmanager
def _running_server(
    tmp_path: Path,
    monkeypatch,
    *,
    auth_token: str,
    codex_command: str | None = None,
):
    from chatgpt_web_oauth_mcp import server

    monkeypatch.setattr(server, "AUTH_TOKEN", auth_token)
    monkeypatch.setattr(server, "WORKSPACE_ROOT", tmp_path)
    registry = ExecutorRegistry(codex_command=codex_command or _python_cmd("print('codex')"))
    monkeypatch.setattr(server, "registry", registry)

    app = server.build_http_app()
    port = _find_free_port()
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        lifespan="on",
    )
    uvicorn_server = uvicorn.Server(config)
    uvicorn_server.install_signal_handlers = lambda: None
    thread = threading.Thread(target=uvicorn_server.run, daemon=True)
    thread.start()

    deadline = time.time() + 10
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                break
        time.sleep(0.05)
    else:
        raise AssertionError("Timed out waiting for MCP test server to start.")

    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        uvicorn_server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive(), "uvicorn test server did not shut down cleanly"


@asynccontextmanager
async def _mcp_session(url: str, *, token: str):
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(headers=headers, timeout=10.0) as client:
        async with streamable_http_client(url, http_client=client) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session


async def _call_tool(session: ClientSession, name: str, arguments: dict[str, object]) -> dict[str, object]:
    result = await session.call_tool(name, arguments)
    assert result.isError is False, result
    assert result.structuredContent is not None
    return result.structuredContent


def test_mcp_run_command_end_to_end(tmp_path: Path, monkeypatch) -> None:
    token = "secret-token"
    with _running_server(tmp_path, monkeypatch, auth_token=token) as url:

        async def scenario() -> None:
            async with _mcp_session(url, token=token) as session:
                result = await _call_tool(
                    session,
                    "run_command",
                    {
                        "command": _python_cmd("print('shell-ok')"),
                        "timeout": 5,
                    },
                )
                assert result["success"] is True
                assert "shell-ok" in result["stdout"]

        anyio.run(scenario)


def test_mcp_run_command_batch_end_to_end(tmp_path: Path, monkeypatch) -> None:
    token = "secret-token"
    with _running_server(tmp_path, monkeypatch, auth_token=token) as url:

        async def scenario() -> None:
            async with _mcp_session(url, token=token) as session:
                result = await _call_tool(
                    session,
                    "run_command",
                    {
                        "commands": [
                            _python_cmd("print('batch-one')"),
                            _python_cmd("print('batch-two')"),
                        ],
                        "mode": "parallel",
                        "max_concurrency": 2,
                        "timeout": 5,
                    },
                )
                assert result["success"] is True
                assert result["mode"] == "batch"
                assert result["execution_mode"] == "parallel"
                assert [item["stdout"].strip() for item in result["results"]] == [
                    "batch-one",
                    "batch-two",
                ]

        anyio.run(scenario)


def test_mcp_removed_task_tools_are_not_exposed(tmp_path: Path, monkeypatch) -> None:
    token = "secret-token"
    with _running_server(tmp_path, monkeypatch, auth_token=token) as url:

        async def scenario() -> None:
            async with _mcp_session(url, token=token) as session:
                tools = await session.list_tools()
                names = {tool.name for tool in tools.tools}
                assert "delegate_task" in names
                assert "run_command" in names
                for removed in {
                    "run_command_stream",
                    "wait_task",
                    "get_task",
                    "cancel_task",
                    "purge_tasks",
                    "taskboard_create",
                    "list_skills",
                }:
                    assert removed not in names
                assert not {name for name in names if name.startswith("obsidian_")}

        anyio.run(scenario)


def test_mcp_delegate_task_structured_output_end_to_end(tmp_path: Path, monkeypatch) -> None:
    token = "secret-token"
    codex_command = _python_cmd("print('{\"ok\": true, \"source\": \"delegate\"}')")
    with _running_server(
        tmp_path,
        monkeypatch,
        auth_token=token,
        codex_command=codex_command,
    ) as url:

        async def scenario() -> None:
            async with _mcp_session(url, token=token) as session:
                result = await _call_tool(
                    session,
                    "delegate_task",
                    {
                        "task": "emit json",
                        "output_schema": {"type": "object"},
                        "parse_structured_output": True,
                    },
                )
                assert result["status"] == "succeeded"
                assert result["serial"] is True
                assert result["structured_output"] == {"ok": True, "source": "delegate"}

        anyio.run(scenario)


def test_mcp_delegate_task_validation_error_end_to_end(tmp_path: Path, monkeypatch) -> None:
    token = "secret-token"
    with _running_server(tmp_path, monkeypatch, auth_token=token) as url:

        async def scenario() -> None:
            async with _mcp_session(url, token=token) as session:
                result = await _call_tool(session, "delegate_task", {})
                assert result["success"] is False
                assert result["status"] == "failed"
                assert result["error"]["code"] == "missing_task_or_goal"

        anyio.run(scenario)


def test_mcp_canonical_search_and_read_text_end_to_end(tmp_path: Path, monkeypatch) -> None:
    token = "secret-token"
    (tmp_path / "demo.py").write_text("alpha\nTODO item\n", encoding="utf-8")
    (tmp_path / ".hidden.py").write_text("TODO hidden\n", encoding="utf-8")

    with _running_server(tmp_path, monkeypatch, auth_token=token) as url:

        async def scenario() -> None:
            async with _mcp_session(url, token=token) as session:
                found = await _call_tool(
                    session,
                    "search",
                    {
                        "mode": "glob",
                        "path": ".",
                        "pattern": "*.py",
                    },
                )
                read = await _call_tool(
                    session,
                    "read_text",
                    {
                        "path": "demo.py",
                        "start_line": 2,
                        "line_limit": 1,
                        "include_line_numbers": True,
                    },
                )
                single_file_search = await _call_tool(
                    session,
                    "search",
                    {
                        "mode": "text",
                        "path": "demo.py",
                        "query": "TODO",
                    },
                )
                batch_search = await _call_tool(
                    session,
                    "search",
                    {
                        "mode": "parallel",
                        "path": ".",
                        "queries": [
                            {"mode": "glob", "pattern": "*.py"},
                            {"mode": "text", "path": "demo.py", "query": "alpha"},
                        ],
                        "max_concurrency": 2,
                    },
                )
                assert found["success"] is True
                assert found["mode"] == "glob"
                assert any(item["path"].endswith("demo.py") for item in found["matches"])
                assert all(not item["path"].endswith(".hidden.py") for item in found["matches"])
                assert read["success"] is True
                assert read["mode"] == "single"
                assert read["content"] == "2: TODO item"
                assert single_file_search["success"] is True
                assert single_file_search["mode"] == "text"
                assert len(single_file_search["matches"]) == 1
                assert single_file_search["matches"][0]["path"].endswith("demo.py")
                assert batch_search["success"] is True
                assert batch_search["mode"] == "batch"
                assert batch_search["execution_mode"] == "parallel"
                assert batch_search["max_concurrency"] == 2
                assert batch_search["results"][0]["mode"] == "glob"
                assert batch_search["results"][1]["mode"] == "text"

        anyio.run(scenario)
