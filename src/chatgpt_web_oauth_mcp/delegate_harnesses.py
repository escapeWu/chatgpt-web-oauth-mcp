from __future__ import annotations

import json
import os
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Protocol

from .delegate_models import DelegateTask, TaskKind
from .delegate_process import Invocation


DEFAULT_VALUE = "default"
IS_WINDOWS = os.name == "nt"


def split_command(command: str) -> list[str]:
    return shlex.split(command)


def binary_name(binary: str) -> str:
    if IS_WINDOWS:
        return PureWindowsPath(binary).stem.lower()
    return Path(binary).stem.lower()


def resolve_command_parts(command: str, *, expected_binary: str | None = None) -> list[str]:
    parts = split_command(command)
    if not IS_WINDOWS or not parts:
        return parts
    if expected_binary is not None and binary_name(parts[0]) != expected_binary:
        return parts
    resolved = shutil.which(parts[0])
    if resolved:
        parts[0] = resolved
    return parts


def command_available(command: str | None) -> bool:
    if not command:
        return False
    parts = split_command(command)
    if not parts:
        return False
    binary = parts[0]
    if Path(binary).exists():
        return True
    return shutil.which(binary) is not None


def optional_value(value: str | None) -> str | None:
    normalized = (value or "").strip()
    if not normalized or normalized.lower() == DEFAULT_VALUE:
        return None
    return normalized


@dataclass(frozen=True)
class HarnessTaskDefaults:
    model: str
    reasoning_effort: str
    sandbox_mode: str


class DelegateHarness(Protocol):
    """Adapter boundary between generic delegate scheduling and a CLI agent."""

    name: str
    display_name: str
    command: str | None

    def task_defaults(self, kind: TaskKind) -> HarnessTaskDefaults: ...

    def command_for(self, kind: TaskKind) -> str | None: ...

    def supports_read_only(self) -> bool: ...

    def build_invocation(self, task: DelegateTask) -> Invocation: ...

    def info(self) -> dict[str, object]: ...


@dataclass(frozen=True)
class CodexHarness:
    command: str | None
    allow_unsafe_explore_command: bool = False
    name: str = "codex"
    display_name: str = "Codex"

    def task_defaults(self, kind: TaskKind) -> HarnessTaskDefaults:
        if kind == "explore":
            return HarnessTaskDefaults(
                model="gpt-5.6-luna",
                reasoning_effort="low",
                sandbox_mode="read-only",
            )
        return HarnessTaskDefaults(
            model="gpt-5.6-sol",
            reasoning_effort="xhigh",
            sandbox_mode="danger-full-access",
        )

    def command_for(self, kind: TaskKind) -> str | None:
        return self.command

    def supports_read_only(self) -> bool:
        if self.allow_unsafe_explore_command:
            return True
        parts = resolve_command_parts(self.command or "", expected_binary="codex")
        return bool(parts and binary_name(parts[0]) == "codex")

    def build_invocation(self, task: DelegateTask) -> Invocation:
        parts = resolve_command_parts(self.command or "", expected_binary="codex")
        if parts and binary_name(parts[0]) == "codex":
            args = [*parts, "exec"]
            model = optional_value(task.model)
            reasoning_effort = optional_value(task.reasoning_effort)
            if model:
                args.extend(["--model", model])
            if reasoning_effort:
                args.extend(
                    ["-c", f"model_reasoning_effort={json.dumps(reasoning_effort)}"]
                )
            if task.kind == "explore":
                args.extend(["--sandbox", "read-only", "--ephemeral"])
            else:
                args.append("--dangerously-bypass-approvals-and-sandbox")
            args.extend(["-C", str(task.cwd)])
            if task.project.git_common_dir is None:
                args.append("--skip-git-repo-check")
            args.append("-")
            return Invocation(
                args=args,
                use_shell=False,
                stdin=task.prompt.encode("utf-8"),
            )

        # Backward compatibility for deployments that used
        # CHATGPT_MCP_CODEX_COMMAND as a complete shell command.
        return Invocation(args=self.command or "", use_shell=True)

    def info(self) -> dict[str, object]:
        return _harness_info(self)


