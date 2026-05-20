#!/usr/bin/env bash
set -euo pipefail

# shellcheck disable=SC1091
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/launchd-common.sh"

FIX=0
QUIET=0
LOCAL_WAIT_SECONDS="${CHATGPT_MCP_DOCTOR_LOCAL_WAIT_SECONDS:-20}"
PUBLIC_WAIT_SECONDS="${CHATGPT_MCP_DOCTOR_PUBLIC_WAIT_SECONDS:-30}"
FAIL_THRESHOLD="${CHATGPT_MCP_DOCTOR_FAILURE_THRESHOLD:-3}"
BASE_BACKOFF_SECONDS="${CHATGPT_MCP_DOCTOR_BASE_BACKOFF_SECONDS:-300}"
MAX_BACKOFF_SECONDS="${CHATGPT_MCP_DOCTOR_MAX_BACKOFF_SECONDS:-3600}"
STATE_FILE="${CHATGPT_MCP_DOCTOR_STATE_FILE:-}"

usage() {
  cat >&2 <<'USAGE'
Usage: ./scripts/launchd-doctor.sh [--fix] [--quiet]

Checks local /mcp and public cloudflared /mcp. With --fix, restarts only a
failed layer after sustained failures. Public checks bypass local proxy env by
default; proxy reachability is logged only as diagnostic evidence.

Auto-fix guardrails:
  - local /mcp down: restart mcp after sustained local failures
  - public /mcp down while local is healthy: restart cloudflared after sustained
    public failures
  - restarts use exponential backoff, capped by
    CHATGPT_MCP_DOCTOR_MAX_BACKOFF_SECONDS
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --fix)
      FIX=1
      ;;
    --quiet)
      QUIET=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage
      exit 2
      ;;
  esac
  shift
done

prepare_launchd_env
require_command curl
require_command launchctl

if [[ -z "${STATE_FILE}" ]]; then
  STATE_FILE="${CHATGPT_MCP_STATE_DIR}/launchd-doctor.state"
fi

MCP_LABEL="$(mcp_label)"
CLOUDFLARED_LABEL="$(cloudflared_label)"
MCP_TARGET="$(launchctl_target "${MCP_LABEL}")"
CLOUDFLARED_TARGET="$(launchctl_target "${CLOUDFLARED_LABEL}")"
LOCAL_URL="http://${CHATGPT_MCP_HOST}:${CHATGPT_MCP_PORT}/mcp"
PUBLIC_URL=""

log() {
  if [[ "${QUIET}" != "1" ]]; then
    printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"
  fi
}

warn() {
  printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >&2
}

if CLOUDFLARED_CONFIG="$(pick_cloudflared_config 2>/dev/null || true)"; then
  hostname="$(awk '
    /hostname:/ {
      for (i = 1; i <= NF; i++) {
        if ($i == "hostname:") { print $(i + 1); exit }
        if ($i ~ /^hostname:/) { sub(/^hostname:/, "", $i); print $i; exit }
      }
    }
  ' "${CLOUDFLARED_CONFIG}" 2>/dev/null || true)"
  if [[ -n "${hostname}" ]]; then
    PUBLIC_URL="https://${hostname}/mcp"
  fi
fi

service_loaded() {
  local target="$1"
  launchctl print "${target}" >/dev/null 2>&1
}

head_ok_direct() {
  local url="$1"
  local max_time="$2"
  env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy -u all_proxy \
    curl --noproxy '*' -fsSI --max-time "${max_time}" "${url}" >/dev/null 2>&1
}

head_ok_env() {
  local url="$1"
  local max_time="$2"
  curl -fsSI --max-time "${max_time}" "${url}" >/dev/null 2>&1
}

wait_head_ok_direct() {
  local url="$1"
  local max_time="$2"
  local deadline_seconds="$3"
  local start now
  start="$(date +%s)"
  while true; do
    if head_ok_direct "${url}" "${max_time}"; then
      return 0
    fi
    now="$(date +%s)"
    if (( now - start >= deadline_seconds )); then
      return 1
    fi
    sleep 2
  done
}

state_local_failures=0
state_public_failures=0
state_mcp_restart_attempts=0
state_cloudflared_restart_attempts=0
state_mcp_next_restart_after=0
state_cloudflared_next_restart_after=0

load_state() {
  [[ -f "${STATE_FILE}" ]] || return 0
  while IFS='=' read -r key value; do
    case "${key}" in
      local_failures|public_failures|mcp_restart_attempts|cloudflared_restart_attempts|mcp_next_restart_after|cloudflared_next_restart_after)
        [[ "${value}" =~ ^[0-9]+$ ]] || value=0
        printf -v "state_${key}" '%s' "${value}"
        ;;
    esac
  done < "${STATE_FILE}"
}

save_state() {
  mkdir -p "$(dirname "${STATE_FILE}")"
  cat > "${STATE_FILE}" <<STATE
local_failures=${state_local_failures}
public_failures=${state_public_failures}
mcp_restart_attempts=${state_mcp_restart_attempts}
cloudflared_restart_attempts=${state_cloudflared_restart_attempts}
mcp_next_restart_after=${state_mcp_next_restart_after}
cloudflared_next_restart_after=${state_cloudflared_next_restart_after}
STATE
  chmod 600 "${STATE_FILE}" 2>/dev/null || true
}

