from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import chatgpt_web_oauth_mcp.executors as executors
from chatgpt_web_oauth_mcp.executors import ExecutorRegistry, Invocation


def test_run_codex_returns_synchronous_result(tmp_path: Path) -> None:
    registry = ExecutorRegistry(codex_command="python3 -c \"print('done')\"")

    result = registry.run_codex(task="finish", cwd=tmp_path, timeout=5)

    assert result["success"] is True
    assert result["status"] == "succeeded"
    assert result["completed"] is True
    assert result["in_progress"] is False
    assert result["executor"] == "codex"
    assert result["serial"] is True
    assert result["delegate_id"]
    assert "task_id" not in result
    assert "stdout" not in result
    assert "stderr" not in result
    assert result["output_omitted"] is True
    logs = result["logs"]
    assert Path(logs["log_dir"]).is_dir()
    assert Path(logs["prompt"]).read_text(encoding="utf-8")
    assert Path(logs["stdout"]).read_text(encoding="utf-8").strip() == "done"
    metadata = json.loads(Path(logs["metadata"]).read_text(encoding="utf-8"))
    assert metadata["delegate_id"] == result["delegate_id"]
    assert metadata["status"] == "succeeded"
    assert metadata["stdout_bytes"] >= 4


def test_run_codex_attaches_concurrent_calls_to_one_active_delegate(tmp_path: Path) -> None:
    registry = ExecutorRegistry(
        codex_command="python3 -c \"import time; time.sleep(0.2); print('done')\""
    )
    results: list[dict[str, object]] = []

    def run_delegate() -> None:
        results.append(registry.run_codex(task="finish", cwd=tmp_path, timeout=5))

    first = threading.Thread(target=run_delegate)
    second = threading.Thread(target=run_delegate)
    start = time.monotonic()
    first.start()
    second.start()
    first.join(timeout=5)
    second.join(timeout=5)
    elapsed = time.monotonic() - start

    assert len(results) == 2
    assert all(result["status"] == "succeeded" for result in results)
    assert {result["delegate_id"] for result in results}
    assert len({result["delegate_id"] for result in results}) == 1
    assert elapsed < 0.35


def test_run_codex_long_poll_returns_running_without_killing_process(tmp_path: Path) -> None:
    registry = ExecutorRegistry(
        codex_command="python3 -c \"import time; time.sleep(0.2); print('done')\""
    )

    running = registry.run_codex(task="finish", cwd=tmp_path, timeout=5, wait_seconds=0.05)

    assert running["success"] is True
    assert running["status"] == "running"
    assert running["in_progress"] is True
    assert running["completed"] is False
    assert running["wait_timed_out"] is True
    assert Path(running["logs"]["log_dir"]).is_dir()
    assert running["activity_state"] in {"active", "starting_or_quiet", "suspected_stalled"}
    assert isinstance(running["last_output_seconds_ago"], float)

    result = registry.run_codex(task=None, cwd=tmp_path, timeout=5, wait_seconds=2)

    assert result["status"] == "succeeded"
    assert result["delegate_id"] == running["delegate_id"]
    assert result["logs"]["log_dir"] == running["logs"]["log_dir"]
    assert "stdout" not in result
    assert Path(result["logs"]["stdout"]).read_text(encoding="utf-8").strip() == "done"


def test_completed_delegate_does_not_hijack_new_task(tmp_path: Path) -> None:
    registry = ExecutorRegistry(
        codex_command="python3 -c \"import time; time.sleep(0.1); print('first')\""
    )

    first = registry.run_codex(
        task="first task",
        task_id="N04",
        cwd=tmp_path,
        timeout=5,
        wait_seconds=0.01,
    )
    time.sleep(0.2)
    registry.codex_command = "python3 -c \"print('second')\""

    second = registry.run_codex(
        task="second task",
        task_id="N05",
        cwd=tmp_path,
        timeout=5,
        wait_seconds=2,
    )

    assert first["status"] == "running"
    assert second["status"] == "succeeded"
    assert second["delegate_id"] != first["delegate_id"]
    assert second["task_id"] == "N05"
    assert Path(second["logs"]["stdout"]).read_text(encoding="utf-8").strip() == "second"


