#!/usr/bin/env bash
# Create what provision.sh assumes, in an empty subscription: the resource
# group, the network, a Key Vault holding every lab password, and a private
# storage container for results. Run once, before provision.sh.
#
#   KV=<new vault name> SA=<new storage account> bash bootstrap.sh
#
# Passwords are generated here and written to Key Vault from a private temp
# file, so they never appear on a command line or in output. No inbound rule
# admits the internet: the operator drives the VMs through Azure run
# commands, and the client reaches the host over the virtual network.
#
# Tear down with: az group delete -n "$RG" --yes; az keyvault purge -n "$KV"
set -euo pipefail

RG="${RG:-openadapt-avd-lab}"
LOC="${LOC:-eastus}"
KV="${KV:?name a new Key Vault (globally unique)}"
SA="${SA:?name a new storage account (globally unique, lowercase)}"

az group create -n "$RG" -l "$LOC" -o none
az network nsg create -g "$RG" -n avdlab-nsg -o none
az network vnet create -g "$RG" -n avdlab-vnet --address-prefixes 10.30.0.0/16 \
  --subnet-name lab --subnet-prefixes 10.30.1.0/24 -o none
az network vnet subnet update -g "$RG" --vnet-name avdlab-vnet -n lab \
  --network-security-group avdlab-nsg -o none

az keyvault create -g "$RG" -n "$KV" -l "$LOC" --enable-rbac-authorization false -o none
umask 077
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
for name in rdlab-admin rdlab-emruser rdlab-pnuser rdlab-harness; do
  # Windows complexity: upper, lower, digit and symbol, 24 characters.
  python3 -I -c 'import secrets,string;a=string.ascii_letters+string.digits;print("Aa1!"+"".join(secrets.choice(a) for _ in range(20)),end="")' > "$tmp"
  az keyvault secret set --vault-name "$KV" -n "$name" --file "$tmp" -o none
done

az storage account create -g "$RG" -n "$SA" -l "$LOC" --sku Standard_LRS \
  --allow-blob-public-access false --min-tls-version TLS1_2 -o none
key=$(az storage account keys list -g "$RG" -n "$SA" --query "[0].value" -o tsv)
az storage container create --account-name "$SA" --account-key "$key" -n results -o none
echo "bootstrapped $RG (vault $KV, storage $SA)"
