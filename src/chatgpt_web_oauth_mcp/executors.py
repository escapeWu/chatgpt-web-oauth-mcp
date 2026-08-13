from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from collections import deque
from pathlib import Path, PureWindowsPath

from .delegate_harnesses import (
    CodexHarness,
    DelegateHarness,
    PiHarness,
    command_available,
)
from .delegate_models import (
    TERMINAL_TASK_STATES,
    DelegateGroup,
    DelegateLogPaths,
    DelegateTask,
    ProjectIdentity,
    TaskKind,
)
from .delegate_process import (
    TIMEOUT_EXIT_CODE,
    DelegateProcessRunner,
    Invocation,
    extract_structured_output,
    harness_display_name,
    log_read_hint,
    safe_chmod,
    write_private_json,
    write_private_text,
)
from .delegate_project import ProjectIdentityResolver
from .delegate_scheduler import DelegateQueueFullError, DelegateScheduler
from .response_budget import (
    DEFAULT_TOOL_OUTPUT_TOKEN_BUDGET,
    ResponseBudget,
    with_budget_metadata,
)


ALLOWED_COMMIT_MODES = {"allowed", "required", "forbidden"}
ALLOWED_REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
ALLOWED_TASK_KINDS = {"explore", "code"}
DEFAULT_MODEL = "default"
DEFAULT_REASONING_EFFORT = "default"
DEFAULT_EXPLORE_MODEL = "gpt-5.6-luna"
DEFAULT_EXPLORE_REASONING_EFFORT = "low"
DEFAULT_CODE_MODEL = "gpt-5.6-sol"
DEFAULT_CODE_REASONING_EFFORT = "xhigh"
DEFAULT_DELEGATE_WAIT_SECONDS = 300.0
DEFAULT_EXPLORE_EXECUTION_TIMEOUT_SECONDS = 900
DEFAULT_CODE_EXECUTION_TIMEOUT_SECONDS = 3600
DEFAULT_CANCEL_GRACE_SECONDS = 5.0
DELEGATE_STALL_HINT_SECONDS = 180.0
DEFAULT_DELEGATE_HISTORY_LIMIT = 20
DEFAULT_DELEGATE_STATUS_POLL_SECONDS = 5.0
MAX_DELEGATE_STATUS_WATCH_SECONDS = 300.0
IS_WINDOWS = os.name == "nt"


def _split_command(command: str) -> list[str]:
    return shlex.split(command)


def _binary_name(binary: str) -> str:
    if IS_WINDOWS:
        return PureWindowsPath(binary).stem.lower()
    return Path(binary).stem.lower()


def _resolve_delegate_command_parts(command: str) -> list[str]:
    parts = _split_command(command)
    if not IS_WINDOWS or not parts:
        return parts
    if _binary_name(parts[0]) != "codex":
        return parts
    resolved = shutil.which(parts[0])
    if resolved:
        parts[0] = resolved
    return parts


def _command_available(command: str | None) -> bool:
    return command_available(command)


def _normalize_reasoning_effort(reasoning_effort: str | None) -> str | None:
    normalized = (reasoning_effort or "").strip().lower()
    if not normalized or normalized == DEFAULT_REASONING_EFFORT:
        return None
    return normalized


def _normalize_model(model: str | None) -> str | None:
    normalized = (model or "").strip()
    if not normalized or normalized.lower() == DEFAULT_MODEL:
        return None
    return normalized


def _extract_structured_output(text: str) -> object | None:
    return extract_structured_output(text)


def _delegate_log_root() -> Path:
    return Path(tempfile.gettempdir()) / "chatgpt-web-oauth-mcp" / "codex-delegates"


def _delegate_log_root_for_harness(harness: str) -> Path:
    safe_name = "".join(
        character if character.isalnum() or character in {"-", "_"} else "-"
        for character in harness.lower()
    ).strip("-") or "cli"
    if safe_name == "codex":
        return _delegate_log_root()
    return Path(tempfile.gettempdir()) / "chatgpt-web-oauth-mcp" / f"{safe_name}-delegates"


def _create_delegate_logs(delegate_id: str, *, harness: str = "codex") -> DelegateLogPaths:
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    log_dir = _delegate_log_root_for_harness(harness) / f"{timestamp}-{delegate_id}"
    log_dir.mkdir(parents=True, exist_ok=False)
    safe_chmod(log_dir, 0o700)
    return DelegateLogPaths(
        log_dir=log_dir,
        prompt=log_dir / "prompt.txt",
        stdout=log_dir / "stdout.log",
        stderr=log_dir / "stderr.log",
        metadata=log_dir / "metadata.json",
    )


def _format_epoch_seconds(value: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value))