def test_running_delegate_rejects_different_new_task_without_starting_it(tmp_path: Path) -> None:
    registry = ExecutorRegistry(
        codex_command="python3 -c \"import time; time.sleep(0.3); print('first')\""
    )

    first = registry.run_codex(
        task="first task",
        task_id="N04",
        cwd=tmp_path,
        timeout=5,
        wait_seconds=0.01,
    )
    second = registry.run_codex(
        task="second task",
        task_id="N05",
        cwd=tmp_path,
        timeout=5,
        wait_seconds=0.01,
    )

    assert first["status"] == "running"
    assert second["status"] == "running"
    assert second["delegate_id"] == first["delegate_id"]
    assert second["task_id"] == "N04"
    assert second["requested_task_id"] == "N05"
    assert second["new_task_started"] is False
    assert second["request_conflict"] is True


def test_delegate_status_lists_active_and_recent_delegates(tmp_path: Path) -> None:
    registry = ExecutorRegistry(
        codex_command="python3 -c \"import time; time.sleep(0.1); print('done')\""
    )

    running = registry.run_codex(
        task="status task",
        cwd=tmp_path,
        timeout=5,
        wait_seconds=0.01,
    )
    active_status = registry.delegate_status()

    assert active_status["success"] is True
    assert active_status["active"]["delegate_id"] == running["delegate_id"]
    assert active_status["active"]["status"] == "running"
    assert active_status["active"]["task_id"] is None
    assert active_status["active"]["logs"]["stdout"]

    time.sleep(0.2)
    recent_status = registry.delegate_status()
    latest = recent_status["latest"]

    assert recent_status["active"] is None
    assert latest["delegate_id"] == running["delegate_id"]
    assert latest["status"] == "succeeded"
    assert latest["output_omitted"] is True
    assert "stdout" not in latest
    by_id = registry.delegate_status(delegate_id=running["delegate_id"])
    assert by_id["delegate"]["status"] == "succeeded"


def test_delegate_status_watch_returns_when_status_changes(tmp_path: Path) -> None:
    registry = ExecutorRegistry(
        codex_command="python3 -c \"import time; time.sleep(0.25); print('done')\""
    )

    running = registry.run_codex(
        task="watch status task",
        cwd=tmp_path,
        timeout=5,
        wait_seconds=0.01,
    )
    watched = registry.delegate_status(
        delegate_id=running["delegate_id"],
        watch_seconds=1,
        poll_seconds=0.05,
    )

    assert watched["delegate"]["delegate_id"] == running["delegate_id"]
    assert watched["delegate"]["status"] == "succeeded"
    assert watched["watch"]["status_changed"] is True
    assert watched["watch"]["timed_out"] is False


def test_delegate_status_watch_timeout_returns_last_snapshot(tmp_path: Path) -> None:
    registry = ExecutorRegistry(
        codex_command="python3 -c \"import time; time.sleep(0.5); print('done')\""
    )

    running = registry.run_codex(
        task="watch timeout task",
        cwd=tmp_path,
        timeout=5,
        wait_seconds=0.01,
    )
    watched = registry.delegate_status(
        delegate_id=running["delegate_id"],
        watch_seconds=0.1,
        poll_seconds=0.05,
    )

    assert watched["delegate"]["delegate_id"] == running["delegate_id"]
    assert watched["delegate"]["status"] == "running"
    assert watched["watch"]["status_changed"] is False
    assert watched["watch"]["timed_out"] is True

    completed = registry.run_codex(task=None, cwd=tmp_path, timeout=5, wait_seconds=1)
    assert completed["status"] == "succeeded"


