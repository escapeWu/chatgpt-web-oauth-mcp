from __future__ import annotations

import asyncio
import json
import os
import shlex
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

import pytest

from chatgpt_web_oauth_mcp.job_supervisor import JOB_METADATA_SCHEMA_VERSION, write_job_metadata
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
    deadline = time.monotonic() + 5
    latest = fetch()
    while not done(latest) and time.monotonic() < deadline:
        time.sleep(0.02)
        latest = fetch()
    return latest


def _process_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if sys.platform.startswith("linux"):
        try:
            state = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8").split(")", 1)[1].split()[0]
        except (FileNotFoundError, IndexError, OSError):
            return False
        return state != "Z"
    try:
        checked = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "stat="],
            text=True,
            capture_output=True,
            timeout=1,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True
    process_state = checked.stdout.strip()
    return checked.returncode == 0 and bool(process_state) and not process_state.startswith("Z")


def _wait_until_process_gone(pid: int) -> bool:
    deadline = time.monotonic() + 5
    while _process_is_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    return not _process_is_alive(pid)


def _install_isolated_job_registry(monkeypatch, tmp_path: Path):
    from chatgpt_web_oauth_mcp import server

    monkeypatch.setattr(server, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(server, "job_registry", JobRegistry())
    return server


def _install_supervisor_proxy(monkeypatch, tmp_path: Path, source: str) -> Path:
    from chatgpt_web_oauth_mcp import shell

    proxy_dir = tmp_path / "supervisor-proxy"
    proxy_dir.mkdir()
    proxy_path = proxy_dir / "job_supervisor.py"
    proxy_path.write_text(source, encoding="utf-8")
    monkeypatch.setattr(shell, "__file__", str(proxy_dir / "shell.py"))
    return proxy_path


def _write_durable_job(
    state_dir: Path,
    *,
    job_id: str,
    started_at: float,
    status: str = "succeeded",
    command: str = "printf done",
    stdout: bytes = b"",
    stderr: bytes = b"",
) -> Path:
    job_dir = state_dir / "jobs" / job_id
    job_dir.mkdir(parents=True, mode=0o700)
    stdout_log = job_dir / "stdout.log"
    stderr_log = job_dir / "stderr.log"
    stdout_log.write_bytes(stdout)
    stderr_log.write_bytes(stderr)
    stdout_log.chmod(0o600)
    stderr_log.chmod(0o600)
    completed_at = started_at + 1 if status in {"succeeded", "failed", "killed", "interrupted"} else None
    write_job_metadata(
        job_dir,
        {
            "schema_version": JOB_METADATA_SCHEMA_VERSION,
            "job_id": job_id,
            "name": job_id.removeprefix("job_"),
            "command": command,
            "cwd": str(state_dir.parent),
            "status": status,
            "pid": None,
            "pgid": None,
            "process_identity": None,
            "supervisor_pid": None,
            "supervisor_identity": None,
            "started_at": started_at,
            "completed_at": completed_at,
            "updated_at": completed_at or started_at,
            "exit_code": 0 if status == "succeeded" else None,
            "kill_signal": None,
            "stdout_log": str(stdout_log),
            "stderr_log": str(stderr_log),
        },
    )
    return job_dir


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


@pytest.mark.skipif(os.name != "posix", reason="Detached supervisor bootstrap uses fork on POSIX.")
def test_job_start_uses_overall_deadline_when_bootstrap_exceeds_old_two_second_wait(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from chatgpt_web_oauth_mcp import job_supervisor

    _install_supervisor_proxy(
        monkeypatch,
        tmp_path,
        "import runpy, time\n"
        "time.sleep(2.1)\n"
        f"runpy.run_path({str(Path(job_supervisor.__file__).resolve())!r}, run_name='__main__')\n",
    )
    state_dir = tmp_path / "state"
    registry = JobRegistry()

    started = registry.start_job(
        command=_python_cmd("print('delayed-bootstrap-ok')"),
        cwd=tmp_path,
        state_dir=state_dir,
        name="delayed-bootstrap",
    )

    assert started["success"] is True
    completed = _wait_for(
        lambda: registry.job_status(job_id=started["job_id"], state_dir=state_dir),
        lambda item: item["status"] != "running",
    )
    assert completed["status"] == "succeeded"
    assert Path(completed["stdout_log"]).read_text(encoding="utf-8").strip() == "delayed-bootstrap-ok"
    metadata = json.loads((state_dir / "jobs" / started["job_id"] / "metadata.json").read_text(encoding="utf-8"))
    assert _wait_until_process_gone(metadata["supervisor_pid"])


def test_job_start_reports_nonzero_bootstrap_exit_with_log_evidence(tmp_path: Path, monkeypatch) -> None:
    _install_supervisor_proxy(
        monkeypatch,
        tmp_path,
        "import sys\n"
        "print('intentional bootstrap failure', file=sys.stderr, flush=True)\n"
        "raise SystemExit(23)\n",
    )
    state_dir = tmp_path / "state"

    started = JobRegistry().start_job(command="true", cwd=tmp_path, state_dir=state_dir)

    assert started["success"] is False
    assert started["error"]["code"] == "job_start_failed"
    assert "code 23" in started["error"]["message"]
    assert str(started["stderr_log"]) in started["error"]["message"]
    assert "intentional bootstrap failure" in Path(started["stderr_log"]).read_text(encoding="utf-8")
    metadata = json.loads((state_dir / "jobs" / started["job_id"] / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["status"] == "failed"
    assert metadata["exit_code"] == 23


@pytest.mark.skipif(os.name != "posix", reason="Detached supervisor bootstrap uses fork on POSIX.")
def test_job_start_timeout_cleans_detached_supervisor_and_unpublished_job(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from chatgpt_web_oauth_mcp import shell

    _install_supervisor_proxy(
        monkeypatch,
        tmp_path,
        "import time\n"
        "from chatgpt_web_oauth_mcp import job_supervisor as target\n"
        "original_update = target.update_job_metadata\n"
        "def delayed_running_publication(job_dir, **updates):\n"
        "    if updates.get('status') == 'running':\n"
        "        time.sleep(3)\n"
        "    return original_update(job_dir, **updates)\n"
        "target.update_job_metadata = delayed_running_publication\n"
        "raise SystemExit(target.main())\n",
    )
    monkeypatch.setattr(shell, "_JOB_SUPERVISOR_START_TIMEOUT_SECONDS", 0.75)
    state_dir = tmp_path / "state"
    pid_marker = tmp_path / "startup-timeout-job.pid"
    command = (
        f"echo $$ > {shlex.quote(str(pid_marker))}; "
        f"exec {shlex.quote(sys.executable)} -c {shlex.quote('import time; time.sleep(30)')}"
    )

    started = JobRegistry().start_job(command=command, cwd=tmp_path, state_dir=state_dir)

    assert started["success"] is False
    assert started["error"]["code"] == "job_start_failed"
    assert "bootstrap_exit_code=0" in started["error"]["message"]
    assert "metadata_status='starting'" in started["error"]["message"]
    assert pid_marker.is_file()
    job_pid = int(pid_marker.read_text(encoding="utf-8").strip())
    metadata = json.loads((state_dir / "jobs" / started["job_id"] / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["status"] == "failed"
    assert _wait_until_process_gone(job_pid)
    assert _wait_until_process_gone(metadata["supervisor_pid"])


def test_repeated_immediate_jobs_publish_recoverable_terminal_metadata_and_logs(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    registry = JobRegistry()
    started_jobs: list[dict[str, object]] = []

    for index in range(8):
        started = registry.start_job(
            command=_python_cmd(f"print('instant-{index}')"),
            cwd=tmp_path,
            state_dir=state_dir,
            name=f"instant-{index}",
        )
        assert started["success"] is True
        started_jobs.append(started)

    for index, started in enumerate(started_jobs):
        completed = _wait_for(
            lambda started=started: registry.job_status(job_id=started["job_id"], state_dir=state_dir),
            lambda item: item["status"] != "running",
        )
        assert completed["status"] == "succeeded"
        assert completed["exit_code"] == 0
        assert Path(completed["stdout_log"]).read_text(encoding="utf-8").strip() == f"instant-{index}"
        metadata_path = state_dir / "jobs" / started["job_id"] / "metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        assert metadata["status"] == "succeeded"
        assert _wait_until_process_gone(metadata["supervisor_pid"])


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


def test_fresh_registry_recovers_running_completed_logs_and_private_metadata(tmp_path: Path, monkeypatch) -> None:
    server = _install_isolated_job_registry(monkeypatch, tmp_path)
    secret_value = "p0-job-env-value-must-not-be-persisted"
    release_path = tmp_path / "release-recovered-job"
    started = _call(
        server.job_start,
        command=_python_cmd(
            "import os, pathlib, time\n"
            "assert os.environ.get('P0_JOB_SECRET')\n"
            "print('recovery-ready', flush=True)\n"
            f"release = pathlib.Path({str(release_path)!r})\n"
            "while not release.exists():\n"
            "    time.sleep(0.02)\n"
            "print('recovery-done', flush=True)\n"
        ),
        cwd=str(tmp_path),
        env={"P0_JOB_SECRET": secret_value},
        name="recover-me",
    )
    assert started["success"] is True
    job_id = started["job_id"]
    monkeypatch.setattr(server, "job_registry", JobRegistry())

    try:
        ready = _wait_for(
            lambda: _call(server.job_tail, job_id=job_id, stream="stdout", lines=10),
            lambda item: "recovery-ready" in item["content"],
        )
        assert "recovery-ready" in ready["content"]

        running = _call(server.job_status, job_id=job_id)
        assert running["success"] is True
        assert running["status"] == "running"
        assert running["pid"] == started["pid"]

        release_path.touch()
        completed = _wait_for(
            lambda: _call(server.job_status, job_id=job_id),
            lambda item: item["status"] != "running",
        )
        assert completed["status"] == "succeeded"
        assert completed["exit_code"] == 0

        finished_tail = _call(server.job_tail, job_id=job_id, stream="stdout", lines=10)
        assert finished_tail["lines"] == ["recovery-ready", "recovery-done"]

        job_dir = tmp_path / "state" / "jobs" / job_id
        metadata_path = job_dir / "metadata.json"
        metadata_text = metadata_path.read_text(encoding="utf-8")
        metadata = json.loads(metadata_text)
        assert metadata["schema_version"] == JOB_METADATA_SCHEMA_VERSION
        assert metadata["status"] == "succeeded"
        assert metadata["pid"] == started["pid"]
        assert metadata["supervisor_pid"] != metadata["pid"]
        assert secret_value not in metadata_text
        assert stat.S_IMODE(job_dir.stat().st_mode) == 0o700
        for private_file in [metadata_path, job_dir / "stdout.log", job_dir / "stderr.log"]:
            assert stat.S_IMODE(private_file.stat().st_mode) == 0o600

        first_kill = _call(server.job_kill, job_id=job_id)
        second_kill = _call(server.job_kill, job_id=job_id, signal="KILL")
        assert first_kill["status"] == "succeeded"
        assert first_kill["signal_sent"] is False
        assert first_kill["already_completed"] is True
        assert second_kill["status"] == "succeeded"
        assert second_kill["signal_sent"] is False
        assert second_kill["already_completed"] is True
    finally:
        release_path.touch(exist_ok=True)
        final_status = _call(server.job_status, job_id=job_id)
        if final_status["status"] == "running":
            _call(server.job_kill, job_id=job_id, signal="KILL")


@pytest.mark.skipif(os.name != "posix", reason="Durable job process-group termination is POSIX-only.")
def test_fresh_registry_kills_child_tree_that_ignores_sigterm(tmp_path: Path, monkeypatch) -> None:
    server = _install_isolated_job_registry(monkeypatch, tmp_path)
    child_code = (
        "import signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "print('child-ready', flush=True); "
        "time.sleep(30)"
    )
    parent_code = (
        "import subprocess, sys, time; "
        f"child = subprocess.Popen([sys.executable, '-u', '-c', {child_code!r}]); "
        "print(f'child-pid:{child.pid}', flush=True); "
        "time.sleep(30)"
    )
    started = _call(server.job_start, command=_python_cmd(parent_code), cwd=str(tmp_path), name="kill-tree")
    assert started["success"] is True
    job_id = started["job_id"]
    child_pid: int | None = None
    monkeypatch.setattr(server, "job_registry", JobRegistry())

    try:
        child_output = _wait_for(
            lambda: _call(server.job_tail, job_id=job_id, stream="stdout", lines=10),
            lambda item: "child-pid:" in item["content"] and "child-ready" in item["content"],
        )
        child_line = next(line for line in child_output["lines"] if line.startswith("child-pid:"))
        child_pid = int(child_line.split(":", 1)[1])
        assert _process_is_alive(child_pid)

        killed = _call(server.job_kill, job_id=job_id)
        assert killed["success"] is True
        assert killed["signal"] == "TERM"
        assert killed["signal_sent"] is True

        terminal = _wait_for(
            lambda: _call(server.job_status, job_id=job_id),
            lambda item: item["status"] != "running",
        )
        assert terminal["status"] == "killed"
        assert terminal["exit_code"] is not None
        assert _wait_until_process_gone(child_pid)
        assert _wait_until_process_gone(started["pid"])
    finally:
        final_status = _call(server.job_status, job_id=job_id)
        if final_status["status"] == "running":
            _call(server.job_kill, job_id=job_id, signal="KILL")
        if child_pid is not None and _process_is_alive(child_pid):
            os.kill(child_pid, signal.SIGKILL)
            assert _wait_until_process_gone(child_pid)


def test_stale_nonterminal_record_becomes_interrupted_idempotently(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    job_id = "job_stale_test"
    job_dir = state_dir / "jobs" / job_id
    job_dir.mkdir(parents=True, mode=0o700)
    (job_dir / "stdout.log").touch(mode=0o600)
    (job_dir / "stderr.log").touch(mode=0o600)
    stale_pid = 999_999_999
    write_job_metadata(
        job_dir,
        {
            "schema_version": JOB_METADATA_SCHEMA_VERSION,
            "job_id": job_id,
            "name": "stale",
            "command": "sleep 30",
            "cwd": str(tmp_path),
            "status": "running",
            "pid": stale_pid,
            "pgid": stale_pid,
            "process_identity": "missing-process",
            "supervisor_pid": stale_pid - 1,
            "supervisor_identity": "missing-supervisor",
            "started_at": time.time() - 30,
            "completed_at": None,
            "updated_at": time.time() - 20,
            "exit_code": None,
            "kill_signal": None,
            "stdout_log": str(job_dir / "stdout.log"),
            "stderr_log": str(job_dir / "stderr.log"),
        },
    )

    first = JobRegistry().job_status(job_id=job_id, state_dir=state_dir)
    second = JobRegistry().job_status(job_id=job_id, state_dir=state_dir)
    assert first["success"] is True
    assert first["status"] == "interrupted"
    assert first["exit_code"] is None
    assert second["status"] == "interrupted"
    metadata = json.loads((job_dir / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["interruption_reason"] == "supervisor_and_command_gone_without_terminal_record"


def test_job_list_discovers_disk_records_sorts_filters_pages_and_skips_corruption(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    long_command = "python -c " + "x" * 2000
    _write_durable_job(
        state_dir,
        job_id="job_new_b",
        started_at=30,
        command=long_command,
    )
    _write_durable_job(state_dir, job_id="job_new_a", started_at=30)
    _write_durable_job(state_dir, job_id="job_failed", started_at=20, status="failed")
    _write_durable_job(state_dir, job_id="job_killed", started_at=10, status="killed")

    stale_dir = _write_durable_job(state_dir, job_id="job_stale", started_at=5)
    stale_metadata = json.loads((stale_dir / "metadata.json").read_text(encoding="utf-8"))
    stale_metadata.update(
        {
            "status": "running",
            "completed_at": None,
            "exit_code": None,
            "pid": 999_999_999,
            "pgid": 999_999_999,
            "process_identity": "missing-process",
            "supervisor_pid": 999_999_998,
            "supervisor_identity": "missing-supervisor",
        }
    )
    write_job_metadata(stale_dir, stale_metadata)

    corrupt_dir = state_dir / "jobs" / "job_corrupt"
    corrupt_dir.mkdir()
    (corrupt_dir / "metadata.json").write_text("{not-json", encoding="utf-8")
    (corrupt_dir / "stdout.log").touch()
    (corrupt_dir / "stderr.log").touch()
    (state_dir / "jobs" / "not-a-job.txt").write_text("ignored", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (state_dir / "jobs" / "job_symlink").symlink_to(outside, target_is_directory=True)
    metadata_link_dir = state_dir / "jobs" / "job_metadata_link"
    metadata_link_dir.mkdir()
    (metadata_link_dir / "stdout.log").touch()
    (metadata_link_dir / "stderr.log").touch()
    outside_metadata = outside / "metadata.json"
    outside_metadata.write_text("{}", encoding="utf-8")
    (metadata_link_dir / "metadata.json").symlink_to(outside_metadata)

    registry = JobRegistry()
    first = registry.list_jobs(state_dir=state_dir, limit=2)
    second = JobRegistry().list_jobs(state_dir=state_dir, offset=first["next_offset"], limit=10)
    interrupted = JobRegistry().list_jobs(state_dir=state_dir, status="interrupted")

    assert first["success"] is True
    assert first["total"] == 5
    assert first["returned"] == 2
    assert [item["job_id"] for item in first["jobs"]] == ["job_new_a", "job_new_b"]
    assert first["next_offset"] == 2
    assert first["page"]["stop_reason"] == "item_limit"
    assert second["next_offset"] is None
    assert [item["job_id"] for item in [*first["jobs"], *second["jobs"]]] == [
        "job_new_a",
        "job_new_b",
        "job_failed",
        "job_killed",
        "job_stale",
    ]
    long_summary = next(item for item in first["jobs"] if item["job_id"] == "job_new_b")
    assert long_summary["command_truncated"] is True
    assert long_summary["command_characters"] == len(long_command)
    assert len(long_summary["command"]) == 512
    assert first["skipped"]["count"] == 4
    assert {warning["code"] for warning in first["skipped"]["warnings"]} == {
        "job_metadata_invalid",
        "non_job_record",
        "symlink_record",
    }
    assert interrupted["total"] == 1
    assert interrupted["jobs"][0]["job_id"] == "job_stale"
    assert interrupted["jobs"][0]["completed_at"] is not None
    assert json.loads((stale_dir / "metadata.json").read_text(encoding="utf-8"))["status"] == "interrupted"


def test_job_list_token_budget_pagination_advances_by_actual_returned_count(tmp_path: Path) -> None:
    from chatgpt_web_oauth_mcp.response_budget import ResponseBudget

    state_dir = tmp_path / "state"
    for index in range(6):
        _write_durable_job(
            state_dir,
            job_id=f"job_budget_{index}",
            started_at=float(index),
            command=f"command-{index}-" + "x" * 400,
        )

    registry = JobRegistry()
    offset = 0
    seen: list[str] = []
    while True:
        page = registry.list_jobs(state_dir=state_dir, offset=offset, limit=6, max_tokens=300)
        assert page["returned"] >= 1
        assert page["next_offset"] in {None, offset + page["returned"]}
        actual_tokens = ResponseBudget(max_tokens=300).count_tokens(
            json.dumps(page, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
        assert page["page"]["estimated_tokens"] == actual_tokens
        if not page["page"]["oversized_item"]:
            assert page["page"]["estimated_tokens"] <= 300
        seen.extend(item["job_id"] for item in page["jobs"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]

    assert seen == [f"job_budget_{index}" for index in reversed(range(6))]


def test_job_output_reconstructs_multibyte_utf8_and_keeps_stream_cursors_independent(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    stdout = "A🙂中B\n".encode()
    stderr = "错误\n".encode()
    _write_durable_job(
        state_dir,
        job_id="job_utf8",
        started_at=1,
        stdout=stdout,
        stderr=stderr,
    )

    registry = JobRegistry()
    cursor = 0
    pieces: list[str] = []
    while True:
        page = registry.output_job(
            job_id="job_utf8",
            state_dir=state_dir,
            stream="stdout",
            cursor=cursor,
            max_bytes=5,
        )
        assert page["cursor"] == cursor
        assert page["bytes_returned"] <= 5
        assert page["decoding"]["replacement_used"] is False
        pieces.append(page["content"])
        cursor = page["next_cursor"]
        if page["eof"]:
            break

    stderr_page = registry.output_job(
        job_id="job_utf8",
        state_dir=state_dir,
        stream="stderr",
        cursor=0,
        max_bytes=65536,
    )
    tiny_byte_page = registry.output_job(
        job_id="job_utf8",
        state_dir=state_dir,
        stream="stderr",
        cursor=0,
        max_bytes=1,
    )
    assert "".join(pieces).encode() == stdout
    assert cursor == len(stdout)
    assert stderr_page["content"].encode() == stderr
    assert stderr_page["cursor"] == 0
    assert stderr_page["next_cursor"] == len(stderr)
    assert tiny_byte_page["content"] == ""
    assert tiny_byte_page["next_cursor"] == 0
    assert tiny_byte_page["stop_reason"] == "byte_budget_before_utf8_unit"
    assert tiny_byte_page["minimum_max_bytes_for_progress"] == 3
    assert tiny_byte_page["decoding"]["replacement_used"] is False


def test_job_output_enforces_token_budget_and_progresses_on_invalid_and_oversized_units(tmp_path: Path) -> None:
    from chatgpt_web_oauth_mcp.response_budget import ResponseBudget

    state_dir = tmp_path / "state"
    _write_durable_job(
        state_dir,
        job_id="job_invalid_utf8",
        started_at=1,
        stdout=b"a\xffb",
    )
    oversized_character = "\U0010ffff"
    assert ResponseBudget(max_tokens=1).count_tokens(oversized_character) > 1
    _write_durable_job(
        state_dir,
        job_id="job_oversized_unit",
        started_at=2,
        stdout=oversized_character.encode(),
    )
    _write_durable_job(
        state_dir,
        job_id="job_token_pages",
        started_at=3,
        stdout=b"abcdefghijklmnopqrstuvwxyz",
    )

    registry = JobRegistry()
    invalid = registry.output_job(job_id="job_invalid_utf8", state_dir=state_dir)
    oversized = registry.output_job(
        job_id="job_oversized_unit",
        state_dir=state_dir,
        max_tokens=1,
    )
    first = registry.output_job(
        job_id="job_token_pages",
        state_dir=state_dir,
        max_tokens=2,
    )

    assert invalid["content"] == "a\ufffdb"
    assert invalid["next_cursor"] == 3
    assert invalid["decoding"]["replacement_count"] == 1
    assert invalid["decoding"]["valid_utf8"] is False
    assert oversized["content"] == oversized_character
    assert oversized["next_cursor"] == len(oversized_character.encode())
    assert oversized["oversized_unit"] is True
    assert oversized["oversized_unit_marker"] == "first_complete_unit_exceeds_token_budget"
    assert oversized["stop_reason"] == "oversized_unit"
    assert 0 < first["next_cursor"] < first["file_size"]
    assert first["estimated_tokens"] <= 2
    assert first["stop_reason"] == "token_budget"


def test_job_output_invalid_cursor_terminal_eof_and_symlink_log_are_structured(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    job_dir = _write_durable_job(
        state_dir,
        job_id="job_output_edges",
        started_at=1,
        stdout=b"done",
    )
    registry = JobRegistry()
    invalid = registry.output_job(
        job_id="job_output_edges",
        state_dir=state_dir,
        cursor=5,
    )
    eof = registry.output_job(
        job_id="job_output_edges",
        state_dir=state_dir,
        cursor=4,
        wait_ms=30000,
    )
    outside = tmp_path / "outside.log"
    outside.write_text("secret", encoding="utf-8")
    (job_dir / "stderr.log").unlink()
    (job_dir / "stderr.log").symlink_to(outside)
    symlink = registry.output_job(
        job_id="job_output_edges",
        state_dir=state_dir,
        stream="stderr",
    )

    assert invalid["success"] is False
    assert invalid["error"]["code"] == "invalid_cursor"
    assert invalid["file_size"] == 4
    assert eof["success"] is True
    assert eof["content"] == ""
    assert eof["eof"] is True
    assert eof["waited_ms"] == 0
    assert symlink["success"] is False
    assert symlink["error"]["code"] == "job_log_invalid"


def test_job_output_long_poll_wakes_for_bytes_and_terminal_status(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    registry = JobRegistry()
    bytes_job = registry.start_job(
        command=_python_cmd("import time; time.sleep(0.3); print('late', flush=True); time.sleep(0.1)"),
        cwd=tmp_path,
        state_dir=state_dir,
    )
    terminal_job: dict[str, object] | None = None
    try:
        bytes_page = JobRegistry().output_job(
            job_id=bytes_job["job_id"],
            state_dir=state_dir,
            cursor=0,
            wait_ms=2000,
        )
        terminal_job = registry.start_job(
            command=_python_cmd("import time; time.sleep(0.3)"),
            cwd=tmp_path,
            state_dir=state_dir,
        )
        terminal_page = JobRegistry().output_job(
            job_id=terminal_job["job_id"],
            state_dir=state_dir,
            cursor=0,
            wait_ms=2000,
        )

        assert bytes_page["content"] == "late\n"
        assert 0 < bytes_page["waited_ms"] < 2000
        assert terminal_page["status"] == "succeeded"
        assert terminal_page["content"] == ""
        assert terminal_page["eof"] is True
        assert 0 < terminal_page["waited_ms"] < 2000
    finally:
        for started in [bytes_job, terminal_job]:
            if started is None:
                continue
            status = registry.job_status(job_id=started["job_id"], state_dir=state_dir)
            if status["status"] == "running":
                registry.kill_job(job_id=started["job_id"], state_dir=state_dir, signal_name="KILL")


def test_job_wrappers_pass_current_runtime_state_dir_on_every_operation(tmp_path: Path, monkeypatch) -> None:
    from chatgpt_web_oauth_mcp import server

    observed: list[tuple[str, Path]] = []

    class CapturingRegistry:
        def start_job(self, **kwargs):
            observed.append(("start", kwargs["state_dir"]))
            return {"success": True}

        def job_status(self, **kwargs):
            observed.append(("status", kwargs["state_dir"]))
            return {"success": True}

        def list_jobs(self, **kwargs):
            observed.append(("list", kwargs["state_dir"]))
            return {"success": True}

        def output_job(self, **kwargs):
            observed.append(("output", kwargs["state_dir"]))
            return {"success": True}

        def tail_job(self, **kwargs):
            observed.append(("tail", kwargs["state_dir"]))
            return {"success": True}

        def kill_job(self, **kwargs):
            observed.append(("kill", kwargs["state_dir"]))
            return {"success": True}

    runtime_state_dir = tmp_path / "runtime-state"
    monkeypatch.setattr(server, "STATE_DIR", runtime_state_dir)
    monkeypatch.setattr(server, "job_registry", CapturingRegistry())
    _call(server.job_start, command="true", cwd=str(tmp_path))
    _call(server.job_list)
    _call(server.job_status, job_id="job_runtime_test")
    _call(server.job_output, job_id="job_runtime_test")
    _call(server.job_tail, job_id="job_runtime_test")
    _call(server.job_kill, job_id="job_runtime_test")
    assert observed == [
        ("start", runtime_state_dir),
        ("list", runtime_state_dir),
        ("status", runtime_state_dir),
        ("output", runtime_state_dir),
        ("tail", runtime_state_dir),
        ("kill", runtime_state_dir),
    ]


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
    for name in ["job_start", "job_list", "job_status", "job_output", "job_tail", "job_kill"]:
        assert name in descriptors

    assert descriptors["job_start"]["annotations"]["openWorldHint"] is True
    assert descriptors["job_kill"]["annotations"]["openWorldHint"] is True
    assert descriptors["job_status"]["annotations"]["readOnlyHint"] is True
    assert descriptors["job_list"]["annotations"]["readOnlyHint"] is True
    assert descriptors["job_output"]["annotations"]["readOnlyHint"] is True
    assert descriptors["job_tail"]["annotations"]["readOnlyHint"] is True
    assert descriptors["job_start"]["parameters"]["required"] == ["command"]
    assert descriptors["job_status"]["parameters"]["required"] == ["job_id"]
    assert "required" not in descriptors["job_list"]["parameters"]
    assert descriptors["job_output"]["parameters"]["required"] == ["job_id"]
    assert descriptors["job_tail"]["parameters"]["required"] == ["job_id"]
    assert descriptors["job_kill"]["parameters"]["required"] == ["job_id"]
    assert descriptors["job_start"]["parameters"]["properties"]["cwd"]["default"] is None
    assert descriptors["job_start"]["parameters"]["properties"]["env"]["default"] is None
    assert descriptors["job_start"]["parameters"]["properties"]["name"]["default"] is None
    assert descriptors["job_tail"]["parameters"]["properties"]["stream"]["enum"] == ["stdout", "stderr"]
    assert descriptors["job_tail"]["parameters"]["properties"]["stream"]["default"] == "stdout"
    assert descriptors["job_tail"]["parameters"]["properties"]["lines"]["default"] == 50
    assert descriptors["job_list"]["parameters"]["properties"]["status"]["enum"] == [
        "all",
        "running",
        "succeeded",
        "failed",
        "killed",
        "interrupted",
    ]
    assert descriptors["job_list"]["parameters"]["properties"]["offset"]["minimum"] == 0
    assert descriptors["job_list"]["parameters"]["properties"]["limit"]["maximum"] == 200
    assert descriptors["job_output"]["parameters"]["properties"]["stream"]["enum"] == ["stdout", "stderr"]
    assert descriptors["job_output"]["parameters"]["properties"]["cursor"]["minimum"] == 0
    assert descriptors["job_output"]["parameters"]["properties"]["max_bytes"]["maximum"] == 262144
    assert descriptors["job_output"]["parameters"]["properties"]["wait_ms"]["maximum"] == 30000
    assert descriptors["job_kill"]["parameters"]["properties"]["signal"]["enum"] == ["TERM", "KILL"]
    assert descriptors["job_kill"]["parameters"]["properties"]["signal"]["default"] == "TERM"
