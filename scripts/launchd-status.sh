#!/usr/bin/env bash
set -euo pipefail

# shellcheck disable=SC1091
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/launchd-common.sh"

prepare_launchd_env
require_command launchctl

labels=("$(mcp_label)" "$(watchdog_label)")
if [[ "${CHATGPT_MCP_EXTERNAL_CLOUDFLARED:-0}" != "1" ]]; then
  labels=("$(mcp_label)" "$(cloudflared_label)" "$(watchdog_label)")
else
  echo "External cloudflared mode enabled; skipping managed cloudflared launchd status."
  echo
fi

for label in "${labels[@]}"; do
  target="$(launchctl_target "${label}")"
  echo "=== ${target} ==="
  if launchctl print "${target}" >/tmp/chatgpt-web-oauth-mcp-launchctl.print 2>&1; then
    sed -n '1,40p' /tmp/chatgpt-web-oauth-mcp-launchctl.print
  else
    cat /tmp/chatgpt-web-oauth-mcp-launchctl.print
  fi
  echo
  rm -f /tmp/chatgpt-web-oauth-mcp-launchctl.print
done

echo "=== local MCP ==="
if curl -fsSI "http://${CHATGPT_MCP_HOST}:${CHATGPT_MCP_PORT}/mcp" >/dev/null 2>&1; then
  curl -sSI "http://${CHATGPT_MCP_HOST}:${CHATGPT_MCP_PORT}/mcp" | sed -n '1,12p'
else
  echo "Local /mcp is not reachable"
fi

PUBLIC_URL=""
if [[ -n "${CHATGPT_MCP_PUBLIC_BASE_URL:-}" ]]; then
  PUBLIC_URL="${CHATGPT_MCP_PUBLIC_BASE_URL%/}/mcp"
elif CLOUDFLARED_CONFIG="$(pick_cloudflared_config 2>/dev/null || true)"; then
  hostname=$(awk '/hostname:/{print $3; exit}' "${CLOUDFLARED_CONFIG}" 2>/dev/null || true)
  if [[ -n "${hostname}" ]]; then
    PUBLIC_URL="https://${hostname}/mcp"
  fi
fi

if [[ -n "${PUBLIC_URL}" ]]; then
  echo
  echo "=== public MCP ==="
  if curl -fsSI --max-time 10 "${PUBLIC_URL}" >/dev/null 2>&1; then
    curl -sSI --max-time 10 "${PUBLIC_URL}" | sed -n '1,12p'
  else
    echo "Public /mcp is not reachable at ${PUBLIC_URL}"
  fi
fi

echo
echo "Logs: ${CHATGPT_MCP_LAUNCHD_LOG_DIR}"
echo
echo "Run ./scripts/launchd-doctor.sh --fix for an immediate health check and targeted restart."
