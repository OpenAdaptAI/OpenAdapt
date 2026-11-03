#!/usr/bin/env bash
# Install the two published apps on vm-avdhost and publish them as RemoteApps:
#   - OpenEMR in Edge app mode, served by vm-emr on the private network;
#   - Patient Notes, the WinForms app from scripts/desktop (SQLite record).
# Python is installed for pn_db.py, which owns the Patient Notes record.
set -euo pipefail
RG="${RG:-openadapt-avd-lab}"
VM="${VM:-vm-avdhost}"
EMR_URL="${EMR_URL:-http://10.30.1.6:8390/interface/login/login.php?site=default}"
here="$(cd "$(dirname "$0")" && pwd)"
desk="$here/../desktop"
PY="C:\\Program Files\\Python312\\python.exe"

script=$(mktemp)
cat > "$script" <<PS
\$ErrorActionPreference = "Stop"
\$ProgressPreference = "SilentlyContinue"
New-Item -ItemType Directory -Force C:\\oa | Out-Null
[IO.File]::WriteAllBytes("C:\\oa\\pn_db.py", [Convert]::FromBase64String("$(base64 < "$desk/pn_db.py" | tr -d '\n')"))
[IO.File]::WriteAllBytes("C:\\oa\\patient_notes.ps1", [Convert]::FromBase64String("$(base64 < "$desk/patient_notes.ps1" | tr -d '\n')"))
if (-not (Test-Path "$PY")) {
  Invoke-WebRequest "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe" -OutFile C:\\oa\\py.exe -UseBasicParsing
  Start-Process C:\\oa\\py.exe -Wait -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_test=0"
}
& "$PY" C:\\oa\\pn_db.py seed | Out-String
# Every session user reads and writes the shared record.
icacls C:\\oa /grant "*S-1-5-32-545:(OI)(CI)M" | Out-Null
Write-Output "apps staged"
PS
name="oa-apps-$(date +%s)"
az vm run-command create -g "$RG" --vm-name "$VM" --name "$name" --script "@$script" \
  --timeout-in-seconds 1800 --async-execution false -o none || true
az vm run-command show -g "$RG" --vm-name "$VM" --name "$name" --instance-view \
  --query "instanceView.{state:executionState,exit:exitCode,out:output,err:error}" -o json
az vm run-command delete -g "$RG" --vm-name "$VM" --name "$name" --yes --no-wait
rm -f "$script"

az desktopvirtualization application create -g "$RG" --application-group-name avdlab-apps \
  -n openemr --friendly-name "OpenEMR" --command-line-setting Require \
  --file-path "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe" \
  --command-line-arguments "--app=$EMR_URL --no-first-run --window-size=1280,860 --ignore-certificate-errors" \
  --show-in-portal true -o none
az desktopvirtualization application create -g "$RG" --application-group-name avdlab-apps \
  -n patientnotes --friendly-name "Patient Notes" --command-line-setting Require \
  --file-path "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" \
  --command-line-arguments "-STA -WindowStyle Hidden -ExecutionPolicy Bypass -File C:\\oa\\patient_notes.ps1 -Python \"$PY\"" \
  --show-in-portal true -o none
az desktopvirtualization application list -g "$RG" --application-group-name avdlab-apps \
  --query "[].{name:name, path:filePath}" -o table
