#!/usr/bin/env bash
set -euo pipefail

# shellcheck disable=SC1091
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/launchd-common.sh"

prepare_launchd_env
require_command launchctl

for label in "$(watchdog_label)" "$(mcp_label)" "$(cloudflared_label)"; do
  target="$(launchctl_target "${label}")"
  plist_path="$(plist_path_for_label "${label}")"
  launchctl bootout "${target}" 2>/dev/null || true
  rm -f "${plist_path}"
  echo "Removed ${target} (${plist_path})"
done

echo "Launchd services removed. Logs remain under ${CHATGPT_MCP_LAUNCHD_LOG_DIR}."
