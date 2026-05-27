#!/usr/bin/env bash
set -euo pipefail

# shellcheck disable=SC1091
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/launchd-common.sh"

MCP_ONLY=0

usage() {
  cat <<'EOF'
Usage:
  ./scripts/install-launchd.sh              # install MCP + managed cloudflared + watchdog
  ./scripts/install-launchd.sh --mcp-only   # install only MCP + watchdog; use an external cloudflared
  ./scripts/install-launchd.sh --help

Use --mcp-only when you already run cloudflared yourself and its ingress points
to http://127.0.0.1:${CHATGPT_MCP_PORT:-8766}. In that mode this project will
not create, bootstrap, restart, or monitor a cloudflared launchd service.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mcp-only|--skip-cloudflared)
      MCP_ONLY=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
  shift
done

if [[ "${MCP_ONLY}" == "1" ]]; then
  export CHATGPT_MCP_EXTERNAL_CLOUDFLARED=1
fi

prepare_launchd_env
require_command launchctl
if [[ "${MCP_ONLY}" != "1" ]]; then
  require_command cloudflared
fi

PYTHON_BIN="$(pick_python)"
if [[ ! -d "${ROOT_DIR}/.venv" ]]; then
  "${PYTHON_BIN}" -m venv "${ROOT_DIR}/.venv"
fi
# shellcheck disable=SC1091
source "${ROOT_DIR}/.venv/bin/activate"

ensure_python_runtime_deps

if [[ -z "${CHATGPT_MCP_AUTH_TOKEN:-}" ]]; then
  echo "Missing CHATGPT_MCP_AUTH_TOKEN. Set it in .env or export it before installing launchd services." >&2
  exit 1
fi

CLOUDFLARED_CONFIG=""
if [[ "${MCP_ONLY}" != "1" ]]; then
  if ! CLOUDFLARED_CONFIG="$(pick_cloudflared_config)"; then
    echo "A named cloudflared config is required for full launchd install. Set CHATGPT_MCP_CLOUDFLARED_CONFIG or add cloudflared.local.yml." >&2
    echo "If you already run cloudflared externally, use: ./scripts/install-launchd.sh --mcp-only" >&2
    exit 1
  fi
fi

CLOUDFLARED_BIN="$(command -v cloudflared 2>/dev/null || printf '/usr/bin/false')"
if [[ -z "${CLOUDFLARED_CONFIG}" ]]; then
  CLOUDFLARED_CONFIG="${ROOT_DIR}/cloudflared.local.yml"
fi
MCP_LABEL="$(mcp_label)"
CLOUDFLARED_LABEL="$(cloudflared_label)"
WATCHDOG_LABEL="$(watchdog_label)"
MCP_TARGET="$(launchctl_target "${MCP_LABEL}")"
CLOUDFLARED_TARGET="$(launchctl_target "${CLOUDFLARED_LABEL}")"
WATCHDOG_TARGET="$(launchctl_target "${WATCHDOG_LABEL}")"
MCP_PLIST="$(plist_path_for_label "${MCP_LABEL}")"
CLOUDFLARED_PLIST="$(plist_path_for_label "${CLOUDFLARED_LABEL}")"
WATCHDOG_PLIST="$(plist_path_for_label "${WATCHDOG_LABEL}")"

launchctl bootout "${WATCHDOG_TARGET}" 2>/dev/null || true
launchctl bootout "${MCP_TARGET}" 2>/dev/null || true
if [[ "${MCP_ONLY}" != "1" ]]; then
  launchctl bootout "${CLOUDFLARED_TARGET}" 2>/dev/null || true
fi
sleep 1

if lsof -nP -iTCP:"${CHATGPT_MCP_PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port ${CHATGPT_MCP_PORT} is already in use. Stop manual dev-tunnel/tmux processes before installing launchd services." >&2
  exit 1
fi

mkdir -p "${CHATGPT_MCP_LAUNCHD_DIR}" "${CHATGPT_MCP_LAUNCHD_LOG_DIR}" "${CHATGPT_MCP_STATE_DIR}"
export ROOT_DIR CLOUDFLARED_BIN CLOUDFLARED_CONFIG MCP_PLIST CLOUDFLARED_PLIST WATCHDOG_PLIST MCP_ONLY
python - <<'PY'
import os
from pathlib import Path

from chatgpt_web_oauth_mcp.launchd_support import (
    LaunchdServiceConfig,
    build_cloudflared_launch_agent,
    build_mcp_launch_agent,
    build_watchdog_launch_agent,
    write_launch_agent,
)

