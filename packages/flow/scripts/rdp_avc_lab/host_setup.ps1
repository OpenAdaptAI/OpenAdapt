# Run ON vm-rdhost through run_ps.sh (passwords arrive as protected
# parameters). Turns the box into a remote-desktop host that streams with the
# H.264/AVC444 graphics policy, creates the two session users, and makes each
# user's logon open exactly one app:
#   emruser -> OpenEMR in Edge app mode (served by vm-emr)
#   pnuser  -> Patient Notes (WinForms; SQLite record in C:\oa\patients.db)
# Files pn_db.py and patient_notes.ps1 arrive base64-encoded from the caller.
param(
  [Parameter(Mandatory = $true)][string]$EmrPassword,
  [Parameter(Mandatory = $true)][string]$PnPassword,
  [Parameter(Mandatory = $true)][string]$PnDbB64,
  [Parameter(Mandatory = $true)][string]$PnAppB64,
  [string]$EmrUrl = "http://10.30.1.6:8390/interface/login/login.php?site=default"
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$py = "C:\Program Files\Python312\python.exe"

# --- Graphics: "Prioritize H.264/AVC 444 graphics mode" and "Configure
# H.264/AVC hardware encoding" (both Enabled), WDDM display driver on. -----
$ts = "HKLM:\SOFTWARE\Policies\Microsoft\Windows NT\Terminal Services"
New-Item -Path $ts -Force | Out-Null
Set-ItemProperty $ts AVC444ModePreferred 1 -Type DWord
Set-ItemProperty $ts AVCHardwareEncodePreferred 1 -Type DWord
Set-ItemProperty $ts bEnumerateHWBeforeSW 1 -Type DWord
Set-ItemProperty $ts fEnableWddmDriver 1 -Type DWord
Set-ItemProperty $ts fDenyTSConnections 0 -Type DWord
# Logon noise that would sit over the app.
Set-ItemProperty "HKLM:\SOFTWARE\Microsoft\ServerManager" DoNotOpenServerManagerAtLogon 1 -Type DWord
$edge = "HKLM:\SOFTWARE\Policies\Microsoft\Edge"
New-Item -Path $edge -Force | Out-Null
Set-ItemProperty $edge HideFirstRunExperience 1 -Type DWord
Set-ItemProperty $edge DefaultBrowserSettingEnabled 0 -Type DWord
Set-ItemProperty $edge PasswordManagerEnabled 0 -Type DWord
Set-ItemProperty $edge TranslateEnabled 0 -Type DWord

# --- Users ------------------------------------------------------------------
foreach ($u in @(@("emruser", $EmrPassword), @("pnuser", $PnPassword))) {
  $sec = ConvertTo-SecureString $u[1] -AsPlainText -Force
  if (Get-LocalUser -Name $u[0] -ErrorAction SilentlyContinue) {
    Set-LocalUser -Name $u[0] -Password $sec -PasswordNeverExpires $true
  } else {
    New-LocalUser -Name $u[0] -Password $sec -PasswordNeverExpires -AccountNeverExpires | Out-Null
  }
  Add-LocalGroupMember -Group "Remote Desktop Users" -Member $u[0] -ErrorAction SilentlyContinue
}

# --- Patient Notes ------------------------------------------------------------
New-Item -ItemType Directory -Force C:\oa | Out-Null
[IO.File]::WriteAllBytes("C:\oa\pn_db.py", [Convert]::FromBase64String($PnDbB64))
[IO.File]::WriteAllBytes("C:\oa\patient_notes.ps1", [Convert]::FromBase64String($PnAppB64))
if (-not (Test-Path $py)) {
  Invoke-WebRequest "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe" -OutFile C:\oa\py.exe -UseBasicParsing
  Start-Process C:\oa\py.exe -Wait -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_test=0"
}
if (-not (Test-Path C:\oa\patients.db)) { & $py C:\oa\pn_db.py seed | Out-Null }
icacls C:\oa /grant "pnuser:(OI)(CI)M" | Out-Null

# --- One app per user at logon ----------------------------------------------
$edgeExe = "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
$apps = @{
  "emruser" = New-ScheduledTaskAction -Execute $edgeExe -Argument "--app=$EmrUrl --start-maximized --no-first-run"
  "pnuser"  = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-STA -WindowStyle Hidden -ExecutionPolicy Bypass -File C:\oa\patient_notes.ps1 -Python `"$py`""
}
foreach ($u in $apps.Keys) {
  $principal = New-ScheduledTaskPrincipal -UserId $u -LogonType Interactive -RunLevel Limited
  $trigger = New-ScheduledTaskTrigger -AtLogOn -User $u
  Register-ScheduledTask -TaskName "oa-app-$u" -Action $apps[$u] -Trigger $trigger -Principal $principal -Force | Out-Null
}
Write-Output "host configured; reboot to apply the graphics policy"
Restart-Computer -Force
