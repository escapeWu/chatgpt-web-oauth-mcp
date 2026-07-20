from __future__ import annotations

import importlib

import pytest

from chatgpt_web_oauth_mcp import config


def _restore_config_after_env_test() -> None:
    importlib.reload(config)


def test_response_token_budgets_default_and_read_inherits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with monkeypatch.context() as patch:
        patch.delenv("CHATGPT_MCP_TOOL_OUTPUT_TOKEN_BUDGET", raising=False)
        patch.delenv("CHATGPT_MCP_READ_TOKEN_BUDGET", raising=False)
        patch.delenv("CHATGPT_MCP_RUN_TOKEN_BUDGET", raising=False)
        patch.delenv("CHATGPT_MCP_JOB_OUTPUT_TOKEN_BUDGET", raising=False)
        patch.delenv("CHATGPT_MCP_RUN_CAPTURE_MAX_BYTES", raising=False)
        importlib.reload(config)

        assert config.TOOL_OUTPUT_TOKEN_BUDGET == 8500
        assert config.READ_TOKEN_BUDGET == 8500
        assert config.RUN_TOKEN_BUDGET == 8500
        assert config.JOB_OUTPUT_TOKEN_BUDGET == 8500
        assert config.RUN_CAPTURE_MAX_BYTES == 1024 * 1024

    _restore_config_after_env_test()


def test_tool_token_budgets_can_inherit_or_override_global(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with monkeypatch.context() as patch:
        patch.setenv("CHATGPT_MCP_TOOL_OUTPUT_TOKEN_BUDGET", "1200")
        patch.delenv("CHATGPT_MCP_READ_TOKEN_BUDGET", raising=False)
        patch.delenv("CHATGPT_MCP_RUN_TOKEN_BUDGET", raising=False)
        patch.delenv("CHATGPT_MCP_JOB_OUTPUT_TOKEN_BUDGET", raising=False)
        importlib.reload(config)
        assert config.READ_TOKEN_BUDGET == 1200
        assert config.RUN_TOKEN_BUDGET == 1200
        assert config.JOB_OUTPUT_TOKEN_BUDGET == 1200

        patch.setenv("CHATGPT_MCP_READ_TOKEN_BUDGET", "321")
        patch.setenv("CHATGPT_MCP_RUN_TOKEN_BUDGET", "654")
        patch.setenv("CHATGPT_MCP_JOB_OUTPUT_TOKEN_BUDGET", "777")
        importlib.reload(config)
        assert config.TOOL_OUTPUT_TOKEN_BUDGET == 1200
        assert config.READ_TOKEN_BUDGET == 321
        assert config.RUN_TOKEN_BUDGET == 654
        assert config.JOB_OUTPUT_TOKEN_BUDGET == 777

    _restore_config_after_env_test()


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("CHATGPT_MCP_TOOL_OUTPUT_TOKEN_BUDGET", "0"),
        ("CHATGPT_MCP_TOOL_OUTPUT_TOKEN_BUDGET", "invalid"),
        ("CHATGPT_MCP_READ_TOKEN_BUDGET", "-1"),
        ("CHATGPT_MCP_READ_TOKEN_BUDGET", "1.5"),
        ("CHATGPT_MCP_RUN_TOKEN_BUDGET", "0"),
        ("CHATGPT_MCP_RUN_TOKEN_BUDGET", "invalid"),
        ("CHATGPT_MCP_JOB_OUTPUT_TOKEN_BUDGET", "0"),
        ("CHATGPT_MCP_JOB_OUTPUT_TOKEN_BUDGET", "invalid"),
        ("CHATGPT_MCP_RUN_CAPTURE_MAX_BYTES", "-1"),
        ("CHATGPT_MCP_RUN_CAPTURE_MAX_BYTES", "1.5"),
    ],
)
def test_response_token_budgets_reject_invalid_values(
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    value: str,
) -> None:
    with monkeypatch.context() as patch:
        patch.setenv("CHATGPT_MCP_TOOL_OUTPUT_TOKEN_BUDGET", "8500")
        patch.delenv("CHATGPT_MCP_READ_TOKEN_BUDGET", raising=False)
        patch.delenv("CHATGPT_MCP_RUN_TOKEN_BUDGET", raising=False)
        patch.delenv("CHATGPT_MCP_JOB_OUTPUT_TOKEN_BUDGET", raising=False)
        patch.delenv("CHATGPT_MCP_RUN_CAPTURE_MAX_BYTES", raising=False)
        patch.setenv(variable, value)

        with pytest.raises(ValueError, match=rf"{variable} must be a positive integer"):
            importlib.reload(config)

    _restore_config_after_env_test()


def test_ripgrep_binary_defaults_and_can_be_overridden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with monkeypatch.context() as patch:
        patch.delenv("CHATGPT_MCP_RIPGREP_BINARY", raising=False)
        importlib.reload(config)
        assert config.RIPGREP_BINARY == "rg"

        patch.setenv("CHATGPT_MCP_RIPGREP_BINARY", "/custom/bin/rg")
        importlib.reload(config)
        assert config.RIPGREP_BINARY == "/custom/bin/rg"

        patch.setenv("CHATGPT_MCP_RIPGREP_BINARY", "   ")
        importlib.reload(config)
        assert config.RIPGREP_BINARY == "rg"

    _restore_config_after_env_test()


def test_ensure_runtime_directories_requires_existing_workspace_root(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_root = tmp_path / "missing-workspace"
    state_dir = tmp_path / "state"

    monkeypatch.setattr(config, "WORKSPACE_ROOT", workspace_root)
    monkeypatch.setattr(config, "STATE_DIR", state_dir)

    with pytest.raises(FileNotFoundError):
        config.ensure_runtime_directories()

    assert workspace_root.exists() is False
    assert state_dir.exists() is False


def test_ensure_runtime_directories_creates_state_dir_for_valid_workspace(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_root = tmp_path / "workspace"
    state_dir = tmp_path / "state"
    workspace_root.mkdir()

    monkeypatch.setattr(config, "WORKSPACE_ROOT", workspace_root)
    monkeypatch.setattr(config, "STATE_DIR", state_dir)

    config.ensure_runtime_directories()

    assert workspace_root.is_dir() is True
    assert state_dir.is_dir() is True
