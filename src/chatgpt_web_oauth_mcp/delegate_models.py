from __future__ import annotations

import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


TaskKind = Literal["explore", "code"]
TaskState = Literal[
    "queued",
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "timed_out",
]

TERMINAL_TASK_STATES = frozenset({"succeeded", "failed", "cancelled", "timed_out"})


@dataclass(frozen=True)
class ProjectIdentity:
    """Server-resolved identity used to serialize work across Git worktrees."""

    project_key: str
    project_root: Path
    git_common_dir: Path | None

    def as_payload(self) -> dict[str, object]:
        return {
            "project_key": self.project_key,
            "root": str(self.project_root),
            "git_common_dir": str(self.git_common_dir) if self.git_common_dir else None,
        }


@dataclass(frozen=True)
class DelegateLogPaths:
    log_dir: Path
    prompt: Path
    stdout: Path
    stderr: Path
    metadata: Path

    def as_payload(self) -> dict[str, str]:
        return {
            "log_dir": str(self.log_dir),
            "prompt": str(self.prompt),
            "stdout": str(self.stdout),
            "stderr": str(self.stderr),
            "metadata": str(self.metadata),
        }


@dataclass
class DelegateTask:
    delegate_id: str
    harness: str
    project: ProjectIdentity
    kind: TaskKind
    cwd: Path

    task: str | None
    goal: str | None
    task_id: str | None
    group_id: str | None

    model: str
    reasoning_effort: str
    sandbox_mode: str
    commit_mode: str

    execution_timeout_seconds: int
    cancel_grace_seconds: float
    request_fingerprint: str
    prompt: str
    log_paths: DelegateLogPaths
    output_schema: dict[str, object] | None
    parse_structured_output: bool
    depends_on_group_ids: tuple[str, ...] = ()

    state: TaskState = "queued"
    submitted_seq: int = 0
    submitted_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    started_monotonic: float | None = None
    process: subprocess.Popen[bytes] | None = None
    completed_event: threading.Event = field(default_factory=threading.Event)
    result: dict[str, object] | None = None
    cancel_requested: bool = False

    output_lock: threading.Lock = field(default_factory=threading.Lock)
    stdout_chunks: list[bytes] = field(default_factory=list)
    stderr_chunks: list[bytes] = field(default_factory=list)
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    last_output_at: float | None = None

    @property
    def lane(self) -> str:
        return "reader" if self.kind == "explore" else "writer"

    @property
    def serial(self) -> bool:
        return self.kind == "code"

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_TASK_STATES


@dataclass
class DelegateGroup:
    group_id: str
    harness: str
    project: ProjectIdentity
    kind: Literal["explore_batch"]
    child_ids: list[str]
    submitted_at: float
    completed_event: threading.Event = field(default_factory=threading.Event)
    state: TaskState = "queued"
    max_concurrency: int | None = None


@dataclass
class ProjectLane:
    project: ProjectIdentity
    pending: deque[str] = field(default_factory=deque)
    active_explores: dict[str, DelegateTask] = field(default_factory=dict)
    active_code: DelegateTask | None = None
