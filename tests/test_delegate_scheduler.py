from __future__ import annotations

import threading
import time
from pathlib import Path

from chatgpt_web_oauth_mcp.delegate_models import DelegateTask
from chatgpt_web_oauth_mcp.executors import ExecutorRegistry


def _install_timed_runner(
    registry: ExecutorRegistry,
    monkeypatch,
    delays: dict[str, float],
    failures: set[str] | None = None,
) -> tuple[dict[str, tuple[float, float]], threading.Lock]:
    intervals: dict[str, tuple[float, float]] = {}
    lock = threading.Lock()
    failures = failures or set()

    def run(*, delegate_task: DelegateTask) -> dict[str, object]:
        name = delegate_task.task or delegate_task.goal or delegate_task.delegate_id
        started = time.monotonic()
        time.sleep(delays.get(name, 0.05))
        finished = time.monotonic()
        with lock:
            intervals[name] = (started, finished)
        status = "failed" if name in failures else "succeeded"
        return {
            "success": status == "succeeded",
            "status": status,
            "completed": True,
            "in_progress": False,
            "delegate_id": delegate_task.delegate_id,
            "kind": delegate_task.kind,
            "group_id": delegate_task.group_id,
        }

    monkeypatch.setattr(registry, "_start_codex_delegate_impl", run)
    return intervals, lock


def _wait_task(registry: ExecutorRegistry, delegate_id: str, timeout: float = 3) -> DelegateTask:
    task = registry.scheduler.get_task(delegate_id)
    assert task is not None
    assert task.completed_event.wait(timeout=timeout)
    return task


def test_same_project_fair_reader_writer_scheduling(tmp_path: Path, monkeypatch) -> None:
    registry = ExecutorRegistry(
        codex_command="true",
        max_explore_per_project=4,
        allow_unsafe_explore_command=True,
    )
    intervals, _ = _install_timed_runner(
        registry,
        monkeypatch,
        {"reader-1": 0.15, "reader-2": 0.15, "writer": 0.05, "late-reader": 0.05},
    )

    reader_1 = registry.run_codex(task="reader-1", kind="explore", cwd=tmp_path, wait_seconds=0)
    reader_2 = registry.run_codex(task="reader-2", kind="explore", cwd=tmp_path, wait_seconds=0)
    writer = registry.run_codex(task="writer", kind="code", cwd=tmp_path, wait_seconds=0)
    late_reader = registry.run_codex(task="late-reader", kind="explore", cwd=tmp_path, wait_seconds=0)

    assert writer["status"] == "queued"
    assert late_reader["status"] == "queued"
    for result in (reader_1, reader_2, writer, late_reader):
        _wait_task(registry, str(result["delegate_id"]))

    assert intervals["reader-1"][0] < intervals["reader-2"][1]
    assert intervals["reader-2"][0] < intervals["reader-1"][1]
    assert intervals["writer"][0] >= max(intervals["reader-1"][1], intervals["reader-2"][1])
    assert intervals["late-reader"][0] >= intervals["writer"][1]


def test_code_tasks_in_different_projects_overlap(tmp_path: Path, monkeypatch) -> None:
    project_a = tmp_path / "a"
    project_b = tmp_path / "b"
    project_a.mkdir()
    project_b.mkdir()
    registry = ExecutorRegistry(
        codex_command="true",
        max_code_global=2,
        allow_unsafe_explore_command=True,
    )
    intervals, _ = _install_timed_runner(
        registry,
        monkeypatch,
        {"code-a": 0.15, "code-b": 0.15},
    )

    results: list[dict[str, object]] = []
    threads = [
        threading.Thread(
            target=lambda name=name, cwd=cwd: results.append(
                registry.run_codex(task=name, kind="code", cwd=cwd, wait_seconds=1)
            )
        )
        for name, cwd in (("code-a", project_a), ("code-b", project_b))
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert len(results) == 2
    assert intervals["code-a"][0] < intervals["code-b"][1]
    assert intervals["code-b"][0] < intervals["code-a"][1]


def test_code_dependency_waits_for_complete_explore_group(tmp_path: Path, monkeypatch) -> None:
    registry = ExecutorRegistry(
        codex_command="true",
        max_explore_per_project=2,
        allow_unsafe_explore_command=True,
    )
    intervals, _ = _install_timed_runner(
        registry,
        monkeypatch,
        {"scan-a": 0.05, "scan-b": 0.15, "dependent-code": 0.01},
    )

    group = registry.run_codex_batch(
        tasks=[{"task": "scan-a"}, {"task": "scan-b"}],
        cwd=tmp_path,
        wait_seconds=0,
    )
    code = registry.run_codex(
        task="dependent-code",
        kind="code",
        cwd=tmp_path,
        depends_on_group_ids=[str(group["group_id"])],
        wait_seconds=0,
    )

    assert code["status"] == "queued"
    _wait_task(registry, str(code["delegate_id"]))
    assert intervals["dependent-code"][0] >= max(
        intervals["scan-a"][1], intervals["scan-b"][1]
    )
