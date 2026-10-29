# Run ON vm-vda via `az vm run-command create --protected-parameters`.
# Joins the session host to citrixlab.local and reboots.
param(
  [Parameter(Mandatory = $true)][string]$Password,
  [string]$Domain = "citrixlab.local",
  [string]$AdminUser = "CITRIXLAB\azureadmin"
)
$ErrorActionPreference = "Stop"

if ((Get-CimInstance Win32_ComputerSystem).PartOfDomain) {
  Write-Output "Already joined to $((Get-CimInstance Win32_ComputerSystem).Domain)."
  exit 0
}
$cred = New-Object System.Management.Automation.PSCredential(
  $AdminUser, (ConvertTo-SecureString $Password -AsPlainText -Force))
Add-Computer -DomainName $Domain -Credential $cred -Restart -Force
