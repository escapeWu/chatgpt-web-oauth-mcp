from __future__ import annotations

import time
from pathlib import Path

from chatgpt_web_oauth_mcp.delegate_models import DelegateTask
from chatgpt_web_oauth_mcp.executors import ExecutorRegistry


def test_batch_waits_all_and_aggregates_failure(tmp_path: Path, monkeypatch) -> None:
    registry = ExecutorRegistry(
        codex_command="true",
        max_explore_per_project=3,
        allow_unsafe_explore_command=True,
    )

    def run(*, delegate_task: DelegateTask) -> dict[str, object]:
        delay = {"fast": 0.03, "failed": 0.05, "slow": 0.15}[delegate_task.task or ""]
        time.sleep(delay)
        status = "failed" if delegate_task.task == "failed" else "succeeded"
        return {
            "success": status == "succeeded",
            "status": status,
            "completed": True,
            "in_progress": False,
            "delegate_id": delegate_task.delegate_id,
        }

    monkeypatch.setattr(registry, "_start_codex_delegate_impl", run)
    started = time.monotonic()
    result = registry.run_codex_batch(
        tasks=[{"task": "fast"}, {"task": "failed"}, {"task": "slow"}],
        cwd=tmp_path,
        wait_seconds=1,
    )

    assert time.monotonic() - started >= 0.14
    assert result["status"] == "failed"
    assert result["completed"] is True
    assert result["results_ready"] is True
    assert result["counts"] == {
        "total": 3,
        "queued": 0,
        "running": 0,
        "succeeded": 2,
        "failed": 1,
        "cancelled": 0,
        "timed_out": 0,
    }
    assert len(result["children"]) == 3
    assert all("result" in child for child in result["children"])


def test_group_watch_returns_on_child_count_change_before_fan_in(tmp_path: Path, monkeypatch) -> None:
    registry = ExecutorRegistry(
        codex_command="true",
        max_explore_per_project=2,
        allow_unsafe_explore_command=True,
    )

    def run(*, delegate_task: DelegateTask) -> dict[str, object]:
        time.sleep(0.05 if delegate_task.task == "fast" else 0.3)
        return {
            "success": True,
            "status": "succeeded",
            "completed": True,
            "in_progress": False,
            "delegate_id": delegate_task.delegate_id,
        }

    monkeypatch.setattr(registry, "_start_codex_delegate_impl", run)
    submitted = registry.run_codex_batch(
        tasks=[{"task": "fast"}, {"task": "slow"}],
        cwd=tmp_path,
        wait_seconds=0,
    )

    watched = registry.delegate_status(
        group_id=str(submitted["group_id"]),
        watch_seconds=1,
        poll_seconds=0.02,
    )

    assert watched["watch"]["status_changed"] is True
    assert watched["group"]["completed"] is False
    assert watched["group"]["counts"]["succeeded"] == 1
    final = registry.delegate_status(
        group_id=str(submitted["group_id"]),
        watch_seconds=1,
        poll_seconds=0.02,
    )
    assert final["group"]["completed"] is True
    assert final["group"]["status"] == "succeeded"
