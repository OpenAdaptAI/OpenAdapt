# Run ON vm-ddc after promotion. Creates the ICA logon user and the group
# the Delivery Group publishes to. Also forwards DNS to Azure so the VNet
# keeps resolving public names (SQL Express download, Windows Update).
param(
  [Parameter(Mandatory = $true)][string]$Password,
  [string]$User = "clinician"
)
$ErrorActionPreference = "Stop"
Import-Module ActiveDirectory

if (-not (Get-DnsServerForwarder).IPAddress) {
  Add-DnsServerForwarder -IPAddress 168.63.129.16
}
if (-not (Get-ADGroup -Filter "Name -eq 'CitrixUsers'")) {
  New-ADGroup -Name CitrixUsers -GroupScope Global
}
if (-not (Get-ADUser -Filter "SamAccountName -eq '$User'")) {
  New-ADUser -Name $User -SamAccountName $User -Enabled $true `
    -AccountPassword (ConvertTo-SecureString $Password -AsPlainText -Force) `
    -PasswordNeverExpires $true
}
Add-ADGroupMember CitrixUsers -Members $User
Get-ADDomain | Select-Object DNSRoot, NetBIOSName | Format-List
