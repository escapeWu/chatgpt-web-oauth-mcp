import os
import shlex
import sys
import time
from pathlib import Path

import pytest

from chatgpt_web_oauth_mcp.shell import (
    MAX_COMMAND_BATCH_CONCURRENCY,
    MAX_COMMAND_TIMEOUT_SECONDS,
    TIMEOUT_EXIT_CODE,
    run_command,
    run_commands,
)
from chatgpt_web_oauth_mcp.response_budget import ResponseBudget


def _python_cmd(code: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


def test_run_command_returns_stdout_and_exit_code(tmp_path: Path) -> None:
    result = run_command(
        command="python3 -c \"print('hello')\"",
        cwd=tmp_path,
        timeout=5,
    )

    assert result["success"] is True
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "hello"
    assert result["timed_out"] is False


def test_run_command_preserves_small_stdout_stderr_and_failure(tmp_path: Path) -> None:
    result = run_command(
        command=_python_cmd(
            "import sys; "
            "sys.stdout.write('α\\nlast'); "
            "sys.stderr.write('β\\r\\n'); "
            "sys.exit(7)"
        ),
        cwd=tmp_path,
        timeout=5,
    )

    assert result["success"] is False
    assert result["exit_code"] == 7
    assert result["stdout"] == "α\nlast"
    assert result["stderr"] == "β\r\n"
    assert result["timed_out"] is False
    assert result["output_metadata"]["aggregate"]["truncated"] is False


def test_run_command_concurrently_drains_and_bounds_both_streams(tmp_path: Path) -> None:
    bytes_per_stream = 2 * 1024 * 1024
    code = (
        "import os, threading; "
        f"size={bytes_per_stream}; "
        "threads=[threading.Thread(target=os.write,args=(1,b'O'*size)), "
        "threading.Thread(target=os.write,args=(2,b'E'*size))]; "
        "[thread.start() for thread in threads]; "
        "[thread.join() for thread in threads]"
    )

    result = run_command(
        command=_python_cmd(code),
        cwd=tmp_path,
        timeout=10,
        max_tokens=1000,
        capture_max_bytes=4096,
    )

    metadata = result["output_metadata"]
    assert result["success"] is True
    assert metadata["stdout"]["total_bytes"] == bytes_per_stream
    assert metadata["stderr"]["total_bytes"] == bytes_per_stream
    assert metadata["aggregate"]["retained_bytes"] <= 4096
    assert metadata["aggregate"]["capture_memory_limit_bytes"] == 4096
    assert metadata["aggregate"]["capture_truncated"] is True
    assert metadata["stdout"]["long_line_truncated"] is True
    assert metadata["stderr"]["long_line_truncated"] is True
    assert "omitted" in result["stdout"]
    assert "omitted" in result["stderr"]


def test_run_command_large_output_keeps_head_tail_and_omission_marker(tmp_path: Path) -> None:
    line_count = 2000
    code = (
        "import sys; "
        f"[sys.stdout.write(f'line-{{index:04d}}\\n') for index in range({line_count})]"
    )

    result = run_command(
        command=_python_cmd(code),
        cwd=tmp_path,
        timeout=5,
        max_tokens=2000,
        capture_max_bytes=2048,
    )

    metadata = result["output_metadata"]
    assert result["success"] is True
    assert "line-0000" in result["stdout"]
    assert "line-1999" in result["stdout"]
    assert "omitted by capture limit" in result["stdout"]
    assert metadata["stdout"]["total_lines"] == line_count
    assert metadata["stdout"]["retained_bytes"] == 1024
    assert metadata["stdout"]["capture_dropped_bytes"] > 0
    assert metadata["stdout"]["displayed_bytes"] == len(result["stdout"].encode("utf-8"))


def test_run_command_combined_streams_respect_token_budget(tmp_path: Path) -> None:
    result = run_command(
        command=_python_cmd(
            "import sys; "
            "[print(f'out-{index:03d}') for index in range(200)]; "
            "[print(f'err-{index:03d}', file=sys.stderr) for index in range(200)]"
        ),
        cwd=tmp_path,
        timeout=5,
        max_tokens=80,
    )

    metadata = result["output_metadata"]
    assert result["success"] is True
    assert ResponseBudget(max_tokens=80).count_tokens(
        result["stdout"] + result["stderr"]
    ) <= 80
    assert metadata["aggregate"]["token_count"] <= 80
    assert metadata["aggregate"]["effective_token_budget"] == 80
    assert metadata["aggregate"]["displayed_payload_fits"] is True
    assert metadata["aggregate"]["token_truncated"] is True
    assert "omitted" in result["stdout"]
    assert "omitted" in result["stderr"]


def test_run_command_marks_one_complete_oversized_line(tmp_path: Path) -> None:
    line = "x" * 1000
    result = run_command(
        command=_python_cmd(f"import sys; sys.stdout.write({line!r})"),
        cwd=tmp_path,
        timeout=5,
        max_tokens=8,
        capture_max_bytes=4096,
    )

    metadata = result["output_metadata"]
    assert result["stdout"] == line
    assert metadata["aggregate"]["oversized_line"] is True
    assert metadata["aggregate"]["oversized_stream"] == "stdout"
    assert metadata["aggregate"]["displayed_payload_fits"] is False
    assert metadata["stdout"]["oversized_line"] is True


def test_run_command_timeout_returns_unified_shape(tmp_path: Path) -> None:
    result = run_command(
        command="sleep 2",
        cwd=tmp_path,
        timeout=1,
    )

    assert result["success"] is False
    assert result["timed_out"] is True
    # exit_code is always an int, never None, so callers can do numeric compares.
    assert isinstance(result["exit_code"], int)
    assert result["exit_code"] == TIMEOUT_EXIT_CODE
    assert result["timeout"] == 1
    assert result["error"]["code"] == "timed_out"
    assert "timeout" in result["error"]["message"].lower()
    assert result["hint"] == "increase_timeout_or_delegate"


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group lifecycle assertion")
def test_run_command_timeout_kills_child_process_and_keeps_output(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "child.pid"
    code = (
        "import pathlib, subprocess, sys, time; "
        "child=subprocess.Popen([sys.executable, '-c', "
        "'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)']); "
        f"pathlib.Path({str(child_pid_path)!r}).write_text(str(child.pid)); "
        "print('ready', flush=True); "
        "time.sleep(30)"
    )

    result = run_command(
        command=_python_cmd(code),
        cwd=tmp_path,
        timeout=1,
    )

    assert result["timed_out"] is True
    assert "ready\n" == result["stdout"]
    child_pid = int(child_pid_path.read_text())
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    else:
        pytest.fail(f"child process {child_pid} survived run_command timeout")


def test_run_command_rejects_timeout_above_limit_without_force(tmp_path: Path) -> None:
    result = run_command(
        command="echo hi",
        cwd=tmp_path,
        timeout=MAX_COMMAND_TIMEOUT_SECONDS + 1,
    )

    assert result["success"] is False
    assert result["timed_out"] is False
    assert result["error"]["code"] == "timeout_exceeds_limit"
    assert result["error"]["force_required"] is True
    assert result["error"]["approval_required"] is True
    assert result["hint"] == "delegate_task_or_force_after_user_approval"


def test_run_command_allows_timeout_above_limit_with_force(tmp_path: Path) -> None:
    result = run_command(
        command=_python_cmd("print('forced')"),
        cwd=tmp_path,
        timeout=MAX_COMMAND_TIMEOUT_SECONDS + 1,
        force=True,
    )

    assert result["success"] is True
    assert result["force"] is True
    assert result["stdout"].strip() == "forced"


def test_run_command_cwd_errors_include_exit_code_field(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    result = run_command(command="echo hi", cwd=missing, timeout=5)

    assert result["success"] is False
    assert result["timed_out"] is False
    # Even error shapes carry exit_code / stdout / stderr so LLM handling is uniform.
    assert isinstance(result["exit_code"], int)
    assert result["stdout"] == ""
    assert result["stderr"] == ""
    assert result["error"]["code"] == "cwd_not_found"


def test_run_commands_sequential_returns_ordered_batch_results(tmp_path: Path) -> None:
    result = run_commands(
        commands=[
            _python_cmd("print('first')"),
            _python_cmd("import sys; print('second'); sys.exit(7)"),
        ],
        cwd=tmp_path,
        timeout=5,
        mode="sequential",
    )

    assert result["success"] is False
    assert result["mode"] == "batch"
    assert result["execution_mode"] == "sequential"
    assert result["max_concurrency"] == 1
    assert result["command_count"] == 2
    assert result["completed_count"] == 2
    assert result["failed_count"] == 1
    assert [item["index"] for item in result["results"]] == [0, 1]
    assert result["results"][0]["stdout"].strip() == "first"
    assert result["results"][1]["stdout"].strip() == "second"
    assert result["results"][1]["exit_code"] == 7


def test_run_commands_parallel_preserves_input_order(tmp_path: Path) -> None:
    result = run_commands(
        commands=[
            _python_cmd("print('alpha')"),
            _python_cmd("print('beta')"),
            _python_cmd("print('gamma')"),
        ],
        cwd=tmp_path,
        timeout=5,
        mode="parallel",
        max_concurrency=2,
    )

    assert result["success"] is True
    assert result["execution_mode"] == "parallel"
    assert result["max_concurrency"] == 2
    assert [item["index"] for item in result["results"]] == [0, 1, 2]
    assert [item["stdout"].strip() for item in result["results"]] == [
        "alpha",
        "beta",
        "gamma",
    ]


def test_run_commands_rejects_concurrency_above_hard_limit(tmp_path: Path) -> None:
    result = run_commands(
        commands=["echo hi"],
        cwd=tmp_path,
        timeout=5,
        mode="parallel",
        max_concurrency=MAX_COMMAND_BATCH_CONCURRENCY + 1,
    )

    assert result["success"] is False
    assert result["error"]["code"] == "invalid_arguments"
    assert str(MAX_COMMAND_BATCH_CONCURRENCY) in result["error"]["message"]


def test_run_commands_rejects_empty_command_items(tmp_path: Path) -> None:
    result = run_commands(
        commands=["echo ok", "   "],
        cwd=tmp_path,
        timeout=5,
        mode="sequential",
    )

    assert result["success"] is False
    assert result["error"]["code"] == "invalid_arguments"
    assert "non-empty" in result["error"]["message"]


def test_run_commands_rejects_timeout_above_limit_without_force(tmp_path: Path) -> None:
    result = run_commands(
        commands=["echo ok"],
        cwd=tmp_path,
        timeout=MAX_COMMAND_TIMEOUT_SECONDS + 1,
        mode="sequential",
    )

    assert result["success"] is False
    assert result["mode"] == "batch"
    assert result["error"]["code"] == "timeout_exceeds_limit"
    assert result["results"] == []
