#!/usr/bin/env bash
# Register vm-avdhost with the host pool: mint a 4-hour registration token and
# install the AVD agent and boot loader with it. The token is a secret; it goes
# to the VM as a protected run-command parameter and is never printed.
set -euo pipefail
RG="${RG:-openadapt-avd-lab}"
POOL="${POOL:-avdlab-pool}"
VM="${VM:-vm-avdhost}"

expiry=$(date -u -v+4H '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date -u -d '+4 hours' '+%Y-%m-%dT%H:%M:%SZ')
token=$(az desktopvirtualization hostpool update -g "$RG" -n "$POOL" \
  --registration-info expiration-time="$expiry" registration-token-operation=Update \
  --query registrationInfo.token -o tsv)

script=$(mktemp)
cat > "$script" <<'PS'
param([Parameter(Mandatory = $true)][string]$Token)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
if (Get-Service RDAgentBootLoader -ErrorAction SilentlyContinue) {
  Write-Output "Agent already installed."; exit 0
}
$agent = "C:\RDAgent.msi"; $boot = "C:\RDAgentBootLoader.msi"
Invoke-WebRequest "https://go.microsoft.com/fwlink/?linkid=2310011" -OutFile $agent -UseBasicParsing
Invoke-WebRequest "https://go.microsoft.com/fwlink/?linkid=2311028" -OutFile $boot -UseBasicParsing
$p = Start-Process msiexec.exe -Wait -PassThru -ArgumentList "/i $agent /quiet /norestart REGISTRATIONTOKEN=$Token"
if ($p.ExitCode -ne 0) { throw "RDAgent install exit $($p.ExitCode)" }
$p = Start-Process msiexec.exe -Wait -PassThru -ArgumentList "/i $boot /quiet /norestart"
if ($p.ExitCode -ne 0) { throw "BootLoader install exit $($p.ExitCode)" }
Write-Output "Agent and boot loader installed."
PS

name="oa-register-$(date +%s)"
az vm run-command create -g "$RG" --vm-name "$VM" --name "$name" --script "@$script" \
  --protected-parameters "Token=$token" --timeout-in-seconds 1800 --async-execution false -o none || true
az vm run-command show -g "$RG" --vm-name "$VM" --name "$name" --instance-view \
  --query "instanceView.{state:executionState,exit:exitCode,out:output,err:error}" -o json
az vm run-command delete -g "$RG" --vm-name "$VM" --name "$name" --yes --no-wait
rm -f "$script"
