from __future__ import annotations

import copy
import json
import shutil
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from . import worktrees
from .notifiers import TaskBoardNotifier


TASKBOARD_STATUSES = {"draft", "running", "completed", "failed", "cancelled"}
SUBTASK_STATUSES = {"pending", "queued", "running", "succeeded", "failed", "cancelled"}
TERMINAL_SUBTASK_STATUSES = {"succeeded", "failed", "cancelled"}
ACTIVE_SUBTASK_STATUSES = {"queued", "running"}
ALLOWED_EXECUTORS = {"auto", "codex", "claude-code"}
ALLOWED_WORKTREE_MODES = {"per_task", "none"}

DEFAULT_EXECUTOR = "auto"
DEFAULT_MAX_PARALLEL = 2
DEFAULT_WORKTREE_MODE = "per_task"
DEFAULT_BASE_REF = "HEAD"
DEFAULT_BRANCH_PREFIX = "taskboard"
DEFAULT_STDIO_TAIL_CHARS = 4000
DEFAULT_DIFF_MAX_BYTES = 20000
DEFAULT_EVENT_LIMIT = 10


class TaskBoardError(ValueError):
    def __init__(self, code: str, message: str, **extra: object) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "success": False,
            "error": {
                "code": self.code,
                "message": self.message,
            },
        }
        payload.update(self.extra)
        return payload


