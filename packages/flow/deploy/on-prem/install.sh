#!/usr/bin/env bash
#
# install.sh — stand up / update the openadapt-flow on-prem (air-gapped) install.
#
# REAL for the parts marked [REAL]; a documented STUB only for the one part that
# needs site-specific hardware input (full-disk encryption). Nothing here reaches
# the internet: no PyPI, no phone-home, no telemetry.
#
# What it does:
#   [REAL] create the storage layout (bundles/runs/jobs/audit/keys) with tight
#          perms under storage_root;
#   [REAL] create the FIRST versioned release (releases/<version>/venv) from a
#          LOCAL WHEELHOUSE (no PyPI/internet) and point `current` at it;
#   [REAL] install the systemd unit + .path watcher (Linux, when --systemd);
#   [REAL] --update: verify a staged offline release, build it in a NEW release
#          dir, smoke-check it, then ATOMICALLY flip `current` (rollback-able);
#   [REAL] --rollback: instantly revert `current` to the previous release;
#   [REAL] run verify-airgap.sh as an acceptance gate at the end;
#   [STUB] full-disk encryption (operator provisions LUKS/BitLocker/FileVault).
#
# Usage:
#   # first install
#   sudo ./install.sh --config onprem.yaml [--wheelhouse ./wheels] [--systemd]
#
#   # apply a staged, signed offline release (atomic; verifies before flipping)
#   sudo ./install.sh --update --config onprem.yaml \
#        [--release ./release-1.8.1.tar.gz] [--checksum ...] \
#        --signature ... --pubkey ... --sig-tool gpg|openssl|minisign|signify
#
#   # revert to the previous release instantly (no rebuild)
#   sudo ./install.sh --rollback --config onprem.yaml
#
# Air-gapped installs use a wheelhouse copied in on removable media (`pip
# download` on a connected build host, then `--wheelhouse`). See UPDATE.md for
# the full update/rollback runbook and how to build a signed release bundle.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=bin/lib-release.sh
source "$HERE/bin/lib-release.sh"

CONFIG=""
WHEELHOUSE=""
DO_SYSTEMD=0
DO_UPDATE=0
DO_ROLLBACK=0
REL_ARCHIVE=""
REL_CHECKSUM=""
REL_SIG=""
REL_PUBKEY=""
REL_SIGTOOL=""
REL_VERSION=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)     CONFIG="${2:-}"; shift 2 ;;
    --wheelhouse) WHEELHOUSE="${2:-}"; shift 2 ;;
    --systemd)    DO_SYSTEMD=1; shift ;;
    --update)     DO_UPDATE=1; shift ;;
    --rollback)   DO_ROLLBACK=1; shift ;;
    --release)    REL_ARCHIVE="${2:-}"; shift 2 ;;
    --checksum)   REL_CHECKSUM="${2:-}"; shift 2 ;;
    --signature)  REL_SIG="${2:-}"; shift 2 ;;
    --pubkey)     REL_PUBKEY="${2:-}"; shift 2 ;;
    --sig-tool)   REL_SIGTOOL="${2:-}"; shift 2 ;;
    --release-version) REL_VERSION="${2:-}"; shift 2 ;;
    -h|--help)    grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "install.sh: unknown arg $1" >&2; exit 2 ;;
  esac
done

[[ -z "$CONFIG" ]] && { echo "install.sh: --config onprem.yaml is required" >&2; exit 2; }
[[ -f "$CONFIG" ]] || { echo "install.sh: config $CONFIG not found" >&2; exit 2; }
if [[ "$DO_UPDATE" -eq 1 && "$DO_ROLLBACK" -eq 1 ]]; then
  echo "install.sh: --update and --rollback are mutually exclusive" >&2
  exit 2
fi
if [[ "$(id -u)" -ne 0 ]]; then
  echo "install.sh: run as root so release code remains immutable to the service user" >&2
  exit 1
fi

