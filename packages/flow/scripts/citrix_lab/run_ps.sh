#!/usr/bin/env bash
# Run one of the lab PowerShell scripts on a VM through a managed run command.
# The lab password goes in as a protected parameter, so Azure does not echo it
# in the run command's instance view. Reads it from $ADMIN_PW, or from the
# macOS keychain item `openadapt-citrix-lab` when unset. Set PROTECTED to one
# more `Name=value` secret (for example a SAS URL) to pass it the same way.
#
#   bash run_ps.sh vm-ddc 10_promote_dc.ps1 [Name=Value ...]
set -euo pipefail
RG="${RG:-openadapt-citrix-lab}"
vm="${1:?vm name}"; script="${2:?script path}"; shift 2
here="$(cd "$(dirname "$0")" && pwd)"
[ -f "$script" ] || script="$here/$script"
pw="${ADMIN_PW:-$(security find-generic-password -s openadapt-citrix-lab -a azureadmin -w)}"
name="oa-$(basename "$script" .ps1 | tr '_' '-')-$(date +%s)"

args=(--resource-group "$RG" --vm-name "$vm" --name "$name"
      --script "@$script" --timeout-in-seconds 5400 --async-execution false
      --protected-parameters "Password=$pw" ${PROTECTED:+"$PROTECTED"} --output none)
[ $# -gt 0 ] && args+=(--parameters "$@")

az vm run-command create "${args[@]}" || true
az vm run-command show --resource-group "$RG" --vm-name "$vm" --name "$name" \
  --instance-view --query "instanceView.{state:executionState,exit:exitCode,out:output,err:error}" -o json
az vm run-command delete --resource-group "$RG" --vm-name "$vm" --name "$name" --yes --no-wait
