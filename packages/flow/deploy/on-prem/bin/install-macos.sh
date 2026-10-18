#!/usr/bin/env bash
#
# install-macos.sh: install the openadapt-flow on-prem service on macOS.
#
#   install-macos.sh --root ROOT [--openadapt-flow PATH]
#                    [--session-profile FILE] [--no-pf] [--auto-login]
#   install-macos.sh --refresh-pf --root ROOT      (re-render pf after an allow-list edit)
#   install-macos.sh --uninstall
#   install-macos.sh --render-only DIR --root ROOT [...]  (write files, apply nothing)
#
# Run it as the service account (a dedicated macOS user). It needs
# administrator rights (sudo) for the boot-time daemons and the pf rules,
# and stops with a clear message if they are not granted.
#
# What it installs:
#   ~/Library/LaunchAgents/ai.openadapt.flow-service  the queue loop (KeepAlive)
#   ~/Library/LaunchAgents/ai.openadapt.flow-health   every 60 s; restarts when unhealthy
#   /Library/LaunchDaemons/ai.openadapt.flow-boot-check  at boot: waits for sign-in, alerts if late
#   /Library/LaunchDaemons/ai.openadapt.flow-pf       at boot: enables pf (strict mode)
#   /etc/pf.anchors/ai.openadapt.flow                 outbound rules for the service account
#
# Strict no-outbound mode is the default. The allow list starts empty (only
# this machine). --session-profile adds the host(s) of that remote-desktop
# session (FreeRDP session JSON or .rdp file), and nothing else, and prints
# what it added. Edit the list later with `openadapt-flow no-outbound allow`.
#
# After a restart the machine does not sign in by itself: the service waits
# for a sign-in and the boot check opens a notice and sends any configured
# alerts. --auto-login records that this site opts into automatic login;
# turning it on in macOS is a separate, documented step (docs/ON_PREM.md).

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT=""
OAF="$(command -v openadapt-flow || true)"
RENDER_ONLY=""
UNINSTALL=0
REFRESH_PF=0
SESSION_PROFILE=""
NO_PF=0
AUTO_LOGIN=0
SERVICE_USER="$(id -un)"
AGENT_LABELS=(ai.openadapt.flow-service ai.openadapt.flow-health)
DAEMON_LABELS=(ai.openadapt.flow-boot-check ai.openadapt.flow-pf)
ANCHOR=ai.openadapt.flow
ANCHOR_PATH="/etc/pf.anchors/$ANCHOR"
PF_CONF=/etc/pf.conf

die() { echo "install-macos.sh: $*" >&2; exit "${2:-2}"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --root) ROOT="${2:-}"; shift 2 ;;
    --openadapt-flow) OAF="${2:-}"; shift 2 ;;
    --render-only) RENDER_ONLY="${2:-}"; shift 2 ;;
    --session-profile) SESSION_PROFILE="${2:-}"; shift 2 ;;
    --no-pf) NO_PF=1; shift ;;
    --auto-login) AUTO_LOGIN=1; shift ;;
    --refresh-pf) REFRESH_PF=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown arg $1" ;;
  esac
done

AGENTS="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"

need_admin() {
  [[ -n "$RENDER_ONLY" ]] && return 0
  if ! sudo -v; then
    die "administrator rights were not granted. They are needed for the boot-time
daemons and the pf rules that hold the service account to the allow list.
Re-run as a user who can sudo. To install without pf rules, pass --no-pf
(the service then relies on the in-process hook and connection sampling,
and the health check reports it)." 3
  fi
}

if [[ "$UNINSTALL" == 1 ]]; then
  for label in "${AGENT_LABELS[@]}"; do
    launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
    rm -f "$AGENTS/$label.plist"
  done
  need_admin
  for label in "${DAEMON_LABELS[@]}"; do
    sudo launchctl bootout "system/$label" 2>/dev/null || true
    sudo rm -f "/Library/LaunchDaemons/$label.plist"
  done
  if [[ -f "$ANCHOR_PATH" ]]; then
    sudo pfctl -a "$ANCHOR" -F rules 2>/dev/null || true
    sudo rm -f "$ANCHOR_PATH"
  fi
  echo "removed ${AGENT_LABELS[*]} ${DAEMON_LABELS[*]} and the $ANCHOR pf anchor"
  echo "the anchor lines in $PF_CONF are left in place (they load nothing now)"
  exit 0
fi

