#!/usr/bin/env bash
#
# audit-log.sh — append a structured, PHI-free record to the LOCAL audit log.
#
# REAL. This is a thin, dependency-free append-only writer. The audit log is a
# newline-delimited JSON file on local disk (never network). It records WHAT ran
# and the OUTCOME — never patient data. The run artifacts (report.json,
# REPORT.md, checkpoints/) hold the per-step detail and live beside the run
# under the encrypted storage root; this log is the tamper-evident index over
# them.
#
# Fields (all non-PHI):
#   ts          UTC ISO-8601 timestamp
#   event       queued | started | verified | halted | failed | resumed |
#               update_prepared | updated | rollback_prepared | rolledback |
#               layout_migration_prepared | layout_migrated |
#               transition_recovered
#   job         opaque job id (operator-chosen; MUST NOT be a patient id)
#   bundle      bundle directory basename (not its contents)
#   run_dir     run output directory (path only)
#   exit        process exit code (for terminal events)
#   actor       OS user that invoked the runner
#   note        short free-text status (operator-supplied; MUST be PHI-free)
#   prev_sha    sha256 of the previous log line (hash chain — tamper-evidence)
#
# The prev_sha field chains each record to the previous one: altering or
# deleting a past line breaks every subsequent hash, so silent edits are
# detectable with `verify-airgap.sh --audit` (or any sha256 walk). This is
# tamper-EVIDENCE, not tamper-PROOF — a determined local root can recompute the
# chain. Pair it with append-only filesystem controls (chattr +a / immutable
# WORM export) for real assurance; see COMPLIANCE.md.
#
# Usage:
#   audit-log.sh <event> <job> [--bundle B] [--run-dir D] [--exit N] [--note "..."]
#
# Env:
#   OPENADAPT_ONPREM_AUDIT_LOG   audit log path (default: ./audit/audit.log)

set -euo pipefail

AUDIT_LOG="${OPENADAPT_ONPREM_AUDIT_LOG:-./audit/audit.log}"

event="${1:-}"
job="${2:-}"
if [[ -z "$event" || -z "$job" ]]; then
  echo "usage: audit-log.sh <event> <job> [--bundle B] [--run-dir D] [--exit N] [--note ...]" >&2
  exit 2
fi
shift 2

bundle=""
run_dir=""
exit_code=""
note=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --bundle)  bundle="${2:-}"; shift 2 ;;
    --run-dir) run_dir="${2:-}"; shift 2 ;;
    --exit)    exit_code="${2:-}"; shift 2 ;;
    --note)    note="${2:-}"; shift 2 ;;
    *) echo "audit-log.sh: unknown arg $1" >&2; exit 2 ;;
  esac
done

mkdir -p "$(dirname "$AUDIT_LOG")"
chmod 0700 "$(dirname "$AUDIT_LOG")" 2>/dev/null || true

# Serialize the read-last-hash + append transaction. O_APPEND prevents byte
# interleaving, but without this lock two writers can both chain to the same
# parent and produce a structurally broken audit log.
lock_dir="${AUDIT_LOG}.lock"
lock_acquired=0
for ((attempt = 0; attempt < 200; attempt++)); do
  if mkdir "$lock_dir" 2>/dev/null; then
    printf '%s\n' "$$" > "$lock_dir/pid"
    lock_acquired=1
    break
  fi
  owner=""
  if [[ -d "$lock_dir" && ! -L "$lock_dir" && -r "$lock_dir/pid" ]]; then
    owner="$(cat "$lock_dir/pid" 2>/dev/null || true)"
  fi
  if [[ -n "$owner" && "$owner" =~ ^[0-9]+$ ]] && ! kill -0 "$owner" 2>/dev/null; then
    rm -f "$lock_dir/pid" 2>/dev/null || true
    rmdir "$lock_dir" 2>/dev/null || true
    continue
  fi
  sleep 0.05
done
if [[ "$lock_acquired" -ne 1 ]]; then
  echo "audit-log.sh: could not acquire audit lock $lock_dir" >&2
  exit 1
fi
release_audit_lock() {
  rm -f "$lock_dir/pid" 2>/dev/null || true
  rmdir "$lock_dir" 2>/dev/null || true
}
trap release_audit_lock EXIT

sha256_stream() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum
  else
    shasum -a 256
  fi
}

# Hash-chain: sha256 of the last line (empty string for the first record).
prev_sha=""
if [[ -s "$AUDIT_LOG" ]]; then
  prev_sha="$(tail -n 1 "$AUDIT_LOG" | sha256_stream | awk '{print $1}')"
fi

ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
actor="$(id -un 2>/dev/null || echo unknown)"

# JSON-escape a value (backslash, quote, control chars). PHI must never reach
# here, but escape defensively so a stray char cannot corrupt the log.
json_escape() {
  local s="$1"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  s="${s//$'\n'/ }"
  s="${s//$'\r'/ }"
  s="${s//$'\t'/ }"
  printf '%s' "$s"
}

line=$(printf '{"ts":"%s","event":"%s","job":"%s","bundle":"%s","run_dir":"%s","exit":"%s","actor":"%s","note":"%s","prev_sha":"%s"}' \
  "$ts" \
  "$(json_escape "$event")" \
  "$(json_escape "$job")" \
  "$(json_escape "$(basename "${bundle:-}")")" \
  "$(json_escape "$run_dir")" \
  "$(json_escape "$exit_code")" \
  "$(json_escape "$actor")" \
  "$(json_escape "$note")" \
  "$prev_sha")

# The lock above makes the chain update atomic across concurrent local writers.
printf '%s\n' "$line" >> "$AUDIT_LOG"
chmod 0600 "$AUDIT_LOG" 2>/dev/null || true
