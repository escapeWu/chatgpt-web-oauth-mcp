from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from types import SimpleNamespace
import stat
import threading
import textwrap

import pytest
from fastmcp import Client, FastMCP
from fastmcp.client.elicitation import ElicitResult

from chatgpt_web_oauth_mcp.codex_runtime.app_server import (
    MAX_CONCURRENT_SERVER_REQUESTS,
    CodexAppServerAdapter,
)
from chatgpt_web_oauth_mcp.codex_runtime.errors import (
    AppServerInteractionRequiredError,
    AppServerUnavailableError,
)
from chatgpt_web_oauth_mcp.tools_codex_runtime import (
    _bridge_elicitation,
    _computer_use_approval_response,
    register_codex_runtime_tools,
)


_FAKE_CODEX = r"""
#!/usr/bin/env python3
import json
import os
import sys

log_path = os.environ["FAKE_CODEX_LOG"]
pending = {}
concurrent_calls = []
ambiguous_calls = []


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def record(message):
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(message) + "\n")


def elicitation(call):
    params = call["params"]
    return {
        "jsonrpc": "2.0",
        "id": 1000 + call["id"],
        "method": "mcpServer/elicitation/request",
        "params": {
            "threadId": params["threadId"],
            "turnId": None,
            "serverName": params["server"],
            "mode": "form",
            "message": "Allow Computer Use to control this app?",
            "requestedSchema": {
                "type": "object",
                "properties": {"approved": {"type": "boolean"}},
                "required": ["approved"],
            },
            "_meta": {"source": "fake-computer-use"},
        },
    }


for line in sys.stdin:
    request = json.loads(line)
    record(request)
    method = request.get("method")
    request_id = request.get("id")
    params = request.get("params", {})

    if method == "initialized":
        continue
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": request_id, "result": {"userAgent": "fake"}})
    elif method == "thread/start" and isinstance(params.get("cwd"), int):
        send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "invalid cwd"}})
    elif method in {"thread/resume", "command/exec"}:
        send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "missing parameter"}})
    elif method == "mcpServerStatus/list":
        send({"jsonrpc": "2.0", "id": request_id, "result": {"data": [], "nextCursor": None}})
    elif method == "mcpServer/tool/call" and not params.get("tool"):
        send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "missing tool"}})
    elif method == "mcpServer/tool/call":
        if params["tool"] == "future_request":
            server_request_id = 1000 + request_id
            pending[server_request_id] = request_id
            message = elicitation(request)
            message["method"] = "future/request"
            send(message)
        elif params["tool"].startswith("concurrent_"):
            concurrent_calls.append(request)
            if len(concurrent_calls) == 2:
                for call in reversed(concurrent_calls):
                    server_request_id = 1000 + call["id"]
                    pending[server_request_id] = call["id"]
                    send(elicitation(call))
        elif params["tool"].startswith("ambiguous_"):
            ambiguous_calls.append(request)
            if len(ambiguous_calls) == 2:
                server_request_id = 1000 + ambiguous_calls[0]["id"]
                pending[server_request_id] = [call["id"] for call in ambiguous_calls]
                send(elicitation(ambiguous_calls[0]))
        else:
            server_request_id = 1000 + request_id
            pending[server_request_id] = request_id
            send(elicitation(request))
    elif method is None and request_id in pending:
        call_ids = pending.pop(request_id)
        if not isinstance(call_ids, list):
            call_ids = [call_ids]
        for call_id in call_ids:
            if "error" in request:
                send({"jsonrpc": "2.0", "id": call_id, "error": {"code": -32001, "message": "server request rejected"}})
            else:
                response = request.get("result", {})
                send({
                    "jsonrpc": "2.0",
                    "id": call_id,
                    "result": {
                        "content": [{"type": "text", "text": response.get("action", "missing")}],
                        "structuredContent": response,
                        "isError": False,
                        "_meta": None,
                    },
                })
    else:
        send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "unsupported test request"}})
"""


@pytest.fixture
def fake_adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    log_path = tmp_path / "messages.jsonl"
    executable = tmp_path / "fake-codex"
    executable.write_text(textwrap.dedent(_FAKE_CODEX).strip() + "\n", encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log_path))
    adapter = CodexAppServerAdapter(
        command=str(executable),
        cwd=tmp_path,
        startup_timeout_seconds=3,
    )
    try:
        yield adapter, log_path
    finally:
        adapter.shutdown()