[[ -n "$ROOT" ]] || die "--root is required"
[[ -n "$OAF" && -x "$OAF" ]] || die "openadapt-flow not found; pass --openadapt-flow PATH"
case "$ROOT" in /*) ;; *) die "--root must be absolute" ;; esac

OUT="${RENDER_ONLY:-$AGENTS}"
DAEMON_OUT="${RENDER_ONLY:-/Library/LaunchDaemons}"
ALLOW_FILE="$ROOT/no-outbound-allow.json"
mkdir -p "$OUT" "$ROOT/service" "$ROOT/audit" "$ROOT/jobs/inbox"
chmod 700 "$ROOT/service" "$ROOT/audit"
VENV_BIN="$(dirname "$OAF")"

render() {
  # Placeholders are absolute paths or a user name; '|' cannot appear in them.
  sed -e "s|@OPENADAPT_FLOW@|$OAF|g" \
      -e "s|@STORAGE_ROOT@|$ROOT|g" \
      -e "s|@HERE@|$HERE|g" \
      -e "s|@SERVICE_USER@|$SERVICE_USER|g" \
      -e "s|@VENV_BIN@|$VENV_BIN|g" "$1"
}

lint() {
  if command -v plutil >/dev/null 2>&1; then
    plutil -lint "$1" >/dev/null || die "invalid plist: $1" 1
  fi
}

# -- allow list: empty unless a session profile names the host ------------
if [[ ! -f "$ALLOW_FILE" ]]; then
  printf '{\n  "entries": [],\n  "schema": "openadapt.no-outbound-allow/v1"\n}\n' > "$ALLOW_FILE"
  chmod 644 "$ALLOW_FILE"
fi
if [[ -n "$SESSION_PROFILE" ]]; then
  echo "no-outbound allow list: deriving from $SESSION_PROFILE"
  "$OAF" no-outbound allow --file "$ALLOW_FILE" derive "$SESSION_PROFILE" \
    || die "could not derive the allow list from $SESSION_PROFILE" 1
fi
echo "no-outbound allow list ($ALLOW_FILE):"
"$OAF" no-outbound allow --file "$ALLOW_FILE" list | sed 's/^/  /'

# -- service settings: reboot policy and (opt-in) alert channels ----------
SETTINGS="$ROOT/service/service.json"
if [[ ! -f "$SETTINGS" ]]; then
  printf '{\n  "alerts": {"channels": []},\n  "reboot": {"alert_after_s": 300, "auto_login": false},\n  "schema": "openadapt.onprem-service-settings/v1"\n}\n' > "$SETTINGS"
  chmod 600 "$SETTINGS"
fi
if [[ "$AUTO_LOGIN" == 1 ]]; then
  sed -i.bak 's/"auto_login": false/"auto_login": true/' "$SETTINGS" && rm -f "$SETTINGS.bak"
  echo "recorded: this site opts into automatic login (see docs/ON_PREM.md to turn it on in macOS)"
fi

# -- pf rules (strict mode default) ----------------------------------------
render_pf() {
  "$OAF" service pf render --user "$SERVICE_USER" --allow-file "$ALLOW_FILE" --out "$1" \
    || die "could not render pf rules" 1
  "$OAF" service pf check "$1" --user "$SERVICE_USER" || die "rendered pf rules failed the check" 1
}

record_firewall() {
  printf '{\n  "at": "%s",\n  "pf": "%s",\n  "user": "%s"\n}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "$SERVICE_USER" > "$ROOT/service/firewall.json"
}

apply_pf() {
  local rendered="$1"
  sudo install -d -m 755 /etc/pf.anchors
  sudo install -m 644 "$rendered" "$ANCHOR_PATH"
  sudo pfctl -a "$ANCHOR" -nf "$ANCHOR_PATH" || die "pfctl rejected $ANCHOR_PATH" 1
  if ! grep -qxF "anchor \"$ANCHOR\"" "$PF_CONF"; then
    sudo cp "$PF_CONF" "$PF_CONF.openadapt-backup"
    printf '\nanchor "%s"\nload anchor "%s" from "%s"\n' "$ANCHOR" "$ANCHOR" "$ANCHOR_PATH" \
      | sudo tee -a "$PF_CONF" >/dev/null
  fi
  sudo pfctl -nf "$PF_CONF" || die "pfctl rejected $PF_CONF (backup: $PF_CONF.openadapt-backup)" 1
  sudo pfctl -E -f "$PF_CONF" 2>/dev/null || die "pfctl could not load $PF_CONF" 1
}

if [[ "$REFRESH_PF" == 1 ]]; then
  need_admin
  TMP="$(mktemp)"; render_pf "$TMP"; apply_pf "$TMP"; rm -f "$TMP"
  record_firewall installed
  echo "pf rules for $SERVICE_USER reloaded from $ALLOW_FILE"
  exit 0
fi

if [[ "$NO_PF" == 0 ]]; then
  render_pf "$OUT/$ANCHOR.pf"
fi

for label in "${AGENT_LABELS[@]}"; do
  render "$HERE/launchd/$label.plist" > "$OUT/$label.plist"
  lint "$OUT/$label.plist"
done
DAEMON_TMP="${RENDER_ONLY:-$(mktemp -d)}"
for label in "${DAEMON_LABELS[@]}"; do
  [[ "$label" == ai.openadapt.flow-pf && "$NO_PF" == 1 ]] && continue
  render "$HERE/launchd/$label.plist" > "$DAEMON_TMP/$label.plist"
  lint "$DAEMON_TMP/$label.plist"
done

if [[ -n "$RENDER_ONLY" ]]; then
  echo "rendered to $OUT"
  exit 0
fi

need_admin

if [[ "$NO_PF" == 0 ]]; then
  apply_pf "$OUT/$ANCHOR.pf"
  rm -f "$OUT/$ANCHOR.pf"
  record_firewall installed
  echo "pf: outbound TCP/UDP for $SERVICE_USER limited to loopback and the allow list"
else
  record_firewall declined
  echo "pf: not installed (--no-pf); the health check will report this"
fi

for label in "${DAEMON_LABELS[@]}"; do
  [[ -f "$DAEMON_TMP/$label.plist" ]] || continue
  sudo install -m 644 -o root -g wheel "$DAEMON_TMP/$label.plist" "$DAEMON_OUT/$label.plist"
  sudo launchctl bootout "system/$label" 2>/dev/null || true
  sudo launchctl bootstrap system "$DAEMON_OUT/$label.plist"
done
rm -rf "$DAEMON_TMP"

for label in "${AGENT_LABELS[@]}"; do
  launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
  launchctl bootstrap "$DOMAIN" "$AGENTS/$label.plist"
done
echo "installed: ${AGENT_LABELS[*]} ${DAEMON_LABELS[*]}"
echo "check:   $OAF service health --root $ROOT"
echo "audit:   $OAF service egress-audit --root $ROOT"
echo "notices: $OAF service notices --root $ROOT"
