from __future__ import annotations

import subprocess
from pathlib import Path

from chatgpt_web_oauth_mcp.executors import ExecutorRegistry
from chatgpt_web_oauth_mcp.taskboard import TaskBoardStore
from chatgpt_web_oauth_mcp.tasks import TaskStore


def _registry(tmp_path: Path, command: str) -> ExecutorRegistry:
    store = TaskStore(tmp_path / "state")
    return ExecutorRegistry(
        store=store,
        codex_command=command,
        claude_command="python3 -c \"print('claude')\"",
    )


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=True)


def _init_repo(repo: Path) -> None:
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "taskboard@example.test")
    _git(repo, "config", "user.name", "TaskBoard Test")
    (repo / "file.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-m", "base")


class CapturingRegistry:
    def __init__(self) -> None:
        self.submissions: list[dict[str, object]] = []
        self.meta: dict[str, dict[str, object]] = {}

    def submit(
        self,
        *,
        task: str | None,
        goal: str | None = None,
        executor: str,
        cwd: Path,
        timeout: int,
        context_files: list[str] | None = None,
        acceptance_criteria: list[str] | None = None,
        verification_commands: list[str] | None = None,
        commit_mode: str = "allowed",
        output_schema: dict[str, object] | None = None,
        parse_structured_output: bool = True,
    ) -> dict[str, object]:
        task_id = f"delegate-{len(self.submissions) + 1}"
        self.submissions.append(
            {
                "task": task,
                "goal": goal,
                "executor": executor,
                "cwd": cwd,
                "timeout": timeout,
                "context_files": context_files or [],
                "acceptance_criteria": acceptance_criteria or [],
                "verification_commands": verification_commands or [],
                "commit_mode": commit_mode,
                "output_schema": output_schema,
                "parse_structured_output": parse_structured_output,
            }
        )
        self.meta[task_id] = {
            "task_id": task_id,
            "status": "queued",
            "summary": "",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "completed": False,
        }
        return {"task_id": task_id, "executor": executor, "status": "queued"}

    def get(self, task_id: str) -> dict[str, object]:
        return dict(self.meta[task_id])

    def cancel(self, task_id: str) -> dict[str, object]:
        self.meta[task_id]["status"] = "cancelled"
        self.meta[task_id]["completed"] = True
        return {"task_id": task_id, "status": "cancelled", "cancelled": True}


def test_taskboard_create_persists_pending_tasks_without_delegating(tmp_path: Path) -> None:
    store = TaskBoardStore(tmp_path / "state")
    original_request = "Original user request with the full board context."

    created = store.create(
        title="Phase 1",
        original_request=original_request,
        tasks=[
            {
                "title": "Persist board",
                "task": "Create a durable board record",
                "context_files": ["README.md"],
                "acceptance_criteria": ["Board survives reload"],
            }
        ],
        cwd=str(tmp_path),
        workspace_root=tmp_path,
    )

    board_id = str(created["board"]["board_id"])
    reloaded_store = TaskBoardStore(tmp_path / "state")
    reloaded = reloaded_store.get(board_id)
    task_detail = reloaded_store.get_task(
        board_id=board_id,
        task_id=str(created["tasks"][0]["task_id"]),
        registry=CapturingRegistry(),
        refresh=False,
    )

    assert created["board"]["status"] == "draft"
    assert "original_request" not in created["board"]
    assert created["board_detail"]["original_request"] == original_request
    assert created["board"]["counts"]["pending"] == 1
    assert created["tasks"][0]["delegate_task_id"] is None
    assert reloaded["original_request"] == original_request
    assert reloaded["tasks"][0]["title"] == "Persist board"
    assert reloaded["tasks"][0]["context_files"] == ["README.md"]
    assert task_detail["board_detail"]["original_request"] == original_request


