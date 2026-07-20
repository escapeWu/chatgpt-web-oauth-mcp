from __future__ import annotations

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
import uuid

import pytest

from chatgpt_web_oauth_mcp import tmux_ops
from chatgpt_web_oauth_mcp.response_budget import ResponseBudget, render_json_payload
from chatgpt_web_oauth_mcp.tmux_ops import MAX_CAPTURE_LINES, MAX_CAPTURE_OUTPUT_LINES, TmuxClient


def _pane_row(*, dead: bool = False, exit_code: int | None = None) -> dict[str, object]:
    return {
        "session_name": "probe",
        "session_id": "$1",
        "session_attached": False,
        "session_windows": 1,
        "session_created": 123,
        "window_id": "@1",
        "window_index": 0,
        "window_name": "main",
        "pane_id": "%1",
        "pane_index": 0,
        "pane_active": True,
        "pane_pid": 100,
        "pane_current_command": "python",
        "pane_current_path": "/tmp",
        "pane_dead": dead,
        "pane_dead_status": exit_code,
        "pane_dead_signal": None,
        "pane_width": 100,
        "pane_height": 30,
        "m5local_managed": True,
    }


def _wait_for_status(client: TmuxClient, session: str, timeout: float = 3.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    latest = client.status(session=session)
    while latest.get("success") and time.monotonic() < deadline:
        panes = latest["session"]["panes"]
        if panes[0]["pane_dead"]:
            return latest
        time.sleep(0.02)
        latest = client.status(session=session)
    return latest


def test_tmux_run_uses_explicit_socket_shell_false_and_clean_client_env(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout=b"ok", stderr=b"")

    monkeypatch.setenv("TMUX", "/tmp/user-socket,1,0")
    monkeypatch.setenv("TMUX_PANE", "%9")
    monkeypatch.setattr(subprocess, "run", fake_run)

    client = TmuxClient(binary="/custom/tmux", socket_name="mcp-test")
    result = client._run(["display-message", "-p", "hello"], input_bytes=b"input")

    assert result.exit_code == 0
    assert captured["argv"] == [
        "/custom/tmux",
        "-L",
        "mcp-test",
        "display-message",
        "-p",
        "hello",
    ]
    assert captured["shell"] is False
    assert captured["check"] is False
    assert captured["input"] == b"input"
    assert "TMUX" not in captured["env"]
    assert "TMUX_PANE" not in captured["env"]


def test_tmux_list_treats_missing_socket_as_empty(monkeypatch) -> None:
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv,
            1,
            stdout=b"",
            stderr=b"error connecting to /tmp/tmux-1/missing (No such file or directory)",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = TmuxClient(socket_name="missing").list_sessions()

    assert result["success"] is True
    assert result["session_count"] == 0
    assert result["sessions"] == []


def test_tmux_rejects_invalid_session_before_running_tmux(monkeypatch, tmp_path: Path) -> None:
    client = TmuxClient(socket_name="test")

    def fail_run(*args, **kwargs):
        raise AssertionError("tmux must not run for an invalid session name")

    monkeypatch.setattr(client, "_run", fail_run)

    status = client.status(session="bad:name")
    start = client.start(session="bad name", cwd=tmp_path)
    kill = client.kill(session="*")

    for result in [status, start, kill]:
        assert result["success"] is False
        assert result["error"]["code"] == "invalid_session_name"


def test_tmux_send_uses_stdin_buffer_and_never_places_text_in_argv(monkeypatch) -> None:
    client = TmuxClient(socket_name="test")
    calls: list[tuple[list[str], bytes | None]] = []

    monkeypatch.setattr(client, "_require_session_rows", lambda session: [_pane_row()])

    def fake_run(args, *, input_bytes=None):
        calls.append((list(args), input_bytes))
        return tmux_ops._RunResult(0, b"", b"")

    monkeypatch.setattr(client, "_run", fake_run)
    text = "多行文本\nwith 'quotes' and $symbols"

    result = client.send(session="probe", text=text, keys=["C-c"], enter_count=1)

    assert result["success"] is True
    assert result["text_bytes"] == len(text.encode("utf-8"))
    load_calls = [call for call in calls if call[0][0] == "load-buffer"]
    assert len(load_calls) == 1
    assert load_calls[0][1] == text.encode("utf-8")
    assert all(text not in " ".join(args) for args, _ in calls)
    assert any(args[0] == "paste-buffer" and "-d" in args for args, _ in calls)
    assert any(args[0] == "send-keys" and args[-2:] == ["C-c", "Enter"] for args, _ in calls)


def test_tmux_capture_enforces_hard_line_limit(monkeypatch) -> None:
    client = TmuxClient(socket_name="test")
    monkeypatch.setattr(client, "_require_session_rows", lambda session: [_pane_row()])

    def fake_run(args, *, input_bytes=None):
        assert args[-1] == f"-{MAX_CAPTURE_LINES}"
        output = "\n".join(f"line-{index}" for index in range(MAX_CAPTURE_OUTPUT_LINES + 50)).encode()
        return tmux_ops._RunResult(0, output, b"")

    monkeypatch.setattr(client, "_run", fake_run)
    result = client.capture(session="probe", lines=10_000)

    assert result["success"] is True
    assert result["effective_line_limit"] == MAX_CAPTURE_LINES
    assert result["lines_returned"] == MAX_CAPTURE_OUTPUT_LINES
    assert result["truncated_by_line_limit"] is True
    assert result["lines"][0] == "line-50"


def test_tmux_capture_applies_token_budget_to_whole_snapshot(monkeypatch) -> None:
    client = TmuxClient(socket_name="test")
    monkeypatch.setattr(client, "_require_session_rows", lambda session: [_pane_row()])

    def fake_run(args, *, input_bytes=None):
        output = "\n".join((f"line-{index} " + "内容" * 30) for index in range(100)).encode()
        return tmux_ops._RunResult(0, output, b"")

    monkeypatch.setattr(client, "_run", fake_run)
    result = client.capture(session="probe", lines=100, max_tokens=400)

    assert result["partial"] is True
    assert result["truncated_by_token_budget"] is True
    assert result["lines"][-1].startswith("line-99")
    assert ResponseBudget(max_tokens=400).count_tokens(render_json_payload(result)) <= 400


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")
def test_tmux_real_session_round_trip(tmp_path: Path) -> None:
    socket_name = f"m5local-test-{uuid.uuid4().hex[:12]}"
    client = TmuxClient(socket_name=socket_name)
    code = "value=input(); print('got:'+value, flush=True); raise SystemExit(7)"
    command = f"{shlex.quote(sys.executable)} -u -c {shlex.quote(code)}"

    try:
        assert client.list_sessions()["sessions"] == []
        started = client.start(
            session="probe",
            cwd=tmp_path,
            command=command,
            width=100,
            height=30,
        )
        assert started["success"] is True
        assert started["command_started"] is True
        assert "command" not in started

        listed = client.list_sessions(include_panes=True)
        assert listed["success"] is True
        assert listed["session_count"] == 1
        assert listed["sessions"][0]["session_name"] == "probe"
        assert listed["sessions"][0]["managed"] is True

        sent = client.send(session="probe", text="中文 hello", enter_count=1)
        assert sent["success"] is True
        assert sent["accepted_by_tmux"] is True
        assert "text" not in sent

        status = _wait_for_status(client, "probe")
        assert status["success"] is True
        pane = status["session"]["panes"][0]
        assert pane["pane_dead"] is True
        assert pane["exit_code"] == 7

        captured = client.capture(session="probe", lines=50)
        assert captured["success"] is True
        assert "got:中文 hello" in captured["content"]

        killed = client.kill(session="probe")
        assert killed["success"] is True
        assert killed["killed"] is True
        killed_again = client.kill(session="probe")
        assert killed_again["success"] is True
        assert killed_again["already_absent"] is True
    finally:
        client.kill(session="probe")
        subprocess.run(
            [shutil.which("tmux") or "tmux", "-L", socket_name, "kill-server"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            env={key: value for key, value in os.environ.items() if key not in {"TMUX", "TMUX_PANE"}},
        )
