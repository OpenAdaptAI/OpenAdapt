#!/usr/bin/env bash
#
# run-queue.sh — a LOCAL, single-host run queue for compiled bundles.
#
# REAL (thin wrapper over the shipped `openadapt-flow run` CLI). No daemon
# framework, no message broker, no cloud: a directory is the queue. An operator
# (or the systemd .path unit) drops a job file into inbox/; this script claims
# it, runs the bundle through the deterministic engine under the on-prem
# deployment config, records the outcome to the local audit log, and files the
# job under done/ or failed/. Everything is local disk; nothing leaves the host.
#
# A "job" is a tiny KEY=VALUE file (PHI-free — patient values are passed as
# params, which the engine substitutes at run time and the audit log never
# stores):
#
#     # jobs/inbox/triage-0007.job
#     bundle=/srv/openadapt/bundles/vitals-triage
#     params=patient_ref=PT-INTERNAL-42;note=Reviewed
#     # (optional) run_dir=/srv/openadapt/runs/triage-0007
#
# Modes:
#   run-queue.sh once     process every job currently in inbox/, then exit
#   run-queue.sh watch    process inbox/, then poll every $POLL_SECONDS
#   run-queue.sh <file>   process exactly one job file
#
# Env (usually sourced from onprem.env / the systemd unit):
#   OPENADAPT_ONPREM_ROOT        storage root (default: .)
#   OPENADAPT_ONPREM_CONFIG      deployment.yaml passed to `openadapt-flow run`
#   OPENADAPT_ONPREM_AUDIT_LOG   audit log path
#   OPENADAPT_FLOW_SCRUB         MUST be 'on' for a clinical deployment (enforced)
#   POLL_SECONDS                 watch-mode poll interval (default 10)

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${OPENADAPT_ONPREM_ROOT:-.}"
CONFIG="${OPENADAPT_ONPREM_CONFIG:-$ROOT/deployment.yaml}"
QUEUE="$ROOT/jobs"
INBOX="$QUEUE/inbox"
PROC="$QUEUE/processing"
DONE="$QUEUE/done"
FAILED="$QUEUE/failed"
RUNS="$ROOT/runs"
POLL_SECONDS="${POLL_SECONDS:-10}"
AUDIT="$HERE/audit-log.sh"

mkdir -p "$INBOX" "$PROC" "$DONE" "$FAILED" "$RUNS"

log() { printf '[run-queue %s] %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }

# Fail-closed: refuse to process PHI unless scrubbing is pinned on. This mirrors
# the engine's own OPENADAPT_FLOW_SCRUB=on contract (docs/PRIVACY.md); we assert
# it here too so a mis-set env never silently writes plaintext PHI.
if [[ "${OPENADAPT_FLOW_SCRUB:-}" != "on" ]]; then
  log "REFUSING to start: OPENADAPT_FLOW_SCRUB is '${OPENADAPT_FLOW_SCRUB:-unset}', not 'on'."
  log "A clinical deployment must fail-closed. Set OPENADAPT_FLOW_SCRUB=on (see onprem.example.yaml)."
  exit 1
fi

if ! command -v openadapt-flow >/dev/null 2>&1; then
  log "REFUSING to start: 'openadapt-flow' not on PATH (activate the venv / install the engine)."
  exit 1
fi

# Read a KEY=VALUE job file into vars (bundle / params / run_dir). Ignores blank
# lines and comments. Values are taken literally (no eval — a job file is data).
process_job() {
  local jobfile="$1"
  local jobid; jobid="$(basename "$jobfile" .job)"
  local bundle="" params="" run_dir=""

  while IFS='=' read -r key val; do
    key="${key#"${key%%[![:space:]]*}"}"   # ltrim
    [[ -z "$key" || "$key" == \#* ]] && continue
    case "$key" in
      bundle)  bundle="$val" ;;
      params)  params="$val" ;;
      run_dir) run_dir="$val" ;;
    esac
  done < "$jobfile"

  if [[ -z "$bundle" ]]; then
    log "job $jobid: no 'bundle=' — sending to failed/"
    "$AUDIT" failed "$jobid" --note "malformed job: no bundle" || true
    mv -f "$jobfile" "$FAILED/" 2>/dev/null || true
    return 1
  fi

  run_dir="${run_dir:-$RUNS/${jobid}-$(date -u +%Y%m%dT%H%M%SZ)}"

  # Build repeatable --param flags from a ';'-separated params string.
  local -a param_flags=()
  if [[ -n "$params" ]]; then
    local IFS=';'
    for kv in $params; do
      [[ -n "$kv" ]] && param_flags+=(--param "$kv")
    done
  fi

  log "job $jobid: running bundle '$bundle' -> $run_dir"
  "$AUDIT" started "$jobid" --bundle "$bundle" --run-dir "$run_dir" || true

  local rc=0
  # The deterministic engine. --config wires backend/effects/actuation/policy;
  # scrubbing + at-rest posture come from the environment. No network egress is
  # added by this wrapper.
  openadapt-flow run "$bundle" \
    --config "$CONFIG" \
    --run-dir "$run_dir" \
    "${param_flags[@]}" \
    >"$run_dir.stdout.log" 2>"$run_dir.stderr.log" || rc=$?

  if [[ "$rc" -eq 0 ]]; then
    log "job $jobid: OK (exit 0)"
    "$AUDIT" verified "$jobid" --bundle "$bundle" --run-dir "$run_dir" --exit 0 || true
    mv -f "$jobfile" "$DONE/" 2>/dev/null || true
  else
    # A non-zero exit is the engine's fail-safe halt (identity mismatch, effect
    # REFUTED, unverifiable write, ...) OR an operational error. Either way the
    # run is NOT silently accepted — it is filed for operator review.
    log "job $jobid: HALT/FAIL (exit $rc) — see $run_dir/REPORT.md"
    "$AUDIT" halted "$jobid" --bundle "$bundle" --run-dir "$run_dir" --exit "$rc" || true
    mv -f "$jobfile" "$FAILED/" 2>/dev/null || true
  fi
  return "$rc"
}

# Claim a job atomically (mv is atomic within a filesystem), then run it. If two
# workers race, only one mv succeeds; the loser skips.
claim_and_run() {
  local jobfile="$1"
  local claimed="$PROC/$(basename "$jobfile")"
  mv -n "$jobfile" "$claimed" 2>/dev/null || return 0
  [[ -f "$claimed" ]] || return 0
  process_job "$claimed" || true
}

drain_inbox() {
  shopt -s nullglob
  for jobfile in "$INBOX"/*.job; do
    claim_and_run "$jobfile"
  done
  shopt -u nullglob
}

mode="${1:-once}"
case "$mode" in
  once)
    drain_inbox
    ;;
  watch)
    log "watching $INBOX (poll ${POLL_SECONDS}s). Ctrl-C to stop."
    while true; do
      drain_inbox
      sleep "$POLL_SECONDS"
    done
    ;;
  *)
    if [[ -f "$mode" ]]; then
      process_job "$mode"
    else
      echo "usage: run-queue.sh [once|watch|<jobfile>]" >&2
      exit 2
    fi
    ;;
esac
