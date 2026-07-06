from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import subprocess
from pathlib import Path
from typing import Literal


# Sentinel exit code used when the process did not produce a real return code
# (e.g. it timed out or never started). Keeping this as an int (not None) so
# callers can always do numeric comparisons like `exit_code == 0` without
# special-casing `None`.
TIMEOUT_EXIT_CODE = -1
MAX_COMMAND_BATCH_CONCURRENCY = 3
MAX_COMMAND_BATCH_SIZE = 20


def run_command(*, command: str, cwd: Path, timeout: int) -> dict[str, object]:
    if not cwd.exists():
        return {
            "success": False,
            "error": {
                "code": "cwd_not_found",
                "message": f"Working directory not found: {cwd}",
            },
            "cwd": str(cwd),
            "command": command,
            "exit_code": TIMEOUT_EXIT_CODE,
            "stdout": "",
            "stderr": "",
            "timed_out": False,
        }
    if not cwd.is_dir():
        return {
            "success": False,
            "error": {
                "code": "cwd_not_directory",
                "message": f"Working directory is not a directory: {cwd}",
            },
            "cwd": str(cwd),
            "command": command,
            "exit_code": TIMEOUT_EXIT_CODE,
            "stdout": "",
            "stderr": "",
            "timed_out": False,
        }

    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            shell=True,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        return {
            "success": completed.returncode == 0,
            "command": command,
            "cwd": str(cwd),
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "success": False,
            "command": command,
            "cwd": str(cwd),
            "exit_code": TIMEOUT_EXIT_CODE,
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "",
            "timed_out": True,
            "timeout": timeout,
            "error": {
                "code": "timed_out",
                "message": (
                    f"Command exceeded the {timeout}s timeout. "
                    "Retry with a larger `timeout` argument, or use "
                    "`delegate_task` for a serialized Codex handoff."
                ),
            },
            "hint": "increase_timeout_or_delegate",
        }


def _invalid_batch_arguments(message: str) -> dict[str, object]:
    return {
        "success": False,
        "mode": "batch",
        "error": {
            "code": "invalid_arguments",
            "message": message,
        },
        "results": [],
    }


def _with_batch_index(index: int, result: dict[str, object]) -> dict[str, object]:
    payload = dict(result)
    payload["index"] = index
    return payload


def run_commands(
    *,
    commands: list[str],
    cwd: Path,
    timeout: int,
    mode: Literal["sequential", "parallel"] = "sequential",
    max_concurrency: int = MAX_COMMAND_BATCH_CONCURRENCY,
) -> dict[str, object]:
    if not commands:
        return _invalid_batch_arguments("Provide at least one command.")
    if any(not command.strip() for command in commands):
        return _invalid_batch_arguments("Batch commands must be non-empty strings.")
    if len(commands) > MAX_COMMAND_BATCH_SIZE:
        return _invalid_batch_arguments(
            f"At most {MAX_COMMAND_BATCH_SIZE} commands may be executed in one batch."
        )
    if mode not in {"sequential", "parallel"}:
        return _invalid_batch_arguments("mode must be one of: sequential, parallel.")
    if max_concurrency < 1 or max_concurrency > MAX_COMMAND_BATCH_CONCURRENCY:
        return _invalid_batch_arguments(
            f"max_concurrency must be between 1 and {MAX_COMMAND_BATCH_CONCURRENCY}."
        )

    if mode == "sequential":
        results = [
            _with_batch_index(
                index,
                run_command(command=command, cwd=cwd, timeout=timeout),
            )
            for index, command in enumerate(commands)
        ]
        effective_concurrency = 1
    else:
        effective_concurrency = min(max_concurrency, len(commands))
        ordered_results: list[dict[str, object] | None] = [None] * len(commands)
        with ThreadPoolExecutor(max_workers=effective_concurrency) as executor:
            futures = {
                executor.submit(run_command, command=command, cwd=cwd, timeout=timeout): index
                for index, command in enumerate(commands)
            }
            for future in as_completed(futures):
                index = futures[future]
                ordered_results[index] = _with_batch_index(index, future.result())
        results = [item for item in ordered_results if item is not None]

    failed_count = sum(1 for item in results if not item.get("success"))
    timed_out_count = sum(1 for item in results if item.get("timed_out"))
    return {
        "success": failed_count == 0,
        "mode": "batch",
        "execution_mode": mode,
        "command_count": len(commands),
        "completed_count": len(results),
        "failed_count": failed_count,
        "timed_out_count": timed_out_count,
        "max_concurrency": effective_concurrency,
        "results": results,
    }
