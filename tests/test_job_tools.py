from __future__ import annotations

import asyncio
import shlex
import sys
import time
from pathlib import Path
from typing import Callable

from chatgpt_web_oauth_mcp.shell import JobRegistry


def _call(tool, *args, **kwargs):
    fn = tool.fn if hasattr(tool, "fn") else tool
    result = fn(*args, **kwargs)
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result


def _python_cmd(code: str) -> str:
    return " ".join([shlex.quote(sys.executable), "-u", "-c", shlex.quote(code)])


def _wait_for(fetch: Callable[[], dict[str, object]], done: Callable[[dict[str, object]], bool]) -> dict[str, object]:
    deadline = time.monotonic() + 3
    latest = fetch()
    while not done(latest) and time.monotonic() < deadline:
        time.sleep(0.02)
        latest = fetch()
    return latest


def _install_isolated_job_registry(monkeypatch, tmp_path: Path):
    from chatgpt_web_oauth_mcp import server

    monkeypatch.setattr(server, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(server, "job_registry", JobRegistry())
    return server


def test_server_job_start_status_and_tail_logs(tmp_path: Path, monkeypatch) -> None:
    server = _install_isolated_job_registry(monkeypatch, tmp_path)

    started = _call(
        server.job_start,
        command=_python_cmd("import sys; print('out-one'); print('err-one', file=sys.stderr)"),
        cwd=str(tmp_path),
        name="tiny-job",
    )
    assert started["success"] is True
    assert started["name"] == "tiny-job"
    assert started["job_id"].startswith("job_")

    status = _wait_for(
        lambda: _call(server.job_status, job_id=started["job_id"]),
        lambda item: item["status"] != "running",
    )
    assert status["success"] is True
    assert status["status"] == "succeeded"
    assert status["exit_code"] == 0
    assert status["cwd"] == str(tmp_path)
    assert Path(status["stdout_log"]).is_file()
    assert Path(status["stderr_log"]).is_file()
    assert str(tmp_path / "state" / "jobs") in status["stdout_log"]
    assert status["last_output_at"] is not None

    stdout_tail = _call(server.job_tail, job_id=started["job_id"], stream="stdout", lines=10)
    stderr_tail = _call(server.job_tail, job_id=started["job_id"], stream="stderr", lines=10)

    assert stdout_tail["success"] is True
    assert stdout_tail["lines"] == ["out-one"]
    assert stdout_tail["content"] == "out-one"
    assert stderr_tail["success"] is True
    assert stderr_tail["lines"] == ["err-one"]


def test_server_job_kill_only_signals_registered_job(tmp_path: Path, monkeypatch) -> None:
    server = _install_isolated_job_registry(monkeypatch, tmp_path)
    started = _call(
        server.job_start,
        command=_python_cmd("import time; print('ready', flush=True); time.sleep(30)"),
        cwd=str(tmp_path),
        name="kill-me",
    )
    job_id = started["job_id"]
    try:
        ready = _wait_for(
            lambda: _call(server.job_tail, job_id=job_id, stream="stdout", lines=5),
            lambda item: "ready" in item["content"],
        )
        assert "ready" in ready["content"]

        killed = _call(server.job_kill, job_id=job_id)
        assert killed["success"] is True
        assert killed["signal"] == "TERM"
        assert killed["signal_sent"] is True

        status = _wait_for(
            lambda: _call(server.job_status, job_id=job_id),
            lambda item: item["status"] != "running",
        )
        assert status["status"] == "killed"
        assert status["exit_code"] is not None
    finally:
        final_status = _call(server.job_status, job_id=job_id)
        if final_status["status"] == "running":
            _call(server.job_kill, job_id=job_id, signal="KILL")


def test_job_tools_are_registered_with_schemas_and_annotations() -> None:
    from chatgpt_web_oauth_mcp import server

    async def scenario() -> dict[str, dict[str, object]]:
        list_tools = getattr(server.mcp, "_list_tools")
        try:
            tools = await list_tools()
        except TypeError:
            tools = await list_tools(None)
        return {
            tool.name: {
                "parameters": tool.parameters,
                "annotations": tool.annotations.model_dump(exclude_none=True),
            }
            for tool in tools
        }

    descriptors = asyncio.run(scenario())
    for name in ["job_start", "job_status", "job_tail", "job_kill"]:
        assert name in descriptors

    assert descriptors["job_start"]["annotations"]["openWorldHint"] is True
    assert descriptors["job_kill"]["annotations"]["openWorldHint"] is True
    assert descriptors["job_status"]["annotations"]["readOnlyHint"] is True
    assert descriptors["job_tail"]["annotations"]["readOnlyHint"] is True
    assert descriptors["job_tail"]["parameters"]["properties"]["stream"]["enum"] == ["stdout", "stderr"]
    assert descriptors["job_kill"]["parameters"]["properties"]["signal"]["enum"] == ["TERM", "KILL"]
