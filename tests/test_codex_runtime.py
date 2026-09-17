from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import textwrap

import pytest

from chatgpt_web_oauth_mcp.codex_runtime.app_server import CodexAppServerAdapter
from chatgpt_web_oauth_mcp.codex_runtime.errors import AppServerRpcError
from chatgpt_web_oauth_mcp.codex_runtime.manager import CodexRuntimeManager
from chatgpt_web_oauth_mcp.codex_runtime.models import sandbox_policy, thread_sandbox
from chatgpt_web_oauth_mcp.tools_codex_runtime import _bounded


def test_full_access_uses_codex_protocol_names() -> None:
    assert thread_sandbox("full-access") == "danger-full-access"
    assert sandbox_policy("full-access") == {"type": "dangerFullAccess"}


class FakeAdapter:
    def __init__(self) -> None:
        self.running = False
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.thread_number = 0
        self.connection_generation = 0

    def info(self) -> dict[str, object]:
        return {
            "process_status": "running" if self.running else "stopped",
            "command": "fake-codex",
            "capabilities": [
                "thread/start",
                "thread/resume",
                "thread/read",
                "command/exec",
                "command/exec/terminate",
                "mcpServerStatus/list",
                "mcpServer/tool/call",
            ],
        }

    def is_running(self) -> bool:
        return self.running

    def thread_start(self, *, cwd: str, sandbox: str) -> dict[str, object]:
        if not self.running:
            self.connection_generation += 1
        self.running = True
        self.thread_number += 1
        thread_id = f"thread-{self.thread_number}"
        self.calls.append(("thread/start", {"cwd": cwd, "sandbox": sandbox}))
        return {
            "thread": {"id": thread_id},
            "cwd": cwd,
            "sandbox": {"type": "workspaceWrite" if sandbox == "workspace-write" else "readOnly"},
        }

    def thread_resume(self, *, thread_id: str, cwd: str, sandbox: str) -> dict[str, object]:
        self.running = True
        self.calls.append(("thread/resume", {"thread_id": thread_id, "cwd": cwd, "sandbox": sandbox}))
        return {
            "thread": {"id": thread_id},
            "cwd": cwd,
            "sandbox": {"type": "workspaceWrite" if sandbox == "workspace-write" else "readOnly"},
        }

    def command_exec(
        self,
        *,
        command: list[str],
        cwd: str,
        sandbox: str,
        timeout_ms: int,
        output_bytes_cap: int,
    ) -> dict[str, object]:
        self.calls.append(("command/exec", {"command": command, "cwd": cwd, "sandbox": sandbox}))
        return {"exitCode": 0, "stdout": "exec-ok", "stderr": ""}

    def mcp_inventory(self, *, thread_id: str, cursor: str | None, limit: int) -> dict[str, object]:
        self.calls.append(("mcpServerStatus/list", {"thread_id": thread_id, "cursor": cursor, "limit": limit}))
        return {
            "data": [
                {
                    "name": "local-tools",
                    "authStatus": "notLoggedIn",
                    "tools": {
                        "read_file": {"name": "read_file", "description": "Read a file"},
                        "write_file": {"name": "write_file", "description": "Write a file"},
                    },
                    "resources": [],
                }
            ],
            "nextCursor": None,
        }

    def mcp_call(
        self,
        *,
        thread_id: str,
        server: str,
        tool: str,
        arguments: dict[str, object],
        meta: dict[str, object] | None,
    ) -> dict[str, object]:
        self.calls.append(("mcpServer/tool/call", {"thread_id": thread_id, "server": server, "tool": tool}))
        return {
            "content": [{"type": "text", "text": "called"}],
            "structuredContent": {"ok": True, "argument": arguments.get("value")},
            "isError": False,
            "_meta": meta,
        }

        self.running = False


def _manager(
    tmp_path: Path,
    adapter: FakeAdapter,
    **kwargs,
) -> CodexRuntimeManager:
    return CodexRuntimeManager(
        state_dir=tmp_path / "state",
        workspace_root=tmp_path,
        adapter=adapter,
        default_timeout_ms=1000,
        max_timeout_ms=5000,
        output_bytes_cap=4096,
        **kwargs,
    )


