from __future__ import annotations

from dataclasses import dataclass
import plistlib
from pathlib import Path
from typing import Mapping, Any

DEFAULT_LAUNCHD_LABEL_PREFIX = "com.chatgpt-web-oauth-mcp"
DEFAULT_LAUNCHD_LOG_DIRNAME = "chatgpt-web-oauth-mcp"
DEFAULT_MCP_MAX_FILES = 4096
DEFAULT_WATCHDOG_INTERVAL_SECONDS = 60
CLOUDFLARED_PROXY_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")


@dataclass(frozen=True)
class LaunchdServiceConfig:
    repo_root: Path
    launch_agents_dir: Path
    logs_dir: Path
    label_prefix: str
    python_bin: Path
    cloudflared_bin: Path
    cloudflared_config: Path
    tunnel_name: str | None
    env: Mapping[str, str]


def mcp_service_label(label_prefix: str = DEFAULT_LAUNCHD_LABEL_PREFIX) -> str:
    return f"{label_prefix}.mcp"


def cloudflared_service_label(label_prefix: str = DEFAULT_LAUNCHD_LABEL_PREFIX) -> str:
    return f"{label_prefix}.cloudflared"


def watchdog_service_label(label_prefix: str = DEFAULT_LAUNCHD_LABEL_PREFIX) -> str:
    return f"{label_prefix}.watchdog"


def plist_path(launch_agents_dir: Path, label: str) -> Path:
    return launch_agents_dir / f"{label}.plist"


def _base_launch_agent(
    *,
    label: str,
    working_directory: Path,
    stdout_path: Path,
    stderr_path: Path,
    program_arguments: list[str],
    environment: Mapping[str, str],
    keep_alive: bool = True,
    start_interval_seconds: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "Label": label,
        "RunAtLoad": True,
        "WorkingDirectory": str(working_directory),
        "ProgramArguments": program_arguments,
        "EnvironmentVariables": dict(environment),
        "ProcessType": "Background",
        "ThrottleInterval": 5,
        "StandardOutPath": str(stdout_path),
        "StandardErrorPath": str(stderr_path),
    }
    if keep_alive:
        payload["KeepAlive"] = True
    if start_interval_seconds is not None:
        payload["StartInterval"] = start_interval_seconds
    return payload


def build_mcp_launch_agent(config: LaunchdServiceConfig) -> dict[str, Any]:
    state_dir = Path(config.env["CHATGPT_MCP_STATE_DIR"])
    environment = {
        key: value
        for key, value in config.env.items()
        if value is not None
        and value != ""
        and key not in CLOUDFLARED_PROXY_ENV_KEYS
    }
    payload = _base_launch_agent(
        label=mcp_service_label(config.label_prefix),
        working_directory=config.repo_root,
        stdout_path=config.logs_dir / "mcp.stdout.log",
        stderr_path=config.logs_dir / "mcp.stderr.log",
        program_arguments=[
            str(config.python_bin),
            "-m",
            "chatgpt_web_oauth_mcp.supervisor",
            "--pid-file",
            str(state_dir / "launchd-supervisor.pid"),
            "--log-file",
            str(config.logs_dir / "mcp-server.log"),
        ],
        environment=environment,
    )
    payload["SoftResourceLimits"] = {"NumberOfFiles": DEFAULT_MCP_MAX_FILES}
    payload["HardResourceLimits"] = {"NumberOfFiles": DEFAULT_MCP_MAX_FILES}
    return payload


def build_cloudflared_launch_agent(config: LaunchdServiceConfig) -> dict[str, Any]:
    arguments = [
        str(config.cloudflared_bin),
        "tunnel",
        "--config",
        str(config.cloudflared_config),
        "run",
    ]
    if config.tunnel_name:
        arguments.append(config.tunnel_name)
    environment = {
        "PATH": config.env["PATH"],
        **{
            key: value
            for key in CLOUDFLARED_PROXY_ENV_KEYS
            if (value := config.env.get(key))
        },
    }
    return _base_launch_agent(
        label=cloudflared_service_label(config.label_prefix),
        working_directory=config.repo_root,
        stdout_path=config.logs_dir / "cloudflared.stdout.log",
        stderr_path=config.logs_dir / "cloudflared.stderr.log",
        program_arguments=arguments,
        environment=environment,
    )


def build_watchdog_launch_agent(
    config: LaunchdServiceConfig,
    *,
    interval_seconds: int = DEFAULT_WATCHDOG_INTERVAL_SECONDS,
) -> dict[str, Any]:
    environment = {
        key: value
        for key, value in {
            "PATH": config.env["PATH"],
            "CHATGPT_MCP_HOST": config.env.get("CHATGPT_MCP_HOST"),
            "CHATGPT_MCP_PORT": config.env.get("CHATGPT_MCP_PORT"),
            "CHATGPT_MCP_STATE_DIR": config.env.get("CHATGPT_MCP_STATE_DIR"),
            "CHATGPT_MCP_LAUNCHD_LABEL_PREFIX": config.label_prefix,
            "CHATGPT_MCP_LAUNCHD_DIR": str(config.launch_agents_dir),
            "CHATGPT_MCP_LAUNCHD_LOG_DIR": str(config.logs_dir),
            "CHATGPT_MCP_DOCTOR_FAILURE_THRESHOLD": config.env.get(
                "CHATGPT_MCP_DOCTOR_FAILURE_THRESHOLD"
            ),
            "CHATGPT_MCP_DOCTOR_BASE_BACKOFF_SECONDS": config.env.get(
                "CHATGPT_MCP_DOCTOR_BASE_BACKOFF_SECONDS"
            ),
            "CHATGPT_MCP_DOCTOR_MAX_BACKOFF_SECONDS": config.env.get(
                "CHATGPT_MCP_DOCTOR_MAX_BACKOFF_SECONDS"
            ),
            "CHATGPT_MCP_PUBLIC_BASE_URL": config.env.get("CHATGPT_MCP_PUBLIC_BASE_URL"),
            "CHATGPT_MCP_EXTERNAL_CLOUDFLARED": config.env.get("CHATGPT_MCP_EXTERNAL_CLOUDFLARED"),
        }.items()
        if value
    }
    return _base_launch_agent(
        label=watchdog_service_label(config.label_prefix),
        working_directory=config.repo_root,
        stdout_path=config.logs_dir / "watchdog.stdout.log",
        stderr_path=config.logs_dir / "watchdog.stderr.log",
        program_arguments=[
            "/bin/bash",
            str(config.repo_root / "scripts" / "launchd-doctor.sh"),
            "--fix",
            "--quiet",
        ],
        environment=environment,
        keep_alive=False,
        start_interval_seconds=interval_seconds,
    )


def write_launch_agent(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(dict(payload), sort_keys=False))
    try:
        path.chmod(0o600)
    except OSError:
        pass