# Minimal YAML scalar reader (top-level or one-level-nested "key: value"). The
# on-prem config is a flat, operator-edited file; we avoid a YAML dependency in
# the bootstrap path so install.sh runs before the venv exists.
cfg() {
  local key="$1" value=""
  value="$(grep -E "^[[:space:]]*${key}[[:space:]]*:" "$CONFIG" | head -n1 \
    | sed -E "s/^[[:space:]]*${key}[[:space:]]*:[[:space:]]*//; s/[[:space:]]*#.*$//; s/^\"//; s/\"$//" \
    | tr -d "'")" || true
  printf '%s' "$value"
}

STORAGE_ROOT="$(cfg storage_root)"; STORAGE_ROOT="${STORAGE_ROOT:-/srv/openadapt}"
SERVICE_USER="$(cfg service_user)"; SERVICE_USER="${SERVICE_USER:-openadapt}"
SCRUB="$(cfg scrub)"; SCRUB="${SCRUB:-on}"

# Release code and activation pointers are root-controlled. Runtime-writable
# data receives service-user ownership separately below.
harden_release_control() {
  mkdir -p "$STORAGE_ROOT/releases"
  chown root:root "$STORAGE_ROOT" "$STORAGE_ROOT/releases"
  chmod 0755 "$STORAGE_ROOT" "$STORAGE_ROOT/releases"
  chown -RP root:root "$STORAGE_ROOT/releases"
  chmod -R go-w "$STORAGE_ROOT/releases"
  for pointer in current previous venv rollback-forward; do
    if [[ -L "$STORAGE_ROOT/$pointer" ]]; then
      chown -h root:root "$STORAGE_ROOT/$pointer"
    fi
  done
}
harden_release_control

# Wire the audit log so update/rollback append a PHI-free record.
export OPENADAPT_ONPREM_AUDIT_BIN="$HERE/bin/audit-log.sh"
export OPENADAPT_ONPREM_AUDIT_LOG="${OPENADAPT_ONPREM_AUDIT_LOG:-$STORAGE_ROOT/audit/audit.log}"

restore_audit_ownership() {
  if id "$SERVICE_USER" >/dev/null 2>&1; then
    chown "$SERVICE_USER" "$STORAGE_ROOT/audit"
    if [[ -e "$OPENADAPT_ONPREM_AUDIT_LOG" ]]; then
      chown "$SERVICE_USER" "$OPENADAPT_ONPREM_AUDIT_LOG"
    fi
  fi
}

echo "openadapt-flow on-prem install"
echo "  storage_root : $STORAGE_ROOT"
echo "  service_user : $SERVICE_USER"
echo "  scrub        : $SCRUB"
echo

# ---------------------------------------------------------------------------
# --rollback : instant revert to the previous release (no rebuild, no network)
# ---------------------------------------------------------------------------
if [[ "$DO_ROLLBACK" -eq 1 ]]; then
  echo "== [REAL] rollback to previous release =="
  if rel_do_rollback "$STORAGE_ROOT" "$CONFIG"; then rc=0; else rc=$?; fi
  harden_release_control
  restore_audit_ownership
  exit "$rc"
fi

# ---------------------------------------------------------------------------
# --update : verify + build + smoke + atomic flip of a staged offline release
# ---------------------------------------------------------------------------
if [[ "$DO_UPDATE" -eq 1 ]]; then
  echo "== [REAL] offline signed-release update =="
  # Inputs come from flags first, then onprem.yaml:updates. Nothing is fetched.
  REL_ARCHIVE="${REL_ARCHIVE:-$(cfg release_archive)}"
  REL_SIG="${REL_SIG:-$(cfg release_signature)}"
  REL_PUBKEY="${REL_PUBKEY:-$(cfg vendor_pubkey)}"
  REL_CHECKSUM="${REL_CHECKSUM:-$(cfg release_checksum)}"
  REL_SIGTOOL="${REL_SIGTOOL:-$(cfg signature_tool)}"
  if [[ -z "$REL_ARCHIVE" ]]; then
    echo "install.sh: no release archive. Pass --release <archive> or set" >&2
    echo "  updates.release_archive in $CONFIG (staged from removable media)." >&2
    exit 2
  fi
  if [[ -z "$REL_SIG" || -z "$REL_PUBKEY" ]]; then
    echo "install.sh: updates require a detached signature and pinned vendor public key" >&2
    exit 2
  fi
  if rel_do_update "$STORAGE_ROOT" "$REL_ARCHIVE" "$REL_CHECKSUM" "$REL_SIG" \
    "$REL_PUBKEY" "$REL_SIGTOOL" "$CONFIG" "$REL_VERSION"; then rc=0; else rc=$?; fi
  harden_release_control
  restore_audit_ownership
  exit "$rc"