def test_runtime_bindings_persist_and_resume_after_manager_restart(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    first_adapter = FakeAdapter()
    first_manager = _manager(tmp_path, first_adapter)

    opened = first_manager.open_runtime(cwd=project, sandbox="workspace-write", name="project")
    runtime_id = str(opened["runtime_id"])
    assert opened["status"] == "ready"
    binding_path = tmp_path / "state" / "codex-runtime" / "bindings.json"
    assert binding_path.exists()
    assert stat.S_IMODE(binding_path.stat().st_mode) == 0o600

    second_adapter = FakeAdapter()
    second_manager = _manager(tmp_path, second_adapter)
    detached = second_manager.runtime_status(runtime_id)
    assert detached["status"] == "detached"

    resumed = second_manager.resume_runtime(
        runtime_id=runtime_id,
        thread_id=None,
        cwd=project,
        sandbox=None,
    )
    assert resumed["status"] == "ready"
    assert resumed["thread_id"] == opened["thread_id"]
    assert [name for name, _ in second_adapter.calls] == ["thread/resume"]


def test_close_preserves_runtime_id_and_reattaches_live_thread(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    adapter = FakeAdapter()
    manager = _manager(tmp_path, adapter)
    opened = manager.open_runtime(cwd=project, sandbox="workspace-write", name="project")
    runtime_id = str(opened["runtime_id"])
    thread_id = str(opened["thread_id"])

    closed = manager.close_runtime(runtime_id)
    assert closed["status"] == "detached"
    assert closed["binding_preserved"] is True
    assert closed["thread_id"] == thread_id

    resumed = manager.resume_runtime(
        runtime_id=runtime_id,
        thread_id=None,
        cwd=None,
        sandbox=None,
    )
    assert resumed["runtime_id"] == runtime_id
    assert resumed["thread_id"] == thread_id
    assert resumed["resume_mode"] == "reattached"
    assert [name for name, _ in adapter.calls].count("thread/resume") == 0

    manager.close_runtime(runtime_id)
    resumed_by_thread = manager.resume_runtime(
        runtime_id=None,
        thread_id=thread_id,
        cwd=None,
        sandbox=None,
    )
    assert resumed_by_thread["runtime_id"] == runtime_id
    assert resumed_by_thread["thread_id"] == thread_id
    assert resumed_by_thread["resume_mode"] == "reattached"
    assert [name for name, _ in adapter.calls].count("thread/resume") == 0


def test_acquire_reuses_stable_named_runtime(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    adapter = FakeAdapter()
    manager = _manager(tmp_path, adapter)

    first = manager.acquire_runtime(
        cwd=project,
        sandbox="workspace-write",
        name="plm-worker-01",
    )
    runtime_id = str(first["runtime_id"])
    assert first["acquire_mode"] == "opened"
    assert first["reused_existing"] is False

    manager.close_runtime(runtime_id)
    second = manager.acquire_runtime(
        cwd=project,
        sandbox="workspace-write",
        name="plm-worker-01",
    )
    assert second["runtime_id"] == runtime_id
    assert second["reused_existing"] is True
    assert second["acquire_mode"] == "reattached"
    assert [name for name, _ in adapter.calls].count("thread/start") == 1


def test_runtime_is_gc_collected_after_idle_ttl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    adapter = FakeAdapter()
    clock = [1000.0]
    monkeypatch.setattr(
        "chatgpt_web_oauth_mcp.codex_runtime.manager.time.time",
        lambda: clock[0],
    )
    manager = _manager(tmp_path, adapter, idle_ttl_seconds=8 * 60 * 60)

    opened = manager.open_runtime(cwd=project, sandbox="workspace-write", name="worker")
    runtime_id = str(opened["runtime_id"])
    clock[0] += 8 * 60 * 60 - 1
    before_gc = manager.info()
    assert before_gc["runtime_count"] == 1
    assert before_gc["ready_count"] == 1

    clock[0] += 2
    info = manager.info()
    assert info["runtime_count"] == 0
    assert info["gc"]["expired_collected_total"] == 1
    with pytest.raises(Exception) as missing:
        manager.runtime_status(runtime_id)
    assert getattr(missing.value, "code", None) == "runtime_not_found"


def test_capacity_lru_evicts_oldest_detached_runtime_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    adapter = FakeAdapter()
    clock = [1000.0]
    monkeypatch.setattr(
        "chatgpt_web_oauth_mcp.codex_runtime.manager.time.time",
        lambda: clock[0],
    )
    manager = _manager(
        tmp_path,
        adapter,
        max_runtimes=2,
        idle_ttl_seconds=24 * 60 * 60,
    )

    oldest = manager.open_runtime(cwd=project, sandbox="workspace-write", name="oldest")
    manager.close_runtime(str(oldest["runtime_id"]))
    clock[0] += 10
    newer = manager.open_runtime(cwd=project, sandbox="workspace-write", name="newer")
    manager.close_runtime(str(newer["runtime_id"]))
    clock[0] += 10

    newest = manager.open_runtime(cwd=project, sandbox="workspace-write", name="newest")
    listed = manager.list_runtimes(name=None, cwd=None, status=None, offset=0, limit=10)
    runtime_ids = {runtime["runtime_id"] for runtime in listed["runtimes"]}
    assert {runtime["name"] for runtime in listed["runtimes"]} == {"newer", "newest"}
    assert str(oldest["runtime_id"]) not in runtime_ids
    assert str(newest["runtime_id"]) in runtime_ids
    assert manager.info()["gc"]["lru_evicted_total"] == 1


def test_capacity_lru_keeps_ready_runtime_even_when_it_is_older(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    adapter = FakeAdapter()
    clock = [1000.0]
    monkeypatch.setattr(
        "chatgpt_web_oauth_mcp.codex_runtime.manager.time.time",
        lambda: clock[0],
    )
    manager = _manager(
        tmp_path,
        adapter,
        max_runtimes=2,
        idle_ttl_seconds=24 * 60 * 60,
    )

    ready = manager.open_runtime(cwd=project, sandbox="workspace-write", name="ready-old")
    clock[0] += 10
    detached = manager.open_runtime(cwd=project, sandbox="workspace-write", name="detached-newer")
    manager.close_runtime(str(detached["runtime_id"]))
    clock[0] += 10

    newest = manager.open_runtime(cwd=project, sandbox="workspace-write", name="newest")
    listed = manager.list_runtimes(name=None, cwd=None, status=None, offset=0, limit=10)
    runtime_ids = {runtime["runtime_id"] for runtime in listed["runtimes"]}
    assert str(ready["runtime_id"]) in runtime_ids
    assert str(detached["runtime_id"]) not in runtime_ids
    assert str(newest["runtime_id"]) in runtime_ids
    assert manager.info()["gc"]["lru_evicted_total"] == 1


def test_resume_recreates_harness_only_thread_after_no_rollout(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    first_adapter = FakeAdapter()
    first_manager = _manager(tmp_path, first_adapter)
    opened = first_manager.open_runtime(cwd=project, sandbox="workspace-write", name=None)
    runtime_id = str(opened["runtime_id"])
    previous_thread_id = str(opened["thread_id"])

    class NoRolloutAdapter(FakeAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.thread_number = 10

        def thread_resume(self, *, thread_id: str, cwd: str, sandbox: str) -> dict[str, object]:
            self.running = True
            self.calls.append(("thread/resume", {"thread_id": thread_id, "cwd": cwd, "sandbox": sandbox}))
            raise AppServerRpcError(
                "thread/resume",
                -32600,
                f"no rollout found for thread id {thread_id}",
            )

    second_adapter = NoRolloutAdapter()
    second_manager = _manager(tmp_path, second_adapter)
    resumed = second_manager.resume_runtime(
        runtime_id=runtime_id,
        thread_id=None,
        cwd=None,
        sandbox=None,
    )
    assert resumed["runtime_id"] == runtime_id
    assert resumed["thread_id"] != previous_thread_id
    assert resumed["resume_mode"] == "recreated"
    assert resumed["thread_recreated"] is True
    assert resumed["previous_thread_id"] == previous_thread_id

    third_adapter = NoRolloutAdapter()
    third_adapter.thread_number = 20
    third_manager = _manager(tmp_path, third_adapter)
    resumed_by_unbound_thread = third_manager.resume_runtime(
        runtime_id=None,
        thread_id=previous_thread_id,
        cwd=project,
        sandbox="workspace-write",
    )
    assert resumed_by_unbound_thread["runtime_id"] != runtime_id
    assert resumed_by_unbound_thread["thread_recreated"] is True
    assert resumed_by_unbound_thread["previous_thread_id"] == previous_thread_id


def test_inventory_budget_preserves_server_identity_and_tool_keys() -> None:
    payload = {
        "success": True,
        "servers": [
            {
                "server": "github",
                "name": "github",
                "authStatus": "authenticated",
                "runtimeStatus": "ready",
                "tools": {
                    "github.search": {
                        "name": "github.search",
                        "description": "search details " * 200,
                    }
                },
                "resources": [{"uri": "resource:" + ("x" * 1000)} for _ in range(8)],
            }
        ],
    }
    result = _bounded(payload, 500, fields=("servers",))
    server = result["servers"][0]
    assert server["server"] == "github"
    assert server["name"] == "github"
    assert "github.search" in server["tools"]
    assert result["truncated"] is True


def test_runtime_exec_inventory_and_mcp_call_remain_bound_to_runtime(tmp_path: Path) -> None:
    project = tmp_path / "project"
    child = project / "src"
    child.mkdir(parents=True)
    adapter = FakeAdapter()
    manager = _manager(tmp_path, adapter)
    opened = manager.open_runtime(cwd=project, sandbox="workspace-write", name=None)
    runtime_id = str(opened["runtime_id"])

    executed = manager.exec_command(
        runtime_id=runtime_id,
        command=["printf", "hello"],
        timeout_ms=1000,
        cwd=child,
    )
    assert executed["success"] is True
    assert executed["cwd"] == str(child.resolve())
    assert executed["command"] == ["printf", "hello"]

    inventory = manager.mcp_inventory(
        runtime_id=runtime_id,
        server="local-tools",
        tool_query="write",
        cursor=None,
        limit=20,
    )
    assert list(inventory["servers"][0]["tools"]) == ["write_file"]
    assert inventory["servers"][0]["runtimeStatus"] is None
    assert inventory["servers"][0]["serverCapabilities"] is None

    called = manager.mcp_call(
        runtime_id=runtime_id,
        server="local-tools",
        tool="write_file",
        arguments={"value": "x"},
        meta={"trace": "1"},
    )
    assert called["success"] is True
    assert called["structuredContent"] == {"ok": True, "argument": "x"}
    assert called["_meta"] == {"trace": "1"}
    assert not any(name in {"turn/start", "thread/shellCommand"} for name, _ in adapter.calls)


def test_runtime_rejects_cross_project_cwd_and_unverified_thread_resume(tmp_path: Path) -> None:
    project = tmp_path / "project"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()
    manager = _manager(tmp_path, FakeAdapter())
    opened = manager.open_runtime(cwd=project, sandbox="workspace-write", name=None)
    runtime_id = str(opened["runtime_id"])

    with pytest.raises(Exception) as cross_project:
        manager.exec_command(
            runtime_id=runtime_id,
            command=["pwd"],
            timeout_ms=1000,
            cwd=other,
        )
    assert getattr(cross_project.value, "code", None) == "runtime_cwd_outside_binding"

    with pytest.raises(Exception) as unverified:
        manager.resume_runtime(
            runtime_id=None,
            thread_id="unbound-thread",
            cwd=project,
            sandbox=None,
        )
    assert getattr(unverified.value, "code", None) == "resume_metadata_required"


def test_codex_app_server_protocol_spike_never_starts_a_turn(tmp_path: Path, monkeypatch) -> None:
    log_path = tmp_path / "methods.jsonl"
    executable = tmp_path / "fake-codex"
    executable.write_text(
        textwrap.dedent(
            """
            #!/usr/bin/env python3
            import json
            import os
            import sys

            log_path = os.environ["FAKE_CODEX_LOG"]
            def send(message):
                sys.stdout.write(json.dumps(message) + "\\n")
                sys.stdout.flush()
            def record(method):
                with open(log_path, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(method) + "\\n")
            for line in sys.stdin:
                request = json.loads(line)
                method = request.get("method")
                if method:
                    record(method)
                request_id = request.get("id")
                if method == "initialized":
                    continue
                if method == "initialize":
                    send({"jsonrpc": "2.0", "id": request_id, "result": {"userAgent": "fake"}})
                elif method == "thread/start" and isinstance(request.get("params", {}).get("cwd"), int):
                    send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "invalid cwd"}})
                elif method in {"thread/resume", "thread/read", "command/exec/terminate"} and not request.get("params", {}).get("threadId") and not request.get("params", {}).get("processId"):
                    send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "missing parameter"}})
                elif method == "command/exec" and not request.get("params", {}).get("command"):
                    send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "missing command"}})
                elif method == "mcpServer/tool/call" and not request.get("params", {}).get("tool"):
                    send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "missing tool"}})
                elif method == "mcpServerStatus/list":
                    send({"jsonrpc": "2.0", "id": request_id, "result": {"data": [], "nextCursor": None}})
                elif method == "thread/start":
                    params = request["params"]
                    send({"jsonrpc": "2.0", "id": request_id, "result": {"thread": {"id": "thread-spike"}, "cwd": params["cwd"], "sandbox": {"type": "workspaceWrite"}}})
                elif method == "thread/resume":
                    params = request["params"]
                    send({"jsonrpc": "2.0", "id": request_id, "result": {"thread": {"id": params["threadId"]}, "cwd": params["cwd"], "sandbox": {"type": "workspaceWrite"}}})
                elif method == "command/exec":
                    send({"jsonrpc": "2.0", "id": request_id, "result": {"exitCode": 0, "stdout": "spike-ok", "stderr": ""}})
                elif method == "mcpServer/tool/call":
                    send({"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": "called"}], "structuredContent": {"ok": True}, "isError": False, "_meta": {"trace": "1"}}})
                elif method == "command/exec/terminate":
                    send({"jsonrpc": "2.0", "id": request_id, "result": {}})
                else:
                    send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "unsupported test request"}})
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log_path))
    adapter = CodexAppServerAdapter(
        command=str(executable),
        cwd=tmp_path,
        startup_timeout_seconds=3,
    )

    try:
        started = adapter.thread_start(cwd=str(tmp_path), sandbox="workspace-write")
        assert started["thread"]["id"] == "thread-spike"
        executed = adapter.command_exec(
            command=["printf", "ok"],
            cwd=str(tmp_path),
            sandbox="workspace-write",
            timeout_ms=1000,
            output_bytes_cap=4096,
        )
        called = adapter.mcp_call(
            thread_id="thread-spike",
            server="local-tools",
            tool="read_file",
            arguments={"path": "x"},
            meta={"trace": "1"},
        )
    finally:
        adapter.shutdown()

    assert executed["stdout"] == "spike-ok"
    assert called["structuredContent"] == {"ok": True}
    methods = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert "turn/start" not in methods
    assert "thread/shellCommand" not in methods
    assert "thread/start" in methods
    assert "command/exec" in methods
    assert "mcpServer/tool/call" in methods


def test_codex_app_server_command_timeout_requests_termination(tmp_path: Path, monkeypatch) -> None:
    executable = tmp_path / "fake-timeout-codex"
    executable.write_text(
        textwrap.dedent(
            """
            #!/usr/bin/env python3
            import json
            import sys
            def send(message):
                sys.stdout.write(json.dumps(message) + "\\n")
                sys.stdout.flush()
            for line in sys.stdin:
                request = json.loads(line)
                method = request.get("method")
                request_id = request.get("id")
                params = request.get("params", {})
                if method == "initialized":
                    continue
                if method == "initialize":
                    send({"jsonrpc": "2.0", "id": request_id, "result": {}})
                elif method == "thread/start" and isinstance(params.get("cwd"), int):
                    send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "invalid"}})
                elif method in {"thread/resume", "thread/read", "command/exec/terminate"} and not params.get("threadId") and not params.get("processId"):
                    send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "missing"}})
                elif method == "command/exec" and not params.get("command"):
                    send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "missing"}})
                elif method == "mcpServer/tool/call" and not params.get("tool"):
                    send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "missing"}})
                elif method == "mcpServerStatus/list":
                    send({"jsonrpc": "2.0", "id": request_id, "result": {"data": []}})
                elif method == "command/exec/terminate":
                    send({"jsonrpc": "2.0", "id": request_id, "result": {}})
                elif method == "command/exec":
                    continue
                else:
                    send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "test"}})
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    adapter = CodexAppServerAdapter(command=str(executable), cwd=tmp_path, startup_timeout_seconds=3)
    with pytest.raises(Exception) as timed_out:
        adapter.command_exec(
            command=["sleep", "10"],
            cwd=str(tmp_path),
            sandbox="workspace-write",
            timeout_ms=25,
            output_bytes_cap=4096,
        )
    adapter.shutdown()
    assert getattr(timed_out.value, "code", None) == "command_timeout"
    assert getattr(timed_out.value, "details", {}).get("terminated") is True