class _FakeSession:
    def __init__(self, action: str, content: dict[str, object] | None) -> None:
        self.action = action
        self.content = content
        self.calls: list[dict[str, object]] = []

    async def elicit_form(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(action=self.action, content=self.content)


def _cua_interaction() -> dict[str, object]:
    return {
        "request_id": 44,
        "method": "mcpServer/elicitation/request",
        "params": {
            "threadId": "thread-1",
            "turnId": None,
            "serverName": "cua_repl",
            "mode": "form",
            "message": 'Allow Computer Use to use "Google Chrome"?',
            "requestedSchema": {"type": "object", "properties": {}},
            "_meta": {
                "codex_approval_kind": "mcp_tool_call",
                "connector_id": "computer-use",
                "connector_name": "Computer Use",
                "persist": ["session", "always"],
                "riskLevel": "high",
                "tool_name": "get_app_state",
                "tool_params": {"app": "com.google.Chrome"},
            },
        },
    }


@pytest.mark.parametrize("action", ["accept", "decline", "cancel"])
def test_bridge_elicitation_forwards_outer_client_action(action: str) -> None:
    content = {"approved": True} if action == "accept" else None
    session = _FakeSession(action, content)
    schema = {
        "type": "object",
        "properties": {"approved": {"type": "boolean"}},
        "required": ["approved"],
    }

    result = asyncio.run(
        _bridge_elicitation(
            session,
            "outer-request-7",
            {
                "request_id": 44,
                "method": "mcpServer/elicitation/request",
                "params": {
                    "threadId": "thread-1",
                    "turnId": None,
                    "serverName": "cua_repl",
                    "mode": "form",
                    "message": "Allow Computer Use?",
                    "requestedSchema": schema,
                    "_meta": {"approval": "native-app"},
                },
            },
        )
    )

    assert result == {"action": action, "content": content, "_meta": None}
    assert session.calls == [
        {
            "message": "Allow Computer Use?",
            "requestedSchema": schema,
            "related_request_id": "outer-request-7",
        }
    ]


def test_bridge_elicitation_cancels_unsupported_mode() -> None:
    session = _FakeSession("accept", {"approved": True})

    result = asyncio.run(
        _bridge_elicitation(
            session,
            "outer-request-8",
            {
                "params": {
                    "mode": "openai/userVerification",
                    "message": "Verify",
                    "requestedSchema": {},
                }
            },
        )
    )

    assert result == {"action": "cancel", "content": None, "_meta": None}
    assert session.calls == []


def test_registered_tool_bridges_current_fastmcp_session() -> None:
    class FakeManager:
        def mcp_call(self, **kwargs):
            response = kwargs["interaction_handler"](
                {
                    "request_id": 72,
                    "method": "mcpServer/elicitation/request",
                    "params": {
                        "threadId": "thread-1",
                        "turnId": None,
                        "serverName": kwargs["server"],
                        "mode": "form",
                        "message": "Allow Computer Use?",
                        "requestedSchema": {
                            "type": "object",
                            "properties": {"approved": {"type": "boolean"}},
                            "required": ["approved"],
                        },
                    },
                }
            )
            return {
                "success": True,
                "runtime_id": kwargs["runtime_id"],
                "thread_id": "thread-1",
                "server": kwargs["server"],
                "tool": kwargs["tool"],
                "content": [],
                "structuredContent": response,
                "isError": False,
                "_meta": None,
            }

    async def run() -> None:
        mcp = FastMCP("elicitation-bridge-test")
        register_codex_runtime_tools(
            mcp,
            SimpleNamespace(
                codex_runtime_manager=FakeManager(),
                tool_output_token_budget=8_000,
            ),
        )
        seen: list[tuple[str, object]] = []

        async def handle_elicitation(message, _response_type, params, _context):
            seen.append((message, params.requestedSchema))
            return ElicitResult(action="accept", content={"approved": True})

        client = Client(mcp, elicitation_handler=handle_elicitation)
        async with client:
            result = await client.call_tool(
                "codex_mcp_call",
                {
                    "runtime_id": "runtime-1",
                    "server": "cua_repl",
                    "tool": "js",
                    "arguments": {"code": "await cua.getApp('Google Chrome')"},
                },
            )

        assert result.is_error is False
        assert result.structured_content["structuredContent"] == {
            "action": "accept",
            "content": {"approved": True},
            "_meta": None,
        }
        assert seen == [
            (
                "Allow Computer Use?",
                {
                    "type": "object",
                    "properties": {"approved": {"type": "boolean"}},
                    "required": ["approved"],
                },
            )
        ]

    asyncio.run(run())


def test_prototype_auto_approves_exact_allowlisted_app_access() -> None:
    response = _computer_use_approval_response(
        _cua_interaction(),
        approval_mode="prototype",
        allowed_apps={"com.google.Chrome"},
    )

    assert response == {
        "action": "accept",
        "content": {},
        "_meta": {"persist": "always"},
    }


def test_prototype_uses_session_when_always_is_not_offered() -> None:
    interaction = _cua_interaction()
    interaction["params"]["_meta"]["persist"] = ["session"]

    response = _computer_use_approval_response(
        interaction,
        approval_mode="prototype",
        allowed_apps={"com.google.Chrome"},
    )

    assert response == {
        "action": "accept",
        "content": {},
        "_meta": {"persist": "session"},
    }


def test_interactive_and_deny_modes_remain_bounded() -> None:
    interaction = _cua_interaction()
    assert (
        _computer_use_approval_response(
            interaction,
            approval_mode="interactive",
            allowed_apps={"com.google.Chrome"},
        )
        is None
    )

    denied = _computer_use_approval_response(
        interaction,
        approval_mode="deny",
        allowed_apps={"com.google.Chrome"},
    )
    assert denied == {"action": "decline", "content": None, "_meta": None}

    interaction["params"]["_meta"]["connector_id"] = "calendar"
    assert (
        _computer_use_approval_response(
            interaction,
            approval_mode="deny",
            allowed_apps={"com.google.Chrome"},
        )
        is None
    )


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("method",), "future/request"),
        (("params", "serverName"), "node_repl"),
        (("params", "turnId"), "turn-1"),
        (("params", "mode"), "url"),
        (("params", "requestedSchema"), {"type": "object", "properties": {"ok": {}}}),
        (("params", "_meta", "codex_approval_kind"), "browser_auth"),
        (("params", "_meta", "connector_id"), "browser-use"),
        (("params", "_meta", "tool_name"), "send_message"),
        (("params", "_meta", "riskLevel"), "critical"),
        (
            ("params", "_meta", "tool_params"),
            {"app": "com.google.Chrome", "recipient": "someone"},
        ),
        (("params", "_meta", "tool_params"), {"app": "com.tencent.xinWeChat"}),
        (("params", "_meta", "persist"), ["session", "session"]),
        (("params", "_meta", "persist"), ["session", "future"]),
        (("params", "_meta"), None),
    ],
)
def test_prototype_rejects_any_contract_mismatch(
    path: tuple[str, ...],
    value: object,
) -> None:
    interaction = _cua_interaction()
    target = interaction
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    assert (
        _computer_use_approval_response(
            interaction,
            approval_mode="prototype",
            allowed_apps={"com.google.Chrome"},
        )
        is None
    )


