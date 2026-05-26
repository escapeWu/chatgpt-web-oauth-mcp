from __future__ import annotations

import json
import subprocess
from pathlib import Path

from chatgpt_web_oauth_mcp.executors import ExecutorRegistry
from chatgpt_web_oauth_mcp.notifiers import (
    TelegramTaskBoardNotifier,
    build_telegram_notifier,
    format_taskboard_terminal_message,
)
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


class RecordingNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def notify_task_terminal(self, *, board: dict[str, object], task: dict[str, object]) -> None:
        self.messages.append(format_taskboard_terminal_message(board=board, task=task))


class FailingNotifier:
    def notify_task_terminal(self, *, board: dict[str, object], task: dict[str, object]) -> None:
        raise RuntimeError("secret-token must not leak")


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
    waited = board_store.wait(
        board_id=board_id,
        registry=registry,
        timeout=2,
        poll_interval=0.05,
        return_on="all_done",
    )
    assert waited["timed_out"] is False
    assert waited["return_reason"] == "all_done"

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


def test_taskboard_refresh_notifies_once_on_terminal_transition(tmp_path: Path) -> None:
    notifier = RecordingNotifier()
    board_store = TaskBoardStore(tmp_path / "state", notifier=notifier)
    registry = CapturingRegistry()
    created = board_store.create(
        title="Notify board",
        tasks=[
            {"title": "Alpha", "task": "Finish alpha"},
            {"title": "Beta", "task": "Keep beta queued"},
        ],
        cwd=str(tmp_path),
        workspace_root=tmp_path,
        worktree_mode="none",
        max_parallel=2,
    )
    board_id = str(created["board"]["board_id"])
    board_store.delegate(board_id=board_id, registry=registry, timeout=5)
    board = board_store.get(board_id)
    first = board["tasks"][0]
    second = board["tasks"][1]
    registry.meta[str(first["delegate_task_id"])].update(
        {
            "status": "succeeded",
            "summary": "Alpha done.",
            "updated_at": "2026-01-01T00:00:01+00:00",
            "completed": True,
        }
    )

    refreshed = board_store.refresh(board_id=board_id, registry=registry, save=True)
    board_store.refresh(board_id=board_id, registry=registry, save=True)

    assert refreshed["changed_tasks"][0]["status"] == "succeeded"
    assert len(notifier.messages) == 1
    message = notifier.messages[0]
    assert "TaskBoard task succeeded" in message
    assert f"Current task: [x] Alpha ({first['task_id']}) - succeeded" in message
    assert f"Board: Notify board ({board_id})" in message
    assert f"[x] Alpha ({first['task_id']}) - succeeded" in message
    assert f"[ ] Beta ({second['task_id']}) - queued" in message


def test_taskboard_refresh_swallow_notify_failure_and_records_safe_event(tmp_path: Path) -> None:
    board_store = TaskBoardStore(tmp_path / "state", notifier=FailingNotifier())
    registry = CapturingRegistry()
    created = board_store.create(
        title="Failure board",
        tasks=[{"title": "Break", "task": "Fail cleanly"}],
        cwd=str(tmp_path),
        workspace_root=tmp_path,
        worktree_mode="none",
    )
    board_id = str(created["board"]["board_id"])
    board_store.delegate(board_id=board_id, registry=registry, timeout=5)
    board = board_store.get(board_id)
    task = board["tasks"][0]
    registry.meta[str(task["delegate_task_id"])].update(
        {
            "status": "failed",
            "updated_at": "2026-01-01T00:00:01+00:00",
            "completed": True,
        }
    )

    refreshed = board_store.refresh(board_id=board_id, registry=registry, save=True)
    events = refreshed["board"]["events"]
    failure_events = [event for event in events if event.get("event") == "telegram_notify_failed"]

    assert refreshed["changed_tasks"][0]["status"] == "failed"
    assert refreshed["board"]["status"] == "failed"
    assert len(failure_events) == 1
    assert failure_events[0]["task_id"] == task["task_id"]
    assert "RuntimeError" in str(failure_events[0].get("message"))
    assert "secret-token" not in json.dumps(failure_events)


def test_format_taskboard_terminal_message_uses_status_checkboxes() -> None:
    board = {
        "board_id": "board-1",
        "title": "Checklist board",
        "tasks": [
            {"task_id": "ok", "title": "Done", "status": "succeeded"},
            {"task_id": "todo", "title": "Todo", "status": "pending"},
            {"task_id": "queued", "title": "Queued", "status": "queued"},
            {"task_id": "active", "title": "Active", "status": "running"},
            {"task_id": "bad", "title": "Bad", "status": "failed"},
            {"task_id": "stop", "title": "Stop", "status": "cancelled"},
        ],
    }
    message = format_taskboard_terminal_message(board=board, task=board["tasks"][4])

    assert "Current task: [!] Bad (bad) - failed" in message
    assert "Board: Checklist board (board-1)" in message
    assert "[x] Done (ok) - succeeded" in message
    assert "[ ] Todo (todo) - pending" in message
    assert "[ ] Queued (queued) - queued" in message
    assert "[ ] Active (active) - running" in message
    assert "[!] Bad (bad) - failed" in message
    assert "[-] Stop (stop) - cancelled" in message


def test_build_telegram_notifier_requires_token_and_receiver() -> None:
    assert build_telegram_notifier(bot_token="", receiver_id="chat") is None
    assert build_telegram_notifier(bot_token="token", receiver_id="") is None
    assert build_telegram_notifier(bot_token="token", receiver_id="chat") is not None


def test_telegram_notifier_posts_send_message_json_without_markdown(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def read(self) -> bytes:
            return b'{"ok":true}'

        def close(self) -> None:
            captured["closed"] = True

    def fake_urlopen(request, *, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(
        "chatgpt_web_oauth_mcp.notifiers.urllib.request.urlopen",
        fake_urlopen,
    )
    notifier = TelegramTaskBoardNotifier(
        bot_token="unit-test-token",
        receiver_id="unit-test-chat",
        timeout_seconds=1.25,
    )
    board = {
        "board_id": "board-1",
        "title": "Notify",
        "tasks": [{"task_id": "task-1", "title": "Done", "status": "succeeded"}],
    }

    notifier.notify_task_terminal(board=board, task=board["tasks"][0])

    request = captured["request"]
    body = json.loads(request.data.decode("utf-8"))
    assert request.full_url == "https://api.telegram.org/botunit-test-token/sendMessage"
    assert request.get_method() == "POST"
    assert request.get_header("Content-type") == "application/json"
    assert captured["timeout"] == 1.25
    assert captured["closed"] is True
    assert body["chat_id"] == "unit-test-chat"
    assert body["disable_web_page_preview"] is True
    assert "parse_mode" not in body
    assert "TaskBoard checklist:" in body["text"]


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
