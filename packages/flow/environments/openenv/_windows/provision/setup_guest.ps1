# Prepare a Windows machine you supply for the OpenAdapt Windows environments.
#
# Run once, as an administrator, on a Windows 10/11 or Windows Server
# 2019/2022/2025 machine built from your own image (or an Azure or AWS Windows
# template). This script does not download or ship Windows. It:
#
#   1. turns on WinRM (the environment server's control channel: reset and
#      the database read-out) and Remote Desktop (the pixels-only surface);
#   2. sets the Remote Desktop host to prefer H.264/AVC444 graphics, as a
#      production session host would, and keeps the desktop session inside
#      the app (machine shell policies);
#   3. installs CPython (official, signature-checked) and the in-guest UI
#      Automation agent's packages;
#   4. creates C:\oa\pack for the apps (the server copies them in and makes
#      the code read-only to the desktop account; only C:\oa\pack\data is
#      writable by it).
#
# The desktop account must be a standard user, or an administrator with UAC
# on, so its desktop session cannot rewrite the code the oracle runs; the
# server checks this and refuses the guest otherwise. The WinRM account must
# be an administrator.
#
# Usage (elevated PowerShell):
#   powershell -ExecutionPolicy Bypass -File setup_guest.ps1 -User <desktop account>
#
# The desktop account must be signed in to its desktop (console or RDP) while
# episodes run: the apps and the agent start in that session.

param(
    [Parameter(Mandatory = $true)][string]$User,
    [string]$PythonVersion = "3.12.10"
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

# 1. WinRM over HTTP with NTLM message encryption, and Remote Desktop.
Enable-PSRemoting -Force -SkipNetworkProfileCheck | Out-Null
Set-Item WSMan:\localhost\Service\AllowUnencrypted -Value $false
Set-ItemProperty "HKLM:\System\CurrentControlSet\Control\Terminal Server" -Name fDenyTSConnections -Value 0
Enable-NetFirewallRule -DisplayGroup "Remote Desktop" | Out-Null
Add-LocalGroupMember -Group "Remote Desktop Users" -Member $User -ErrorAction SilentlyContinue

# 2. Prefer the AVC444 graphics mode (group policy "Prioritize H.264/AVC 444
#    graphics mode for Remote Desktop Connections") and allow the software
#    encoder when there is no GPU.
$ts = "HKLM:\SOFTWARE\Policies\Microsoft\Windows NT\Terminal Services"
New-Item -Path $ts -Force | Out-Null
Set-ItemProperty $ts -Name AVC444ModePreferred -Value 1 -Type DWord
Set-ItemProperty $ts -Name AVCHardwareEncodePreferred -Value 1 -Type DWord

# 2b. Keep the desktop session inside the app. The apps run full screen over
#     the taskbar and the environment refuses the Windows key, Alt and the
#     function keys; these machine policies close the remaining exits (Win
#     key shortcuts, Run, Task Manager). Use a machine dedicated to the
#     environments: they apply to every account.
$ex = "HKLM:\Software\Microsoft\Windows\CurrentVersion\Policies\Explorer"
$sy = "HKLM:\Software\Microsoft\Windows\CurrentVersion\Policies\System"
New-Item -Path $ex -Force | Out-Null
New-Item -Path $sy -Force | Out-Null
Set-ItemProperty $ex -Name NoWinKeys -Value 1 -Type DWord
Set-ItemProperty $ex -Name NoRun -Value 1 -Type DWord
Set-ItemProperty $sy -Name DisableTaskMgr -Value 1 -Type DWord

# 3. CPython for this machine's architecture, and the agent's packages.
$arch = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "arm64" } else { "amd64" }
$dir = "C:\Program Files\Python312-$arch"
$py = Join-Path $dir "python.exe"
if (-not (Test-Path $py)) {
    New-Item -ItemType Directory -Force C:\oa\dl | Out-Null
    $exe = "C:\oa\dl\python-$PythonVersion-$arch.exe"
    curl.exe -s -f -L -o $exe "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-$arch.exe"
    $sig = Get-AuthenticodeSignature $exe
    if ($sig.Status -ne "Valid" -or $sig.SignerCertificate.Subject -notmatch "Python Software Foundation") {
        throw "the Python installer's signature is not valid"
    }
    Start-Process -Wait -FilePath $exe -ArgumentList '/quiet', 'InstallAllUsers=1', 'Include_test=0', "TargetDir=`"$dir`""
}
& $py -m pip install --disable-pip-version-check -q "uiautomation==2.0.29" "comtypes==1.4.17" "pyautogui==0.9.54" "pillow==13.0.0" "mss==10.2.0"

# 4. The pack directory. The server sets its permissions at deploy.
New-Item -ItemType Directory -Force C:\oa\pack\data | Out-Null
Set-Content -Encoding ascii -Path C:\oa\pack\python.txt -Value $py

Write-Output "ready: python=$py; set OPENADAPT_WINENV_PYTHON to this path on the server"
