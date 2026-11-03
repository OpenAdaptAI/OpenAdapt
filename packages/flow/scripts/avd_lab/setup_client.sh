#!/usr/bin/env bash
# Prepare vm-client, the Windows box whose Remote Desktop client window the
# harness drives: the Microsoft Remote Desktop client (MSI, per machine),
# Python 3.12, openadapt-flow at a pinned commit, and two desktop shortcuts
# for the one human step (subscribe to the AVD feed; keep the session alive).
#
#   bash setup_client.sh <git-sha-or-branch>
set -euo pipefail
RG="${RG:-openadapt-avd-lab}"
VM="${VM:-vm-client}"
REF="${1:?git ref of OpenAdaptAI/OpenAdapt to install openadapt-flow from}"
PY="C:\\Program Files\\Python312\\python.exe"

script=$(mktemp)
cat > "$script" <<PS
\$ErrorActionPreference = "Stop"
\$ProgressPreference = "SilentlyContinue"
New-Item -ItemType Directory -Force C:\\oa | Out-Null
if (-not (Test-Path "C:\\Program Files\\Remote Desktop\\msrdc.exe")) {
  Invoke-WebRequest "https://go.microsoft.com/fwlink/?linkid=2139369" -OutFile C:\\oa\\rdc.msi -UseBasicParsing
  \$p = Start-Process msiexec.exe -Wait -PassThru -ArgumentList "/i C:\\oa\\rdc.msi /qn ALLUSERS=1"
  if (\$p.ExitCode -ne 0) { throw "Remote Desktop client install exit \$(\$p.ExitCode)" }
}
if (-not (Test-Path "$PY")) {
  Invoke-WebRequest "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe" -OutFile C:\\oa\\py.exe -UseBasicParsing
  Start-Process C:\\oa\\py.exe -Wait -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_test=0"
}
& "$PY" -m pip install --quiet --upgrade pip
& "$PY" -m pip install --quiet "openadapt-flow @ https://github.com/OpenAdaptAI/OpenAdapt/archive/$REF.zip#subdirectory=packages/flow"
& "$PY" -c "import openadapt_flow, sys; print('openadapt_flow', openadapt_flow.__file__)"

# The human step. Subscribe opens the client's feed sign-in; KeepAlive moves
# this RDP/Bastion session to the console so the desktop keeps rendering
# after the operator disconnects.
\$desk = "C:\\Users\\Public\\Desktop"
Set-Content "\$desk\\1 - Subscribe to AVD.cmd" 'start "" "ms-rd:subscribe?url=https://rdweb.wvd.microsoft.com"'
Set-Content "\$desk\\2 - Keep session alive.cmd" 'for /f "skip=1 tokens=3" %%s in (''query user %USERNAME%'') do tscon %%s /dest:console'
Write-Output "client ready"
PS
name="oa-client-$(date +%s)"
az vm run-command create -g "$RG" --vm-name "$VM" --name "$name" --script "@$script" \
  --timeout-in-seconds 3600 --async-execution false -o none || true
az vm run-command show -g "$RG" --vm-name "$VM" --name "$name" --instance-view \
  --query "instanceView.{state:executionState,exit:exitCode,out:output,err:error}" -o json
az vm run-command delete -g "$RG" --vm-name "$VM" --name "$name" --yes --no-wait
rm -f "$script"
