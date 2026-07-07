from __future__ import annotations

import stat
from pathlib import Path

from chatgpt_web_oauth_mcp.launchd_support import (
    LaunchdServiceConfig,
    build_cloudflared_launch_agent,
    build_mcp_launch_agent,
    build_watchdog_launch_agent,
    mcp_service_label,
    cloudflared_service_label,
    watchdog_service_label,
    plist_path,
    write_launch_agent,
)


def _config(tmp_path: Path) -> LaunchdServiceConfig:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)
    return LaunchdServiceConfig(
        repo_root=repo_root,
        launch_agents_dir=tmp_path / "LaunchAgents",
        logs_dir=tmp_path / "logs",
        label_prefix="com.example.chatgpt-web-oauth-mcp",
        python_bin=repo_root / ".venv" / "bin" / "python",
        cloudflared_bin=Path("/opt/homebrew/bin/cloudflared"),
        cloudflared_config=repo_root / "cloudflared.local.yml",
        tunnel_name="named-tunnel",
        env={
            "PATH": "/opt/homebrew/bin:/usr/bin:/bin",
            "CHATGPT_MCP_HOST": "127.0.0.1",
            "CHATGPT_MCP_PORT": "8766",
            "CHATGPT_MCP_WORKSPACE_ROOT": "/tmp/workspace",
            "CHATGPT_MCP_STATE_DIR": "/tmp/state",
            "CHATGPT_MCP_AUTH_TOKEN": "secret-token",
            "CHATGPT_MCP_AUTH_MODE": "oauth",
            "CHATGPT_MCP_PUBLIC_BASE_URL": "https://mcp.example.test",
            "CHATGPT_MCP_OAUTH_SCOPES": "local-ops",
            "CHATGPT_MCP_OAUTH_TOKEN_TTL_SECONDS": "86400",
            "CHATGPT_MCP_CODEX_COMMAND": "codex",
            "CHATGPT_MCP_COMMAND_TIMEOUT": "120",
            "CHATGPT_MCP_DELEGATE_TIMEOUT": "300",
            "CHATGPT_MCP_DEBUG_MCP_LOGGING": "1",
            "CHATGPT_MCP_GRACEFUL_SHUTDOWN_SECONDS": "30",
        },
    )


def test_build_mcp_launch_agent_contains_supervisor_and_runtime_env(tmp_path: Path) -> None:
    config = _config(tmp_path)

    payload = build_mcp_launch_agent(config)

    assert payload["Label"] == "com.example.chatgpt-web-oauth-mcp.mcp"
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] is True
    assert payload["WorkingDirectory"] == str(config.repo_root)
    assert payload["ProgramArguments"] == [
        str(config.python_bin),
        "-m",
        "chatgpt_web_oauth_mcp.supervisor",
        "--pid-file",
        str(Path("/tmp/state") / "launchd-supervisor.pid"),
        "--log-file",
        str(config.logs_dir / "mcp-server.log"),
    ]
    assert payload["EnvironmentVariables"]["CHATGPT_MCP_AUTH_TOKEN"] == "secret-token"
    assert payload["EnvironmentVariables"]["CHATGPT_MCP_AUTH_MODE"] == "oauth"
    assert payload["EnvironmentVariables"]["CHATGPT_MCP_PUBLIC_BASE_URL"] == "https://mcp.example.test"
    assert payload["EnvironmentVariables"]["PATH"] == "/opt/homebrew/bin:/usr/bin:/bin"
    assert payload["StandardOutPath"] == str(config.logs_dir / "mcp.stdout.log")
    assert payload["StandardErrorPath"] == str(config.logs_dir / "mcp.stderr.log")
    assert payload["SoftResourceLimits"] == {"NumberOfFiles": 4096}
    assert payload["HardResourceLimits"] == {"NumberOfFiles": 4096}


def test_build_cloudflared_launch_agent_uses_named_tunnel_when_present(tmp_path: Path) -> None:
    config = _config(tmp_path)

    payload = build_cloudflared_launch_agent(config)

    assert payload["Label"] == "com.example.chatgpt-web-oauth-mcp.cloudflared"
    assert payload["ProgramArguments"] == [
        "/opt/homebrew/bin/cloudflared",
        "tunnel",
        "--config",
        str(config.cloudflared_config),
        "run",
        "named-tunnel",
    ]
    assert payload["EnvironmentVariables"] == {"PATH": "/opt/homebrew/bin:/usr/bin:/bin"}
    assert payload["StandardOutPath"] == str(config.logs_dir / "cloudflared.stdout.log")
    assert payload["StandardErrorPath"] == str(config.logs_dir / "cloudflared.stderr.log")