def test_taskboard_delegate_respects_max_parallel_and_waits_for_change(tmp_path: Path) -> None:
    board_store = TaskBoardStore(tmp_path / "state")
    registry = _registry(
        tmp_path,
        "python3 -c \"import time; time.sleep(0.15); print('done')\"",
    )
    created = board_store.create(
        title="Limited wave",
        tasks=[
            {"title": "First", "task": "Run first"},
            {"title": "Second", "task": "Run second"},
        ],
        cwd=str(tmp_path),
        workspace_root=tmp_path,
        worktree_mode="none",
        max_parallel=1,
    )
    board_id = str(created["board"]["board_id"])

    delegated = board_store.delegate(
        board_id=board_id,
        registry=registry,
        timeout=5,
    )

    assert delegated["submitted_task_ids"] and len(delegated["submitted_task_ids"]) == 1
    assert delegated["board"]["counts"]["pending"] == 1
    waited = board_store.wait(
        board_id=board_id,
        registry=registry,
        timeout=2,
        poll_interval=0.05,
    )

    assert waited["timed_out"] is False
    assert waited["return_reason"] == "status_change"
    assert waited["changed_tasks"][0]["status"] in {"running", "succeeded"}
    assert waited["board"]["status"] == "running"
    assert waited["board"]["counts"]["pending"] == 1


def test_taskboard_wait_any_done_timeout_does_not_cancel_and_summary_propagates(tmp_path: Path) -> None:
    board_store = TaskBoardStore(tmp_path / "state")
    registry = _registry(
        tmp_path,
        "python3 -c \"import time; time.sleep(0.25); print('delegated summary')\"",
    )
    created = board_store.create(
        title="Summary propagation",
        tasks=[{"title": "Finish", "task": "Emit a summary"}],
        cwd=str(tmp_path),
        workspace_root=tmp_path,
        worktree_mode="none",
    )
    board_id = str(created["board"]["board_id"])
    delegated = board_store.delegate(board_id=board_id, registry=registry, timeout=5)
    delegate_task_id = board_store.get(board_id)["tasks"][0]["delegate_task_id"]

    timed_out = board_store.wait(
        board_id=board_id,
        registry=registry,
        timeout=0.01,
        poll_interval=0.05,
        return_on="any_done",
    )
    assert timed_out["timed_out"] is True
    assert timed_out["return_reason"] == "timeout"
    assert registry.get(str(delegate_task_id))["status"] != "cancelled"

    waited = board_store.wait(
        board_id=board_id,
        registry=registry,
        timeout=2,
        poll_interval=0.05,
        return_on="any_done",
    )
    status = board_store.status(board_id=board_id, registry=registry)
    detail = board_store.get_task(
        board_id=board_id,
        task_id=str(delegated["submitted_task_ids"][0]),
        registry=registry,
        include_done_report=True,
    )

    assert waited["timed_out"] is False
    assert waited["return_reason"] == "any_done"
    assert waited["changed_tasks"][0]["status"] == "succeeded"
    assert waited["tasks"][0]["summary"] == "delegated summary"
    assert status["tasks"][0]["summary"] == "delegated summary"
    assert detail["task"]["summary"] == "delegated summary"
    assert detail["done_report"]["summary"] == "delegated summary"


def test_taskboard_wait_all_done_uses_terminal_board_status(tmp_path: Path) -> None:
    board_store = TaskBoardStore(tmp_path / "state")
    registry = _registry(tmp_path, "python3 -c \"print('all done')\"")
    created = board_store.create(
        title="All done",
        tasks=[
            {"title": "First", "task": "Run first"},
            {"title": "Second", "task": "Run second"},
        ],
        cwd=str(tmp_path),
        workspace_root=tmp_path,
        worktree_mode="none",
        max_parallel=2,
    )
    board_id = str(created["board"]["board_id"])
    board_store.delegate(board_id=board_id, registry=registry, timeout=5)

    waited = board_store.wait(
        board_id=board_id,
        registry=registry,
        timeout=2,
        poll_interval=0.05,
        return_on="all_done",
    )

    assert waited["timed_out"] is False
    assert waited["return_reason"] == "all_done"
    assert waited["board"]["status"] == "completed"
    assert waited["board"]["counts"]["succeeded"] == 2


