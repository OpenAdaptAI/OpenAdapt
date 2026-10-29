#!/usr/bin/env bash
# Run a lab PowerShell script on a VM as a managed run command.
#   bash run_ps.sh <vm> <script.ps1> [Name=value ...]
# Secrets: set SECRETS="Param=kv-secret-name ..."; each is read from Key Vault
# and passed as a protected parameter, so it is neither printed nor shown in
# the run command's instance view.
set -euo pipefail
RG="${RG:-openadapt-avd-lab}"
KV="${KV:-oa-rdlab-b8ba32}"
vm="${1:?vm name}"; script="${2:?script path}"; shift 2
here="$(cd "$(dirname "$0")" && pwd)"
[ -f "$script" ] || script="$here/$script"
name="oa-$(basename "$script" .ps1 | tr '_' '-')-$(date +%s)"

protected=()
for pair in ${SECRETS:-}; do
  protected+=("${pair%%=*}=$(az keyvault secret show --vault-name "$KV" -n "${pair#*=}" --query value -o tsv)")
done
args=(--resource-group "$RG" --vm-name "$vm" --name "$name" --script "@$script"
      --timeout-in-seconds "${TIMEOUT:-3600}" --async-execution false --output none)
[ ${#protected[@]} -gt 0 ] && args+=(--protected-parameters "${protected[@]}")
[ $# -gt 0 ] && args+=(--parameters "$@")

az vm run-command create "${args[@]}" || true
az vm run-command show --resource-group "$RG" --vm-name "$vm" --name "$name" \
  --instance-view --query "instanceView.{state:executionState,exit:exitCode,out:output,err:error}" -o json
az vm run-command delete --resource-group "$RG" --vm-name "$vm" --name "$name" --yes --no-wait
