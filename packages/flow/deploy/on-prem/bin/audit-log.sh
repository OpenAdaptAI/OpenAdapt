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
#
# Ownership is the exclusive creation of the pid file, done by the shell itself
# with noclobber (open(2) with O_CREAT|O_EXCL). The exit status of mkdir(1) is
# not trusted for exclusion: the uutils mkdir that Ubuntu 26.04 ships reports
# success to both callers when two processes race to create one directory.
lock_dir="${AUDIT_LOG}.lock"
lock_file="$lock_dir/pid"
break_file="${AUDIT_LOG}.lock-breaker"
lock_acquired=0

# Create $1 holding this process's pid; fails if $1 already exists as any
# kind of file. noclobber alone is not enough: bash opens an existing FIFO or
# device without O_EXCL, so two writers could both "create" the same FIFO. The
# pid goes into a fresh staged name and is hard-linked into place; link(2)
# refuses an existing path of every type. Ownership is then read back from the
# file itself, not from the exit status of ln(1).
create_exclusive() {
  local target="$1" staged="" owner="" linked=1
  # Refuse any existing path up front, whatever its type.
  [[ -e "$target" || -L "$target" ]] && return 1
  staged="$target.$$.$RANDOM$RANDOM"
  ( set -C; printf '%s\n' "$$" > "$staged" ) 2>/dev/null || return 1
  [[ -f "$staged" && ! -L "$staged" ]] || return 1
  ln "$staged" "$target" 2>/dev/null && linked=0
  rm -f "$staged" 2>/dev/null || true
  [[ "$linked" -eq 0 && -f "$target" && ! -L "$target" ]] || return 1
  owner="$(< "$target")" 2>/dev/null || return 1
  [[ "$owner" == "$$" ]]
}

# Remove a lock whose owner has exited. Between reading the owner and removing
# the lock, that owner can release it and another writer can take it, so the
# removal happens only under a second exclusive lock that serializes breakers,
# and only if the lock still names the same dead owner. Without this, a breaker
# could delete a live writer's lock and let two writers chain to the same parent.
break_stale_lock() {
  local stale="$1" current=""
  create_exclusive "$break_file" || return 0
  current="$(cat "$lock_file" 2>/dev/null || true)"
  if [[ "$current" == "$stale" ]] && ! kill -0 "$stale" 2>/dev/null; then
    rm -f "$lock_file" 2>/dev/null || true
  fi
  rm -f "$break_file" 2>/dev/null || true
}

for ((attempt = 0; attempt < 200; attempt++)); do
  if [[ ! -L "$lock_dir" ]]; then
    mkdir -p "$lock_dir" 2>/dev/null || true
    if [[ -d "$lock_dir" && ! -L "$lock_dir" ]] && create_exclusive "$lock_file"; then
      lock_acquired=1
      break
    fi
  fi
  owner=""
  if [[ -L "$lock_file" ]] || [[ -e "$lock_file" && ! -f "$lock_file" ]]; then
    echo "audit-log.sh: audit lock $lock_file is not a regular file; refusing" >&2
    exit 1
  fi
  if [[ -d "$lock_dir" && ! -L "$lock_dir" && -f "$lock_file" && ! -L "$lock_file" ]]; then
    owner="$(cat "$lock_file" 2>/dev/null || true)"
  fi
  if [[ -n "$owner" && "$owner" =~ ^[0-9]+$ ]] && ! kill -0 "$owner" 2>/dev/null; then
    break_stale_lock "$owner"
    continue
  fi
  sleep 0.05
done
if [[ "$lock_acquired" -ne 1 ]]; then
  echo "audit-log.sh: could not acquire audit lock $lock_dir" >&2
  exit 1
fi
release_audit_lock() {
  rm -f "$lock_file" 2>/dev/null || true
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