def test_taskboard_worktree_result_collection_uses_per_task_cwd(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    board_store = TaskBoardStore(tmp_path / "state")
    registry = _registry(
        tmp_path,
        "python3 -c \"from pathlib import Path; Path('file.txt').write_text('changed\\\\n', encoding='utf-8')\"",
    )
    created = board_store.create(
        title="Worktree board",
        tasks=[{"title": "Modify tracked file", "task": "Change file.txt"}],
        cwd=str(repo),
        workspace_root=tmp_path,
        worktree_mode="per_task",
    )
    board_id = str(created["board"]["board_id"])

    delegated = board_store.delegate(board_id=board_id, registry=registry, timeout=5)
    task_id = delegated["submitted_task_ids"][0]
    board_store.wait(board_id=board_id, registry=registry, timeout=2, poll_interval=0.05)
    collected = board_store.collect_results(board_id=board_id, registry=registry)
    result = collected["results"][0]

    assert result["task_id"] == task_id
    assert result["branch_name"].startswith("taskboard/")
    assert result["base_sha"]
    assert result["head_sha"] == result["base_sha"]
    assert result["commit_sha"] is None
    assert Path(result["worktree_path"]).exists()
    assert (Path(result["worktree_path"]) / "file.txt").read_text(encoding="utf-8") == "changed\n"
    assert result["changed_files"] == ["file.txt"]
    assert "file.txt" in result["diff_summary"]


def test_taskboard_delegate_prompt_includes_board_worktree_and_safety_context(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    board_store = TaskBoardStore(tmp_path / "state")
    registry = CapturingRegistry()
    original_request = "Fix the reviewed Phase 1 TaskBoard gaps."
    created = board_store.create(
        title="Prompt board",
        original_request=original_request,
        tasks=[
            {
                "title": "Patch prompt",
                "task": "Update the delegated prompt with board context.",
                "notes": "Keep it concise.",
            }
        ],
        cwd=str(repo),
        workspace_root=tmp_path,
        worktree_mode="per_task",
        base_ref="HEAD",
    )
    board_id = str(created["board"]["board_id"])

    delegated = board_store.delegate(board_id=board_id, registry=registry, timeout=5)
    task_id = delegated["submitted_task_ids"][0]
    detail = board_store.get_task(
        board_id=board_id,
        task_id=task_id,
        registry=registry,
        refresh=False,
        include_prompt=True,
    )
    task = detail["task"]
    prompt = str(registry.submissions[0]["task"])

    assert registry.submissions[0]["cwd"] == Path(str(task["worktree_path"]))
    assert f"board_id: {board_id}" in prompt
    assert "title: Prompt board" in prompt
    assert original_request in prompt
    assert f"task_id: {task_id}" in prompt
    assert "title: Patch prompt" in prompt
    assert "Update the delegated prompt with board context." in prompt
    assert str(task["worktree_path"]) in prompt
    assert str(task["branch_name"]) in prompt
    assert str(task["base_sha"]) in prompt
    assert "Stay in the assigned cwd" in prompt
    assert "Do not switch branches." in prompt
    assert "Do not edit the parent workspace" in prompt
    assert "Do not delete or prune worktrees." in prompt
    assert "Done report expectations:" in prompt
    assert detail["prompt"] == prompt


def test_taskboard_cancel_does_not_delete_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    board_store = TaskBoardStore(tmp_path / "state")
    registry = _registry(
        tmp_path,
        "python3 -c \"import time; time.sleep(2)\"",
    )
    created = board_store.create(
        title="Cancel board",
        tasks=[{"title": "Long task", "task": "Sleep"}],
        cwd=str(repo),
        workspace_root=tmp_path,
    )
    board_id = str(created["board"]["board_id"])
    delegated = board_store.delegate(board_id=board_id, registry=registry, timeout=10)
    task_id = delegated["submitted_task_ids"][0]
    task = board_store.get_task(board_id=board_id, task_id=task_id, registry=registry)["task"]
    worktree_path = Path(str(task["worktree_path"]))

    cancelled = board_store.cancel(board_id=board_id, registry=registry)

    assert cancelled["cancelled_task_ids"] == [task_id]
    assert worktree_path.exists()
    assert cancelled["board"]["status"] == "cancelled"