env_keys = {
    "PATH",
    "CHATGPT_MCP_HOST",
    "CHATGPT_MCP_PORT",
    "CHATGPT_MCP_WORKSPACE_ROOT",
    "CHATGPT_MCP_STATE_DIR",
    "CHATGPT_MCP_AUTH_TOKEN",
    "CHATGPT_MCP_AUTH_MODE",
    "CHATGPT_MCP_PUBLIC_BASE_URL",
    "CHATGPT_MCP_OAUTH_LOGIN_TOKEN",
    "CHATGPT_MCP_OAUTH_SCOPES",
    "CHATGPT_MCP_OAUTH_TOKEN_TTL_SECONDS",
    "CHATGPT_MCP_CODEX_COMMAND",
    "CHATGPT_MCP_CLAUDE_COMMAND",
    "CHATGPT_MCP_COMMAND_TIMEOUT",
    "CHATGPT_MCP_DELEGATE_TIMEOUT",
    "CHATGPT_MCP_DEBUG_MCP_LOGGING",
    "CHATGPT_MCP_GRACEFUL_SHUTDOWN_SECONDS",
    "CHATGPT_MCP_WATCHDOG_INTERVAL_SECONDS",
    "CHATGPT_MCP_DOCTOR_FAILURE_THRESHOLD",
    "CHATGPT_MCP_DOCTOR_BASE_BACKOFF_SECONDS",
    "CHATGPT_MCP_DOCTOR_MAX_BACKOFF_SECONDS",
    "CHATGPT_MCP_EXTERNAL_CLOUDFLARED",
    "CHATGPT_MCP_ENABLE_OBSIDIAN",
    "CHATGPT_MCP_ENABLE_NOTEBOOKLM",
    "NOTEBOOKLM_STORAGE_PATH",
    "NOTEBOOKLM_PROFILE",
    "CHATGPT_MCP_NOTEBOOKLM_DEFAULT_NOTEBOOK_ID",
    "NOTEBOOKLM_NOTEBOOK",
    "NOTEBOOKLM_TIMEOUT_SECONDS",
    "OBSIDIAN_API_KEY",
    "OBSIDIAN_HOST",
    "OBSIDIAN_PORT",
    "OBSIDIAN_PROTOCOL",
    "OBSIDIAN_MCP_URL",
    "OBSIDIAN_VERIFY_SSL",
    "OBSIDIAN_TIMEOUT_SECONDS",
    "TG_BOT_TOKEN",
    "TG_RECEIVER_ID",
    "TG_NOTIFY_TIMEOUT_SECONDS",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
}
config = LaunchdServiceConfig(
    repo_root=Path(os.environ["ROOT_DIR"]),
    launch_agents_dir=Path(os.environ["CHATGPT_MCP_LAUNCHD_DIR"]),
    logs_dir=Path(os.environ["CHATGPT_MCP_LAUNCHD_LOG_DIR"]),
    label_prefix=os.environ["CHATGPT_MCP_LAUNCHD_LABEL_PREFIX"],
    python_bin=Path(os.environ["ROOT_DIR"]) / ".venv" / "bin" / "python",
    cloudflared_bin=Path(os.environ["CLOUDFLARED_BIN"]),
    cloudflared_config=Path(os.environ["CLOUDFLARED_CONFIG"]),
    tunnel_name=os.environ.get("CHATGPT_MCP_TUNNEL_NAME") or None,
    env={key: value for key, value in os.environ.items() if key in env_keys},
)
config = LaunchdServiceConfig(
    repo_root=config.repo_root,
    launch_agents_dir=config.launch_agents_dir,
    logs_dir=config.logs_dir,
    label_prefix=config.label_prefix,
    python_bin=config.python_bin,
    cloudflared_bin=config.cloudflared_bin,
    cloudflared_config=config.cloudflared_config,
    tunnel_name=config.tunnel_name,
    env={**config.env, "PATH": os.environ["CHATGPT_MCP_LAUNCHD_PATH"]},
)
write_launch_agent(Path(os.environ["MCP_PLIST"]), build_mcp_launch_agent(config))
if os.environ.get("MCP_ONLY") != "1":
    write_launch_agent(Path(os.environ["CLOUDFLARED_PLIST"]), build_cloudflared_launch_agent(config))
write_launch_agent(
    Path(os.environ["WATCHDOG_PLIST"]),
    build_watchdog_launch_agent(
        config,
        interval_seconds=int(os.environ["CHATGPT_MCP_WATCHDOG_INTERVAL_SECONDS"]),
    ),
)
PY

launchctl bootstrap "gui/${UID}" "${MCP_PLIST}"
if [[ "${MCP_ONLY}" != "1" ]]; then
  launchctl bootstrap "gui/${UID}" "${CLOUDFLARED_PLIST}"
fi
sleep 2
launchctl kickstart -k "${MCP_TARGET}"
if [[ "${MCP_ONLY}" != "1" ]]; then
  launchctl kickstart -k "${CLOUDFLARED_TARGET}"
fi
sleep 4

if ! curl -fsSI "http://${CHATGPT_MCP_HOST}:${CHATGPT_MCP_PORT}/mcp" >/dev/null 2>&1; then
  echo "Launchd services installed, but local /mcp is not reachable yet. Check launchctl print ${MCP_TARGET} and logs under ${CHATGPT_MCP_LAUNCHD_LOG_DIR}." >&2
  exit 1
fi
launchctl bootstrap "gui/${UID}" "${WATCHDOG_PLIST}"
launchctl kickstart -k "${WATCHDOG_TARGET}" || true

echo "Installed launchd services:"
echo "- MCP:         ${MCP_TARGET}"
if [[ "${MCP_ONLY}" == "1" ]]; then
  echo "- cloudflared: external / not managed by this project"
else
  echo "- cloudflared: ${CLOUDFLARED_TARGET}"
fi
echo "- watchdog:    ${WATCHDOG_TARGET} (every ${CHATGPT_MCP_WATCHDOG_INTERVAL_SECONDS}s)"
echo "Plists:"
echo "- ${MCP_PLIST}"
if [[ "${MCP_ONLY}" != "1" ]]; then
  echo "- ${CLOUDFLARED_PLIST}"
fi
echo "- ${WATCHDOG_PLIST}"
echo "Logs: ${CHATGPT_MCP_LAUNCHD_LOG_DIR}"
echo "Use ./scripts/launchd-status.sh to inspect, ./scripts/launchd-doctor.sh --fix for one-shot repair, ./scripts/launchd-reload.sh for code reload, and ./scripts/launchd-restart.sh mcp for MCP restarts."
