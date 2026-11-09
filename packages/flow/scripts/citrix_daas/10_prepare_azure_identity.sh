#!/usr/bin/env bash
# 10_prepare_azure_identity.sh — create the Azure AD app registration + service
# principal + Contributor role assignment that Citrix DaaS Standard for Azure
# uses to link OUR (customer-managed) Azure subscription during the trial.
#
# Per docs.citrix.com/.../subscriptions.html a non-Global-Admin links a
# subscription by registering an app, giving it Contributor on the subscription,
# and pasting tenant/client/secret into the Citrix "add subscription" dialog.
#
# Cost: $0 (an app reg + role assignment are free).
# Clock: does NOT start the 7-day trial clock.
# Secret: written ONLY to ./secrets/ (gitignored). Never printed to logs beyond
#         the local file, never committed.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
require_flag "--i-understand-this-modifies-azure" "$@"

require_logged_in
info "Preparing Citrix connector identity in subscription ${SUB_NAME} (${SUB_ID})"
echo

# Preflight the directory privilege so we fail with a helpful message.
if ! az ad app list --filter "displayName eq '__probe__'" -o none 2>/dev/null; then
  err "This identity cannot create app registrations (insufficient directory privileges)."
  err "Alternatives (pick one, then re-run or skip this script):"
  err "  (a) az login as a Global Administrator and re-run."
  err "  (b) SKIP this script: in the Citrix dashboard choose 'authenticate to Azure'"
  err "      as a Global Admin — Citrix discovers subscriptions automatically, no SP."
  err "  (c) Ask a directory admin to create app '${CITRIX_DAAS_APP_NAME}' with a"
  err "      client secret and Contributor on subscription ${SUB_ID}, then hand you"
  err "      tenant/client/secret to paste into Citrix."
  exit 1
fi

mkdir -p "$SECRETS_DIR"; chmod 700 "$SECRETS_DIR"

# Idempotent: reuse the app if it already exists.
APP_ID="$(az ad app list --display-name "$CITRIX_DAAS_APP_NAME" --query "[0].appId" -o tsv 2>/dev/null || true)"
if [ -z "$APP_ID" ] || [ "$APP_ID" = "None" ]; then
  info "Creating app registration '${CITRIX_DAAS_APP_NAME}'"
  APP_ID="$(az ad app create --display-name "$CITRIX_DAAS_APP_NAME" --query appId -o tsv)"
  ok "app created: ${APP_ID}"
else
  ok "reusing existing app: ${APP_ID}"
fi

# Ensure a service principal exists for the app.
if ! az ad sp show --id "$APP_ID" -o none 2>/dev/null; then
  info "Creating service principal for the app"
  az ad sp create --id "$APP_ID" -o none
  ok "service principal created"
else
  ok "service principal already exists"
fi

# Contributor on the subscription (scope = whole sub, per Citrix docs).
info "Assigning Contributor on subscription ${SUB_ID}"
az role assignment create \
  --assignee "$APP_ID" \
  --role Contributor \
  --scope "/subscriptions/${SUB_ID}" -o none 2>/dev/null \
  && ok "Contributor assigned" \
  || warn "role assignment may already exist (or needs Owner/User-Access-Admin on the sub)"

# Fresh client secret (2y). Written ONLY to the gitignored secrets file.
info "Creating a client secret (2-year expiry)"
SECRET="$(az ad app credential reset --id "$APP_ID" --years 2 --query password -o tsv)"
CRED_FILE="${SECRETS_DIR}/citrix_daas_connector.env"
umask 077
cat > "$CRED_FILE" <<EOF
# Citrix DaaS 'add subscription' credentials — SECRET, gitignored, do not commit.
# Paste these into the Citrix dashboard when linking the customer-managed sub.
CITRIX_DAAS_TENANT_ID=${TENANT_ID}
CITRIX_DAAS_SUBSCRIPTION_ID=${SUB_ID}
CITRIX_DAAS_CLIENT_ID=${APP_ID}
CITRIX_DAAS_CLIENT_SECRET=${SECRET}
EOF
chmod 600 "$CRED_FILE"
echo
ok "Identity prepared. Credentials written to: ${CRED_FILE}"
info "  Tenant (Directory) ID : ${TENANT_ID}"
info "  Subscription ID       : ${SUB_ID}"
info "  Client (Application) ID: ${APP_ID}"
info "  Client secret         : (in ${CRED_FILE}, not shown here)"
echo
info "In the Citrix DaaS console, add the customer-managed subscription using the"
info "tenant/client/secret above. This did NOT start the trial clock and costs \$0."
