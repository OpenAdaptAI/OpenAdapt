# Run ON vm-rdhost through run_ps.sh (SECRETS="Password=rdlab-pnreader").
# Creates pnreader, the read-back session's own logon. Its logon opens Patient
# Notes like pnuser's, but C:\oa grants it read only (pnuser alone may modify),
# so the read-back session cannot change the record even by mistake.
param([Parameter(Mandatory=$true)][string]$Password)
$ErrorActionPreference = "Stop"
$sec = ConvertTo-SecureString $Password -AsPlainText -Force
if (Get-LocalUser -Name pnreader -ErrorAction SilentlyContinue) { Set-LocalUser -Name pnreader -Password $sec -PasswordNeverExpires $true }
else { New-LocalUser -Name pnreader -Password $sec -PasswordNeverExpires -AccountNeverExpires | Out-Null }
Add-LocalGroupMember -Group "Remote Desktop Users" -Member pnreader -ErrorAction SilentlyContinue
$py = "C:\Program Files\Python312\python.exe"
$a = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-STA -WindowStyle Hidden -ExecutionPolicy Bypass -File C:\oa\patient_notes.ps1 -Python `"$py`""
$p = New-ScheduledTaskPrincipal -UserId pnreader -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName oa-app-pnreader -Action $a -Trigger (New-ScheduledTaskTrigger -AtLogOn -User pnreader) -Principal $p -Force | Out-Null
"pnreader ready"
