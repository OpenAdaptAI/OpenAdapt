#!/usr/bin/env bash
#
# verify-airgap.sh — assert this clinic install cannot phone home.
#
# REAL (best-effort). This is the operator's pre-flight + periodic attestation
# that the runner has NO outbound path and is configured fail-closed. It is a
# defence-in-depth CHECK, not the control itself: the actual air-gap is your
# network firewall / host egress rules. This script proves the software-side
# posture and (optionally) probes that egress is in fact blocked.
#
# Checks (each prints PASS / FAIL / WARN):
#   1. config      onprem.yaml exists and egress-sensitive knobs are safe:
#                    runtime.allow_model_grounding == false
#                    no VLM appliance URL, OR it is a private/LAN address
#                  and PHI scrubbing is fail-closed (OPENADAPT_FLOW_SCRUB=on).
#   2. deployment  the referenced deployment.yaml has no non-loopback/non-LAN
#                  backend/effects/actuation URLs (grep for public hosts).
#   3. env         no OPENADAPT_FLOW_VLM_URL / *_TELEMETRY / proxy vars pointing
#                  off-LAN; ANTHROPIC_API_KEY / OPENAI_API_KEY not set.
#   4. egress      (optional, --probe) actively curl a public canary host and
#                  assert it FAILS. On a correctly air-gapped host this is the
#                  strongest signal. Skipped without --probe (a probe that
#                  SUCCEEDS on a mis-scoped run could itself be an egress).
#   5. audit       (optional, --audit) walk the audit-log hash chain and report
#                  the first broken link, if any.
#
# Exit code: 0 = all PASS (WARN allowed), 1 = at least one FAIL.
#
# Usage:
#   verify-airgap.sh [--config onprem.yaml] [--probe] [--audit]

set -uo pipefail

CONFIG="onprem.yaml"
DO_PROBE=0
DO_AUDIT=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) CONFIG="${2:-}"; shift 2 ;;
    --probe)  DO_PROBE=1; shift ;;
    --audit)  DO_AUDIT=1; shift ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "verify-airgap.sh: unknown arg $1" >&2; exit 2 ;;
  esac
done

fails=0
pass() { printf '  \033[32mPASS\033[0m  %s\n' "$1"; }
warn() { printf '  \033[33mWARN\033[0m  %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; fails=$((fails+1)); }

# A host token is "off-LAN" if it is not loopback and not an RFC1918 / .lan /
# .local name. Deliberately conservative: unknown => treat as off-LAN (FAIL),
# because the whole point is that nothing unexpected leaves the building.
is_lan_host() {
  local h="$1"
  case "$h" in
    localhost|127.*|::1|0.0.0.0) return 0 ;;
    10.*|192.168.*) return 0 ;;
    172.1[6-9].*|172.2[0-9].*|172.3[0-1].*) return 0 ;;
    *.lan|*.local|*.internal|*.intranet) return 0 ;;
    *) return 1 ;;
  esac
}

echo "== 1. on-prem config ($CONFIG) =="
if [[ ! -f "$CONFIG" ]]; then
  fail "config $CONFIG not found"
else
  if grep -Eq '^\s*allow_model_grounding\s*:\s*true' "$CONFIG"; then
    fail "runtime.allow_model_grounding: true — off-box model egress is permitted"
  else
    pass "allow_model_grounding is not true (model egress disabled)"
  fi
  # A VLM appliance URL is allowed ONLY if it is a LAN address.
  vlm_url="$(grep -Eo 'vlm_url\s*:\s*\S+' "$CONFIG" | awk -F: '{print $3}' | tr -d '"'"'"' /' | head -n1 || true)"
  if [[ -n "${vlm_url:-}" ]]; then
    if is_lan_host "$vlm_url"; then
      pass "VLM appliance host '$vlm_url' is on the LAN"
    else
      fail "VLM appliance host '$vlm_url' is NOT a private/LAN address"
    fi
  else
    pass "no VLM appliance configured (fully local, model-free)"
  fi
  if grep -Eq '^\s*scrub\s*:\s*on' "$CONFIG" || [[ "${OPENADAPT_FLOW_SCRUB:-}" == "on" ]]; then
    pass "PHI scrubbing is fail-closed (SCRUB=on)"
  else
    warn "PHI scrubbing not pinned to 'on' — set OPENADAPT_FLOW_SCRUB=on for a clinical deployment"
  fi
