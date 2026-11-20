# Run ON vm-rdhost through run_ps.sh, after host_setup.ps1. Starts the lab's
# reset and record service (benchmark/rdp_avc_real/oracle_service.py, "pn"
# mode) as SYSTEM at boot, outside every session the harness drives, and
# admits only the client VM to it.
param(
  [Parameter(Mandatory = $true)][string]$OracleB64,
  [string]$ClientIp = "10.30.1.5",
  [string]$HostIp = "10.30.1.7",
  [int]$Port = 8770
)
$ErrorActionPreference = "Stop"
$py = "C:\Program Files\Python312\python.exe"
New-Item -ItemType Directory -Force C:\oa\svc | Out-Null
[IO.File]::WriteAllBytes("C:\oa\svc\oracle_service.py", [Convert]::FromBase64String($OracleB64))
$action = New-ScheduledTaskAction -Execute $py -Argument "C:\oa\svc\oracle_service.py pn $HostIp $Port" -WorkingDirectory C:\oa\svc
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId SYSTEM -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Days 7)
Register-ScheduledTask -TaskName oa-oracle -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Get-NetFirewallRule -DisplayName "oa-oracle" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName "oa-oracle" -Direction Inbound -Protocol TCP -LocalPort $Port -RemoteAddress $ClientIp -Action Allow | Out-Null
Stop-ScheduledTask -TaskName oa-oracle -ErrorAction SilentlyContinue
Start-ScheduledTask -TaskName oa-oracle
Start-Sleep 3
Write-Output ("oracle listening: " + [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue))