@dataclass(frozen=True)
class PiHarness:
    command: str | None
    name: str = "pi"
    display_name: str = "Pi"

    def task_defaults(self, kind: TaskKind) -> HarnessTaskDefaults:
        if kind == "explore":
            return HarnessTaskDefaults(
                model=DEFAULT_VALUE,
                reasoning_effort=DEFAULT_VALUE,
                sandbox_mode="tool-allowlist-read-only",
            )
        return HarnessTaskDefaults(
            model=DEFAULT_VALUE,
            reasoning_effort=DEFAULT_VALUE,
            sandbox_mode="full-tool-access",
        )

    def command_for(self, kind: TaskKind) -> str | None:
        return self.command

    def supports_read_only(self) -> bool:
        parts = resolve_command_parts(self.command or "", expected_binary="pi")
        return bool(parts and binary_name(parts[0]) == "pi")

    def build_invocation(self, task: DelegateTask) -> Invocation:
        parts = resolve_command_parts(self.command or "", expected_binary="pi")
        args = [
            *parts,
            "--no-session",
            "--mode",
            "text",
        ]
        model = optional_value(task.model)
        reasoning_effort = optional_value(task.reasoning_effort)
        if model:
            args.extend(["--model", model])
        if reasoning_effort:
            args.extend(["--thinking", reasoning_effort])
        if task.kind == "explore":
            args.extend(
                [
                    "--no-approve",
                    "--no-extensions",
                    "--no-skills",
                    "--no-context-files",
                    "--tools",
                    "read,grep,find,ls",
                ]
            )
        else:
            args.append("--approve")
        args.append("--print")
        return Invocation(
            args=args,
            use_shell=False,
            stdin=task.prompt.encode("utf-8"),
        )

    def info(self) -> dict[str, object]:
        return _harness_info(self)


@dataclass(frozen=True)
class GenericCliHarness:
    """Programmatic adapter for stdin-driven CLI agents.

    Integrations can register this adapter without changing the scheduler. A separate
    ``explore_command`` is required before read-only tasks are accepted, so a generic
    command never gains read-only status from prompt wording alone.
    """

    name: str
    command: str | None
    explore_command: str | None = None
    display_name: str = "CLI agent"
    model_args: tuple[str, ...] = ()
    reasoning_args: tuple[str, ...] = ()
    explore_defaults: HarnessTaskDefaults = HarnessTaskDefaults(
        model=DEFAULT_VALUE,
        reasoning_effort=DEFAULT_VALUE,
        sandbox_mode="adapter-enforced-read-only",
    )
    code_defaults: HarnessTaskDefaults = HarnessTaskDefaults(
        model=DEFAULT_VALUE,
        reasoning_effort=DEFAULT_VALUE,
        sandbox_mode="adapter-defined",
    )

    def task_defaults(self, kind: TaskKind) -> HarnessTaskDefaults:
        return self.explore_defaults if kind == "explore" else self.code_defaults

    def command_for(self, kind: TaskKind) -> str | None:
        return self.explore_command if kind == "explore" else self.command

    def supports_read_only(self) -> bool:
        return bool(self.explore_command)

    def build_invocation(self, task: DelegateTask) -> Invocation:
        command = self.explore_command if task.kind == "explore" else self.command
        parts = resolve_command_parts(command or "")
        args = [*parts]
        model = optional_value(task.model)
        reasoning_effort = optional_value(task.reasoning_effort)
        if model:
            args.extend(part.replace("{value}", model) for part in self.model_args)
        if reasoning_effort:
            args.extend(
                part.replace("{value}", reasoning_effort) for part in self.reasoning_args
            )
        return Invocation(
            args=args,
            use_shell=False,
            stdin=task.prompt.encode("utf-8"),
        )

    def info(self) -> dict[str, object]:
        return _harness_info(self)


def _harness_info(harness: DelegateHarness) -> dict[str, object]:
    explore = harness.task_defaults("explore")
    code = harness.task_defaults("code")
    return {
        "name": harness.name,
        "display_name": harness.display_name,
        "command": harness.command,
        "available": command_available(harness.command_for("code")),
        "explore_available": command_available(harness.command_for("explore")),
        "read_only_supported": harness.supports_read_only(),
        "explore": {
            "model": explore.model,
            "reasoning_effort": explore.reasoning_effort,
            "sandbox_mode": explore.sandbox_mode,
        },
        "code": {
            "model": code.model,
            "reasoning_effort": code.reasoning_effort,
            "sandbox_mode": code.sandbox_mode,
        },
    }