fi

echo "== 2. deployment.yaml URLs =="
dep="$(grep -Eo 'deployment_config\s*:\s*\S+' "$CONFIG" 2>/dev/null | awk '{print $2}' | tr -d '"'"'"'' || true)"
dep="${dep:-deployment.yaml}"
if [[ -f "$dep" ]]; then
  offlan=0
  while IFS= read -r host; do
    [[ -z "$host" ]] && continue
    if ! is_lan_host "$host"; then
      fail "deployment references off-LAN host: $host"
      offlan=1
    fi
  done < <(grep -Eio 'https?://[a-z0-9._-]+' "$dep" | sed -E 's#https?://##' | sort -u)
  [[ "$offlan" -eq 0 ]] && pass "no off-LAN URLs in $dep"
else
  warn "deployment config $dep not found (skipping URL scan)"
fi

echo "== 3. environment =="
env_bad=0
for v in ANTHROPIC_API_KEY OPENAI_API_KEY; do
  if [[ -n "${!v:-}" ]]; then fail "$v is set — a cloud model key is present in the runner env"; env_bad=1; fi
done
if [[ -n "${OPENADAPT_FLOW_VLM_URL:-}" ]] && ! is_lan_host "$(printf '%s' "$OPENADAPT_FLOW_VLM_URL" | sed -E 's#https?://##; s#[:/].*##')"; then
  fail "OPENADAPT_FLOW_VLM_URL points off-LAN: $OPENADAPT_FLOW_VLM_URL"; env_bad=1
fi
for v in HTTP_PROXY HTTPS_PROXY http_proxy https_proxy; do
  if [[ -n "${!v:-}" ]]; then warn "$v is set ($v=${!v}) — confirm it is a LAN-only proxy, not an internet gateway"; fi
done
[[ "$env_bad" -eq 0 ]] && pass "no cloud API keys or off-LAN model URL in env"

if [[ "$DO_PROBE" -eq 1 ]]; then
  echo "== 4. active egress probe =="
  canary="https://example.com"
  if command -v curl >/dev/null 2>&1; then
    if curl -sS --max-time 5 -o /dev/null "$canary" 2>/dev/null; then
      fail "reached $canary — THIS HOST HAS INTERNET EGRESS (not air-gapped)"
    else
      pass "could not reach $canary — outbound egress is blocked"
    fi
  else
    warn "curl not available — cannot run active egress probe"
  fi
fi

if [[ "$DO_AUDIT" -eq 1 ]]; then
  echo "== 5. audit-log hash chain =="
  log="${OPENADAPT_ONPREM_AUDIT_LOG:-./audit/audit.log}"
  if [[ ! -s "$log" ]]; then
    warn "audit log $log is empty or missing"
  else
    prev=""
    n=0
    broken=0
    while IFS= read -r line; do
      n=$((n+1))
      claimed="$(printf '%s' "$line" | sed -E 's/.*"prev_sha":"([a-f0-9]*)".*/\1/')"
      if [[ "$n" -gt 1 && "$claimed" != "$prev" ]]; then
        fail "audit chain broken at line $n (expected prev_sha $prev, got $claimed)"
        broken=1
        break
      fi
      # Hash the line WITH its trailing newline, matching how audit-log.sh
      # computes prev_sha (`tail -n 1` emits the line including its newline).
      prev="$(printf '%s\n' "$line" | shasum -a 256 | awk '{print $1}')"
    done < "$log"
    [[ "$broken" -eq 0 ]] && pass "audit chain intact across $n record(s)"
  fi
fi

echo
if [[ "$fails" -eq 0 ]]; then
  echo "AIR-GAP ATTESTATION: PASS (no FAIL checks)"
  exit 0
else
  echo "AIR-GAP ATTESTATION: FAIL ($fails failing check(s)) — do NOT process PHI until resolved"
  exit 1
fi
