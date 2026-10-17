#!/usr/bin/env bash
# 30_fire_trial_gate.sh — READ-ONLY readiness gate. Prints the exact MANUAL
# Citrix Cloud steps to start the trial, but performs NO action that starts it.
#
# There is no Azure CLI / Citrix API command that starts the 7-day trial: it is
# a manual click ('Request Trial') in the Citrix Cloud web console. This script
# therefore CANNOT auto-start the clock. It refuses to even print the go-checklist
# unless you pass the acknowledgement flag AND every readiness check passes.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
require_flag "--i-understand-this-starts-the-7day-clock" "$@"

echo
warn "================================================================"
warn " This gate only PRINTS the manual steps. The 7-day clock starts"
warn " ONLY when a HUMAN clicks 'Request Trial' in the Citrix Cloud"
warn " console. No script here does that. Nothing below starts it."
warn "================================================================"
echo

fail=0

# 1) HARD GATE: pixel backend proven against the fixture first.
info "Readiness check 1/4 — pixel backend proven against the Guacamole/RDP fixture"
ATTEST="${CITRIX_PIXEL_BACKEND_PROVEN:-}"
if [ "$ATTEST" = "yes" ]; then
  ok "  attested via CITRIX_PIXEL_BACKEND_PROVEN=yes"
else
  err "  NOT attested. The Citrix-Workspace-window pixel backend MUST pass the"
  err "  fixture (record->compile->replay pixel-only, model_calls==0, halt-under-"
  err "  drift) BEFORE burning a trial day. Set CITRIX_PIXEL_BACKEND_PROVEN=yes"
  err "  ONLY when that is genuinely true."
  fail=1
fi

# 2) Azure preflight green.
info "Readiness check 2/4 — Azure preflight"
if require_logged_in 2>/dev/null; then ok "  az logged in (${SUB_NAME})"; else err "  az not ready — run 00_preflight.sh"; fail=1; fi

# 3) Connector identity prepared (or explicit Global-Admin-auth choice).
info "Readiness check 3/4 — Citrix connector identity"
if [ -f "${SECRETS_DIR}/citrix_daas_connector.env" ]; then
  ok "  connector credentials present (from 10_prepare_azure_identity.sh)"
elif [ "${CITRIX_USE_GLOBAL_ADMIN_AUTH:-}" = "yes" ]; then
  ok "  will use Global-Admin dashboard auth (no SP) — CITRIX_USE_GLOBAL_ADMIN_AUTH=yes"
else
  err "  no connector creds and Global-Admin-auth not chosen. Run"
  err "  10_prepare_azure_identity.sh, or set CITRIX_USE_GLOBAL_ADMIN_AUTH=yes."
  fail=1
fi

# 4) Windows client box for the Workspace app.
info "Readiness check 4/4 — Windows client box for Citrix Workspace app"
if [ "${CITRIX_CLIENT_BOX_READY:-}" = "yes" ]; then
  ok "  attested via CITRIX_CLIENT_BOX_READY=yes"
else
  warn "  not attested. You need a Windows box (parked-lab vm-vda, a new small VM,"
  warn "  or the Parallels Win11 guest) running the free Citrix Workspace app for"
  warn "  the pixel backend to screenshot. Set CITRIX_CLIENT_BOX_READY=yes when true."
  fail=1
fi

echo
if [ "$fail" -ne 0 ]; then
  err "NOT READY — do not start the trial. Resolve the failures above first."
  err "Clock NOT started (nothing here starts it anyway)."
  exit 1
fi

ok "All readiness checks passed. The following steps are MANUAL and human-run."
cat <<'STEPS'

  ============================================================
  MANUAL GO-CHECKLIST (a human performs these in a browser)
  ============================================================
  1. Sign in / sign up for Citrix Cloud at https://onboarding.cloud.com
     (uses the existing Azure AD identity where the Marketplace SaaS path
     is offered; otherwise a self-serve Citrix Cloud org). Verify this
     completes — if citrix.com-style self-serve is blocked, use the Azure
     Marketplace 'Citrix DaaS' offer 'Get It Now' -> subscribe path instead.

  2. In the Citrix Cloud console, find the 'Citrix DaaS Standard for Azure'
     (a.k.a. Virtual Apps and Desktops Standard for Azure) service tile.

  3.  >>> THIS SINGLE CLICK STARTS THE 7-DAY CLOCK <<<
      Click 'Request Trial'. It is AUTO-APPROVED and the 7 days begin
      immediately. You can request a trial for a service only ONCE.
      DO NOT click this until steps 1-4 of the readiness checklist are
      genuinely true and you have ~2-3 focused hours to use the day.

  4. Add subscription -> customer-managed Azure. Either authenticate as
     Global Admin (Citrix discovers subscriptions) OR paste the tenant/
     client/secret from ./secrets/citrix_daas_connector.env.

  5. Create a catalog (this provisions VDAs = FIRST Azure cost). Publish a
     desktop + a target app (reuse the patient_notes WinForms clone so the
     identity/effect oracle carries over from the RDP harness).

  6. Open the Workspace URL, launch the published desktop/app -> real ICA/HDX.

  7. Install the free Citrix Workspace app on the client box; point the
     Citrix-Workspace-window pixel backend at the session window and run the
     record->compile->replay pixel-only ladder harness. Commit
     benchmark/citrix/... (PHI-free, sanitized).

  8. Delete the catalog / deallocate VDAs when idle (stops Azure cost; the
     7-day clock keeps running regardless).
  ============================================================

STEPS
ok "Gate printed the manual steps. It did NOT start the trial."
