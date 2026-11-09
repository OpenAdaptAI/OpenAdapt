# Run ON vm-ddc, in an elevated PowerShell, AFTER the CVAD ISO has been placed
# on the box (the ISO is a gated Citrix-account download - see README step 3).
#
# Installs SQL Server Express (public MS download), then the CVAD core
# components in TRIAL MODE (no license file, no LAS, no registration -> the
# built-in 30-day / 10-connection trial). Site creation is done afterward in
# Web Studio (browse to https://localhost/Citrix/StoreWeb after) or via the
# Citrix.XenDesktop.Admin SDK.
param(
  [string]$IsoPath = "C:\cvad.iso",
  [string]$SqlExprUrl = "https://download.microsoft.com/download/3/8/d/38de7036-2433-4207-8eae-06e247e17b25/SQLEXPR_x64_ENU.exe"
)
$ErrorActionPreference = "Stop"

# --- 1. SQL Server Express (site database backend) ---
if (-not (Get-Service 'MSSQL$SQLEXPRESS' -ErrorAction SilentlyContinue)) {
  $sql = "C:\SQLEXPR_x64_ENU.exe"
  Invoke-WebRequest $SqlExprUrl -OutFile $sql
  Start-Process $sql -ArgumentList "/QS /x:C:\sqlexpr" -Wait
  Start-Process "C:\sqlexpr\SETUP.EXE" -ArgumentList @(
    "/Q","/ACTION=Install","/FEATURES=SQLEngine","/INSTANCENAME=SQLEXPRESS",
    "/SQLSYSADMINACCOUNTS=BUILTIN\Administrators","CITRIXLAB\Domain Admins",
    "/TCPENABLED=1","/IACCEPTSQLSERVERLICENSETERMS"
  ) -Wait
}

# --- 2. Mount the CVAD ISO ---
$mount = Mount-DiskImage -ImagePath $IsoPath -PassThru
$drive = ($mount | Get-Volume).DriveLetter + ":"
Write-Output "CVAD ISO mounted at $drive"

# --- 3. Install Delivery Controller + Studio + StoreFront + Director + License Server ---
# No license is supplied -> Studio reports 30-day trial mode automatically.
$setup = Join-Path $drive "x64\XenDesktop Setup\XenDesktopServerSetup.exe"
Start-Process $setup -ArgumentList @(
  "/components","CONTROLLER,DESKTOPSTUDIO,DESKTOPDIRECTOR,LICENSESERVER,STOREFRONT",
  "/configure_firewall","/quiet","/noreboot"
) -Wait
Write-Output "CVAD core install complete. Reboot, then create the Site in Web Studio."

# --- 4. (post-reboot) create the Site programmatically instead of Web Studio ---
# asnp Citrix*
# New-XDSite -DatabaseServer "localhost\SQLEXPRESS" -SiteName "OpenAdaptLab" `
#   -AdminAddress "vm-ddc.citrixlab.local" `
#   -SiteDatabaseName "CitrixSite" -LoggingDatabaseName "CitrixLogging" `
#   -MonitorDatabaseName "CitrixMonitor"
# Licensing is left unconfigured -> 30-day trial mode (Get-BrokerSite shows LicensingModel).