def test_delegate_status_watch_returns_immediately_for_completed_delegate(tmp_path: Path) -> None:
    registry = ExecutorRegistry(codex_command="python3 -c \"print('done')\"")

    result = registry.run_codex(task="already done", cwd=tmp_path, timeout=5)
    started_at = time.monotonic()
    watched = registry.delegate_status(delegate_id=result["delegate_id"], watch_seconds=1)

    assert watched["delegate"]["status"] == "succeeded"
    assert watched["watch"]["already_terminal"] is True
    assert time.monotonic() - started_at < 0.5


def test_delegate_status_watch_returns_immediately_for_missing_delegate(tmp_path: Path) -> None:
    registry = ExecutorRegistry(codex_command="python3 -c \"print('done')\"")

    started_at = time.monotonic()
    watched = registry.delegate_status(delegate_id="missing", watch_seconds=1)

    assert watched["success"] is False
    assert watched["error"]["code"] == "delegate_not_found"
    assert watched["watch"]["error_returned"] is True
    assert time.monotonic() - started_at < 0.5


def test_run_codex_missing_task_returns_structured_failure(tmp_path: Path) -> None:
    registry = ExecutorRegistry(codex_command="python3 -c \"print('should-not-run')\"")

    result = registry.run_codex(task=None, goal=None, cwd=tmp_path, timeout=5)

    assert result["success"] is False
    assert result["status"] == "failed"
    assert result["completed"] is True
    assert result["in_progress"] is False
    assert result["error"]["code"] == "missing_task_or_goal"
    assert result["executor"] == "codex"
    assert "stdout" not in result
    assert "stderr" not in result


def test_run_codex_invalid_commit_mode_returns_structured_failure(tmp_path: Path) -> None:
    registry = ExecutorRegistry(codex_command="python3 -c \"print('should-not-run')\"")

    result = registry.run_codex(
        task="finish",
        cwd=tmp_path,
        timeout=5,
        commit_mode="disallowed",
    )

    assert result["success"] is False
    assert result["status"] == "failed"
    assert result["error"]["code"] == "unsupported_commit_mode"
    assert result["error"]["allowed_commit_modes"] == ["allowed", "forbidden", "required"]
    assert "stdout" not in result
    assert "stderr" not in result


def test_run_codex_soft_timeout_returns_running_without_killing_process(tmp_path: Path) -> None:
    registry = ExecutorRegistry(
        codex_command="python3 -c \"import time; time.sleep(2)\""
    )

    result = registry.run_codex(task="finish", cwd=tmp_path, timeout=1)

    assert result["success"] is True
    assert result["status"] == "running"
    assert result["completed"] is False
    assert result["timed_out"] is False
    assert result["soft_timeout_elapsed"] is True
    assert "stdout" in result["logs"]
    assert result["log_read_hint"]["tool"] == "read_text"

    time.sleep(1.2)
    completed = registry.run_codex(task=None, cwd=tmp_path, timeout=1, wait_seconds=5)

    assert completed["success"] is True
    assert completed["status"] == "succeeded"
    assert completed["timed_out"] is False
    assert completed["soft_timeout_elapsed"] is True
    assert completed["delegate_id"] == result["delegate_id"]


def test_run_codex_extracts_structured_json_output(tmp_path: Path) -> None:
    registry = ExecutorRegistry(codex_command="python3 -c \"print('{\\\"ok\\\": true}')\"")

    result = registry.run_codex(
        task="emit json",
        cwd=tmp_path,
        timeout=5,
        output_schema={"type": "object"},
        parse_structured_output=True,
    )

    assert result["status"] == "succeeded"
    assert result["structured_output"] == {"ok": True}
    assert result["output_schema"] == {"type": "object"}