fi

# ---------------------------------------------------------------------------
# first install
# ---------------------------------------------------------------------------
echo "== [REAL] storage layout under $STORAGE_ROOT =="
for d in bundles runs jobs jobs/inbox jobs/processing jobs/done jobs/failed audit keys releases; do
  mkdir -p "$STORAGE_ROOT/$d"
done
# Runtime state is service-writable. Workflow bundles and the pinned update key
# are operator-controlled and only group-readable by the service.
chmod 0700 "$STORAGE_ROOT"/{bundles,runs,jobs,audit,keys} 2>/dev/null || true
if id "$SERVICE_USER" >/dev/null 2>&1; then
  SERVICE_GROUP="$(id -gn "$SERVICE_USER")"
  chown -RP "$SERVICE_USER:$SERVICE_GROUP" "$STORAGE_ROOT"/{runs,jobs,audit}
  chown -RP "root:$SERVICE_GROUP" "$STORAGE_ROOT"/{bundles,keys}
  chmod 0750 "$STORAGE_ROOT"/{bundles,keys}
else
  echo "  NOTE: service user '$SERVICE_USER' does not exist; create it, then re-run to chown."
  chown -RP root:root "$STORAGE_ROOT"/{bundles,keys}
fi
echo "  created bundles/ runs/ jobs/{inbox,processing,done,failed} audit/ keys/ releases/"

echo
echo "== [STUB] full-disk encryption check =="
cat <<EOF
  PHI-at-rest is protected by FULL-DISK ENCRYPTION on the volume holding
  $STORAGE_ROOT — the operator provisions this (LUKS on Linux, BitLocker on
  Windows, FileVault on macOS). openadapt-flow never holds the disk key.
  This installer cannot verify or provision it for you; confirm the volume is
  encrypted before storing any real bundle. See COMPLIANCE.md.
EOF

echo
if [[ -n "$WHEELHOUSE" ]]; then
  if [[ ! -d "$WHEELHOUSE" ]]; then
    echo "  wheelhouse $WHEELHOUSE not found" >&2; exit 2
  fi
  # Build the FIRST versioned release in place, then flip `current` to it. The
  # version is read from the wheelhouse wheel name (no build/network needed).
  WHEEL_VERSION="$(rel_wheelhouse_version "$WHEELHOUSE")" || exit 1
  VER="${REL_VERSION:-$WHEEL_VERSION}"
  rel_validate_version "$VER" || exit 1
  if [[ "$VER" != "$WHEEL_VERSION" ]]; then
    echo "install.sh: --release-version must match wheel version $WHEEL_VERSION" >&2
    exit 2
  fi
  RELDIR="$STORAGE_ROOT/releases/$VER"
  echo "== [REAL] first release from local wheelhouse ($WHEELHOUSE) -> $RELDIR =="
  if [[ -d "$RELDIR" ]]; then
    if [[ "$(rel_current_path "$STORAGE_ROOT")" == "$RELDIR" ]] \
      && rel_validate_release_dir "$STORAGE_ROOT" "$RELDIR" \
      && rel_smoke "$RELDIR" "$CONFIG"; then
      echo "  release $VER is already active and passed smoke validation"
      RELDIR_ALREADY_ACTIVE=1
    else
      echo "install.sh: immutable release path already exists but is not a healthy current release" >&2
      exit 1
    fi
  else
    mkdir -p "$RELDIR"
    if ! rel_build_release "$RELDIR" "$WHEELHOUSE" "$VER"; then
      echo "install.sh: first release build failed" >&2; rm -rf "$RELDIR"; exit 1
    fi
    {
      echo "version=$VER"
      echo "applied_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
      echo "source_archive=wheelhouse:$(basename "$WHEELHOUSE")"
      echo "source_sha256="
      echo "verify_method=local-wheelhouse"
    } > "$RELDIR/RELEASE"
    chmod 0600 "$RELDIR/RELEASE"
    chmod -R go-w "$RELDIR" 2>/dev/null || true
  fi
  if [[ "${RELDIR_ALREADY_ACTIVE:-0}" -ne 1 ]]; then
    rel_activate "$STORAGE_ROOT" "$RELDIR"
  fi
  harden_release_control
  restore_audit_ownership
  echo "  installed openadapt-flow[privacy] into $RELDIR/venv (offline)"
  echo "  active engine path: $STORAGE_ROOT/current/venv/bin"
  echo "  NOTE: also install the spaCy NER model offline:"
  echo "    $STORAGE_ROOT/current/venv/bin/python -m spacy download en_core_web_trf   # model staged locally"
