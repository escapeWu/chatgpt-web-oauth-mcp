from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable, Iterable

from .delegate_models import (
    TERMINAL_TASK_STATES,
    DelegateGroup,
    DelegateTask,
    ProjectIdentity,
    ProjectLane,
    TaskState,
)


TaskRunner = Callable[[DelegateTask], dict[str, object]]
TaskTerminator = Callable[[DelegateTask], None]
TerminalCallback = Callable[[DelegateTask], None]
CancelledResultFactory = Callable[[DelegateTask], dict[str, object]]


class DelegateQueueFullError(RuntimeError):
    def __init__(self, *, scope: str, limit: int) -> None:
        self.scope = scope
        self.limit = limit
        super().__init__(f"Delegate queue limit reached for {scope}: {limit}")


class DelegateScheduler:
    """Project-scoped fair reader/writer scheduler with global safety valves."""

    def __init__(
        self,
        *,
        runner: TaskRunner,
        terminator: TaskTerminator,
        on_terminal: TerminalCallback,
        cancelled_result_factory: CancelledResultFactory,
        max_explore_per_project: int = 4,
        max_explore_global: int = 8,
        max_code_per_project: int = 1,
        max_code_global: int = 4,
        queue_limit_per_project: int = 32,
        queue_limit_global: int = 128,
    ) -> None:
        self.runner = runner
        self.terminator = terminator
        self.on_terminal = on_terminal
        self.cancelled_result_factory = cancelled_result_factory
        self.max_explore_per_project = max(1, int(max_explore_per_project))
        self.max_explore_global = max(1, int(max_explore_global))
        self.max_code_per_project = max(1, int(max_code_per_project))
        self.max_code_global = max(1, int(max_code_global))
        self.queue_limit_per_project = max(1, int(queue_limit_per_project))
        self.queue_limit_global = max(1, int(queue_limit_global))

        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.tasks: dict[str, DelegateTask] = {}
        self.groups: dict[str, DelegateGroup] = {}
        self.lanes: dict[str, ProjectLane] = {}
        self._project_order: deque[str] = deque()
        self._active_explore_global = 0
        self._active_code_global = 0

    def submit_task(self, task: DelegateTask) -> None:
        with self.lock:
            self._ensure_capacity_locked(task.project.project_key, 1)
            self._register_task_locked(task)
            starts = self._dispatch_locked()
            self.condition.notify_all()
        self._start_threads(starts)

    def submit_group(self, group: DelegateGroup, tasks: list[DelegateTask]) -> None:
        if not tasks:
            raise ValueError("delegate group requires at least one child task")
        if any(task.project.project_key != group.project.project_key for task in tasks):
            raise ValueError("all delegate group children must belong to the group project")
        with self.lock:
            self._ensure_capacity_locked(group.project.project_key, len(tasks))
            self.groups[group.group_id] = group
            for task in tasks:
                self._register_task_locked(task)
            self._refresh_group_locked(group)
            starts = self._dispatch_locked()
            self.condition.notify_all()
        self._start_threads(starts)

    def get_task(self, delegate_id: str) -> DelegateTask | None:
        with self.lock:
            return self.tasks.get(delegate_id)

    def get_group(self, group_id: str) -> DelegateGroup | None:
        with self.lock:
            return self.groups.get(group_id)

    def nonterminal_tasks(self) -> list[DelegateTask]:
        with self.lock:
            return [task for task in self.tasks.values() if not task.is_terminal]

    def tasks_for_project(self, project_key: str) -> list[DelegateTask]:
        with self.lock:
            return [task for task in self.tasks.values() if task.project.project_key == project_key]

    def task_counts(self, tasks: Iterable[DelegateTask]) -> dict[str, int]:
        counts = {
            "total": 0,
            "queued": 0,
            "running": 0,
            "succeeded": 0,
            "failed": 0,
            "cancelled": 0,
            "timed_out": 0,
        }
        for task in tasks:
            counts["total"] += 1
            counts[task.state] += 1
        return counts

    def cancel_task(self, delegate_id: str) -> DelegateTask | None:
        running: DelegateTask | None = None
        completed: DelegateTask | None = None
        with self.lock:
            task = self.tasks.get(delegate_id)
            if task is None or task.is_terminal:
                return task
            task.cancel_requested = True
            if task.state == "queued":
                lane = self.lanes[task.project.project_key]
                try:
                    lane.pending.remove(task.delegate_id)
                except ValueError:
                    pass
                task.state = "cancelled"
                task.completed_at = time.time()
                task.result = self.cancelled_result_factory(task)
                task.completed_event.set()
                self._refresh_task_group_locked(task)
                completed = task
                starts = self._dispatch_locked()
            else:
                running = task
                starts = []
            self.condition.notify_all()
        if completed is not None:
            self.on_terminal(completed)
        if running is not None:
            self.terminator(running)
        self._start_threads(starts)
        return task

    def cancel_group(self, group_id: str) -> DelegateGroup | None:
        with self.lock:
            group = self.groups.get(group_id)
            child_ids = list(group.child_ids) if group else []
        if group is None:
            return None
        for delegate_id in child_ids:
            self.cancel_task(delegate_id)
        return group

    def _register_task_locked(self, task: DelegateTask) -> None:
        self.tasks[task.delegate_id] = task
        lane = self.lanes.get(task.project.project_key)
        if lane is None:
            lane = ProjectLane(project=task.project)
            self.lanes[task.project.project_key] = lane
            self._project_order.append(task.project.project_key)
        lane.pending.append(task.delegate_id)

    def _ensure_capacity_locked(self, project_key: str, new_count: int) -> None:
        global_queued = sum(len(lane.pending) for lane in self.lanes.values())
        if global_queued + new_count > self.queue_limit_global:
            raise DelegateQueueFullError(scope="global", limit=self.queue_limit_global)
        lane = self.lanes.get(project_key)
        project_queued = len(lane.pending) if lane else 0
        if project_queued + new_count > self.queue_limit_per_project:
            raise DelegateQueueFullError(scope="project", limit=self.queue_limit_per_project)

    def _dispatch_locked(self) -> list[DelegateTask]:
        starts: list[DelegateTask] = []
        if not self._project_order:
            return starts

        # One task per project per pass provides round-robin fairness while a
        # lane's FIFO head preserves writer preference within that project.
        made_progress = True
        while made_progress:
            made_progress = False
            project_count = len(self._project_order)
            for _ in range(project_count):
                project_key = self._project_order[0]
                self._project_order.rotate(-1)
                lane = self.lanes.get(project_key)
                if lane is None:
                    continue
                task = self._next_runnable_locked(lane)
                if task is None:
                    continue
                lane.pending.popleft()
                task.state = "running"
                task.started_at = time.time()
                task.started_monotonic = time.monotonic()
                if task.kind == "explore":
                    lane.active_explores[task.delegate_id] = task
                    self._active_explore_global += 1
                else:
                    lane.active_code = task
                    self._active_code_global += 1
                self._refresh_task_group_locked(task)
                starts.append(task)
                made_progress = True
        return starts

    def _next_runnable_locked(self, lane: ProjectLane) -> DelegateTask | None:
        if lane.active_code is not None or not lane.pending:
            return None
        task = self.tasks[lane.pending[0]]
        if task.kind == "code":
            if lane.active_explores:
                return None
            if self._active_code_global >= self.max_code_global:
                return None
            if not self._dependencies_complete_locked(task):
                return None
            return task

        if len(lane.active_explores) >= self.max_explore_per_project:
            return None
        if self._active_explore_global >= self.max_explore_global:
            return None
        if task.group_id:
            group = self.groups.get(task.group_id)
            if group and group.max_concurrency is not None:
                running_in_group = sum(
                    1
                    for child_id in group.child_ids
                    if self.tasks[child_id].state == "running"
                )
                if running_in_group >= group.max_concurrency:
                    return None
        return task

    def _dependencies_complete_locked(self, task: DelegateTask) -> bool:
        for group_id in task.depends_on_group_ids:
            group = self.groups.get(group_id)
            if group is None or not group.completed_event.is_set():
                return False
        return True

    def _start_threads(self, tasks: list[DelegateTask]) -> None:
        for task in tasks:
            threading.Thread(
                target=self._execute_task,
                args=(task,),
                name=f"delegate-{task.delegate_id}",
                daemon=True,
            ).start()

    def _execute_task(self, task: DelegateTask) -> None:
        try:
            result = self.runner(task)
        except Exception as exc:  # pragma: no cover - defensive scheduler boundary
            result = {
                "success": False,
                "status": "failed",
                "completed": True,
                "in_progress": False,
                "error": {"code": "delegate_runner_failed", "message": str(exc)},
            }

        status = str(result.get("status", "failed"))
        state: TaskState = status if status in TERMINAL_TASK_STATES else "failed"  # type: ignore[assignment]
        if state == "failed" and status != "failed":
            result["status"] = "failed"
        with self.lock:
            task.result = result
            task.state = state
            task.completed_at = time.time()
            lane = self.lanes[task.project.project_key]
            if task.kind == "explore":
                lane.active_explores.pop(task.delegate_id, None)
                self._active_explore_global = max(0, self._active_explore_global - 1)
            elif lane.active_code is task:
                lane.active_code = None
                self._active_code_global = max(0, self._active_code_global - 1)
            task.completed_event.set()
            self._refresh_task_group_locked(task)
            starts = self._dispatch_locked()
            self.condition.notify_all()
        self.on_terminal(task)
        self._start_threads(starts)

    def _refresh_task_group_locked(self, task: DelegateTask) -> None:
        if task.group_id:
            group = self.groups.get(task.group_id)
            if group:
                self._refresh_group_locked(group)

    def _refresh_group_locked(self, group: DelegateGroup) -> None:
        children = [self.tasks[child_id] for child_id in group.child_ids]
        states = {task.state for task in children}
        if states and states <= {"succeeded"}:
            group.state = "succeeded"
            group.completed_event.set()
        elif children and all(task.state in TERMINAL_TASK_STATES for task in children):
            group.state = "failed"
            group.completed_event.set()
        else:
            # A submitted group is an in-progress fan-in barrier even when all
            # children are still waiting for project/global capacity.
            group.state = "running"
