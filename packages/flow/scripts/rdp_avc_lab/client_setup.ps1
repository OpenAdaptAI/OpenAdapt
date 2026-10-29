# Run ON vm-client through run_ps.sh. Prepares the box whose Remote Desktop
# Connection (mstsc) window the harness drives:
#   - Media Foundation, so mstsc can decode H.264/AVC444;
#   - Python 3.12 and openadapt-flow from the given git ref, with the repo
#     checkout at C:\oa\src (the harness lives under packages/flow/benchmark);
#   - a local `harness` user that logs on automatically (Sysinternals
#     Autologon keeps the password as an LSA secret, not in the registry);
#   - the job runner, started in that user's interactive session at logon.
# The two session passwords are left in a file only `harness` can read; the
# runner stores them with cmdkey on first logon and deletes the file.
param(
  [Parameter(Mandatory = $true)][string]$HarnessPassword,
  [Parameter(Mandatory = $true)][string]$EmrPassword,
  [Parameter(Mandatory = $true)][string]$PnPassword,
  [Parameter(Mandatory = $true)][string]$Ref,
  [string]$HostIp = "10.30.1.7"
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$py = "C:\Program Files\Python312\python.exe"
New-Item -ItemType Directory -Force C:\oa, C:\oa\jobs, C:\oa\results, C:\oa\secret | Out-Null

Install-WindowsFeature Server-Media-Foundation | Out-Null

if (-not (Test-Path $py)) {
  Invoke-WebRequest "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe" -OutFile C:\oa\py.exe -UseBasicParsing
  Start-Process C:\oa\py.exe -Wait -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_test=0"
}
Invoke-WebRequest "https://github.com/OpenAdaptAI/OpenAdapt/archive/$Ref.zip" -OutFile C:\oa\src.zip -UseBasicParsing
if (Test-Path C:\oa\src) { Remove-Item C:\oa\src -Recurse -Force }
Expand-Archive C:\oa\src.zip C:\oa\unz -Force
Move-Item (Get-ChildItem C:\oa\unz | Select-Object -First 1).FullName C:\oa\src
Remove-Item C:\oa\unz, C:\oa\src.zip -Recurse -Force
& $py -m pip install --quiet --upgrade pip
# openadapt-types from the same checkout, so flow's pin is met at this ref.
& $py -m pip install --quiet "C:\oa\src\packages\types" "C:\oa\src\packages\flow"
if ($LASTEXITCODE -ne 0) { throw "openadapt-flow install failed" }
Set-Content C:\oa\ref.txt $Ref

# --- harness user + autologon ------------------------------------------------
$sec = ConvertTo-SecureString $HarnessPassword -AsPlainText -Force
if (Get-LocalUser -Name harness -ErrorAction SilentlyContinue) {
  Set-LocalUser -Name harness -Password $sec -PasswordNeverExpires $true
} else {
  New-LocalUser -Name harness -Password $sec -PasswordNeverExpires -AccountNeverExpires | Out-Null
}
icacls C:\oa /grant "harness:(OI)(CI)M" | Out-Null
if (-not (Test-Path C:\oa\Autologon64.exe)) {
  Invoke-WebRequest "https://download.sysinternals.com/files/AutoLogon.zip" -OutFile C:\oa\al.zip -UseBasicParsing
  Expand-Archive C:\oa\al.zip C:\oa -Force
}
& C:\oa\Autologon64.exe /accepteula harness $env:COMPUTERNAME $HarnessPassword | Out-Null
# No lock screen, no idle lock, monitor never sleeps: the desktop must stay
# unlocked for SendInput and PrintWindow.
$pol = "HKLM:\SOFTWARE\Policies\Microsoft\Windows\Personalization"
New-Item -Path $pol -Force | Out-Null
Set-ItemProperty $pol NoLockScreen 1 -Type DWord
Set-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System" InactivityTimeoutSecs 0 -Type DWord
Set-ItemProperty "HKLM:\SOFTWARE\Microsoft\ServerManager" DoNotOpenServerManagerAtLogon 1 -Type DWord
powercfg /change monitor-timeout-ac 0

# --- session credentials for the runner (read once, then deleted) -----------
@{ host = $HostIp; emruser = $EmrPassword; pnuser = $PnPassword } | ConvertTo-Json |
  Set-Content C:\oa\secret\creds.json
icacls C:\oa\secret /inheritance:r /grant "harness:(OI)(CI)F" /grant "SYSTEM:(OI)(CI)F" | Out-Null

# --- job runner at logon -------------------------------------------------------
$action = New-ScheduledTaskAction -Execute $py `
  -Argument "C:\oa\src\packages\flow\benchmark\rdp_avc_real\runner.py" -WorkingDirectory C:\oa
$principal = New-ScheduledTaskPrincipal -UserId harness -LogonType Interactive -RunLevel Limited
$trigger = New-ScheduledTaskTrigger -AtLogOn -User harness
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Days 3) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName oa-runner -Action $action -Trigger $trigger -Principal $principal `
  -Settings $settings -Force | Out-Null
Write-Output "client configured; rebooting into the harness session"
Restart-Computer -Force
