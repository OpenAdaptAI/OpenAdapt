#!/usr/bin/env bash
#
# install-macos.sh: install the openadapt-flow on-prem service as launchd
# agents for the current user.
#
#   install-macos.sh --root /Users/Shared/openadapt [--openadapt-flow PATH]
#   install-macos.sh --uninstall
#   install-macos.sh --render-only DIR --root ROOT   (write plists, load nothing)
#
# Installs two LaunchAgents in ~/Library/LaunchAgents:
#   ai.openadapt.flow-service  the queue loop; KeepAlive restarts it on exit
#   ai.openadapt.flow-health   every 60 s; restarts the service when unhealthy
#
# A LaunchAgent runs in the user's login session, which a GUI or RDP client
# window needs. It starts at login; enable automatic login for the service
# account if the machine must resume unattended after a reboot.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT=""
OAF="$(command -v openadapt-flow || true)"
RENDER_ONLY=""
UNINSTALL=0
LABELS=(ai.openadapt.flow-service ai.openadapt.flow-health)

while [[ $# -gt 0 ]]; do
  case "$1" in
    --root) ROOT="${2:-}"; shift 2 ;;
    --openadapt-flow) OAF="${2:-}"; shift 2 ;;
    --render-only) RENDER_ONLY="${2:-}"; shift 2 ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "install-macos.sh: unknown arg $1" >&2; exit 2 ;;
  esac
done

AGENTS="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"

if [[ "$UNINSTALL" == 1 ]]; then
  for label in "${LABELS[@]}"; do
    launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
    rm -f "$AGENTS/$label.plist"
  done
  echo "removed ${LABELS[*]}"
  exit 0
fi

[[ -n "$ROOT" ]] || { echo "install-macos.sh: --root is required" >&2; exit 2; }
[[ -n "$OAF" && -x "$OAF" ]] || {
  echo "install-macos.sh: openadapt-flow not found; pass --openadapt-flow PATH" >&2; exit 2; }
case "$ROOT" in /*) ;; *) echo "install-macos.sh: --root must be absolute" >&2; exit 2 ;; esac

OUT="${RENDER_ONLY:-$AGENTS}"
mkdir -p "$OUT" "$ROOT/service" "$ROOT/audit" "$ROOT/jobs/inbox"
chmod 700 "$ROOT/service" "$ROOT/audit"
VENV_BIN="$(dirname "$OAF")"

render() {
  # Placeholders are absolute paths; '|' cannot appear in them unescaped.
  sed -e "s|@OPENADAPT_FLOW@|$OAF|g" \
      -e "s|@STORAGE_ROOT@|$ROOT|g" \
      -e "s|@HERE@|$HERE|g" \
      -e "s|@VENV_BIN@|$VENV_BIN|g" "$1"
}

for label in "${LABELS[@]}"; do
  render "$HERE/launchd/$label.plist" > "$OUT/$label.plist"
  plutil -lint "$OUT/$label.plist" >/dev/null 2>&1 || {
    command -v plutil >/dev/null 2>&1 && { echo "invalid plist: $OUT/$label.plist" >&2; exit 1; }
  }
done

if [[ -n "$RENDER_ONLY" ]]; then
  echo "rendered to $OUT"
  exit 0
fi

for label in "${LABELS[@]}"; do
  launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
  launchctl bootstrap "$DOMAIN" "$AGENTS/$label.plist"
done
echo "installed: ${LABELS[*]}"
echo "check:   $OAF service health --root $ROOT"
echo "audit:   $OAF service egress-audit --root $ROOT"
