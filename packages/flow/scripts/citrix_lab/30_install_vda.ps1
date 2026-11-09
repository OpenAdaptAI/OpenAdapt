# Run ON vm-vda (domain-joined session host), elevated, AFTER the CVAD ISO is
# on the box. Installs the multi-session (Server OS) VDA and registers it with
# the Delivery Controller. Reboots automatically.
param(
  [string]$IsoPath    = "C:\cvad.iso",
  [string]$Controller = "vm-ddc.citrixlab.local"
)
$ErrorActionPreference = "Stop"

$mount = Mount-DiskImage -ImagePath $IsoPath -PassThru
$drive = ($mount | Get-Volume).DriveLetter + ":"
$setup = Join-Path $drive "x64\XenDesktop Setup\XenDesktopVdaSetup.exe"

# Server OS / multi-session VDA, persistent machine (NOT an MCS master image).
Start-Process $setup -ArgumentList @(
  "/quiet",
  "/components","vda",
  "/controllers","`"$Controller`"",
  "/enable_hdx_ports","/enable_real_time_transport",
  "/enable_remote_assistance","/servervdi"
) -Wait
Write-Output "VDA install complete; the box will reboot and register with $Controller."

# After reboot + a Machine Catalog/Delivery Group in Studio that includes this
# machine, the published desktop/app is reachable over ICA/HDX (1494/2598).
# Install your target app here too, e.g. the patient_notes WinForms clone, so the
# ladder's identity/effect oracle carries over from the Parallels RDP harness.
