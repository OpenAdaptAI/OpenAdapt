# Run ON vm-ddc and vm-vda via run_ps.sh with PROTECTED="SasUrl=<url>".
# Downloads the CVAD ISO that stage_iso.sh put in the lab's private blob
# container and checks its SHA-256 against the local copy's.
param(
  [Parameter(Mandatory = $true)][string]$Password,  # unused; run_ps.sh always sends it
  [Parameter(Mandatory = $true)][string]$SasUrl,
  [Parameter(Mandatory = $true)][string]$Sha256,
  [string]$IsoPath = "C:\cvad.iso"
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

if (-not ((Test-Path $IsoPath) -and ((Get-FileHash $IsoPath -Algorithm SHA256).Hash -eq $Sha256.ToUpper()))) {
  Invoke-WebRequest -Uri $SasUrl -OutFile $IsoPath -UseBasicParsing
}
$got = (Get-FileHash $IsoPath -Algorithm SHA256).Hash
if ($got -ne $Sha256.ToUpper()) { throw "ISO hash mismatch: $got" }
Write-Output "ISO at $IsoPath, SHA-256 $got"