def _delegate_request_fingerprint(
    *,
    task: str | None,
    goal: str | None,
    task_id: str | None,
    cwd: Path,
    harness: str,
    kind: str,
    group_id: str | None,
    files_in_scope: list[str] | None,
    out_of_scope: list[str] | None,
    context_files: list[str] | None,
    acceptance_criteria: list[str] | None,
    done_means: list[str] | None,
    verification_commands: list[str] | None,
    commit_mode: str,
    model: str,
    reasoning_effort: str,
    output_schema: dict[str, object] | None,
    parse_structured_output: bool,
    depends_on_group_ids: list[str] | None,
) -> str:
    payload = {
        "task": task or "",
        "goal": goal or "",
        "task_id": task_id or "",
        "cwd": str(cwd),
        "harness": harness,
        "kind": kind,
        "group_id": group_id or "",
        "files_in_scope": files_in_scope or [],
        "out_of_scope": out_of_scope or [],
        "context_files": context_files or [],
        "acceptance_criteria": acceptance_criteria or [],
        "done_means": done_means or [],
        "verification_commands": verification_commands or [],
        "commit_mode": commit_mode,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "output_schema": output_schema or None,
        "parse_structured_output": parse_structured_output,
        "depends_on_group_ids": depends_on_group_ids or [],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _status_entry_lifecycle_signature(entry: dict[str, object] | None) -> tuple[object, ...]:
    if not entry:
        return ("none",)
    error = entry.get("error")
    error_outcome: object = error
    if isinstance(error, dict):
        error_outcome = (error.get("code"), error.get("message"))
    counts = entry.get("counts")
    count_signature: tuple[object, ...] = ()
    if isinstance(counts, dict):
        count_signature = tuple(
            counts.get(key)
            for key in ("queued", "running", "succeeded", "failed", "cancelled", "timed_out")
        )
    return (
        entry.get("delegate_id") or entry.get("group_id"),
        entry.get("status"),
        entry.get("completed"),
        entry.get("in_progress"),
        entry.get("success"),
        entry.get("exit_code"),
        entry.get("timed_out"),
        error_outcome,
        *count_signature,
    )


def _status_response_lifecycle_signature(payload: dict[str, object]) -> tuple[object, ...]:
    for key in ("delegate", "group", "project"):
        entry = payload.get(key)
        if isinstance(entry, dict):
            return (key, *_status_entry_lifecycle_signature(entry))
    latest = payload.get("active") or payload.get("latest")
    return ("list", *_status_entry_lifecycle_signature(latest if isinstance(latest, dict) else None))


def _status_focus_entry(payload: dict[str, object]) -> dict[str, object] | None:
    for key in ("delegate", "group", "project", "active", "latest"):
        entry = payload.get(key)
        if isinstance(entry, dict):
            return entry
    return None


class ExecutorRegistry:
    """Compatibility facade over project-scoped delegate scheduling."""

    def __init__(
        self,
        *,
        codex_command: str | None = None,
        pi_command: str | None = None,
        default_harness: str = "codex",
        harnesses: list[DelegateHarness] | tuple[DelegateHarness, ...] | None = None,
        max_explore_per_project: int = 4,
        max_explore_global: int = 8,
        max_code_per_project: int = 1,
        max_code_global: int = 4,
        queue_limit_per_project: int = 32,
        queue_limit_global: int = 128,
        explore_execution_timeout_seconds: int = DEFAULT_EXPLORE_EXECUTION_TIMEOUT_SECONDS,
        code_execution_timeout_seconds: int = DEFAULT_CODE_EXECUTION_TIMEOUT_SECONDS,
        cancel_grace_seconds: float = DEFAULT_CANCEL_GRACE_SECONDS,
        allow_unsafe_explore_command: bool = False,
    ) -> None:
        self.codex_command = codex_command
        self.pi_command = pi_command
        self.default_harness = default_harness.strip().lower() or "codex"
        configured_harnesses: dict[str, DelegateHarness] = {
            "codex": CodexHarness(
                command=codex_command,
                allow_unsafe_explore_command=allow_unsafe_explore_command,
            ),
        }
        if pi_command is not None:
            configured_harnesses["pi"] = PiHarness(command=pi_command)
        for adapter in harnesses or ():
            configured_harnesses[adapter.name.strip().lower()] = adapter
        self.harnesses = configured_harnesses
        self.explore_execution_timeout_seconds = max(1, int(explore_execution_timeout_seconds))
        self.code_execution_timeout_seconds = max(1, int(code_execution_timeout_seconds))
        self.cancel_grace_seconds = max(0.0, float(cancel_grace_seconds))
        self.allow_unsafe_explore_command = bool(allow_unsafe_explore_command)
        self.project_resolver = ProjectIdentityResolver()
        self._process_runner = DelegateProcessRunner(
            popen_factory=lambda *args, **kwargs: subprocess.Popen(*args, **kwargs)
        )
        self._history: deque[dict[str, object]] = deque(maxlen=DEFAULT_DELEGATE_HISTORY_LIMIT)
        self._submitted_seq = 0
        self.scheduler = DelegateScheduler(
            runner=self._run_scheduled_task,
            terminator=self._process_runner.cancel,
            on_terminal=self._on_task_terminal,
            cancelled_result_factory=self._cancelled_result,
            max_explore_per_project=max_explore_per_project,
            max_explore_global=max_explore_global,
            max_code_per_project=max_code_per_project,
            max_code_global=max_code_global,
            queue_limit_per_project=queue_limit_per_project,
            queue_limit_global=queue_limit_global,
        )
        # Kept for callers/tests that used the old registry synchronization hook.
        self._lock = self.scheduler.lock

    def harness_info(self) -> dict[str, dict[str, object]]:
        result: dict[str, dict[str, object]] = {}
        for name in sorted(self.harnesses):
            _, adapter = self._resolve_harness(name)
            if adapter is not None:
                result[name] = adapter.info()
        return result

    def _resolve_harness(self, harness: str | None) -> tuple[str, DelegateHarness | None]:
        name = (harness or self.default_harness).strip().lower()
        adapter = self.harnesses.get(name)
        if name == "codex" and (
            adapter is None or adapter.command != self.codex_command
        ):
            adapter = CodexHarness(
                command=self.codex_command,
                allow_unsafe_explore_command=self.allow_unsafe_explore_command,
            )
            self.harnesses[name] = adapter
        elif name == "pi" and self.pi_command is not None and (
            adapter is None or adapter.command != self.pi_command
        ):
            adapter = PiHarness(command=self.pi_command)
            self.harnesses[name] = adapter
        return name, adapter

    @property
    def _active(self) -> DelegateTask | None:
        active = self.scheduler.nonterminal_tasks()
        return active[0] if len(active) == 1 else None

    def run_delegate(
        self,
        *,
        task: str | None,
        goal: str | None = None,
        task_id: str | None = None,
        cwd: Path,
        timeout: int | None = None,
        wait_seconds: float = DEFAULT_DELEGATE_WAIT_SECONDS,
        execution_timeout_seconds: int | None = None,
        harness: str | None = None,
        kind: TaskKind = "code",
        group_id: str | None = None,
        depends_on_group_ids: list[str] | None = None,
        files_in_scope: list[str] | None = None,
        out_of_scope: list[str] | None = None,
        context_files: list[str] | None = None,
        acceptance_criteria: list[str] | None = None,
        done_means: list[str] | None = None,
        verification_commands: list[str] | None = None,
        commit_mode: str = "allowed",
        model: str | None = None,
        reasoning_effort: str | None = None,
        output_schema: dict[str, object] | None = None,
        parse_structured_output: bool = True,
    ) -> dict[str, object]:
        harness_name, adapter = self._resolve_harness(harness)
        if adapter is None:
            return self._argument_error(
                cwd=cwd,
                timeout=int(timeout or execution_timeout_seconds or 0),
                code="unsupported_delegate_harness",
                message=f"Unsupported delegate harness: {harness_name}",
                details={"available_harnesses": sorted(self.harnesses)},
                harness=harness_name,
            )
        normalized_task = (task or "").strip()
        normalized_goal = (goal or "").strip()
        normalized_task_id = (task_id or "").strip() or None
        if not normalized_task and not normalized_goal:
            active = self.scheduler.nonterminal_tasks()
            if len(active) == 1:
                return self._wait_for_task(active[0], wait_seconds=wait_seconds, attached=True)
            if len(active) > 1:
                return self._argument_error(
                    cwd=cwd,
                    timeout=int(timeout or 0),
                    code="ambiguous_delegate",
                    message="Multiple delegates are active; provide delegate_id or group_id.",
                )
            return self._argument_error(
                cwd=cwd,
                timeout=int(timeout or 0),
                code="missing_task_or_goal",
                message="delegate_task requires task or goal when no delegate is already running.",
            )

        validation = self._validate_task_arguments(
            cwd=cwd,
            kind=kind,
            commit_mode=commit_mode,
            reasoning_effort=reasoning_effort,
            timeout=int(timeout or execution_timeout_seconds or 0),
        )
        if validation is not None:
            return validation
        if kind == "explore" and not adapter.supports_read_only():
            return self._argument_error(
                cwd=cwd,
                timeout=int(timeout or execution_timeout_seconds or 0),
                code="readonly_sandbox_unavailable",
                message=(
                    f"Explore tasks require harness {harness_name!r} to enforce read-only execution."
                ),
                harness=harness_name,
            )
        project = self.project_resolver.resolve(cwd)
        dependencies = tuple(dict.fromkeys(depends_on_group_ids or []))
        dependency_error = self._validate_dependencies(project, dependencies)
        if dependency_error is not None:
            return dependency_error

        effective_model, effective_reasoning, effective_commit, sandbox_mode = self._task_defaults(
            adapter=adapter,
            kind=kind,
            model=model,
            reasoning_effort=reasoning_effort,
            commit_mode=commit_mode,
        )
        execution_timeout = int(
            execution_timeout_seconds
            or timeout
            or (
                self.explore_execution_timeout_seconds
                if kind == "explore"
                else self.code_execution_timeout_seconds
            )
        )
        fingerprint = _delegate_request_fingerprint(
            task=normalized_task or None,
            goal=normalized_goal or None,
            task_id=normalized_task_id,
            cwd=cwd,
            harness=harness_name,
            kind=kind,
            group_id=group_id,
            files_in_scope=files_in_scope,
            out_of_scope=out_of_scope,
            context_files=context_files,
            acceptance_criteria=acceptance_criteria,
            done_means=done_means,
            verification_commands=verification_commands,
            commit_mode=effective_commit,
            model=effective_model,
            reasoning_effort=effective_reasoning,
            output_schema=output_schema,
            parse_structured_output=parse_structured_output,
            depends_on_group_ids=list(dependencies),
        )
        matching: DelegateTask | None = None
        with self._lock:
            target_group = None
            if group_id:
                target_group = self.scheduler.groups.get(group_id)
                if target_group is None:
                    return self._argument_error(
                        cwd=cwd,
                        timeout=execution_timeout,
                        code="group_not_found",
                        message=f"Delegate group not found: {group_id}",
                    )
                if (
                    kind != "explore"
                    or target_group.project.project_key != project.project_key
                    or target_group.harness != harness_name
                ):
                    return self._argument_error(
                        cwd=cwd,
                        timeout=execution_timeout,
                        code="invalid_delegate_group",
                        message="Only explore tasks using the group's project and harness may join it.",
                    )
                if target_group.completed_event.is_set():
                    return self._argument_error(
                        cwd=cwd,
                        timeout=execution_timeout,
                        code="delegate_group_completed",
                        message=f"Delegate group is already complete: {group_id}",
                    )
            matching = next(
                (
                    item
                    for item in self.scheduler.tasks.values()
                    if not item.is_terminal and item.request_fingerprint == fingerprint
                ),
                None,
            )
            if matching is None and not _command_available(adapter.command_for(kind)):
                return self._argument_error(
                    cwd=cwd,
                    timeout=execution_timeout,
                    code=(
                        "codex_unavailable"
                        if harness_name == "codex"
                        else "delegate_harness_unavailable"
                    ),
                    message=f"Delegate harness command is not available: {harness_name}",
                    details={"harness": harness_name, "command": adapter.command_for(kind)},
                    harness=harness_name,
                )
            if matching is None:
                delegate = self._make_task(
                    harness=harness_name,
                    project=project,
                    cwd=cwd,
                    kind=kind,
                    task=normalized_task or None,
                    goal=normalized_goal or None,
                    task_id=normalized_task_id,
                    group_id=group_id,
                    model=effective_model,
                    reasoning_effort=effective_reasoning,
                    sandbox_mode=sandbox_mode,
                    commit_mode=effective_commit,
                    execution_timeout_seconds=execution_timeout,
                    depends_on_group_ids=dependencies,
                    files_in_scope=files_in_scope or [],
                    out_of_scope=out_of_scope or [],
                    context_files=context_files or [],
                    acceptance_criteria=acceptance_criteria or [],
                    done_means=done_means or [],
                    verification_commands=verification_commands or [],
                    output_schema=output_schema,
                    parse_structured_output=parse_structured_output,
                    request_fingerprint=fingerprint,
                )
                if target_group is not None:
                    target_group.child_ids.append(delegate.delegate_id)
                try:
                    self.scheduler.submit_task(delegate)
                except DelegateQueueFullError as exc:
                    if target_group is not None:
                        target_group.child_ids.remove(delegate.delegate_id)
                    return self._argument_error(
                        cwd=cwd,
                        timeout=execution_timeout,
                        code="delegate_queue_full",
                        message=str(exc),
                        details={"scope": exc.scope, "limit": exc.limit},
                    )
        if matching is not None:
            return self._wait_for_task(matching, wait_seconds=wait_seconds, attached=True)
        return self._wait_for_task(delegate, wait_seconds=wait_seconds, attached=False)

    def run_codex(self, **kwargs: object) -> dict[str, object]:
        """Backward-compatible entry point pinned to the Codex harness."""
        kwargs["harness"] = "codex"
        return self.run_delegate(**kwargs)  # type: ignore[arg-type]

    def run_delegate_batch(
        self,
        *,
        tasks: list[dict[str, object]],
        cwd: Path,
        harness: str | None = None,
        max_concurrency: int | None = None,
        wait_seconds: float = DEFAULT_DELEGATE_WAIT_SECONDS,
        execution_timeout_seconds: int | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, object]:
        harness_name, adapter = self._resolve_harness(harness)
        if adapter is None:
            return self._argument_error(
                cwd=cwd,
                timeout=int(execution_timeout_seconds or self.explore_execution_timeout_seconds),
                code="unsupported_delegate_harness",
                message=f"Unsupported delegate harness: {harness_name}",
                details={"available_harnesses": sorted(self.harnesses)},
                harness=harness_name,
            )
        if not tasks:
            return self._argument_error(
                cwd=cwd,
                timeout=int(execution_timeout_seconds or self.explore_execution_timeout_seconds),
                code="empty_delegate_batch",
                message="delegate_batch requires at least one exploration task.",
            )
        if max_concurrency is not None and max_concurrency <= 0:
            return self._argument_error(
                cwd=cwd,
                timeout=int(execution_timeout_seconds or self.explore_execution_timeout_seconds),
                code="invalid_max_concurrency",
                message="max_concurrency must be a positive integer.",
            )
        normalized_batch_reasoning = _normalize_reasoning_effort(reasoning_effort)
        if (
            normalized_batch_reasoning is not None
            and normalized_batch_reasoning not in ALLOWED_REASONING_EFFORTS
        ):
            return self._argument_error(
                cwd=cwd,
                timeout=int(execution_timeout_seconds or self.explore_execution_timeout_seconds),
                code="unsupported_reasoning_effort",
                message=f"Unsupported reasoning_effort: {reasoning_effort}",
                details={
                    "allowed_reasoning_efforts": [
                        DEFAULT_REASONING_EFFORT,
                        *ALLOWED_REASONING_EFFORTS,
                    ]
                },
            )
        cwd_error = self._cwd_error(cwd)
        if cwd_error is not None:
            return cwd_error
        if not _command_available(adapter.command_for("explore")):
            return self._argument_error(
                cwd=cwd,
                timeout=int(execution_timeout_seconds or self.explore_execution_timeout_seconds),
                code=(
                    "codex_unavailable"
                    if harness_name == "codex"
                    else "delegate_harness_unavailable"
                ),
                message=f"Delegate harness command is not available: {harness_name}",
                details={
                    "harness": harness_name,
                    "command": adapter.command_for("explore"),
                },
                harness=harness_name,
            )
        if not adapter.supports_read_only():
            return self._argument_error(
                cwd=cwd,
                timeout=int(execution_timeout_seconds or self.explore_execution_timeout_seconds),
                code="readonly_sandbox_unavailable",
                message=(
                    f"Explore batches require harness {harness_name!r} to enforce read-only execution."
                ),
                harness=harness_name,
            )
        project = self.project_resolver.resolve(cwd)
        effective_model, effective_reasoning, commit_mode, sandbox_mode = self._task_defaults(
            adapter=adapter,
            kind="explore",
            model=model,
            reasoning_effort=reasoning_effort,
            commit_mode="forbidden",
        )
        execution_timeout = int(
            execution_timeout_seconds or self.explore_execution_timeout_seconds
        )
        group_id = f"grp-{uuid.uuid4().hex[:12]}"
        children: list[DelegateTask] = []
        with self._lock:
            for index, spec in enumerate(tasks):
                task_text = str(spec.get("task") or "").strip()
                goal_text = str(spec.get("goal") or "").strip()
                if not task_text and not goal_text:
                    return self._argument_error(
                        cwd=cwd,
                        timeout=execution_timeout,
                        code="invalid_batch_task",
                        message=f"Batch task at index {index} requires task or goal.",
                        details={"index": index},
                    )
                child_reasoning = (
                    str(spec.get("reasoning_effort"))
                    if spec.get("reasoning_effort") is not None
                    else effective_reasoning
                )
                normalized_child_reasoning = _normalize_reasoning_effort(child_reasoning)
                if (
                    normalized_child_reasoning is not None
                    and normalized_child_reasoning not in ALLOWED_REASONING_EFFORTS
                ):
                    return self._argument_error(
                        cwd=cwd,
                        timeout=execution_timeout,
                        code="unsupported_reasoning_effort",
                        message=f"Unsupported reasoning_effort in batch task {index}: {child_reasoning}",
                        details={"index": index},
                    )
                task_model, task_reasoning, _, _ = self._task_defaults(
                    adapter=adapter,
                    kind="explore",
                    model=str(spec.get("model")) if spec.get("model") is not None else effective_model,
                    reasoning_effort=child_reasoning,
                    commit_mode="forbidden",
                )
                task_id = str(spec.get("task_id") or "").strip() or None
                scopes = self._batch_lists(spec)
                fingerprint = _delegate_request_fingerprint(
                    task=task_text or None,
                    goal=goal_text or None,
                    task_id=task_id,
                    cwd=cwd,
                    harness=harness_name,
                    kind="explore",
                    group_id=group_id,
                    files_in_scope=scopes["files_in_scope"],
                    out_of_scope=scopes["out_of_scope"],
                    context_files=scopes["context_files"],
                    acceptance_criteria=scopes["acceptance_criteria"],
                    done_means=scopes["done_means"],
                    verification_commands=scopes["verification_commands"],
                    commit_mode=commit_mode,
                    model=task_model,
                    reasoning_effort=task_reasoning,
                    output_schema=spec.get("output_schema") if isinstance(spec.get("output_schema"), dict) else None,
                    parse_structured_output=bool(spec.get("parse_structured_output", True)),
                    depends_on_group_ids=None,
                )
                children.append(
                    self._make_task(
                        harness=harness_name,
                        project=project,
                        cwd=cwd,
                        kind="explore",
                        task=task_text or None,
                        goal=goal_text or None,
                        task_id=task_id,
                        group_id=group_id,
                        model=task_model,
                        reasoning_effort=task_reasoning,
                        sandbox_mode=sandbox_mode,
                        commit_mode=commit_mode,
                        execution_timeout_seconds=execution_timeout,
                        depends_on_group_ids=(),
                        files_in_scope=scopes["files_in_scope"],
                        out_of_scope=scopes["out_of_scope"],
                        context_files=scopes["context_files"],
                        acceptance_criteria=scopes["acceptance_criteria"],
                        done_means=scopes["done_means"],
                        verification_commands=scopes["verification_commands"],
                        output_schema=(
                            spec.get("output_schema")
                            if isinstance(spec.get("output_schema"), dict)
                            else None
                        ),
                        parse_structured_output=bool(spec.get("parse_structured_output", True)),
                        request_fingerprint=fingerprint,
                    )
                )
            group = DelegateGroup(
                group_id=group_id,
                harness=harness_name,
                project=project,
                kind="explore_batch",
                child_ids=[child.delegate_id for child in children],
                submitted_at=time.time(),
                max_concurrency=(
                    min(max_concurrency, self.scheduler.max_explore_per_project)
                    if max_concurrency is not None
                    else None
                ),
            )
            try:
                self.scheduler.submit_group(group, children)
            except DelegateQueueFullError as exc:
                return self._argument_error(
                    cwd=cwd,
                    timeout=execution_timeout,
                    code="delegate_queue_full",
                    message=str(exc),
                    details={"scope": exc.scope, "limit": exc.limit},
                )
        group.completed_event.wait(timeout=max(0.0, float(wait_seconds)))
        return self._group_snapshot(group, include_results=group.completed_event.is_set())

    def run_codex_batch(self, **kwargs: object) -> dict[str, object]:
        """Backward-compatible batch entry point pinned to the Codex harness."""
        kwargs["harness"] = "codex"
        return self.run_delegate_batch(**kwargs)  # type: ignore[arg-type]

    def delegate_cancel(
        self,
        *,
        delegate_id: str | None = None,
        group_id: str | None = None,
    ) -> dict[str, object]:
        if bool(delegate_id) == bool(group_id):
            return {
                "success": False,
                "error": {
                    "code": "invalid_cancel_filter",
                    "message": "Provide exactly one of delegate_id or group_id.",
                },
            }
        if delegate_id:
            task = self.scheduler.cancel_task(delegate_id.strip())
            if task is None:
                return self._not_found("delegate", delegate_id.strip())
            if not task.is_terminal:
                task.completed_event.wait(timeout=task.cancel_grace_seconds + 1)
            return {"success": True, "delegate": self._task_snapshot(task)}
        normalized_group_id = (group_id or "").strip()
        group = self.scheduler.cancel_group(normalized_group_id)
        if group is None:
            return self._not_found("group", normalized_group_id)
        group.completed_event.wait(timeout=self.cancel_grace_seconds + 1)
        return {"success": True, "group": self._group_snapshot(group, include_results=True)}

    def delegate_status(
        self,
        *,
        delegate_id: str | None = None,
        group_id: str | None = None,
        project_cwd: str | Path | None = None,
        limit: int = 10,
        offset: int = 0,
        watch_seconds: float = 0.0,
        poll_seconds: float = DEFAULT_DELEGATE_STATUS_POLL_SECONDS,
        max_tokens: int = DEFAULT_TOOL_OUTPUT_TOKEN_BUDGET,
    ) -> dict[str, object]:
        selected = sum(bool(value) for value in (delegate_id, group_id, project_cwd))
        if selected > 1:
            return {
                "success": False,
                "error": {
                    "code": "invalid_status_filter",
                    "message": "Choose at most one of delegate_id, group_id, or project_cwd.",
                },
            }
        watch_seconds = max(0.0, min(float(watch_seconds), MAX_DELEGATE_STATUS_WATCH_SECONDS))
        poll_seconds = max(0.1, min(float(poll_seconds), 60.0))

        def status_once() -> dict[str, object]:
            if group_id or project_cwd or offset:
                return self._delegate_status_once(
                    delegate_id=delegate_id,
                    group_id=group_id,
                    project_cwd=project_cwd,
                    limit=limit,
                    offset=offset,
                )
            # Preserve the old private hook signature used by local callers.
            return self._delegate_status_once(delegate_id=delegate_id, limit=limit)

        initial = status_once()
        if watch_seconds <= 0:
            return self._budget_delegate_status(initial, max_tokens=max_tokens, offset=offset)
        started_at = time.monotonic()
        if initial.get("success") is False:
            initial["watch"] = self._watch_payload(
                started_at, watch_seconds, poll_seconds, error_returned=True
            )
            return self._budget_delegate_status(initial, max_tokens=max_tokens, offset=offset)
        focus = _status_focus_entry(initial)
        if focus and focus.get("completed"):
            initial["watch"] = self._watch_payload(
                started_at, watch_seconds, poll_seconds, already_terminal=True
            )
            return self._budget_delegate_status(initial, max_tokens=max_tokens, offset=offset)
        signature = _status_response_lifecycle_signature(initial)
        deadline = started_at + watch_seconds
        current = initial
        while time.monotonic() < deadline:
            time.sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))
            current = status_once()
            if _status_response_lifecycle_signature(current) != signature:
                current["watch"] = self._watch_payload(
                    started_at, watch_seconds, poll_seconds, status_changed=True
                )
                return self._budget_delegate_status(current, max_tokens=max_tokens, offset=offset)
        current["watch"] = self._watch_payload(
            started_at, watch_seconds, poll_seconds, timed_out=True
        )
        return self._budget_delegate_status(current, max_tokens=max_tokens, offset=offset)

    def _delegate_status_once(
        self,
        *,
        delegate_id: str | None,
        limit: int,
        group_id: str | None = None,
        project_cwd: str | Path | None = None,
        offset: int = 0,
    ) -> dict[str, object]:
        limit = max(1, min(int(limit), DEFAULT_DELEGATE_HISTORY_LIMIT))
        offset = max(0, int(offset))
        if delegate_id:
            task = self.scheduler.get_task(delegate_id.strip())
            if task is None:
                return self._not_found("delegate", delegate_id.strip())
            return {"success": True, "delegate": self._task_snapshot(task)}
        if group_id:
            group = self.scheduler.get_group(group_id.strip())
            if group is None:
                return self._not_found("group", group_id.strip())
            return {"success": True, "group": self._group_snapshot(group, include_results=False)}
        if project_cwd:
            project = self.project_resolver.resolve(Path(project_cwd))
            tasks = self.scheduler.tasks_for_project(project.project_key)
            counts = self.scheduler.task_counts(tasks)
            active = [self._task_snapshot(task) for task in tasks if not task.is_terminal]
            return {
                "success": True,
                "project": {
                    **project.as_payload(),
                    "status": "running" if active else "idle",
                    "completed": not active,
                    "in_progress": bool(active),
                    "counts": counts,
                    "active": active,
                },
            }

        active_tasks = sorted(
            self.scheduler.nonterminal_tasks(), key=lambda item: item.submitted_seq
        )
        active_snapshots = [self._task_snapshot(task) for task in active_tasks]
        with self._lock:
            history = list(reversed(self._history))
        all_recent = [*active_snapshots]
        seen = {str(item.get("delegate_id")) for item in all_recent}
        all_recent.extend(
            item for item in history if str(item.get("delegate_id")) not in seen
        )
        recent = all_recent[offset : offset + limit]
        return {
            "success": True,
            "active": active_snapshots[0] if active_snapshots else None,
            "active_delegates": active_snapshots,
            "latest": all_recent[0] if all_recent else None,
            "recent": recent,
            "history_limit": DEFAULT_DELEGATE_HISTORY_LIMIT,
            "truncated": offset + len(recent) < len(all_recent),
            "next_offset": offset + len(recent) if offset + len(recent) < len(all_recent) else None,
        }

    def _make_task(
        self,
        *,
        harness: str,
        project: ProjectIdentity,
        cwd: Path,
        kind: TaskKind,
        task: str | None,
        goal: str | None,
        task_id: str | None,
        group_id: str | None,
        model: str,
        reasoning_effort: str,
        sandbox_mode: str,
        commit_mode: str,
        execution_timeout_seconds: int,
        depends_on_group_ids: tuple[str, ...],
        files_in_scope: list[str],
        out_of_scope: list[str],
        context_files: list[str],
        acceptance_criteria: list[str],
        done_means: list[str],
        verification_commands: list[str],
        output_schema: dict[str, object] | None,
        parse_structured_output: bool,
        request_fingerprint: str,
    ) -> DelegateTask:
        self._submitted_seq += 1
        delegate_id = uuid.uuid4().hex[:12]
        log_paths = _create_delegate_logs(delegate_id, harness=harness)
        prompt = self._build_prompt(
            harness=harness,
            task=task,
            goal=goal,
            task_id=task_id,
            files_in_scope=files_in_scope,
            out_of_scope=out_of_scope,
            context_files=context_files,
            acceptance_criteria=acceptance_criteria,
            done_means=done_means,
            verification_commands=verification_commands,
            commit_mode=commit_mode,
            kind=kind,
        )
        write_private_text(log_paths.prompt, prompt)
        log_paths.stdout.touch()
        log_paths.stderr.touch()
        safe_chmod(log_paths.stdout, 0o600)
        safe_chmod(log_paths.stderr, 0o600)
        delegate = DelegateTask(
            delegate_id=delegate_id,
            harness=harness,
            project=project,
            kind=kind,
            cwd=cwd,
            task=task,
            goal=goal,
            task_id=task_id,
            group_id=group_id,
            model=model,
            reasoning_effort=reasoning_effort,
            sandbox_mode=sandbox_mode,
            commit_mode=commit_mode,
            execution_timeout_seconds=execution_timeout_seconds,
            cancel_grace_seconds=self.cancel_grace_seconds,
            request_fingerprint=request_fingerprint,
            prompt=prompt,
            log_paths=log_paths,
            output_schema=output_schema,
            parse_structured_output=parse_structured_output,
            depends_on_group_ids=depends_on_group_ids,
            submitted_seq=self._submitted_seq,
        )
        write_private_json(
            log_paths.metadata,
            {
                "delegate_id": delegate_id,
                "executor": harness,
                "harness": harness,
                "group_id": group_id,
                "status": "queued",
                "kind": kind,
                "lane": delegate.lane,
                "cwd": str(cwd),
                "project": project.as_payload(),
                "sandbox_mode": sandbox_mode,
                "commit_mode": commit_mode,
                "model": model,
                "reasoning_effort": reasoning_effort,
                "execution_timeout_seconds": execution_timeout_seconds,
                "task_id": task_id,
                "request_fingerprint": request_fingerprint,
                "depends_on_group_ids": list(depends_on_group_ids),
                "submitted_at_epoch": delegate.submitted_at,
            },
        )
        return delegate

    def _run_scheduled_task(self, task: DelegateTask) -> dict[str, object]:
        return self._start_codex_delegate_impl(delegate_task=task)

    def _start_codex_delegate_impl(self, *, delegate_task: DelegateTask) -> dict[str, object]:
        """Legacy hook retained for callers that instrument delegate execution."""
        return self._start_delegate_impl(delegate_task=delegate_task)

    def _start_delegate_impl(self, *, delegate_task: DelegateTask) -> dict[str, object]:
        return self._process_runner.run(
            delegate_task,
            invocation_builder=self._build_invocation_for_task,
        )

    def _build_invocation_for_task(self, task: DelegateTask) -> Invocation:
        adapter = self.harnesses.get(task.harness)
        if adapter is None:
            raise OSError(f"Delegate harness is no longer configured: {task.harness}")
        return adapter.build_invocation(task)

    def _build_invocation(
        self,
        *,
        command: str,
        task: str | None,
        goal: str | None,
        task_id: str | None = None,
        cwd: Path,
        files_in_scope: list[str] | None = None,
        out_of_scope: list[str] | None = None,
        context_files: list[str] | None = None,
        acceptance_criteria: list[str] | None = None,
        done_means: list[str] | None = None,
        verification_commands: list[str] | None = None,
        commit_mode: str,
        model: str | None = None,
        reasoning_effort: str | None = None,
        kind: TaskKind = "code",
        prompt: str | None = None,
        is_git: bool | None = None,
    ) -> Invocation:
        effective_prompt = prompt or self._build_prompt(
            task=task,
            goal=goal,
            task_id=task_id,
            files_in_scope=files_in_scope or [],
            out_of_scope=out_of_scope or [],
            context_files=context_files or [],
            acceptance_criteria=acceptance_criteria or [],
            done_means=done_means or [],
            verification_commands=verification_commands or [],
            commit_mode="forbidden" if kind == "explore" else commit_mode,
            kind=kind,
        )
        return self._build_invocation_from_prompt(
            command=command,
            cwd=cwd,
            prompt=effective_prompt,
            model=_normalize_model(model),
            reasoning_effort=_normalize_reasoning_effort(reasoning_effort),
            kind=kind,
            is_git=is_git,
        )

    def _build_invocation_from_prompt(
        self,
        *,
        command: str,
        cwd: Path,
        prompt: str,
        model: str | None = None,
        reasoning_effort: str | None = None,
        kind: TaskKind = "code",
        is_git: bool | None = None,
    ) -> Invocation:
        parts = _resolve_delegate_command_parts(command)
        if parts and _binary_name(parts[0]) == "codex":
            args = [*parts, "exec"]
            if model:
                args.extend(["--model", model])
            if reasoning_effort:
                args.extend(["-c", f"model_reasoning_effort={json.dumps(reasoning_effort)}"])
            if kind == "explore":
                args.extend(["--sandbox", "read-only", "--ephemeral"])
            else:
                args.append("--dangerously-bypass-approvals-and-sandbox")
            args.extend(["-C", str(cwd)])
            git_repo = (cwd / ".git").exists() if is_git is None else is_git
            if not git_repo:
                args.append("--skip-git-repo-check")
            args.append("-")
            return Invocation(args=args, use_shell=False, stdin=prompt.encode("utf-8"))
        return Invocation(args=command, use_shell=True)

    def _build_prompt(
        self,
        *,
        harness: str = "codex",
        task: str | None,
        goal: str | None,
        task_id: str | None = None,
        files_in_scope: list[str] | None = None,
        out_of_scope: list[str] | None = None,
        context_files: list[str],
        acceptance_criteria: list[str],
        done_means: list[str] | None = None,
        verification_commands: list[str],
        commit_mode: str,
        kind: TaskKind = "code",
    ) -> str:
        executor_contract = (
            "- Codex is the local executor for exactly one bounded execution slice."
            if harness == "codex"
            else f"- The {harness} CLI harness is the local executor for exactly one bounded execution slice."
        )
        lines: list[str] = [
            "Architecture contract:",
            "- ChatGPT Web is the architect/manager/reviewer.",
            executor_contract,
            "- Execute only the scoped task below; do not expand into a broad planning or research loop.",
            "- If the task is too broad or underspecified, stop and report blocked with the smallest useful next execution prompt.",
            "",
        ]
        if kind == "explore":
            lines.extend(
                [
                    "Read-only contract:",
                    "- This is a read-only exploration task.",
                    "- Do not create, modify, delete, rename, stage, commit, or format files.",
                    "- Do not run commands whose purpose is to mutate the repository.",
                    "- Return findings and evidence only.",
                    "",
                ]
            )
        if task_id:
            lines.extend(["Task ID:", task_id, ""])
        if goal:
            lines.extend(["Goal:", goal, ""])
        if task:
            lines.extend(["Task:", task, ""])
        for title, values in (
            ("Files in scope:", files_in_scope or []),
            ("Out of scope:", out_of_scope or []),
            ("Acceptance criteria:", acceptance_criteria),
            ("Done means:", done_means or []),
            ("Verification commands:", verification_commands),
        ):
            if values:
                lines.append(title)
                lines.extend(f"- {item}" for item in values)
                lines.append("")
        lines.append(f"Commit mode: {'forbidden' if kind == 'explore' else commit_mode}")
        if context_files:
            lines.extend(["", "Context files:"])
            lines.extend(f"- {path}" for path in context_files)
        lines.extend(
            [
                "",
                "Progress logging contract:",
                "- Print concise progress updates to stderr as work advances, especially before long-running commands.",
                "- Keep stdout quiet unless emitting the compact final manifest or requested structured JSON.",
                "- The MCP server persists stdout/stderr to local delegate logs, and the caller may inspect them with read_text while status=running.",
                "",
                "Output contract:",
                "- Return a compact execution manifest: status, files changed, commands run, verification result, deviations or blockers.",
                "- Do not claim done unless the acceptance criteria passed locally, or clearly state which checks were not run.",
                "- Suggest at most one next small execution prompt if more work remains.",
            ]
        )
        return "\n".join(lines)

    def _task_defaults(
        self,
        *,
        adapter: DelegateHarness,
        kind: TaskKind,
        model: str | None,
        reasoning_effort: str | None,
        commit_mode: str,
    ) -> tuple[str, str, str, str]:
        normalized_model = _normalize_model(model)
        normalized_reasoning = _normalize_reasoning_effort(reasoning_effort)
        defaults = adapter.task_defaults(kind)
        if kind == "explore":
            return (
                normalized_model or defaults.model,
                normalized_reasoning or defaults.reasoning_effort,
                "forbidden",
                defaults.sandbox_mode,
            )
        return (
            normalized_model or defaults.model,
            normalized_reasoning or defaults.reasoning_effort,
            commit_mode,
            defaults.sandbox_mode,
        )

    def _explore_command_is_sandboxed(self, harness: str = "codex") -> bool:
        adapter = self.harnesses.get(harness)
        return bool(adapter and adapter.supports_read_only())

    def _task_snapshot(self, task: DelegateTask) -> dict[str, object]:
        if task.result is not None and task.is_terminal:
            return dict(task.result)
        with task.output_lock:
            stdout_bytes = task.stdout_bytes
            stderr_bytes = task.stderr_bytes
            last_output_at = task.last_output_at
            process = task.process
        now = time.monotonic()
        reference = last_output_at or task.started_monotonic
        quiet_seconds = round(now - reference, 3) if reference is not None else None
        activity_state = "queued" if task.state == "queued" else "starting_or_quiet"
        if task.state == "running" and last_output_at is not None:
            activity_state = "active"
        if quiet_seconds is not None and quiet_seconds >= DELEGATE_STALL_HINT_SECONDS:
            activity_state = "suspected_stalled"
        payload: dict[str, object] = {
            "success": True,
            "status": task.state,
            "completed": False,
            "in_progress": True,
            "executor": task.harness,
            "harness": task.harness,
            "cwd": str(task.cwd),
            "delegate_id": task.delegate_id,
            "task_id": task.task_id,
            "group_id": task.group_id,
            "request_fingerprint": task.request_fingerprint,
            "kind": task.kind,
            "lane": task.lane,
            "concurrency_scope": "project",
            "serial": task.serial,
            "sandbox_mode": task.sandbox_mode,
            "commit_mode": task.commit_mode,
            "project": task.project.as_payload(),
            "model": task.model,
            "reasoning_effort": task.reasoning_effort,
            "submitted_at": _format_epoch_seconds(task.submitted_at),
            "submitted_at_epoch": task.submitted_at,
            "started_at": _format_epoch_seconds(task.started_at) if task.started_at else None,
            "started_at_epoch": task.started_at,
            "elapsed_seconds": round(
                now - (task.started_monotonic or now), 3
            ),
            "pid": getattr(process, "pid", None),
            "activity_state": activity_state,
            "last_output_seconds_ago": quiet_seconds,
            "stdout_bytes": stdout_bytes,
            "stderr_bytes": stderr_bytes,
            "logs": task.log_paths.as_payload(),
            "log_read_hint": log_read_hint(task),
            "output_omitted": True,
            "timed_out": False,
            "wait_timed_out": False,
            "timeout": task.execution_timeout_seconds,
            "execution_timeout_seconds": task.execution_timeout_seconds,
            "depends_on_group_ids": list(task.depends_on_group_ids),
        }
        return payload

    def _wait_for_task(
        self,
        task: DelegateTask,
        *,
        wait_seconds: float,
        attached: bool,
    ) -> dict[str, object]:
        task.completed_event.wait(timeout=max(0.0, float(wait_seconds)))
        if task.result is not None and task.is_terminal:
            return task.result
        payload = self._task_snapshot(task)
        payload.update(
            {
                "attached_to_running_delegate": attached,
                "wait_seconds": round(max(0.0, float(wait_seconds)), 3),
                "wait_timed_out": True,
                "message": "CLI delegate has not completed. Use delegate_status with delegate_id.",
                "next": "call delegate_status with this delegate_id",
            }
        )
        return payload

    def _group_snapshot(
        self,
        group: DelegateGroup,
        *,
        include_results: bool,
    ) -> dict[str, object]:
        children = [self.scheduler.get_task(child_id) for child_id in group.child_ids]
        child_tasks = [child for child in children if child is not None]
        counts = self.scheduler.task_counts(child_tasks)
        completed = group.completed_event.is_set()
        child_payloads = [
            {
                "delegate_id": child.delegate_id,
                "task_id": child.task_id,
                "status": child.state,
                "completed": child.is_terminal,
                **(
                    {"result": self._task_snapshot(child)}
                    if include_results and child.is_terminal
                    else {}
                ),
            }
            for child in child_tasks
        ]
        payload: dict[str, object] = {
            "success": group.state == "succeeded" if completed else True,
            "status": group.state,
            "in_progress": not completed,
            "completed": completed,
            "group_id": group.group_id,
            "executor": group.harness,
            "harness": group.harness,
            "kind": group.kind,
            "project": group.project.as_payload(),
            "counts": counts,
            "children": child_payloads,
            "results_ready": completed,
        }
        if completed and group.state == "failed":
            payload["error"] = {
                "code": "delegate_group_failed",
                "message": "One or more exploration delegates did not succeed.",
            }
        return payload

    def _on_task_terminal(self, task: DelegateTask) -> None:
        snapshot = self._task_snapshot(task)
        with self._lock:
            maxlen = self._history.maxlen or DEFAULT_DELEGATE_HISTORY_LIMIT
            self._history = deque(
                (
                    item
                    for item in self._history
                    if item.get("delegate_id") != task.delegate_id
                ),
                maxlen=maxlen,
            )
            self._history.append(snapshot)

    def _cancelled_result(self, task: DelegateTask) -> dict[str, object]:
        result = self._terminal_without_process(
            task,
            status="cancelled",
            error={
                "code": "cancelled",
                "message": (
                    f"Queued {harness_display_name(task.harness)} delegate was cancelled."
                ),
            },
        )
        write_private_json(task.log_paths.metadata, result)
        return result

    def _terminal_without_process(
        self,
        task: DelegateTask,
        *,
        status: str,
        error: dict[str, object],
    ) -> dict[str, object]:
        return {
            "success": False,
            "status": status,
            "completed": True,
            "in_progress": False,
            "executor": task.harness,
            "harness": task.harness,
            "cwd": str(task.cwd),
            "delegate_id": task.delegate_id,
            "group_id": task.group_id,
            "task_id": task.task_id,
            "kind": task.kind,
            "lane": task.lane,
            "concurrency_scope": "project",
            "serial": task.serial,
            "sandbox_mode": task.sandbox_mode,
            "commit_mode": task.commit_mode,
            "project": task.project.as_payload(),
            "logs": task.log_paths.as_payload(),
            "log_read_hint": log_read_hint(task),
            "exit_code": TIMEOUT_EXIT_CODE,
            "summary": error["message"],
            "output_omitted": True,
            "timed_out": False,
            "wait_timed_out": False,
            "timeout": task.execution_timeout_seconds,
            "execution_timeout_seconds": task.execution_timeout_seconds,
            "structured_output": None,
            "output_schema": task.output_schema,
            "request_fingerprint": task.request_fingerprint,
            "model": task.model,
            "reasoning_effort": task.reasoning_effort,
            "error": error,
        }

    def _validate_task_arguments(
        self,
        *,
        cwd: Path,
        kind: str,
        commit_mode: str,
        reasoning_effort: str | None,
        timeout: int,
    ) -> dict[str, object] | None:
        if kind not in ALLOWED_TASK_KINDS:
            return self._argument_error(
                cwd=cwd,
                timeout=timeout,
                code="unsupported_delegate_kind",
                message=f"Unsupported delegate kind: {kind}",
                details={"allowed_kinds": sorted(ALLOWED_TASK_KINDS)},
            )
        if commit_mode not in ALLOWED_COMMIT_MODES:
            return self._argument_error(
                cwd=cwd,
                timeout=timeout,
                code="unsupported_commit_mode",
                message=f"Unsupported commit_mode: {commit_mode}",
                details={"allowed_commit_modes": sorted(ALLOWED_COMMIT_MODES)},
            )
        normalized_reasoning = _normalize_reasoning_effort(reasoning_effort)
        if normalized_reasoning is not None and normalized_reasoning not in ALLOWED_REASONING_EFFORTS:
            return self._argument_error(
                cwd=cwd,
                timeout=timeout,
                code="unsupported_reasoning_effort",
                message=f"Unsupported reasoning_effort: {reasoning_effort}",
                details={
                    "allowed_reasoning_efforts": [
                        DEFAULT_REASONING_EFFORT,
                        *ALLOWED_REASONING_EFFORTS,
                    ]
                },
            )
        return self._cwd_error(cwd)

    def _validate_dependencies(
        self,
        project: ProjectIdentity,
        dependencies: tuple[str, ...],
    ) -> dict[str, object] | None:
        for group_id in dependencies:
            group = self.scheduler.get_group(group_id)
            if group is None:
                return self._argument_error(
                    cwd=project.project_root,
                    timeout=0,
                    code="dependency_group_not_found",
                    message=f"Dependency group not found: {group_id}",
                )
        return None

    def _cwd_error(self, cwd: Path) -> dict[str, object] | None:
        if not cwd.exists():
            return self._argument_error(
                cwd=cwd,
                timeout=0,
                code="cwd_not_found",
                message=f"Working directory not found: {cwd}",
            )
        if not cwd.is_dir():
            return self._argument_error(
                cwd=cwd,
                timeout=0,
                code="cwd_not_directory",
                message=f"Working directory is not a directory: {cwd}",
            )
        return None

    def _argument_error(
        self,
        *,
        cwd: Path,
        timeout: int,
        code: str,
        message: str,
        details: dict[str, object] | None = None,
        harness: str | None = None,
    ) -> dict[str, object]:
        error: dict[str, object] = {"code": code, "message": message}
        if details:
            error.update(details)
        return {
            "success": False,
            "status": "failed",
            "completed": True,
            "in_progress": False,
            "error": error,
            "cwd": str(cwd),
            "executor": harness or self.default_harness,
            "harness": harness or self.default_harness,
            "exit_code": TIMEOUT_EXIT_CODE,
            "summary": message,
            "timed_out": False,
            "wait_timed_out": False,
            "timeout": timeout,
            "serial": True,
        }

    def _not_found(self, kind: str, identifier: str) -> dict[str, object]:
        code = f"{kind}_not_found"
        return {
            "success": False,
            "error": {"code": code, "message": f"{kind.title()} not found: {identifier}"},
        }

    def _batch_lists(self, spec: dict[str, object]) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for key in (
            "files_in_scope",
            "out_of_scope",
            "context_files",
            "acceptance_criteria",
            "done_means",
            "verification_commands",
        ):
            value = spec.get(key)
            result[key] = [str(item) for item in value] if isinstance(value, list) else []
        return result

    def _budget_delegate_status(
        self,
        payload: dict[str, object],
        *,
        max_tokens: int,
        offset: int,
    ) -> dict[str, object]:
        budget = ResponseBudget(max_tokens=max_tokens)
        truncated = bool(payload.get("truncated"))
        rendered, measurement = with_budget_metadata(
            payload,
            budget=budget,
            truncated=truncated,
            stop_reason="limit" if truncated else "end_of_results",
        )
        recent = rendered.get("recent")
        while not measurement.fits and isinstance(recent, list) and recent:
            recent.pop()
            rendered["next_offset"] = offset + len(recent)
            rendered, measurement = with_budget_metadata(
                rendered,
                budget=budget,
                truncated=True,
                stop_reason="token_budget",
            )
            recent = rendered.get("recent")
        return rendered

    def _watch_payload(
        self,
        started_at: float,
        watch_seconds: float,
        poll_seconds: float,
        *,
        status_changed: bool = False,
        timed_out: bool = False,
        error_returned: bool = False,
        already_terminal: bool = False,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "enabled": True,
            "status_changed": status_changed,
            "timed_out": timed_out,
            "watch_seconds": watch_seconds,
            "poll_seconds": poll_seconds,
            "elapsed_seconds": round(time.monotonic() - started_at, 3),
        }
        if error_returned:
            payload["error_returned"] = True
        if already_terminal:
            payload["already_terminal"] = True
        return payload
