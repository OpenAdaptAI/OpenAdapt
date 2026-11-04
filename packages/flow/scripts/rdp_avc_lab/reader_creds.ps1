# Run ON vm-client through run_ps.sh (SECRETS="ReaderPassword=rdlab-pnreader").
# Leaves the read-back logon's password where only `harness` can read it; the
# next read-back job stores it with cmdkey and deletes the file.
param(
  [Parameter(Mandatory = $true)][string]$Password,  # unused; run_ps.sh always sends it
  [Parameter(Mandatory = $true)][string]$ReaderPassword
)
$ErrorActionPreference = "Stop"
@{ pnreader = $ReaderPassword } | ConvertTo-Json | Set-Content C:\oa\secret\creds.json
icacls C:\oa\secret\creds.json /inheritance:r /grant "harness:F" /grant "SYSTEM:F" | Out-Null
"reader credentials staged"
