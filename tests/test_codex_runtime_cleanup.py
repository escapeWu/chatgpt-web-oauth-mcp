from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading

import pytest

from chatgpt_web_oauth_mcp.codex_runtime.errors import AppServerProtocolError, BindingStoreError
from test_codex_runtime import FakeAdapter, _manager


def test_release_on_close_preserves_binding_and_resume(tmp_path: Path) -> None:
    adapter = FakeAdapter()
    manager = _manager(tmp_path, adapter)
    opened = manager.open_runtime(cwd=tmp_path, sandbox="read-only", name="worker")
    closed = manager.close_runtime(opened["runtime_id"], release_resources=True)
    assert closed["resources_released"] is True
    assert not adapter.live_threads
    resumed = manager.resume_runtime(runtime_id=opened["runtime_id"], thread_id=None, cwd=None, sandbox=None)
    assert resumed["runtime_id"] == opened["runtime_id"]
    assert resumed["resume_mode"] == "resumed"


def test_ttl_and_lru_release_actual_threads_and_retry_failure(tmp_path: Path, monkeypatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr("chatgpt_web_oauth_mcp.codex_runtime.manager.time.time", lambda: clock[0])
    adapter = FakeAdapter()
    manager = _manager(tmp_path, adapter, max_runtimes=1, idle_ttl_seconds=10)
    old = manager.open_runtime(cwd=tmp_path, sandbox="read-only", name="old")
    manager.close_runtime(old["runtime_id"])
    new = manager.open_runtime(cwd=tmp_path, sandbox="read-only", name="new")
    assert adapter.live_threads == {new["thread_id"]}
    real_release = adapter.release_thread
    def fail(**kwargs):
        raise AppServerProtocolError("temporary shutdown failure")
    monkeypatch.setattr(adapter, "release_thread", fail)
    clock[0] += 11
    info = manager.info()
    assert info["runtime_count"] == 1
    assert info["gc"]["cleanup_pending_count"] == 1
    monkeypatch.setattr(adapter, "release_thread", real_release)
    assert manager.info()["runtime_count"] == 0
    assert not adapter.live_threads


def test_concurrent_acquire_creates_one_runtime(tmp_path: Path) -> None:
    adapter = FakeAdapter()
    manager = _manager(tmp_path, adapter)
    barrier = threading.Barrier(8)
    def acquire(_):
        barrier.wait(timeout=5)
        return manager.acquire_runtime(cwd=tmp_path, sandbox="read-only", name="worker")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(acquire, range(8)))
    assert len({item["runtime_id"] for item in results}) == 1
    assert adapter.thread_number == 1


def test_active_and_unfinished_upstream_requests_block_release(tmp_path: Path, monkeypatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr("chatgpt_web_oauth_mcp.codex_runtime.manager.time.time", lambda: clock[0])
    adapter = FakeAdapter()
    manager = _manager(tmp_path, adapter, idle_ttl_seconds=10)
    runtime = manager.open_runtime(cwd=tmp_path, sandbox="read-only", name="worker")
    with manager._request_slot(runtime_id=runtime["runtime_id"], thread_id=runtime["thread_id"]):
        clock[0] += 11
        assert manager.info()["runtime_count"] == 1
        with pytest.raises(Exception) as busy:
            manager.close_runtime(runtime["runtime_id"], release_resources=True)
        assert busy.value.code == "runtime_busy"
    monkeypatch.setattr(adapter, "has_pending_operations", lambda _: True)
    clock[0] += 11
    assert manager.info()["runtime_count"] == 1
    monkeypatch.setattr(adapter, "has_pending_operations", lambda _: False)
    assert manager.info()["runtime_count"] == 0
    assert not adapter.live_threads


def test_failed_binding_save_compensates_created_thread(tmp_path: Path, monkeypatch) -> None:
    adapter = FakeAdapter()
    manager = _manager(tmp_path, adapter)
    def fail(_):
        raise BindingStoreError("disk unavailable")
    monkeypatch.setattr(manager._store, "save", fail)
    with pytest.raises(BindingStoreError) as failure:
        manager.open_runtime(cwd=tmp_path, sandbox="read-only", name="worker")
    assert failure.value.side_effects["resources_released"] is True
    assert not adapter.live_threads