def test_registered_tool_applies_prototype_without_outer_elicitation() -> None:
    class FakeManager:
        def mcp_call(self, **kwargs):
            response = kwargs["interaction_handler"](_cua_interaction())
            return {
                "success": True,
                "runtime_id": kwargs["runtime_id"],
                "thread_id": "thread-1",
                "server": kwargs["server"],
                "tool": kwargs["tool"],
                "content": [],
                "structuredContent": response,
                "isError": False,
                "_meta": None,
            }

    async def run() -> None:
        mcp = FastMCP("prototype-cua-approval-test")
        register_codex_runtime_tools(
            mcp,
            SimpleNamespace(
                codex_runtime_manager=FakeManager(),
                codex_runtime_cua_approval_mode="prototype",
                codex_runtime_cua_allowed_apps=frozenset({"com.google.Chrome"}),
                tool_output_token_budget=8_000,
            ),
        )
        client = Client(mcp)
        async with client:
            result = await client.call_tool(
                "codex_mcp_call",
                {
                    "runtime_id": "runtime-1",
                    "server": "cua_repl",
                    "tool": "js",
                    "arguments": {"code": "await cua.getApp('Google Chrome')"},
                },
            )

        assert result.is_error is False
        assert result.structured_content["structuredContent"] == {
            "action": "accept",
            "content": {},
            "_meta": {"persist": "always"},
        }

    asyncio.run(run())


