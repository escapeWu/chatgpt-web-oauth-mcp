#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CURRENT_SHELL_PATH="${PATH:-/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin}"

load_env_file() {
  if [[ -f "${ROOT_DIR}/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "${ROOT_DIR}/.env"
    set +a
  fi
}

resolve_path() {
  local value="$1"
  if [[ "${value}" = /* ]]; then
    printf '%s\n' "${value}"
    return 0
  fi
  printf '%s\n' "${ROOT_DIR}/${value}"
}

pick_cloudflared_config() {
  local candidate

  if [[ -n "${CHATGPT_MCP_CLOUDFLARED_CONFIG:-}" ]]; then
    resolve_path "${CHATGPT_MCP_CLOUDFLARED_CONFIG}"
    return 0
  fi

  for candidate in \
    "${ROOT_DIR}/cloudflared.local.yml" \
    "${ROOT_DIR}/cloudflared.local.yaml"; do
    if [[ -f "${candidate}" ]]; then
      printf '%s\n' "${candidate}"
      return 0
    fi
  done

  return 1
}

pick_python() {
  local candidate
  for candidate in "${PYTHON_BIN:-}" python3.11 python3; do
    if [[ -n "${candidate}" ]] && command -v "${candidate}" >/dev/null 2>&1; then
      printf '%s\n' "${candidate}"
      return 0
    fi
  done
  echo "Python 3.11+ is required but no suitable interpreter was found." >&2
  exit 1
}

python_runtime_deps_ok() {
  ROOT_DIR="${ROOT_DIR}" python - <<'PY' >/dev/null 2>&1
from __future__ import annotations

import sys
from importlib.metadata import PackageNotFoundError, metadata, version
import os
from pathlib import Path

if sys.version_info < (3, 11):
    raise SystemExit("Python 3.11+ is required.")

import tomllib
from packaging.requirements import Requirement

root = Path(os.environ["ROOT_DIR"])
project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
dependencies = project.get("project", {}).get("dependencies", [])
fastmcp_req = next(
    Requirement(item)
    for item in dependencies
    if Requirement(item).name.lower() == "fastmcp"
)

try:
    installed_fastmcp = version("fastmcp")
except PackageNotFoundError as exc:
    raise SystemExit("fastmcp is not installed") from exc

if installed_fastmcp not in fastmcp_req.specifier:
    raise SystemExit(
        f"fastmcp {installed_fastmcp} does not satisfy {fastmcp_req.specifier}"
    )

try:
    project_metadata = metadata("chatgpt-web-oauth-mcp")
except PackageNotFoundError as exc:
    raise SystemExit("chatgpt-web-oauth-mcp is not installed") from exc

runtime_reqs = project_metadata.get_all("Requires-Dist") or []
metadata_fastmcp_reqs = [
    Requirement(item)
    for item in runtime_reqs
    if Requirement(item).name.lower() == "fastmcp"
]
expected_parts = set(str(fastmcp_req.specifier).split(","))
metadata_parts = [
    set(str(item.specifier).split(","))
    for item in metadata_fastmcp_reqs
]
if expected_parts and expected_parts not in metadata_parts:
    raise SystemExit(
        "installed editable metadata has stale fastmcp requirement: "
        + ", ".join(str(item.specifier) for item in metadata_fastmcp_reqs)
    )

import fastmcp  # noqa: F401
import chatgpt_web_oauth_mcp.launchd_support  # noqa: F401
import chatgpt_web_oauth_mcp.supervisor  # noqa: F401
import uvicorn  # noqa: F401
PY
}

ensure_python_runtime_deps() {
  if ! command -v chatgpt-web-oauth-mcp >/dev/null 2>&1 || ! python_runtime_deps_ok; then
    python -m pip install -e .
  fi
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

prepare_launchd_env() {
  local override_host="${CHATGPT_MCP_HOST:-}"
  local override_port="${CHATGPT_MCP_PORT:-}"
  local override_workspace_root="${CHATGPT_MCP_WORKSPACE_ROOT:-}"
  local override_state_dir="${CHATGPT_MCP_STATE_DIR:-}"
  local override_auth_token="${CHATGPT_MCP_AUTH_TOKEN:-}"
  local override_cloudflared_config="${CHATGPT_MCP_CLOUDFLARED_CONFIG:-}"
  local override_tunnel_name="${CHATGPT_MCP_TUNNEL_NAME:-}"
  local override_codex_command="${CHATGPT_MCP_CODEX_COMMAND:-}"
  local override_claude_command="${CHATGPT_MCP_CLAUDE_COMMAND:-}"
  local override_command_timeout="${CHATGPT_MCP_COMMAND_TIMEOUT:-}"
  local override_delegate_timeout="${CHATGPT_MCP_DELEGATE_TIMEOUT:-}"
  local override_debug_mcp_logging="${CHATGPT_MCP_DEBUG_MCP_LOGGING:-}"
  local override_graceful_shutdown_seconds="${CHATGPT_MCP_GRACEFUL_SHUTDOWN_SECONDS:-}"
  local override_watchdog_interval_seconds="${CHATGPT_MCP_WATCHDOG_INTERVAL_SECONDS:-}"
  local override_doctor_failure_threshold="${CHATGPT_MCP_DOCTOR_FAILURE_THRESHOLD:-}"
  local override_doctor_base_backoff_seconds="${CHATGPT_MCP_DOCTOR_BASE_BACKOFF_SECONDS:-}"
  local override_doctor_max_backoff_seconds="${CHATGPT_MCP_DOCTOR_MAX_BACKOFF_SECONDS:-}"
  local override_label_prefix="${CHATGPT_MCP_LAUNCHD_LABEL_PREFIX:-}"
  local override_external_cloudflared="${CHATGPT_MCP_EXTERNAL_CLOUDFLARED:-}"
  local override_enable_obsidian="${CHATGPT_MCP_ENABLE_OBSIDIAN:-}"
  local override_launchd_dir="${CHATGPT_MCP_LAUNCHD_DIR:-}"
  local override_launchd_log_dir="${CHATGPT_MCP_LAUNCHD_LOG_DIR:-}"
  local override_launchd_path="${CHATGPT_MCP_LAUNCHD_PATH:-}"
  local override_obsidian_api_key="${OBSIDIAN_API_KEY:-}"
  local override_obsidian_host="${OBSIDIAN_HOST:-}"
  local override_obsidian_port="${OBSIDIAN_PORT:-}"
  local override_obsidian_protocol="${OBSIDIAN_PROTOCOL:-}"
  local override_obsidian_mcp_url="${OBSIDIAN_MCP_URL:-}"
  local override_obsidian_verify_ssl="${OBSIDIAN_VERIFY_SSL:-}"
  local override_obsidian_timeout_seconds="${OBSIDIAN_TIMEOUT_SECONDS:-}"
  local override_tg_bot_token="${TG_BOT_TOKEN:-}"
  local override_tg_receiver_id="${TG_RECEIVER_ID:-}"
  local override_tg_notify_timeout_seconds="${TG_NOTIFY_TIMEOUT_SECONDS:-}"

  load_env_file

  export CHATGPT_MCP_HOST="${override_host:-${CHATGPT_MCP_HOST:-127.0.0.1}}"
  export CHATGPT_MCP_PORT="${override_port:-${CHATGPT_MCP_PORT:-8766}}"
  export CHATGPT_MCP_WORKSPACE_ROOT="${override_workspace_root:-${CHATGPT_MCP_WORKSPACE_ROOT:-${ROOT_DIR}}}"
  export CHATGPT_MCP_STATE_DIR="${override_state_dir:-${CHATGPT_MCP_STATE_DIR:-${HOME}/.chatgpt-web-oauth-mcp}}"
  export CHATGPT_MCP_CLOUDFLARED_CONFIG="${override_cloudflared_config:-${CHATGPT_MCP_CLOUDFLARED_CONFIG:-}}"
  export CHATGPT_MCP_TUNNEL_NAME="${override_tunnel_name:-${CHATGPT_MCP_TUNNEL_NAME:-}}"
  export CHATGPT_MCP_CODEX_COMMAND="${override_codex_command:-${CHATGPT_MCP_CODEX_COMMAND:-codex}}"
  export CHATGPT_MCP_CLAUDE_COMMAND="${override_claude_command:-${CHATGPT_MCP_CLAUDE_COMMAND:-claude}}"
  export CHATGPT_MCP_COMMAND_TIMEOUT="${override_command_timeout:-${CHATGPT_MCP_COMMAND_TIMEOUT:-120}}"
  export CHATGPT_MCP_DELEGATE_TIMEOUT="${override_delegate_timeout:-${CHATGPT_MCP_DELEGATE_TIMEOUT:-1800}}"
  export CHATGPT_MCP_DEBUG_MCP_LOGGING="${override_debug_mcp_logging:-${CHATGPT_MCP_DEBUG_MCP_LOGGING:-0}}"
  export CHATGPT_MCP_GRACEFUL_SHUTDOWN_SECONDS="${override_graceful_shutdown_seconds:-${CHATGPT_MCP_GRACEFUL_SHUTDOWN_SECONDS:-30}}"
  export CHATGPT_MCP_WATCHDOG_INTERVAL_SECONDS="${override_watchdog_interval_seconds:-${CHATGPT_MCP_WATCHDOG_INTERVAL_SECONDS:-60}}"
  export CHATGPT_MCP_DOCTOR_FAILURE_THRESHOLD="${override_doctor_failure_threshold:-${CHATGPT_MCP_DOCTOR_FAILURE_THRESHOLD:-3}}"
  export CHATGPT_MCP_DOCTOR_BASE_BACKOFF_SECONDS="${override_doctor_base_backoff_seconds:-${CHATGPT_MCP_DOCTOR_BASE_BACKOFF_SECONDS:-300}}"
  export CHATGPT_MCP_DOCTOR_MAX_BACKOFF_SECONDS="${override_doctor_max_backoff_seconds:-${CHATGPT_MCP_DOCTOR_MAX_BACKOFF_SECONDS:-3600}}"
  export CHATGPT_MCP_LAUNCHD_LABEL_PREFIX="${override_label_prefix:-${CHATGPT_MCP_LAUNCHD_LABEL_PREFIX:-com.chatgpt-web-oauth-mcp}}"
  export CHATGPT_MCP_LAUNCHD_DIR="${override_launchd_dir:-${CHATGPT_MCP_LAUNCHD_DIR:-${HOME}/Library/LaunchAgents}}"
  export CHATGPT_MCP_LAUNCHD_LOG_DIR="${override_launchd_log_dir:-${CHATGPT_MCP_LAUNCHD_LOG_DIR:-${HOME}/Library/Logs/chatgpt-web-oauth-mcp}}"
  export CHATGPT_MCP_LAUNCHD_PATH="${override_launchd_path:-${CHATGPT_MCP_LAUNCHD_PATH:-${CURRENT_SHELL_PATH}}}"
  export CHATGPT_MCP_EXTERNAL_CLOUDFLARED="${override_external_cloudflared:-${CHATGPT_MCP_EXTERNAL_CLOUDFLARED:-0}}"
  export CHATGPT_MCP_ENABLE_OBSIDIAN="${override_enable_obsidian:-${CHATGPT_MCP_ENABLE_OBSIDIAN:-0}}"
  export OBSIDIAN_HOST="${override_obsidian_host:-${OBSIDIAN_HOST:-127.0.0.1}}"
  export OBSIDIAN_PORT="${override_obsidian_port:-${OBSIDIAN_PORT:-27124}}"
  export OBSIDIAN_PROTOCOL="${override_obsidian_protocol:-${OBSIDIAN_PROTOCOL:-https}}"
  export OBSIDIAN_MCP_URL="${override_obsidian_mcp_url:-${OBSIDIAN_MCP_URL:-}}"
  export OBSIDIAN_VERIFY_SSL="${override_obsidian_verify_ssl:-${OBSIDIAN_VERIFY_SSL:-0}}"
  export OBSIDIAN_TIMEOUT_SECONDS="${override_obsidian_timeout_seconds:-${OBSIDIAN_TIMEOUT_SECONDS:-10}}"
  export TG_RECEIVER_ID="${override_tg_receiver_id:-${TG_RECEIVER_ID:-}}"
  export TG_NOTIFY_TIMEOUT_SECONDS="${override_tg_notify_timeout_seconds:-${TG_NOTIFY_TIMEOUT_SECONDS:-5}}"

  if [[ -n "${override_auth_token}" ]]; then
    export CHATGPT_MCP_AUTH_TOKEN="${override_auth_token}"
  fi
  if [[ -n "${override_obsidian_api_key}" ]]; then
    export OBSIDIAN_API_KEY="${override_obsidian_api_key}"
  elif [[ -n "${OBSIDIAN_API_KEY:-}" ]]; then
    export OBSIDIAN_API_KEY
  fi
  if [[ -n "${override_tg_bot_token}" ]]; then
    export TG_BOT_TOKEN="${override_tg_bot_token}"
  elif [[ -n "${TG_BOT_TOKEN:-}" ]]; then
    export TG_BOT_TOKEN
  fi
}

mcp_label() {
  printf '%s.mcp\n' "${CHATGPT_MCP_LAUNCHD_LABEL_PREFIX}"
}

cloudflared_label() {
  printf '%s.cloudflared\n' "${CHATGPT_MCP_LAUNCHD_LABEL_PREFIX}"
}

watchdog_label() {
  printf '%s.watchdog\n' "${CHATGPT_MCP_LAUNCHD_LABEL_PREFIX}"
}

launchctl_target() {
  local label="$1"
  printf 'gui/%s/%s\n' "${UID}" "${label}"
}

plist_path_for_label() {
  local label="$1"
  printf '%s/%s.plist\n' "${CHATGPT_MCP_LAUNCHD_DIR}" "${label}"
}