def test_build_cloudflared_launch_agent_preserves_proxy_env(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config = LaunchdServiceConfig(
        repo_root=config.repo_root,
        launch_agents_dir=config.launch_agents_dir,
        logs_dir=config.logs_dir,
        label_prefix=config.label_prefix,
        python_bin=config.python_bin,
        cloudflared_bin=config.cloudflared_bin,
        cloudflared_config=config.cloudflared_config,
        tunnel_name=config.tunnel_name,
        env={
            **config.env,
            "HTTPS_PROXY": "http://127.0.0.1:7890",
            "HTTP_PROXY": "http://127.0.0.1:7890",
            "ALL_PROXY": "http://127.0.0.1:7890",
            "NO_PROXY": "127.0.0.1,localhost",
        },
    )

    payload = build_cloudflared_launch_agent(config)

    assert payload["EnvironmentVariables"] == {
        "PATH": "/opt/homebrew/bin:/usr/bin:/bin",
        "HTTP_PROXY": "http://127.0.0.1:7890",
        "HTTPS_PROXY": "http://127.0.0.1:7890",
        "ALL_PROXY": "http://127.0.0.1:7890",
        "NO_PROXY": "127.0.0.1,localhost",
    }


def test_build_watchdog_launch_agent_runs_doctor_on_interval(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config = LaunchdServiceConfig(
        repo_root=config.repo_root,
        launch_agents_dir=config.launch_agents_dir,
        logs_dir=config.logs_dir,
        label_prefix=config.label_prefix,
        python_bin=config.python_bin,
        cloudflared_bin=config.cloudflared_bin,
        cloudflared_config=config.cloudflared_config,
        tunnel_name=config.tunnel_name,
        env={
            **config.env,
            "HTTPS_PROXY": "http://127.0.0.1:7890",
            "NO_PROXY": "127.0.0.1,localhost",
            "CHATGPT_MCP_DOCTOR_FAILURE_THRESHOLD": "3",
            "CHATGPT_MCP_DOCTOR_BASE_BACKOFF_SECONDS": "300",
            "CHATGPT_MCP_DOCTOR_MAX_BACKOFF_SECONDS": "3600",
        },
    )

    payload = build_watchdog_launch_agent(config, interval_seconds=45)

    assert payload["Label"] == "com.example.chatgpt-web-oauth-mcp.watchdog"
    assert payload["RunAtLoad"] is True
    assert "KeepAlive" not in payload
    assert payload["StartInterval"] == 45
    assert payload["ProgramArguments"] == [
        "/bin/bash",
        str(config.repo_root / "scripts" / "launchd-doctor.sh"),
        "--fix",
        "--quiet",
    ]
    assert payload["EnvironmentVariables"] == {
        "PATH": "/opt/homebrew/bin:/usr/bin:/bin",
        "CHATGPT_MCP_HOST": "127.0.0.1",
        "CHATGPT_MCP_PORT": "8766",
        "CHATGPT_MCP_STATE_DIR": "/tmp/state",
        "CHATGPT_MCP_LAUNCHD_LABEL_PREFIX": "com.example.chatgpt-web-oauth-mcp",
        "CHATGPT_MCP_LAUNCHD_DIR": str(config.launch_agents_dir),
        "CHATGPT_MCP_LAUNCHD_LOG_DIR": str(config.logs_dir),
        "CHATGPT_MCP_DOCTOR_FAILURE_THRESHOLD": "3",
        "CHATGPT_MCP_DOCTOR_BASE_BACKOFF_SECONDS": "300",
        "CHATGPT_MCP_DOCTOR_MAX_BACKOFF_SECONDS": "3600",
        "CHATGPT_MCP_PUBLIC_BASE_URL": "https://mcp.example.test",
    }
    assert payload["StandardOutPath"] == str(config.logs_dir / "watchdog.stdout.log")
    assert payload["StandardErrorPath"] == str(config.logs_dir / "watchdog.stderr.log")


def test_launchd_label_helpers_and_plist_path(tmp_path: Path) -> None:
    launch_agents_dir = tmp_path / "LaunchAgents"
    prefix = "com.example.chatgpt-web-oauth-mcp"

    assert mcp_service_label(prefix) == "com.example.chatgpt-web-oauth-mcp.mcp"
    assert cloudflared_service_label(prefix) == "com.example.chatgpt-web-oauth-mcp.cloudflared"
    assert watchdog_service_label(prefix) == "com.example.chatgpt-web-oauth-mcp.watchdog"
    assert plist_path(launch_agents_dir, mcp_service_label(prefix)) == (
        launch_agents_dir / "com.example.chatgpt-web-oauth-mcp.mcp.plist"
    )


def test_write_launch_agent_locks_down_plist_permissions(tmp_path: Path) -> None:
    target = tmp_path / "LaunchAgents" / "com.example.mcp.plist"
    payload = {"Label": "com.example.mcp", "RunAtLoad": True}

    write_launch_agent(target, payload)

    assert target.exists()
    file_mode = stat.S_IMODE(target.stat().st_mode)
    assert file_mode == 0o600, f"plist mode={oct(file_mode)} (expected 0o600)"