@pytest.mark.parametrize("action", ["accept", "decline", "cancel"])
def test_app_server_elicitation_round_trip(fake_adapter, action: str) -> None:
    adapter, log_path = fake_adapter
    interactions: list[dict[str, object]] = []
    content = {"approved": True} if action == "accept" else None

    def handle(interaction: dict[str, object]) -> dict[str, object]:
        interactions.append(interaction)
        return {"action": action, "content": content, "_meta": None}

    result = adapter.mcp_call(
        thread_id="thread-1",
        server="cua_repl",
        tool="js",
        arguments={"code": "await cua.getApp('Google Chrome')"},
        meta=None,
        interaction_handler=handle,
    )

    assert result["structuredContent"] == {
        "action": action,
        "content": content,
        "_meta": None,
    }
    assert len(interactions) == 1
    params = interactions[0]["params"]
    assert params["threadId"] == "thread-1"
    assert params["turnId"] is None
    assert params["serverName"] == "cua_repl"
    assert params["mode"] == "form"
    assert params["_meta"] == {"source": "fake-computer-use"}

    messages = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    initialize = next(message for message in messages if message.get("method") == "initialize")
    capabilities = initialize["params"]["capabilities"]
    assert "experimentalApi" not in capabilities
    assert capabilities["extensions"] == {"openai/form": {}}
    assert capabilities["mcpServerOpenaiFormElicitation"] is True
    assert not any(message.get("method") == "turn/start" for message in messages)


def test_unknown_server_request_remains_fail_closed(fake_adapter) -> None:
    adapter, _ = fake_adapter
    handler_called = False

    def handle(_interaction: dict[str, object]) -> dict[str, object]:
        nonlocal handler_called
        handler_called = True
        return {"action": "accept", "content": {}, "_meta": None}

    with pytest.raises(AppServerInteractionRequiredError) as exc_info:
        adapter.mcp_call(
            thread_id="thread-unknown",
            server="cua_repl",
            tool="future_request",
            arguments={},
            meta=None,
            interaction_handler=handle,
        )

    assert handler_called is False
    interaction = exc_info.value.details["interactions"][0]
    assert interaction["method"] == "future/request"
    assert interaction["params"]["threadId"] == "thread-unknown"
    assert interaction["params"]["serverName"] == "cua_repl"


def test_outer_elicitation_failure_cancels_and_returns_clear_error(fake_adapter) -> None:
    adapter, _ = fake_adapter

    def unavailable(_interaction: dict[str, object]) -> dict[str, object]:
        raise RuntimeError("client has no elicitation capability")

    with pytest.raises(AppServerInteractionRequiredError) as exc_info:
        adapter.mcp_call(
            thread_id="thread-failed",
            server="cua_repl",
            tool="js",
            arguments={},
            meta=None,
            interaction_handler=unavailable,
        )

    interaction = exc_info.value.details["interactions"][0]
    assert interaction["bridge_error"] == "Outer MCP elicitation failed or timed out."


def test_ambiguous_concurrent_calls_fail_closed(fake_adapter) -> None:
    adapter, _ = fake_adapter
    adapter.start()
    handler_calls: list[str] = []

    def invoke(tool: str) -> dict[str, object]:
        def handle(_interaction: dict[str, object]) -> dict[str, object]:
            handler_calls.append(tool)
            return {"action": "accept", "content": {}, "_meta": None}

        return adapter.mcp_call(
            thread_id="shared-thread",
            server="cua_repl",
            tool=tool,
            arguments={},
            meta=None,
            interaction_handler=handle,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(invoke, "ambiguous_first"),
            pool.submit(invoke, "ambiguous_second"),
        ]
        for future in futures:
            with pytest.raises(AppServerInteractionRequiredError):
                future.result(timeout=5)

    assert handler_calls == []


