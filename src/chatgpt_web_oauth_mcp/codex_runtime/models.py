from __future__ import annotations

from dataclasses import dataclass
import uuid
from typing import Any, Literal, cast


SandboxMode = Literal["read-only", "workspace-write", "full-access"]
SANDBOX_MODES: tuple[SandboxMode, ...] = ("read-only", "workspace-write", "full-access")
_SANDBOX_POLICY_TYPES = {
    "read-only": "readOnly",
    "workspace-write": "workspaceWrite",
    "full-access": "dangerFullAccess",
}
_THREAD_SANDBOX_MODES = {
    "read-only": "read-only",
    "workspace-write": "workspace-write",
    "full-access": "danger-full-access",
}
_RUNTIME_STATUSES = {"ready", "detached", "error"}


def validate_sandbox(value: object) -> SandboxMode:
    if value not in SANDBOX_MODES:
        allowed = ", ".join(SANDBOX_MODES)
        raise ValueError(f"sandbox must be one of: {allowed}.")
    return cast(SandboxMode, value)


def sandbox_policy(mode: SandboxMode) -> dict[str, object]:
    policy_type = _SANDBOX_POLICY_TYPES[mode]
    if mode == "read-only":
        return {"type": policy_type, "networkAccess": False}
    if mode == "workspace-write":
        return {"type": policy_type, "networkAccess": False}
    return {"type": policy_type}


def thread_sandbox(mode: SandboxMode) -> str:
    """Translate the public runtime sandbox name to Codex thread/start syntax."""
    return _THREAD_SANDBOX_MODES[mode]


def sandbox_matches(mode: SandboxMode, value: object) -> bool:
    if not isinstance(value, dict):
        return False
    return value.get("type") == _SANDBOX_POLICY_TYPES[mode]


@dataclass
class RuntimeBinding:
    runtime_id: str
    thread_id: str
    cwd: str
    sandbox: SandboxMode
    name: str | None
    created_at: float
    last_used_at: float
    status: str = "detached"
    mcp_server_count: int | None = None

    def __post_init__(self) -> None:
        if self.status not in _RUNTIME_STATUSES:
            raise ValueError(f"Unknown runtime status: {self.status!r}.")
        self.sandbox = validate_sandbox(self.sandbox)

    def to_dict(self) -> dict[str, Any]:
        return {
            "runtime_id": self.runtime_id,
            "thread_id": self.thread_id,
            "cwd": self.cwd,
            "sandbox": self.sandbox,
            "name": self.name,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "status": self.status,
            "mcp_server_count": self.mcp_server_count,
        }

    @classmethod
    def from_dict(cls, value: object) -> RuntimeBinding:
        if not isinstance(value, dict):
            raise ValueError("runtime binding must be a JSON object.")
        required = ("runtime_id", "thread_id", "cwd", "sandbox", "created_at", "last_used_at")
        missing = [key for key in required if key not in value]
        if missing:
            raise ValueError(f"runtime binding is missing fields: {', '.join(missing)}.")
        runtime_id = value["runtime_id"]
        thread_id = value["thread_id"]
        cwd = value["cwd"]
        name = value.get("name")
        if not all(isinstance(item, str) and item.strip() for item in (runtime_id, thread_id, cwd)):
            raise ValueError("runtime_id, thread_id, and cwd must be non-empty strings.")
        if name is not None and not isinstance(name, str):
            raise ValueError("runtime name must be a string or null.")
        try:
            created_at = float(value["created_at"])
            last_used_at = float(value["last_used_at"])
        except (TypeError, ValueError):
            raise ValueError("runtime timestamps must be numbers.") from None
        mcp_server_count = value.get("mcp_server_count")
        if mcp_server_count is not None:
            if not isinstance(mcp_server_count, int) or mcp_server_count < 0:
                raise ValueError("mcp_server_count must be a non-negative integer or null.")
        status = value.get("status", "detached")
        if status in {"ready", "error"}:
            status = "detached"
        return cls(
            runtime_id=runtime_id,
            thread_id=thread_id,
            cwd=cwd,
            sandbox=validate_sandbox(value["sandbox"]),
            name=name.strip() if isinstance(name, str) and name.strip() else None,
            created_at=created_at,
            last_used_at=last_used_at,
            status=status,
            mcp_server_count=mcp_server_count,
        )


def new_runtime_id() -> str:
    return f"rt_{uuid.uuid4().hex}"


def runtime_public_dict(binding: RuntimeBinding) -> dict[str, Any]:
    return binding.to_dict()
