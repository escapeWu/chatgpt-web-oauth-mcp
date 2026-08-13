from __future__ import annotations

from pathlib import Path

from chatgpt_web_oauth_mcp import executors
from chatgpt_web_oauth_mcp.delegate_harnesses import GenericCliHarness
from chatgpt_web_oauth_mcp.executors import ExecutorRegistry


def test_explore_invocation_is_hard_readonly_and_uses_task_defaults(tmp_path: Path) -> None:
    registry = ExecutorRegistry(codex_command="codex")

    invocation = registry._build_invocation(
        command="codex",
        task="inspect the scheduler",
        goal=None,
        cwd=tmp_path,
        context_files=[],
        acceptance_criteria=[],
        verification_commands=[],
        commit_mode="allowed",
        model="gpt-5.6-luna",
        reasoning_effort="low",
        kind="explore",
    )

    assert invocation.args[1:6] == [
        "exec",
        "--model",
        "gpt-5.6-luna",
        "-c",
        'model_reasoning_effort="low"',
    ]
    assert "--sandbox" in invocation.args
    assert invocation.args[invocation.args.index("--sandbox") + 1] == "read-only"
    assert "--ephemeral" in invocation.args
    assert "--dangerously-bypass-approvals-and-sandbox" not in invocation.args
    prompt = (invocation.stdin or b"").decode("utf-8")
    assert "This is a read-only exploration task." in prompt
    assert "Commit mode: forbidden" in prompt


def test_code_defaults_are_sol_xhigh_and_full_access(tmp_path: Path) -> None:
    registry = ExecutorRegistry(codex_command="python3 -c \"print('done')\"")

    result = registry.run_codex(task="implement", cwd=tmp_path, wait_seconds=2)

    assert result["status"] == "succeeded"
    assert result["model"] == "gpt-5.6-sol"
    assert result["reasoning_effort"] == "xhigh"
    assert result["sandbox_mode"] == "danger-full-access"
    assert result["kind"] == "code"


def test_explore_forces_permissions_even_with_model_override(tmp_path: Path) -> None:
    registry = ExecutorRegistry(
        codex_command="python3 -c \"print('done')\"",
        allow_unsafe_explore_command=True,
    )

    result = registry.run_codex(
        task="inspect",
        kind="explore",
        cwd=tmp_path,
        model="custom-model",
        reasoning_effort="medium",
        commit_mode="required",
        wait_seconds=2,
    )

    assert result["status"] == "succeeded"
    assert result["model"] == "custom-model"
    assert result["reasoning_effort"] == "medium"
    assert result["sandbox_mode"] == "read-only"
    assert result["commit_mode"] == "forbidden"


def test_explore_fails_closed_when_command_cannot_enforce_codex_sandbox(tmp_path: Path) -> None:
    registry = ExecutorRegistry(codex_command="python3 -c \"print('unsafe')\"")

    result = registry.run_codex(
        task="inspect",
        kind="explore",
        cwd=tmp_path,
        wait_seconds=0,
    )

    assert result["status"] == "failed"
    assert result["error"]["code"] == "readonly_sandbox_unavailable"


def test_pi_explore_invocation_enforces_read_only_tool_allowlist(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeProcess:
        stdin = None
        stdout = None
        stderr = None
        returncode = 0

        def __init__(self, args, **kwargs) -> None:
            captured["args"] = args
            captured["kwargs"] = kwargs

        def communicate(self, timeout=None):
            return b'{"ok": true}', b""

    registry = ExecutorRegistry(
        codex_command=None,
        pi_command="pi",
        default_harness="pi",
    )
    monkeypatch.setattr(executors, "_command_available", lambda command: True)
    monkeypatch.setattr(executors.subprocess, "Popen", FakeProcess)

    result = registry.run_delegate(
        kind="explore",
        task="inspect the scheduler",
        cwd=tmp_path,
        wait_seconds=2,
    )

    args = captured["args"]
    assert isinstance(args, list)
    assert args[0] == "pi"
    assert "--print" in args
    assert "--no-session" in args
    assert "--no-approve" in args
    assert "--no-extensions" in args
    assert "--no-skills" in args
    assert "--no-context-files" in args
    assert args[args.index("--tools") + 1] == "read,grep,find,ls"
    assert "--approve" not in args
    assert "--model" not in args
    assert "--thinking" not in args
    assert result["executor"] == "pi"
    assert result["harness"] == "pi"
    assert result["sandbox_mode"] == "tool-allowlist-read-only"
    assert result["commit_mode"] == "forbidden"
    assert result["structured_output"] == {"ok": True}
    assert "pi-delegates" in result["logs"]["log_dir"]


def test_pi_code_invocation_maps_model_and_reasoning_flags(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeProcess:
        stdin = None
        stdout = None
        stderr = None
        returncode = 0

        def __init__(self, args, **kwargs) -> None:
            captured["args"] = args

        def communicate(self, timeout=None):
            return b"done", b""

    registry = ExecutorRegistry(codex_command=None, pi_command="pi")
    monkeypatch.setattr(executors, "_command_available", lambda command: True)
    monkeypatch.setattr(executors.subprocess, "Popen", FakeProcess)

    result = registry.run_delegate(
        harness="pi",
        kind="code",
        task="implement the change",
        cwd=tmp_path,
        model="anthropic/claude-sonnet-4",
        reasoning_effort="high",
        wait_seconds=2,
    )

    args = captured["args"]
    assert isinstance(args, list)
    assert args[args.index("--model") + 1] == "anthropic/claude-sonnet-4"
    assert args[args.index("--thinking") + 1] == "high"
    assert "--approve" in args
    assert "--tools" not in args
    assert result["status"] == "succeeded"
    assert result["executor"] == "pi"
    assert result["sandbox_mode"] == "full-tool-access"


def test_delegate_rejects_unknown_harness_with_available_names(tmp_path: Path) -> None:
    registry = ExecutorRegistry(codex_command="codex", pi_command="pi")

    result = registry.run_delegate(
        harness="missing-agent",
        task="inspect",
        cwd=tmp_path,
    )

    assert result["error"]["code"] == "unsupported_delegate_harness"
    assert result["error"]["available_harnesses"] == ["codex", "pi"]
    assert result["harness"] == "missing-agent"


def test_registry_accepts_programmatic_generic_cli_harness() -> None:
    registry = ExecutorRegistry(
        harnesses=[
            GenericCliHarness(
                name="custom",
                command="custom-agent run",
                explore_command="custom-agent inspect --read-only",
            )
        ]
    )

    info = registry.harness_info()["custom"]
    assert info["read_only_supported"] is True
    assert info["explore"]["sandbox_mode"] == "adapter-enforced-read-only"
