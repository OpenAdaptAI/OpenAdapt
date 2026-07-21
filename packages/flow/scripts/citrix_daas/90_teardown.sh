#!/usr/bin/env bash
# 90_teardown.sh — remove the Azure prep this kit created (app registration,
# role assignment, network RG). Frees the (near-zero) standing cost.
#
# This does NOT and cannot cancel the Citrix trial (that is a Citrix-side
# concept). It only cleans up OUR Azure resources. Citrix retains trial data for
# 30 days (7-day trial) after expiry per subscribe-to-service.html.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
require_flag "--i-understand-this-deletes-resources" "$@"

require_logged_in

warn "Tearing down Citrix DaaS prep resources in ${SUB_NAME} (${SUB_ID})"
echo

# 1) Remove the app registration (also removes its SP + role assignments).
APP_ID="$(az ad app list --display-name "$CITRIX_DAAS_APP_NAME" --query "[0].appId" -o tsv 2>/dev/null || true)"
if [ -n "$APP_ID" ] && [ "$APP_ID" != "None" ]; then
  info "Deleting app registration ${CITRIX_DAAS_APP_NAME} (${APP_ID})"
  az role assignment delete --assignee "$APP_ID" --scope "/subscriptions/${SUB_ID}" 2>/dev/null || true
  az ad app delete --id "$APP_ID" && ok "app registration deleted" || warn "could not delete app (may need directory privileges)"
else
  info "no app registration named ${CITRIX_DAAS_APP_NAME} found"
fi

# 2) Delete the network RG (empty or with any leftover VDA network bits).
if az group show -n "$CITRIX_DAAS_RG" -o none 2>/dev/null; then
  info "Deleting resource group ${CITRIX_DAAS_RG} (async)"
  az group delete -n "$CITRIX_DAAS_RG" --yes --no-wait && ok "RG delete queued"
else
  info "resource group ${CITRIX_DAAS_RG} does not exist"
fi

# 3) Local secrets.
if [ -d "$SECRETS_DIR" ]; then
  info "Removing local secrets dir ${SECRETS_DIR}"
  rm -rf "$SECRETS_DIR" && ok "secrets removed"
fi

echo
ok "Azure prep torn down."
warn "REMINDER: any Citrix-CREATED VDAs live in whatever RG the catalog used —"
warn "delete the catalog in the Citrix console (or 'az vm deallocate' those VMs)"
warn "to stop their compute cost. This script does not touch Citrix-managed RGs."