reset_local_state() {
  state_local_failures=0
  state_mcp_restart_attempts=0
  state_mcp_next_restart_after=0
}

reset_public_state() {
  state_public_failures=0
  state_cloudflared_restart_attempts=0
  state_cloudflared_next_restart_after=0
}

backoff_for_attempt() {
  local attempt="$1"
  local backoff="${BASE_BACKOFF_SECONDS}"
  local i
  for ((i = 1; i < attempt; i++)); do
    backoff=$(( backoff * 2 ))
    if (( backoff >= MAX_BACKOFF_SECONDS )); then
      backoff="${MAX_BACKOFF_SECONDS}"
      break
    fi
  done
  printf '%s\n' "${backoff}"
}

restart_service() {
  local target="$1"
  local name="$2"
  warn "Restarting ${name}: ${target}"
  launchctl kickstart -k "${target}"
}

maybe_restart_mcp() {
  local now backoff
  now="$(date +%s)"
  if (( state_local_failures < FAIL_THRESHOLD )); then
    warn "Local /mcp failure ${state_local_failures}/${FAIL_THRESHOLD}; not restarting yet."
    return 1
  fi
  if (( now < state_mcp_next_restart_after )); then
    warn "Local /mcp still failing, but mcp restart is in backoff for $(( state_mcp_next_restart_after - now ))s."
    return 1
  fi
  state_mcp_restart_attempts=$(( state_mcp_restart_attempts + 1 ))
  backoff="$(backoff_for_attempt "${state_mcp_restart_attempts}")"
  state_mcp_next_restart_after=$(( now + backoff ))
  save_state
  restart_service "${MCP_TARGET}" "mcp"
  if wait_head_ok_direct "${LOCAL_URL}" 5 "${LOCAL_WAIT_SECONDS}"; then
    warn "Recovered local /mcp: ${LOCAL_URL}"
    reset_local_state
    save_state
    return 0
  fi
  warn "MCP restart did not recover local /mcp within ${LOCAL_WAIT_SECONDS}s. Next restart allowed in ${backoff}s. Check ${CHATGPT_MCP_LAUNCHD_LOG_DIR}/mcp-server.log"
  return 1
}

maybe_restart_cloudflared() {
  local now backoff
  now="$(date +%s)"
  if (( state_public_failures < FAIL_THRESHOLD )); then
    warn "Public /mcp failure ${state_public_failures}/${FAIL_THRESHOLD}; not restarting cloudflared yet."
    return 1
  fi
  if (( now < state_cloudflared_next_restart_after )); then
    warn "Public /mcp still failing, but cloudflared restart is in backoff for $(( state_cloudflared_next_restart_after - now ))s."
    return 1
  fi
  state_cloudflared_restart_attempts=$(( state_cloudflared_restart_attempts + 1 ))
  backoff="$(backoff_for_attempt "${state_cloudflared_restart_attempts}")"
  state_cloudflared_next_restart_after=$(( now + backoff ))
  save_state
  restart_service "${CLOUDFLARED_TARGET}" "cloudflared"
  if wait_head_ok_direct "${PUBLIC_URL}" 10 "${PUBLIC_WAIT_SECONDS}"; then
    warn "Recovered public /mcp: ${PUBLIC_URL}"
    reset_public_state
    save_state
    return 0
  fi
  warn "cloudflared restart did not recover public /mcp within ${PUBLIC_WAIT_SECONDS}s. Next restart allowed in ${backoff}s. Check ${CHATGPT_MCP_LAUNCHD_LOG_DIR}/cloudflared.stderr.log"
  return 1
}

load_state

if ! service_loaded "${MCP_TARGET}"; then
  warn "MCP launchd service is not loaded: ${MCP_TARGET}"
  exit 1
fi
if ! service_loaded "${CLOUDFLARED_TARGET}"; then
  warn "cloudflared launchd service is not loaded: ${CLOUDFLARED_TARGET}"
  exit 1
fi

log "=== local MCP ==="
if head_ok_direct "${LOCAL_URL}" 5; then
  log "OK direct ${LOCAL_URL}"
  reset_local_state
else
  state_local_failures=$(( state_local_failures + 1 ))
  warn "Local /mcp direct check failed: ${LOCAL_URL}"
  save_state
  if [[ "${FIX}" == "1" ]]; then
    maybe_restart_mcp || exit 1
    exit 0
  fi
  exit 1
fi

if [[ -z "${PUBLIC_URL}" ]]; then
  log "No cloudflared hostname configured; skipped public /mcp check."
  reset_public_state
  save_state
  exit 0
fi

log "=== public MCP ==="
if head_ok_direct "${PUBLIC_URL}" 10; then
  log "OK direct ${PUBLIC_URL}"
  reset_public_state
  save_state
  exit 0
fi

state_public_failures=$(( state_public_failures + 1 ))
warn "Public /mcp direct check failed while local /mcp is healthy: ${PUBLIC_URL}"
if head_ok_env "${PUBLIC_URL}" 10; then
  warn "Public /mcp succeeds with current proxy env; treating this as local egress/proxy-path mismatch, not cloudflared failure."
else
  warn "Public /mcp also fails with current proxy env."
fi
save_state

if [[ "${FIX}" == "1" ]]; then
  maybe_restart_cloudflared || exit 1
  exit 0
fi
exit 1