def test_build_prompt_includes_structured_delegate_sections(tmp_path: Path) -> None:
    registry = ExecutorRegistry(codex_command="python3 -c \"print('codex')\"")

    prompt = registry._build_prompt(
        task="Implement the fallback flow",
        goal="Ship a working fallback task runner",
        task_id="T1",
        files_in_scope=["src/app.py"],
        out_of_scope=["git commit"],
        context_files=["README.md", "src/app.py"],
        acceptance_criteria=["Tool returns structured status", "Tests pass"],
        done_means=["Compact result reported"],
        verification_commands=["pytest -q", "python -m compileall src tests"],
        commit_mode="required",
    )

    assert "Architecture contract:" in prompt
    assert "Codex is the local executor for exactly one bounded execution slice." in prompt
    assert "Task ID:" in prompt
    assert "T1" in prompt
    assert "Goal:" in prompt
    assert "Ship a working fallback task runner" in prompt
    assert "Files in scope:" in prompt
    assert "- src/app.py" in prompt
    assert "Out of scope:" in prompt
    assert "- git commit" in prompt
    assert "Acceptance criteria:" in prompt
    assert "- Tool returns structured status" in prompt
    assert "Done means:" in prompt
    assert "- Compact result reported" in prompt
    assert "Verification commands:" in prompt
    assert "- pytest -q" in prompt
    assert "Commit mode: required" in prompt
    assert "Progress logging contract:" in prompt
    assert "read_text" in prompt
    assert "Output contract:" in prompt


def test_build_invocation_resolves_windows_codex_shim(tmp_path: Path, monkeypatch) -> None:
    registry = ExecutorRegistry(codex_command="codex")
    shim_path = r"C:\Users\test\AppData\Local\Programs\Codex\bin\codex.cmd"

    monkeypatch.setattr(executors, "IS_WINDOWS", True)
    monkeypatch.setattr(executors.shutil, "which", lambda binary: shim_path if binary == "codex" else None)

    invocation = registry._build_invocation(
        command="codex",
        task="Fix Windows startup",
        goal=None,
        cwd=tmp_path,
        context_files=[],
        acceptance_criteria=[],
        verification_commands=[],
        commit_mode="allowed",
    )

    assert invocation.use_shell is False
    assert invocation.args[0] == shim_path
    assert invocation.args[1:4] == ["exec", "--dangerously-bypass-approvals-and-sandbox", "-C"]
    assert invocation.args[-1] == "-"
    assert b"Fix Windows startup" in (invocation.stdin or b"")


def test_run_codex_decodes_utf8_process_output(tmp_path: Path, monkeypatch) -> None:
    registry = ExecutorRegistry(codex_command="codex")
    monkeypatch.setattr(
        registry,
        "_build_invocation",
        lambda **_: Invocation(args=["codex"], use_shell=False),
    )
    monkeypatch.setattr(executors, "_command_available", lambda command: True)

    popen_kwargs: dict[str, object] = {}

    class FakeProcess:
        def __init__(self, *args, **kwargs) -> None:
            popen_kwargs.update(kwargs)
            self.returncode = 0

        def communicate(self, timeout=None):
            return (b"done \xe2\x98\x83\xff", b"warn \xff")

        def kill(self) -> None:
            return None

    monkeypatch.setattr(executors.subprocess, "Popen", FakeProcess)

    result = registry.run_codex(task="Run codex", cwd=tmp_path, timeout=5)

    assert popen_kwargs["text"] is False
    assert result["status"] == "succeeded"
    assert "stdout" not in result
    assert "stderr" not in result
    logs = result["logs"]
    assert (
        Path(logs["stdout"]).read_bytes().decode("utf-8", errors="replace")
        == "done \u2603\ufffd"
    )
    assert Path(logs["stderr"]).read_bytes().decode("utf-8", errors="replace") == "warn \ufffd"


def test_run_codex_rejects_non_codex_delegate_modes(tmp_path: Path) -> None:
    registry = ExecutorRegistry(codex_command=None)

    result = registry.run_codex(task="finish", cwd=tmp_path, timeout=5)

    assert result["success"] is False
    assert result["completed"] is True
    assert result["error"]["code"] == "codex_unavailable"