def test_concurrent_calls_route_elicitation_by_thread_and_server(fake_adapter) -> None:
    adapter, _ = fake_adapter
    adapter.start()
    seen: list[tuple[str, str]] = []

    def invoke(thread_id: str, server: str) -> dict[str, object]:
        def handle(interaction: dict[str, object]) -> dict[str, object]:
            params = interaction["params"]
            seen.append((params["threadId"], params["serverName"]))
            return {
                "action": "accept",
                "content": {"route": thread_id},
                "_meta": None,
            }

        return adapter.mcp_call(
            thread_id=thread_id,
            server=server,
            tool=f"concurrent_{thread_id}",
            arguments={},
            meta=None,
            interaction_handler=handle,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(invoke, "thread-a", "cua_repl")
        second = pool.submit(invoke, "thread-b", "node_repl")
        first_result = first.result(timeout=5)
        second_result = second.result(timeout=5)

    assert first_result["structuredContent"]["content"] == {"route": "thread-a"}
    assert second_result["structuredContent"]["content"] == {"route": "thread-b"}
    assert set(seen) == {("thread-a", "cua_repl"), ("thread-b", "node_repl")}


def test_concurrent_elicitation_handlers_do_not_block_protocol_reader(fake_adapter) -> None:
    adapter, _ = fake_adapter
    adapter.start()
    barrier = threading.Barrier(2)

    def invoke(thread_id: str, server: str) -> dict[str, object]:
        def handle(_interaction: dict[str, object]) -> dict[str, object]:
            barrier.wait(timeout=1.5)
            return {
                "action": "accept",
                "content": {"route": thread_id},
                "_meta": None,
            }

        return adapter.mcp_call(
            thread_id=thread_id,
            server=server,
            tool=f"concurrent_{thread_id}",
            arguments={},
            meta=None,
            interaction_handler=handle,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(invoke, "parallel-a", "cua_repl")
        second = pool.submit(invoke, "parallel-b", "node_repl")
        first_result = first.result(timeout=5)
        second_result = second.result(timeout=5)

    assert first_result["structuredContent"]["content"] == {"route": "parallel-a"}
    assert second_result["structuredContent"]["content"] == {"route": "parallel-b"}


def test_inflight_elicitations_do_not_block_restart_capacity(
    fake_adapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _ = fake_adapter
    adapter.start()
    old_process = adapter._process
    old_pool = adapter._server_request_pool
    assert old_process is not None
    assert old_pool is not None

    entered = threading.Barrier(MAX_CONCURRENT_SERVER_REQUESTS + 1)
    releases = [threading.Event() for _ in range(MAX_CONCURRENT_SERVER_REQUESTS)]
    late_old_send = threading.Event()
    original_send_message = adapter._send_message

    def recording_send(message, *, expected_process=None):
        # Normal RPCs are process-bound too; only sends after retirement are late.
        if expected_process is old_process and old_pool.retired.is_set():
            late_old_send.set()
        return original_send_message(message, expected_process=expected_process)

    monkeypatch.setattr(adapter, "_send_message", recording_send)

    def invoke(index: int) -> dict[str, object]:
        def handle(_interaction: dict[str, object]) -> dict[str, object]:
            entered.wait(timeout=3)
            assert releases[index].wait(timeout=5)
            return {
                "action": "accept",
                "content": {"index": index},
                "_meta": None,
            }

        return adapter.mcp_call(
            thread_id=f"restart-thread-{index}",
            server=f"restart-server-{index}",
            tool=f"restart-tool-{index}",
            arguments={},
            meta=None,
            interaction_handler=handle,
        )

    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_SERVER_REQUESTS) as executor:
        futures = [
            executor.submit(invoke, index)
            for index in range(MAX_CONCURRENT_SERVER_REQUESTS)
        ]
        try:
            entered.wait(timeout=3)
            generation = adapter.connection_generation
            adapter.shutdown()

            for future in futures:
                with pytest.raises(AppServerUnavailableError):
                    future.result(timeout=2)

            with adapter._pending_lock:
                assert not any(
                    pending.process is old_process
                    for pending in adapter._pending.values()
                )

            adapter.start()
            assert adapter.connection_generation == generation + 1
            recovered = adapter.mcp_call(
                thread_id="restart-recovered",
                server="restart-recovered-server",
                tool="restart-recovered-tool",
                arguments={},
                meta=None,
                interaction_handler=lambda _interaction: {
                    "action": "accept",
                    "content": {"recovered": True},
                    "_meta": None,
                },
            )
            assert recovered["structuredContent"]["content"] == {"recovered": True}
        finally:
            for release in releases:
                release.set()

        for _ in range(MAX_CONCURRENT_SERVER_REQUESTS):
            assert old_pool.slots.acquire(timeout=2)
        for _ in range(MAX_CONCURRENT_SERVER_REQUESTS):
            old_pool.slots.release()

    assert late_old_send.is_set() is False


def test_elicitation_concurrency_saturation_rejects_and_reuses_slot(fake_adapter) -> None:
    adapter, _ = fake_adapter
    adapter.start()
    entered = threading.Barrier(MAX_CONCURRENT_SERVER_REQUESTS + 1)
    releases = [threading.Event() for _ in range(MAX_CONCURRENT_SERVER_REQUESTS)]
    ninth_handler_called = threading.Event()

    def invoke(index: int) -> dict[str, object]:
        def handle(_interaction: dict[str, object]) -> dict[str, object]:
            entered.wait(timeout=3)
            assert releases[index].wait(timeout=5)
            return {
                "action": "accept",
                "content": {"index": index},
                "_meta": None,
            }

        return adapter.mcp_call(
            thread_id=f"saturated-thread-{index}",
            server=f"saturated-server-{index}",
            tool=f"saturated-tool-{index}",
            arguments={},
            meta=None,
            interaction_handler=handle,
        )

    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_SERVER_REQUESTS) as executor:
        futures = [
            executor.submit(invoke, index)
            for index in range(MAX_CONCURRENT_SERVER_REQUESTS)
        ]
        try:
            entered.wait(timeout=3)

            def ninth_handler(_interaction: dict[str, object]) -> dict[str, object]:
                ninth_handler_called.set()
                return {"action": "accept", "content": {}, "_meta": None}

            with pytest.raises(AppServerInteractionRequiredError) as exc_info:
                adapter.mcp_call(
                    thread_id="saturated-ninth",
                    server="saturated-ninth-server",
                    tool="saturated-ninth-tool",
                    arguments={},
                    meta=None,
                    interaction_handler=ninth_handler,
                )
            assert ninth_handler_called.is_set() is False
            assert (
                exc_info.value.details["interactions"][0]["bridge_error"]
                == "Outer MCP elicitation concurrency limit reached."
            )

            inventory = adapter.mcp_inventory(
                thread_id="saturated-inventory",
                cursor=None,
                limit=1,
            )
            assert inventory == {"data": [], "nextCursor": None}

            releases[0].set()
            first = futures[0].result(timeout=2)
            assert first["structuredContent"]["content"] == {"index": 0}

            reused = adapter.mcp_call(
                thread_id="saturated-reused",
                server="saturated-reused-server",
                tool="saturated-reused-tool",
                arguments={},
                meta=None,
                interaction_handler=lambda _interaction: {
                    "action": "accept",
                    "content": {"reused": True},
                    "_meta": None,
                },
            )
            assert reused["structuredContent"]["content"] == {"reused": True}
        finally:
            for release in releases:
                release.set()
            for future in futures[1:]:
                future.result(timeout=2)


def test_elicitation_worker_start_failure_releases_capacity(
    fake_adapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _ = fake_adapter
    adapter.start()
    original_start = threading.Thread.start

    def fail_start(_thread: threading.Thread) -> None:
        raise RuntimeError("worker start failed")

    monkeypatch.setattr(threading.Thread, "start", fail_start)
    for index in range(MAX_CONCURRENT_SERVER_REQUESTS):
        with pytest.raises(AppServerInteractionRequiredError) as exc_info:
            adapter.mcp_call(
                thread_id=f"start-failure-{index}",
                server=f"start-failure-server-{index}",
                tool=f"start-failure-tool-{index}",
                arguments={},
                meta=None,
                interaction_handler=lambda _interaction: {
                    "action": "accept",
                    "content": {},
                    "_meta": None,
                },
            )
        assert (
            exc_info.value.details["interactions"][0]["bridge_error"]
            == "Outer MCP elicitation worker could not start."
        )

    monkeypatch.setattr(threading.Thread, "start", original_start)
    recovered = adapter.mcp_call(
        thread_id="start-failure-recovered",
        server="start-failure-recovered-server",
        tool="start-failure-recovered-tool",
        arguments={},
        meta=None,
        interaction_handler=lambda _interaction: {
            "action": "accept",
            "content": {"recovered": True},
            "_meta": None,
        },
    )
    assert recovered["structuredContent"]["content"] == {"recovered": True}