else
  echo "== [STUB] engine install skipped (no --wheelhouse) =="
  echo "  Provide --wheelhouse ./wheels (built off-site with 'pip download"
  echo "  openadapt-flow[privacy]') to install fully offline. Air-gapped hosts"
  echo "  must NOT install from PyPI."
fi

if [[ "$DO_SYSTEMD" -eq 1 ]]; then
  echo
  echo "== [REAL] systemd units =="
  if command -v systemctl >/dev/null 2>&1; then
    sed "s#@STORAGE_ROOT@#$STORAGE_ROOT#g; s#@SERVICE_USER@#$SERVICE_USER#g; s#@HERE@#$HERE#g; s#@CONFIG@#$(cd "$(dirname "$CONFIG")" && pwd)/$(basename "$CONFIG")#g" \
      "$HERE/systemd/openadapt-flow-runner.service" > /etc/systemd/system/openadapt-flow-runner.service
    cp "$HERE/systemd/openadapt-flow-runner.path" /etc/systemd/system/openadapt-flow-runner.path
    systemctl daemon-reload
    echo "  installed openadapt-flow-runner.service + .path"
    echo "  the unit runs the engine via $STORAGE_ROOT/current/venv/bin — an"
    echo "  --update flip / --rollback takes effect on the NEXT job (no unit edit)."
    echo "  enable with: systemctl enable --now openadapt-flow-runner.path"
  else
    echo "  systemctl not found — this host is not systemd. Use docker-compose.yml"
    echo "  or a Windows Scheduled Task calling bin/run-queue.sh watch."
  fi
fi

echo
echo "== [REAL] air-gap acceptance gate =="
OPENADAPT_FLOW_SCRUB="$SCRUB" bash "$HERE/bin/verify-airgap.sh" --config "$CONFIG" || {
  echo
  echo "install.sh: air-gap verification FAILED — resolve before processing PHI." >&2
  exit 1
}

echo
echo "Install complete. Next:"
echo "  1. Confirm $STORAGE_ROOT is on a full-disk-encrypted volume."
echo "  2. Place a compiled bundle under $STORAGE_ROOT/bundles and a"
echo "     deployment.yaml at the path named in $CONFIG (deployment_config)."
echo "  3. Drop a .job file into $STORAGE_ROOT/jobs/inbox and start the runner:"
echo "       OPENADAPT_FLOW_SCRUB=$SCRUB bin/run-queue.sh watch"
echo "  4. To patch later: stage a signed release and run"
echo "       sudo ./install.sh --update --config $CONFIG      (atomic, rollback-able)"
echo "     Revert instantly with:  sudo ./install.sh --rollback --config $CONFIG"
echo "     See deploy/on-prem/UPDATE.md for the full runbook."