class DelegateRegistry(Protocol):
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
    ) -> dict[str, object]: ...

    def get(self, task_id: str) -> dict[str, object]: ...

    def cancel(self, task_id: str) -> dict[str, object]: ...


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _coerce_string(value: object | None, *, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _string_list(value: object | None, *, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise TaskBoardError("invalid_task_spec", f"{field} must be a list of strings.")
    return [str(item) for item in value]


def _resolve_cwd(raw_cwd: object | None, *, default_cwd: str, workspace_root: Path) -> str:
    if raw_cwd in {None, ""}:
        return default_cwd
    raw = Path(str(raw_cwd)).expanduser()
    if raw.is_absolute():
        return str(raw.resolve(strict=False))
    return str((workspace_root / raw).resolve(strict=False))


def _validate_executor(executor: str) -> str:
    if executor not in ALLOWED_EXECUTORS:
        raise TaskBoardError(
            "invalid_executor",
            f"executor must be one of: {', '.join(sorted(ALLOWED_EXECUTORS))}.",
            executor=executor,
        )
    return executor


def _validate_worktree_mode(worktree_mode: str) -> str:
    if worktree_mode not in ALLOWED_WORKTREE_MODES:
        raise TaskBoardError(
            "invalid_worktree_mode",
            f"worktree_mode must be one of: {', '.join(sorted(ALLOWED_WORKTREE_MODES))}.",
            worktree_mode=worktree_mode,
        )
    return worktree_mode


def _normalize_task_spec(
    raw: dict[str, object],
    *,
    default_cwd: str,
    default_executor: str,
    workspace_root: Path,
) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise TaskBoardError("invalid_task_spec", "Each task spec must be an object.")
    title = _coerce_string(raw.get("title")).strip()
    task = _coerce_string(raw.get("task")).strip()
    if not title:
        raise TaskBoardError("invalid_task_spec", "TaskSpec.title is required.")
    if not task:
        raise TaskBoardError("invalid_task_spec", "TaskSpec.task is required.")

    executor = _coerce_string(raw.get("executor"), default=default_executor).strip() or default_executor
    _validate_executor(executor)

    return {
        "task_id": uuid.uuid4().hex[:10],
        "title": title,
        "task": task,
        "cwd": _resolve_cwd(raw.get("cwd"), default_cwd=default_cwd, workspace_root=workspace_root),
        "executor": executor,
        "context_files": _string_list(raw.get("context_files"), field="context_files"),
        "acceptance_criteria": _string_list(raw.get("acceptance_criteria"), field="acceptance_criteria"),
        "verification_commands": _string_list(raw.get("verification_commands"), field="verification_commands"),
        "notes": _coerce_string(raw.get("notes")).strip() or None,
        "status": "pending",
        "delegate_task_id": None,
        "worktree_path": None,
        "branch_name": None,
        "base_ref": None,
        "base_sha": None,
        "head_sha": None,
        "commit_sha": None,
        "last_error": None,
        "summary": "",
        "created_at": _now(),
        "updated_at": _now(),
    }


def _delegate_status_to_subtask(status: object) -> str:
    value = str(status or "")
    if value in {"queued", "running", "succeeded", "failed", "cancelled"}:
        return value
    return "running"


def _counts(tasks: list[dict[str, object]]) -> dict[str, int]:
    counts = {status: 0 for status in sorted(SUBTASK_STATUSES)}
    for task in tasks:
        status = str(task.get("status") or "pending")
        if status in counts:
            counts[status] += 1
    return counts


def _compute_board_status(board: dict[str, object]) -> str:
    tasks = list(board.get("tasks") or [])
    if not tasks:
        return "draft"

    statuses = [str(task.get("status") or "pending") for task in tasks]
    has_pending = any(status == "pending" for status in statuses)
    has_active = any(status in ACTIVE_SUBTASK_STATUSES for status in statuses)
    has_failed = any(status == "failed" for status in statuses)
    has_cancelled = any(status == "cancelled" for status in statuses)
    all_succeeded = all(status == "succeeded" for status in statuses)
    all_terminal = all(status in TERMINAL_SUBTASK_STATUSES for status in statuses)

    if all_succeeded:
        return "completed"
    if all_terminal and has_failed:
        return "failed"
    if all_terminal and has_cancelled:
        return "cancelled"
    if has_active:
        return "running"
    if board.get("started_at") and (has_pending or not all_terminal):
        return "running"
    if has_failed:
        return "failed"
    if has_cancelled and not has_pending:
        return "cancelled"
    return "draft"


def _compact_task(task: dict[str, object]) -> dict[str, object]:
    return {
        "task_id": task.get("task_id"),
        "title": task.get("title"),
        "status": task.get("status"),
        "executor": task.get("executor"),
        "cwd": task.get("cwd"),
        "delegate_task_id": task.get("delegate_task_id"),
        "worktree_path": task.get("worktree_path"),
        "branch_name": task.get("branch_name"),
        "summary": task.get("summary") or "",
        "last_error": task.get("last_error"),
        "updated_at": task.get("updated_at"),
    }


def _compact_board(board: dict[str, object]) -> dict[str, object]:
    tasks = list(board.get("tasks") or [])
    return {
        "board_id": board.get("board_id"),
        "title": board.get("title"),
        "status": board.get("status"),
        "cwd": board.get("cwd"),
        "executor": board.get("executor"),
        "max_parallel": board.get("max_parallel"),
        "worktree_mode": board.get("worktree_mode"),
        "base_ref": board.get("base_ref"),
        "branch_prefix": board.get("branch_prefix"),
        "task_count": len(tasks),
        "counts": _counts(tasks),
        "created_at": board.get("created_at"),
        "updated_at": board.get("updated_at"),
    }


def _detailed_board(board: dict[str, object]) -> dict[str, object]:
    detail = _compact_board(board)
    detail.update(
        {
            "original_request": board.get("original_request") or "",
            "notes": board.get("notes"),
            "started_at": board.get("started_at"),
            "completed_at": board.get("completed_at"),
        }
    )
    return detail


def _task_prompt(board: dict[str, object], task: dict[str, object], *, assigned_cwd: str | None = None) -> str:
    worker_cwd = assigned_cwd or str(task.get("worktree_path") or task.get("cwd") or "")
    lines = [
        "TaskBoard delegated subtask",
        "",
        "Parent TaskBoard:",
        f"- board_id: {board.get('board_id')}",
        f"- title: {board.get('title')}",
    ]
    original_request = str(board.get("original_request") or "").strip()
    if original_request:
        lines.extend(["- original_request:", original_request])
    lines.extend(
        [
            "",
            "Subtask:",
            f"- task_id: {task.get('task_id')}",
            f"- title: {task.get('title')}",
            f"- assigned_cwd: {worker_cwd}",
        ]
    )
    for label in ("worktree_path", "branch_name", "base_ref", "base_sha"):
        value = task.get(label)
        if value:
            lines.append(f"- {label}: {value}")
    lines.extend(
        [
            "",
            "Subtask body:",
            str(task.get("task") or ""),
        ]
    )
    notes = str(task.get("notes") or "").strip()
    if notes:
        lines.extend(["", "Notes:", notes])
    lines.extend(
        [
            "",
            "Worktree safety rules:",
            f"- Stay in the assigned cwd: {worker_cwd}",
            "- Do not switch branches.",
            "- Do not edit the parent workspace or files outside the assigned cwd.",
            "- Do not delete or prune worktrees.",
            "- Keep changes scoped to this subtask.",
            "",
            "Done report expectations:",
            "- Summarize changed files, tests/commands run, remaining risks, and blockers.",
        ]
    )
    return "\n".join(lines).strip()


class TaskBoardStore:
    def __init__(self, root: Path, notifier: TaskBoardNotifier | None = None) -> None:
        self.root = root
        self.notifier = notifier
        self.board_root = self.root / "taskboards" / "boards"
        self.worktree_root = self.root / "taskboards" / "worktrees"
        self.board_root.mkdir(parents=True, exist_ok=True)
        self.worktree_root.mkdir(parents=True, exist_ok=True)
        try:
            (self.root / "taskboards").chmod(0o700)
            self.board_root.chmod(0o700)
            self.worktree_root.chmod(0o700)
        except OSError:
            pass
        self._lock = threading.RLock()

    def _board_dir(self, board_id: str) -> Path:
        return self.board_root / board_id

    def _board_path(self, board_id: str) -> Path:
        return self._board_dir(board_id) / "board.json"

    def _write_json(self, path: Path, payload: dict[str, object]) -> None:
        temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        try:
            temp_path.chmod(0o600)
        except OSError:
            pass
        temp_path.replace(path)

    def _save(self, board: dict[str, object]) -> dict[str, object]:
        board["status"] = _compute_board_status(board)
        board["updated_at"] = _now()
        events = list(board.get("events") or [])
        board["events"] = events[-200:]
        self._write_json(self._board_path(str(board["board_id"])), board)
        return board

    def _event(
        self,
        board: dict[str, object],
        *,
        event: str,
        task_id: str | None = None,
        message: str | None = None,
        from_status: str | None = None,
        to_status: str | None = None,
    ) -> None:
        events = list(board.get("events") or [])
        payload: dict[str, object] = {"at": _now(), "event": event}
        if task_id is not None:
            payload["task_id"] = task_id
        if message:
            payload["message"] = message
        if from_status is not None:
            payload["from_status"] = from_status
        if to_status is not None:
            payload["to_status"] = to_status
        events.append(payload)
        board["events"] = events

    def _terminal_notification_payload(self, board: dict[str, object], task: dict[str, object]) -> dict[str, dict[str, object]]:
        return {"board": copy.deepcopy(board), "task": copy.deepcopy(task)}

    def _dispatch_terminal_notifications(
        self,
        *,
        board_id: str,
        notifications: list[dict[str, dict[str, object]]],
    ) -> dict[str, object] | None:
        if self.notifier is None or not notifications:
            return None
        latest_board: dict[str, object] | None = None
        for notification in notifications:
            task = notification["task"]
            try:
                self.notifier.notify_task_terminal(board=notification["board"], task=task)
            except Exception as exc:
                with self._lock:
                    board = self.get(board_id)
                    self._event(
                        board,
                        event="telegram_notify_failed",
                        task_id=str(task.get("task_id") or ""),
                        message=f"Telegram notification failed ({exc.__class__.__name__}).",
                    )
                    latest_board = self._save(board)
        return latest_board

    def create(
        self,
        *,
        tasks: list[dict[str, object]] | None,
        cwd: str,
        workspace_root: Path,
        title: str | None = None,
        executor: str = DEFAULT_EXECUTOR,
        max_parallel: int = DEFAULT_MAX_PARALLEL,
        worktree_mode: str = DEFAULT_WORKTREE_MODE,
        base_ref: str = DEFAULT_BASE_REF,
        branch_prefix: str = DEFAULT_BRANCH_PREFIX,
        notes: str | None = None,
        original_request: str | None = None,
    ) -> dict[str, object]:
        executor = _validate_executor((executor or DEFAULT_EXECUTOR).strip())
        worktree_mode = _validate_worktree_mode((worktree_mode or DEFAULT_WORKTREE_MODE).strip())
        effective_max_parallel = max(int(max_parallel or DEFAULT_MAX_PARALLEL), 1)
        board_id = uuid.uuid4().hex[:12]
        now = _now()
        normalized_tasks = [
            _normalize_task_spec(
                raw,
                default_cwd=cwd,
                default_executor=executor,
                workspace_root=workspace_root,
            )
            for raw in (tasks or [])
        ]
        board = {
            "board_id": board_id,
            "title": (title or f"TaskBoard {board_id}").strip(),
            "status": "draft",
            "cwd": cwd,
            "executor": executor,
            "max_parallel": effective_max_parallel,
            "worktree_mode": worktree_mode,
            "base_ref": (base_ref or DEFAULT_BASE_REF).strip() or DEFAULT_BASE_REF,
            "branch_prefix": (branch_prefix or DEFAULT_BRANCH_PREFIX).strip() or DEFAULT_BRANCH_PREFIX,
            "notes": notes,
            "original_request": (original_request or "").strip(),
            "tasks": normalized_tasks,
            "events": [],
            "created_at": now,
            "updated_at": now,
            "started_at": None,
            "completed_at": None,
        }
        board_dir = self._board_dir(board_id)
        with self._lock:
            board_dir.mkdir(parents=True, exist_ok=True)
            try:
                board_dir.chmod(0o700)
            except OSError:
                pass
            self._event(board, event="created", message=f"Created board with {len(normalized_tasks)} task(s).")
            self._save(board)
        return self._response(board, created=True, include_board_detail=True)

    def get(self, board_id: str) -> dict[str, object]:
        with self._lock:
            path = self._board_path(board_id)
            if not path.exists():
                raise TaskBoardError("board_not_found", f"TaskBoard not found: {board_id}", board_id=board_id)
            return json.loads(path.read_text(encoding="utf-8"))

    def _save_loaded(self, board: dict[str, object]) -> dict[str, object]:
        with self._lock:
            return self._save(board)

    def add_tasks(
        self,
        *,
        board_id: str,
        tasks: list[dict[str, object]],
        workspace_root: Path,
    ) -> dict[str, object]:
        with self._lock:
            board = self.get(board_id)
            if str(board.get("status")) in {"completed", "cancelled"}:
                raise TaskBoardError(
                    "board_not_appendable",
                    f"Cannot append tasks to a {board.get('status')} board.",
                    board_id=board_id,
                    status=board.get("status"),
                )
            normalized = [
                _normalize_task_spec(
                    raw,
                    default_cwd=str(board["cwd"]),
                    default_executor=str(board["executor"]),
                    workspace_root=workspace_root,
                )
                for raw in tasks
            ]
            existing = list(board.get("tasks") or [])
            existing.extend(normalized)
            board["tasks"] = existing
            self._event(board, event="tasks_added", message=f"Added {len(normalized)} task(s).")
            self._save(board)
        return self._response(board, added_task_ids=[task["task_id"] for task in normalized])

    def _find_task(self, board: dict[str, object], task_id: str) -> dict[str, object]:
        for task in list(board.get("tasks") or []):
            if task.get("task_id") == task_id:
                return task
        raise TaskBoardError("task_not_found", f"TaskBoard subtask not found: {task_id}", board_id=board.get("board_id"), task_id=task_id)

    def _selected_tasks(self, board: dict[str, object], task_ids: list[str] | None) -> list[dict[str, object]]:
        tasks = list(board.get("tasks") or [])
        if not task_ids:
            return tasks
        wanted = set(task_ids)
        selected = [task for task in tasks if str(task.get("task_id")) in wanted]
        missing = wanted - {str(task.get("task_id")) for task in selected}
        if missing:
            raise TaskBoardError(
                "task_not_found",
                f"TaskBoard subtask(s) not found: {', '.join(sorted(missing))}",
                board_id=board.get("board_id"),
                task_ids=sorted(missing),
            )
        return selected

    def _task_worktree_path(self, board: dict[str, object], task: dict[str, object]) -> Path:
        return self.worktree_root / str(board["board_id"]) / str(task["task_id"])

    def _prepare_worktree(self, board: dict[str, object], task: dict[str, object]) -> dict[str, object]:
        if task.get("worktree_path") and task.get("branch_name") and task.get("base_sha"):
            return {
                "success": True,
                "worktree_path": task["worktree_path"],
                "branch_name": task["branch_name"],
                "base_ref": task.get("base_ref") or board.get("base_ref"),
                "base_sha": task["base_sha"],
                "head_sha": task.get("head_sha"),
                "reused": True,
            }

        branch_name = worktrees.safe_branch_name(
            str(board.get("branch_prefix") or DEFAULT_BRANCH_PREFIX),
            str(board["board_id"]),
            str(task["task_id"]),
        )
        result = worktrees.create_worktree(
            repo_cwd=Path(str(task["cwd"])),
            worktree_path=self._task_worktree_path(board, task),
            branch_name=branch_name,
            base_ref=str(board.get("base_ref") or DEFAULT_BASE_REF),
        )
        if not result.get("success"):
            return result

        task["worktree_path"] = result["worktree_path"]
        task["branch_name"] = result["branch_name"]
        task["base_ref"] = result["base_ref"]
        task["base_sha"] = result["base_sha"]
        task["head_sha"] = result.get("head_sha")
        task["updated_at"] = _now()
        return result

    def delegate(
        self,
        *,
        board_id: str,
        registry: DelegateRegistry,
        timeout: int,
        task_ids: list[str] | None = None,
        max_parallel: int | None = None,
        dry_run: bool = False,
    ) -> dict[str, object]:
        notifications: list[dict[str, dict[str, object]]] = []
        self.refresh(board_id=board_id, registry=registry, save=True)
        with self._lock:
            board = self.get(board_id)

            effective_max = max(int(max_parallel or board.get("max_parallel") or DEFAULT_MAX_PARALLEL), 1)
            tasks = list(board.get("tasks") or [])
            active = [task for task in tasks if str(task.get("status")) in ACTIVE_SUBTASK_STATUSES]
            capacity = max(effective_max - len(active), 0)
            selected = self._selected_tasks(board, task_ids)
            eligible = [task for task in selected if task.get("status") == "pending"]

            if dry_run:
                candidates = eligible[:capacity]
                return self._response(
                    board,
                    dry_run=True,
                    max_parallel=effective_max,
                    active_count=len(active),
                    capacity=capacity,
                    would_submit=[_compact_task(task) for task in candidates],
                    skipped_count=max(len(eligible) - len(candidates), 0),
                )

            submitted: list[str] = []
            failed: list[dict[str, object]] = []
            for task in eligible:
                if len(submitted) >= capacity:
                    break
                worker_cwd = Path(str(task["cwd"]))
                if board.get("worktree_mode") == "per_task":
                    prepared = self._prepare_worktree(board, task)
                    if not prepared.get("success"):
                        previous = str(task.get("status") or "pending")
                        task["status"] = "failed"
                        task["last_error"] = prepared.get("error")
                        task["updated_at"] = _now()
                        failed.append({"task_id": task["task_id"], "error": prepared.get("error")})
                        self._event(
                            board,
                            event="task_failed",
                            task_id=str(task["task_id"]),
                            from_status=previous,
                            to_status="failed",
                            message=str(prepared.get("error", {}).get("message") if isinstance(prepared.get("error"), dict) else "worktree preparation failed"),
                        )
                        if previous not in TERMINAL_SUBTASK_STATUSES:
                            notifications.append(self._terminal_notification_payload(board, task))
                        continue
                    worker_cwd = Path(str(prepared["worktree_path"]))

                try:
                    delegated = registry.submit(
                        task=_task_prompt(board, task, assigned_cwd=str(worker_cwd)),
                        goal=None,
                        executor=str(task.get("executor") or board.get("executor") or DEFAULT_EXECUTOR),
                        cwd=worker_cwd,
                        timeout=timeout,
                        context_files=list(task.get("context_files") or []),
                        acceptance_criteria=list(task.get("acceptance_criteria") or []),
                        verification_commands=list(task.get("verification_commands") or []),
                        commit_mode="allowed",
                        output_schema=None,
                        parse_structured_output=True,
                    )
                except Exception as exc:
                    previous = str(task.get("status") or "pending")
                    task["status"] = "failed"
                    task["last_error"] = {"code": "delegate_submit_failed", "message": str(exc)}
                    task["updated_at"] = _now()
                    failed.append({"task_id": task["task_id"], "error": task["last_error"]})
                    self._event(
                        board,
                        event="task_failed",
                        task_id=str(task["task_id"]),
                        from_status=previous,
                        to_status="failed",
                        message=str(exc),
                    )
                    if previous not in TERMINAL_SUBTASK_STATUSES:
                        notifications.append(self._terminal_notification_payload(board, task))
                    continue

                previous = str(task.get("status") or "pending")
                task["delegate_task_id"] = delegated.get("task_id")
                task["executor"] = delegated.get("executor") or task.get("executor")
                task["status"] = _delegate_status_to_subtask(delegated.get("status"))
                task["updated_at"] = _now()
                task["last_error"] = None
                submitted.append(str(task["task_id"]))
                self._event(
                    board,
                    event="task_delegated",
                    task_id=str(task["task_id"]),
                    from_status=previous,
                    to_status=str(task["status"]),
                    message=f"Delegated as {task.get('delegate_task_id')}.",
                )
                if previous not in TERMINAL_SUBTASK_STATUSES and str(task["status"]) in TERMINAL_SUBTASK_STATUSES:
                    notifications.append(self._terminal_notification_payload(board, task))

            if submitted and not board.get("started_at"):
                board["started_at"] = _now()
            self._save(board)

        active_count = len([task for task in board.get("tasks", []) if str(task.get("status")) in ACTIVE_SUBTASK_STATUSES])
        latest_board = self._dispatch_terminal_notifications(board_id=board_id, notifications=notifications)
        if latest_board is not None:
            board = latest_board

        return self._response(
            board,
            submitted_task_ids=submitted,
            failed_tasks=failed,
            max_parallel=effective_max,
            active_count=active_count,
            capacity=capacity,
            dry_run=False,
        )

    def _refresh_head_metadata(self, task: dict[str, object]) -> None:
        worktree_path = task.get("worktree_path")
        if not worktree_path:
            return
        head = worktrees.rev_parse(Path(str(worktree_path)), "HEAD")
        if not head.get("success"):
            return
        head_sha = str(head.get("sha") or "")
        task["head_sha"] = head_sha
        base_sha = str(task.get("base_sha") or "")
        task["commit_sha"] = head_sha if base_sha and head_sha != base_sha else None

    def refresh(
        self,
        *,
        board_id: str,
        registry: DelegateRegistry,
        save: bool = True,
        board: dict[str, object] | None = None,
    ) -> dict[str, object]:
        notifications: list[dict[str, dict[str, object]]] = []
        with self._lock:
            board = board or self.get(board_id)
            changed: list[dict[str, object]] = []
            for task in list(board.get("tasks") or []):
                delegate_task_id = task.get("delegate_task_id")
                if not delegate_task_id:
                    self._refresh_head_metadata(task)
                    continue
                previous = str(task.get("status") or "pending")
                try:
                    meta = registry.get(str(delegate_task_id))
                except Exception as exc:
                    task["last_error"] = {"code": "delegate_task_unavailable", "message": str(exc)}
                    continue
                current = _delegate_status_to_subtask(meta.get("status"))
                task["summary"] = str(meta.get("summary") or task.get("summary") or "")
                task["delegate_status"] = meta.get("status")
                task["delegate_updated_at"] = meta.get("updated_at")
                task["delegate_completed"] = bool(meta.get("completed"))
                if "exit_code" in meta:
                    task["exit_code"] = meta.get("exit_code")
                self._refresh_head_metadata(task)
                if current != previous:
                    task["status"] = current
                    task["updated_at"] = _now()
                    changed.append(_compact_task(task))
                    self._event(
                        board,
                        event="task_status_changed",
                        task_id=str(task["task_id"]),
                        from_status=previous,
                        to_status=current,
                    )
                    if previous not in TERMINAL_SUBTASK_STATUSES and current in TERMINAL_SUBTASK_STATUSES:
                        notifications.append(self._terminal_notification_payload(board, task))

            new_status = _compute_board_status(board)
            if new_status in {"completed", "failed", "cancelled"} and not board.get("completed_at"):
                board["completed_at"] = _now()
            board["status"] = new_status
            if save:
                self._save(board)
            result = {"board": board, "changed_tasks": changed}
        latest_board = self._dispatch_terminal_notifications(board_id=board_id, notifications=notifications)
        if latest_board is not None:
            result["board"] = latest_board
        return result

    def status(
        self,
        *,
        board_id: str,
        registry: DelegateRegistry,
        refresh: bool = True,
        event_limit: int = DEFAULT_EVENT_LIMIT,
    ) -> dict[str, object]:
        if refresh:
            refreshed = self.refresh(board_id=board_id, registry=registry, save=True)
            board = refreshed["board"]
            changed = refreshed["changed_tasks"]
        else:
            board = self.get(board_id)
            changed = []
        return self._response(board, changed_tasks=changed, event_limit=event_limit)

    def wait(
        self,
        *,
        board_id: str,
        registry: DelegateRegistry,
        timeout: float = 30,
        poll_interval: float = 0.5,
        return_on: str = "change",
        event_limit: int = DEFAULT_EVENT_LIMIT,
    ) -> dict[str, object]:
        if return_on not in {"change", "any_done", "all_done"}:
            raise TaskBoardError(
                "invalid_return_on",
                "return_on must be one of: change, any_done, all_done.",
                return_on=return_on,
            )

        initial = self.status(board_id=board_id, registry=registry, refresh=True, event_limit=event_limit)
        board = self.get(board_id)
        initial_statuses = {str(task["task_id"]): str(task["status"]) for task in board.get("tasks", [])}
        initial_changed = list(initial.get("changed_tasks") or [])
        initial_terminal_changes = [
            task for task in initial_changed if str(task.get("status")) in TERMINAL_SUBTASK_STATUSES
        ]
        board_status = str(board.get("status"))
        has_active = any(status in ACTIVE_SUBTASK_STATUSES for status in initial_statuses.values())
        if return_on == "change" and initial_changed:
            initial.update({"timed_out": False, "return_reason": "status_change", "changed_tasks": initial_changed})
            return initial
        if return_on == "any_done" and initial_terminal_changes:
            initial.update({"timed_out": False, "return_reason": "any_done", "changed_tasks": initial_terminal_changes})
            return initial
        if return_on == "all_done" and board_status in {"completed", "failed", "cancelled"}:
            initial.update({"timed_out": False, "return_reason": "all_done", "changed_tasks": []})
            return initial
        if not has_active:
            initial.update({"timed_out": False, "return_reason": "no_active_tasks", "changed_tasks": []})
            return initial

        deadline = time.monotonic() + max(float(timeout), 0.0)
        interval = max(float(poll_interval), 0.05)
        changed: list[dict[str, object]] = []
        while True:
            current = self.status(board_id=board_id, registry=registry, refresh=True, event_limit=event_limit)
            board = self.get(board_id)
            changed = [
                _compact_task(task)
                for task in board.get("tasks", [])
                if initial_statuses.get(str(task["task_id"])) != str(task.get("status"))
            ]
            terminal_changes = [
                _compact_task(task)
                for task in board.get("tasks", [])
                if initial_statuses.get(str(task["task_id"])) not in TERMINAL_SUBTASK_STATUSES
                and str(task.get("status")) in TERMINAL_SUBTASK_STATUSES
            ]
            board_status = str(board.get("status"))
            if return_on == "change" and changed:
                current.update({"timed_out": False, "return_reason": "status_change", "changed_tasks": changed})
                return current
            if return_on == "any_done" and terminal_changes:
                current.update({"timed_out": False, "return_reason": "any_done", "changed_tasks": terminal_changes})
                return current
            if return_on == "all_done" and board_status in {"completed", "failed", "cancelled"}:
                current.update({"timed_out": False, "return_reason": "all_done", "changed_tasks": changed})
                return current
            if time.monotonic() >= deadline:
                current.update({"timed_out": True, "return_reason": "timeout", "changed_tasks": changed})
                return current
            time.sleep(interval)

    def get_task(
        self,
        *,
        board_id: str,
        task_id: str,
        registry: DelegateRegistry,
        refresh: bool = True,
        include_prompt: bool = False,
        include_done_report: bool = False,
        include_stdout_tail: bool = False,
        include_stderr_tail: bool = False,
        tail_chars: int = DEFAULT_STDIO_TAIL_CHARS,
    ) -> dict[str, object]:
        if refresh:
            self.refresh(board_id=board_id, registry=registry, save=True)
        board = self.get(board_id)
        task = self._find_task(board, task_id)
        payload = {
            "success": True,
            "board": _compact_board(board),
            "board_detail": _detailed_board(board),
            "task": dict(task),
        }
        delegate_task_id = task.get("delegate_task_id")
        delegate_meta: dict[str, object] | None = None
        if delegate_task_id and (include_done_report or include_stdout_tail or include_stderr_tail):
            try:
                delegate_meta = registry.get(str(delegate_task_id))
            except Exception as exc:
                payload["delegate_error"] = {"code": "delegate_task_unavailable", "message": str(exc)}

        if include_prompt:
            payload["prompt"] = _task_prompt(board, task)
        if include_done_report and delegate_meta is not None:
            payload["done_report"] = {
                "status": delegate_meta.get("status"),
                "summary": delegate_meta.get("summary"),
                "structured_output": delegate_meta.get("structured_output"),
                "exit_code": delegate_meta.get("exit_code"),
            }
        if include_stdout_tail and delegate_meta is not None:
            payload["stdout_tail"] = str(delegate_meta.get("stdout_tail") or "")[-max(int(tail_chars), 0) :]
        if include_stderr_tail and delegate_meta is not None:
            payload["stderr_tail"] = str(delegate_meta.get("stderr_tail") or "")[-max(int(tail_chars), 0) :]
        return payload

    def collect_results(
        self,
        *,
        board_id: str,
        registry: DelegateRegistry,
        task_ids: list[str] | None = None,
        include_diff: bool = False,
        include_log_tail: bool = False,
        diff_max_bytes: int = DEFAULT_DIFF_MAX_BYTES,
        tail_chars: int = DEFAULT_STDIO_TAIL_CHARS,
        event_limit: int = DEFAULT_EVENT_LIMIT,
    ) -> dict[str, object]:
        self.refresh(board_id=board_id, registry=registry, save=True)
        board = self.get(board_id)
        selected = self._selected_tasks(board, task_ids)
        results: list[dict[str, object]] = []
        for task in selected:
            task_result: dict[str, object] = {
                "task_id": task.get("task_id"),
                "title": task.get("title"),
                "status": task.get("status"),
                "delegate_task_id": task.get("delegate_task_id"),
                "worktree_path": task.get("worktree_path"),
                "branch_name": task.get("branch_name"),
                "base_ref": task.get("base_ref"),
                "base_sha": task.get("base_sha"),
                "head_sha": task.get("head_sha"),
                "commit_sha": task.get("commit_sha"),
                "changed_files": [],
                "diff_summary": "",
            }
            if task.get("worktree_path"):
                collected = worktrees.collect_result(
                    worktree_path=Path(str(task["worktree_path"])),
                    base_sha=str(task.get("base_sha") or "") or None,
                    include_diff=include_diff,
                    diff_max_bytes=diff_max_bytes,
                    include_log_tail=include_log_tail,
                    log_tail_chars=tail_chars,
                )
                if collected.get("success"):
                    task["head_sha"] = collected.get("head_sha")
                    task["commit_sha"] = collected.get("commit_sha")
                    task_result.update(
                        {
                            "head_sha": collected.get("head_sha"),
                            "commit_sha": collected.get("commit_sha"),
                            "changed_files": collected.get("changed_files", []),
                            "diff_summary": collected.get("diff_summary", ""),
                            "diff_summary_truncated": collected.get("diff_summary_truncated", False),
                            "status": task.get("status"),
                        }
                    )
                    if include_diff:
                        task_result["diff"] = collected.get("diff", "")
                        task_result["diff_truncated"] = collected.get("diff_truncated", False)
                        task_result["diff_total_bytes"] = collected.get("diff_total_bytes", 0)
                    if include_log_tail:
                        task_result["log_tail"] = collected.get("log_tail", "")
                else:
                    task_result["result_error"] = collected.get("error")

            if task.get("delegate_task_id") and include_log_tail:
                try:
                    meta = registry.get(str(task["delegate_task_id"]))
                    task_result["stdout_tail"] = str(meta.get("stdout_tail") or "")[-max(int(tail_chars), 0) :]
                    task_result["stderr_tail"] = str(meta.get("stderr_tail") or "")[-max(int(tail_chars), 0) :]
                except Exception as exc:
                    task_result["delegate_error"] = {"code": "delegate_task_unavailable", "message": str(exc)}
            results.append(task_result)

        self._save_loaded(board)
        return self._response(board, results=results, event_limit=event_limit)

    def cancel(
        self,
        *,
        board_id: str,
        registry: DelegateRegistry,
        task_ids: list[str] | None = None,
        event_limit: int = DEFAULT_EVENT_LIMIT,
    ) -> dict[str, object]:
        notifications: list[dict[str, dict[str, object]]] = []
        with self._lock:
            board = self.get(board_id)
            selected = self._selected_tasks(board, task_ids)
            cancelled: list[str] = []
            errors: list[dict[str, object]] = []
            for task in selected:
                status = str(task.get("status") or "pending")
                if status in TERMINAL_SUBTASK_STATUSES:
                    continue
                delegate_task_id = task.get("delegate_task_id")
                if delegate_task_id:
                    try:
                        registry.cancel(str(delegate_task_id))
                    except Exception as exc:
                        errors.append({"task_id": task.get("task_id"), "error": str(exc)})
                previous = status
                task["status"] = "cancelled"
                task["updated_at"] = _now()
                cancelled.append(str(task["task_id"]))
                self._event(
                    board,
                    event="task_cancelled",
                    task_id=str(task["task_id"]),
                    from_status=previous,
                    to_status="cancelled",
                )
                if previous not in TERMINAL_SUBTASK_STATUSES:
                    notifications.append(self._terminal_notification_payload(board, task))
            if not task_ids:
                board["cancelled_at"] = _now()
            self._save(board)
        latest_board = self._dispatch_terminal_notifications(board_id=board_id, notifications=notifications)
        if latest_board is not None:
            board = latest_board
        return self._response(board, cancelled_task_ids=cancelled, cancel_errors=errors, event_limit=event_limit)

    def list_boards(
        self,
        *,
        status: str | None = None,
        cwd: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, object]:
        if status and status not in TASKBOARD_STATUSES:
            raise TaskBoardError(
                "invalid_status",
                f"status must be one of: {', '.join(sorted(TASKBOARD_STATUSES))}.",
                status=status,
            )
        boards: list[dict[str, object]] = []
        with self._lock:
            if self.board_root.exists():
                for path in self.board_root.glob("*/board.json"):
                    try:
                        board = json.loads(path.read_text(encoding="utf-8"))
                    except Exception:
                        continue
                    if status and board.get("status") != status:
                        continue
                    if cwd and str(board.get("cwd")) != cwd:
                        continue
                    boards.append(board)
        boards.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        start = max(int(offset), 0)
        end = start + max(int(limit), 0)
        page = boards[start:end]
        return {
            "success": True,
            "boards": [_compact_board(board) for board in page],
            "total": len(boards),
            "limit": limit,
            "offset": offset,
            "has_more": end < len(boards),
        }

    def purge(
        self,
        *,
        older_than_seconds: float,
        dry_run: bool = False,
    ) -> dict[str, object]:
        cutoff = datetime.now(UTC) - timedelta(seconds=max(float(older_than_seconds), 0.0))
        scanned = 0
        purged = 0
        board_ids: list[str] = []
        with self._lock:
            if not self.board_root.exists():
                return {
                    "success": True,
                    "scanned": 0,
                    "purged": 0,
                    "board_ids": [],
                    "dry_run": dry_run,
                    "cutoff": cutoff.isoformat(),
                    "worktrees_deleted": False,
                }
            for board_dir in sorted(self.board_root.iterdir()):
                if not board_dir.is_dir():
                    continue
                scanned += 1
                board_id = board_dir.name
                board_path = board_dir / "board.json"
                should_purge = False
                try:
                    board = json.loads(board_path.read_text(encoding="utf-8"))
                    updated_at = _parse_time(str(board.get("updated_at") or board.get("created_at") or ""))
                    should_purge = updated_at < cutoff
                except Exception:
                    should_purge = True
                if not should_purge:
                    continue
                purged += 1
                board_ids.append(board_id)
                if not dry_run:
                    shutil.rmtree(board_dir, ignore_errors=True)
        return {
            "success": True,
            "scanned": scanned,
            "purged": purged,
            "board_ids": board_ids,
            "dry_run": dry_run,
            "cutoff": cutoff.isoformat(),
            "worktrees_deleted": False,
        }

    def _response(
        self,
        board: dict[str, object],
        *,
        event_limit: int = DEFAULT_EVENT_LIMIT,
        include_board_detail: bool = False,
        **extra: object,
    ) -> dict[str, object]:
        events = list(board.get("events") or [])
        limit = max(int(event_limit), 0)
        payload: dict[str, object] = {
            "success": True,
            "board": _compact_board(board),
            "tasks": [_compact_task(task) for task in list(board.get("tasks") or [])],
            "events": events[-limit:] if limit else [],
        }
        if include_board_detail:
            payload["board_detail"] = _detailed_board(board)
        payload.update(extra)
        return payload
