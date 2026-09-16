from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class CodexRuntimeError(RuntimeError):
    """A safe, machine-readable runtime failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: Mapping[str, Any] | None = None,
        side_effects: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = dict(details or {})
        self.side_effects = dict(side_effects or {})
        super().__init__(message)


class BindingStoreError(CodexRuntimeError):
    def __init__(self, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__("binding_store_unavailable", message, retryable=True, details=details)


class AppServerUnavailableError(CodexRuntimeError):
    def __init__(self, message: str = "Codex App Server is unavailable.") -> None:
        super().__init__("codex_app_server_unavailable", message, retryable=True)


class AppServerCapabilityError(CodexRuntimeError):
    def __init__(self, method: str) -> None:
        super().__init__(
            "codex_capability_missing",
            f"Codex App Server does not support required method: {method}.",
            details={"method": method},
        )


class AppServerRequestTimeoutError(CodexRuntimeError):
    def __init__(self, method: str, timeout_seconds: float) -> None:
        super().__init__(
            "codex_request_timeout",
            f"Codex App Server request timed out: {method}.",
            retryable=True,
            details={"method": method, "timeout_seconds": timeout_seconds},
        )


class AppServerRpcError(CodexRuntimeError):
    def __init__(
        self,
        method: str,
        upstream_code: int | str | None,
        message: str,
        *,
        data: Any = None,
        interactions: list[Mapping[str, Any]] | None = None,
    ) -> None:
        details: dict[str, Any] = {
            "method": method,
            "upstream_code": upstream_code,
        }
        if data is not None:
            details["upstream_data"] = data
        if interactions:
            details["interactions"] = [dict(item) for item in interactions]
        super().__init__(
            "codex_rpc_error",
            message,
            retryable=False,
            details=details,
        )


class AppServerInteractionRequiredError(CodexRuntimeError):
    def __init__(self, method: str, interactions: list[Mapping[str, Any]]) -> None:
        super().__init__(
            "interaction_required",
            "Codex requested approval or elicitation; the runtime does not auto-approve it.",
            details={
                "method": method,
                "interactions": [dict(item) for item in interactions],
                "recovery": "Resolve the request in a client that can review it, then retry.",
            },
        )


class AppServerProtocolError(CodexRuntimeError):
    def __init__(self, message: str) -> None:
        super().__init__("codex_protocol_error", message, retryable=True)


class CommandExecutionTimeoutError(CodexRuntimeError):
    def __init__(self, process_id: str, *, terminated: bool) -> None:
        super().__init__(
            "command_timeout",
            "Codex command execution exceeded its timeout.",
            retryable=True,
            details={"process_id": process_id, "terminated": terminated},
            side_effects={"command_may_have_run": True},
        )
