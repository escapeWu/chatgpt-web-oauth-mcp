import shlex
import sys
from pathlib import Path

from chatgpt_web_oauth_mcp.shell import (
    MAX_COMMAND_BATCH_CONCURRENCY,
    MAX_COMMAND_TIMEOUT_SECONDS,
    TIMEOUT_EXIT_CODE,
    run_command,
    run_commands,
)


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
