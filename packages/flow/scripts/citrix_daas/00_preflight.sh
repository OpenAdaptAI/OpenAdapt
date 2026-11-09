#!/usr/bin/env bash
# 00_preflight.sh — READ-ONLY. Verifies the Azure side is ready to host a
# Citrix DaaS Standard for Azure trial's customer-managed VDAs.
# Mutates nothing. Costs nothing. Does NOT start the 7-day trial clock.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

info "Citrix DaaS Standard for Azure — PREFLIGHT (read-only)"
echo

require_logged_in
ok "az logged in"
info "  subscription : ${SUB_NAME} (${SUB_ID})"
info "  tenant       : ${TENANT_ID}"
info "  billing state: ${SUB_STATE}"
case "$SUB_STATE" in
  Enabled) ok "subscription Enabled" ;;
  Warned)  warn "subscription state 'Warned' — parked lab provisioned fine in this state, but confirm before creating VDAs" ;;
  *)       warn "subscription state '${SUB_STATE}' — verify it can create VMs" ;;
esac
echo

# --- region + quota (VDAs need vCPU quota; quick-create uses small/medium) ---
LOC="${CITRIX_DAAS_LOCATION}"
info "Region for customer-managed VDAs: ${LOC}"
if az vm list-usage -l "$LOC" -o json >/dev/null 2>&1; then
  az vm list-usage -l "$LOC" -o json \
    | python3 -c '
import sys,json
rows=json.load(sys.stdin)
want=("Total Regional vCPUs","Standard DSv4 Family vCPUs","Standard DSv5 Family vCPUs","Standard DSv2 Family vCPUs")
for r in rows:
    name=r["name"]["localizedValue"]
    if name in want:
        cur=int(r["currentValue"]); lim=int(r["limit"])
        free=lim-cur
        flag="OK" if free>=2 else "LOW"
        print(f"  [{flag}] {name}: {cur}/{lim} used, {free} free")
'
  ok "quota queried (need >=2 free vCPUs in a family the catalog will use)"
else
  warn "could not query vm quota for ${LOC}"
fi
echo

# --- resource providers Citrix-created VDAs rely on -------------------------
info "Resource provider registration (needed once per subscription):"
for rp in Microsoft.Compute Microsoft.Network Microsoft.Storage Microsoft.Resources; do
  st="$(az provider show -n "$rp" --query registrationState -o tsv 2>/dev/null || echo Unknown)"
  if [ "$st" = "Registered" ]; then ok "  ${rp}: ${st}"; else warn "  ${rp}: ${st} (register: az provider register -n ${rp})"; fi
done
echo

# --- directory privilege probe for app-registration path --------------------
info "Checking whether THIS identity can create an app registration"
info "(needed by 10_prepare_azure_identity.sh unless you use Global-Admin dashboard auth):"
if az ad signed-in-user show -o none 2>/dev/null; then
  ok "  can read the directory as the signed-in user"
else
  warn "  cannot read directory as signed-in user (may be an SP login)"
fi
if az ad app list --filter "displayName eq '__preflight_probe_nonexistent__'" -o none 2>/dev/null; then
  ok "  directory read for app registrations permitted"
else
  warn "  INSUFFICIENT directory privileges to list/create app registrations."
  warn "  => 10_prepare_azure_identity.sh will fail. Use one of:"
  warn "       (a) sign in as a Global Administrator, or"
  warn "       (b) skip the SP and use 'Global Admin auth' in the Citrix dashboard"
  warn "           (Citrix discovers subscriptions automatically), or"
  warn "       (c) have an admin create the app reg + Contributor role for you."
fi
echo

# --- reuse-the-parked-lab hint ---------------------------------------------
if az group show -n openadapt-citrix-lab -o none 2>/dev/null; then
  info "Parked CVAD lab 'openadapt-citrix-lab' exists — its VNet/NSG IP-lock"
  info "pattern is reused by 20_prepare_network.sh. (That lab is a SEPARATE path;"
  info "this DaaS trial does not need it.)"
fi

MYIP="$(my_ip)"
[ -n "$MYIP" ] && info "This machine's egress IP (for NSG lockdown): ${MYIP}"
echo
ok "PREFLIGHT complete — nothing was created, no cost, clock NOT started."
