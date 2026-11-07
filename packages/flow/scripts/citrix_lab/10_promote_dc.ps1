# Run ON vm-ddc via `az vm run-command create --protected-parameters`.
# Promotes the box to the single domain controller for citrixlab.local
# (NetBIOS CITRIXLAB). The VM reboots on its own when promotion finishes.
# Run 12_lab_users.ps1 after the reboot to create the ICA logon user.
param(
  [Parameter(Mandatory = $true)][string]$Password,
  [string]$Domain = "citrixlab.local",
  [string]$NetBios = "CITRIXLAB"
)
$ErrorActionPreference = "Stop"

if ((Get-CimInstance Win32_ComputerSystem).DomainRole -ge 4) {
  Write-Output "Already a domain controller."
  exit 0
}
Install-WindowsFeature AD-Domain-Services, DNS -IncludeManagementTools | Out-Null
$safe = ConvertTo-SecureString $Password -AsPlainText -Force
Install-ADDSForest -DomainName $Domain -DomainNetbiosName $NetBios `
  -SafeModeAdministratorPassword $safe -InstallDns -Force -NoRebootOnCompletion:$false
