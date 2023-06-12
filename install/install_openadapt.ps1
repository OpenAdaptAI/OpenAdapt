# PowerShell script to pull OpenAdapt and install

# Change these if a different version of is required
$pythonCmd = "python3.10"
$pythonVerStr = "Python 3.10*"
$pythonInstaller = "python-3.10.11-amd64.exe"
$pythonInstallerLoc = "https://www.python.org/ftp/python/3.10.11/python-3.10.11-amd64.exe"

$gitInstaller = "Git-2.40.1-64-bit.exe"
$gitInstallerLoc = "https://github.com/git-for-windows/git/releases/download/v2.40.1.windows.1/Git-2.40.1-64-bit.exe"
$gitUninstaller = "C:\Program Files\Git\unins000.exe"

$VCRedistInstaller = "vc_redist.x64.exe"
$VCRedistInstallerLoc = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
$VCRedistRegPath = "HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\X64"


# Return true if a command/exe is available
function CheckCMDExists() {
    Param
    (
        [Parameter(Mandatory = $true)] [string] $command
    )

    Get-Command $command -errorvariable getErr -erroraction 'silentlycontinue'
    If ($getErr -eq $null) {
        $true;
    }
    return $false;
}

# Get command for python, install python if required version is unavailable
function GetPythonCMD() {
    # Use python alias of required version if it exists
    if (CheckCMDExists($pythonCmd)) {
        return $pythonCmd
    }

    # Use python exe if it exists and is the required version
    $pythonCmd = "python"
    if (CheckCMDExists($pythonCmd)) {
        $res = Invoke-Expression "python -V"
        if ($res -like $pythonVerStr) {
            return $pythonCmd
        }
    }

    # Install required python version
    Write-Host "Downloading python installer"
    $ProgressPreference = 'SilentlyContinue'
    Invoke-WebRequest -Uri $pythonInstallerLoc -OutFile $pythonInstaller
    $exists = Test-Path -Path $pythonInstaller -PathType Leaf
    if(!$exists) {
        Write-Host "Failed to download python installer"
        Cleanup
        exit
    }

    Write-Host "Installing python, click 'Yes' if prompted for permission"
    Start-Process -FilePath $pythonInstaller -Verb runAs -ArgumentList '/quiet','InstallAllUsers=0','PrependPath=1' -Wait

    #Refresh Path Environment Variable
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")

    # Make sure python is now available and the right version
    if (CheckCMDExists($pythonCmd)) {
        $res = Invoke-Expression "python -V"
        if ($res -like $pythonVerStr) {
            Remove-Item $pythonInstaller
            return $pythonCmd
        }
    }

    Write-Host "Error after installing python. Uninstalling, click 'Yes' if prompted for permission"
    Start-Process -FilePath $pythonInstaller -Verb runAs -ArgumentList '/quiet','/uninstall' -Wait
    Remove-Item $pythonInstaller

    # Stop OpenAdapt install
    Cleanup
    exit
}

function Cleanup {
    $exists = Test-Path -Path "..\OpenAdapt"
    if($exists) {
        Set-Location ..\
        Remove-Item -LiteralPath "OpenAdapt" -Force -Recurse
    }
}

# Run a command and ensure it did not fail
function RunAndCheck {
    Param
    (
        [Parameter(Mandatory = $true)] [string] $Command,
        [Parameter(Mandatory = $true)] [string] $Desc
    )

    Invoke-Expression $Command
    if ($LastExitCode) {
        Write-Host "Failed: $Desc : $LastExitCode"
        Cleanup
        exit
    }

    Write-Host "Success: $Desc"
}

$gitExists = CheckCMDExists "git"
if (!$gitExists) {
    # Install git
    Write-Host "Downloading git installer"
    $ProgressPreference = 'SilentlyContinue'
    Invoke-WebRequest -Uri $gitInstallerLoc -OutFile $gitInstaller
    $exists = Test-Path -Path $gitInstaller -PathType Leaf
    if(!$exists) {
        Write-Host "Failed to download git installer"
        exit
    }

    Write-Host "Installing git, click 'Yes' if prompted for permission"
    Start-Process -FilePath $gitInstaller -Verb runAs -ArgumentList '/VERYSILENT /NORESTART /NOCANCEL /SP- /CLOSEAPPLICATIONS /RESTARTAPPLICATIONS /COMPONENTS="icons,ext\reg\shellhere,assoc,assoc_sh"' -Wait
    Remove-Item $gitInstaller

    #Refresh Path Environment Variable
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")

    # Make sure git is now available
    $gitExists = CheckCMDExists "git"
    if (!$gitExists) {
        Write-Host "Error after installing git. Uninstalling, click 'Yes' if prompted for permission"
        Start-Process -FilePath $gitUninstaller -Verb runAs -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART' -Wait
        exit
    }
}

# Check if Visual C++ Redist is installed
# Note: Temporarily setting $ErrorActionPreference as -erroraction 'silentlycontinue' doesn't prevent non-terminating error with Get-ItemPropertyValue
$ErrorActionPreference="SilentlyContinue"
$vcredistExists = Get-ItemPropertyValue -Path $VCRedistRegPath -erroraction 'silentlycontinue' -Name Installed
$ErrorActionPreference="Continue"

if (!$vcredistExists) {
    # Install Visual C++ Redist
    Write-Host "Downloading Visual C++ Redist"
    $ProgressPreference = 'SilentlyContinue'
    Invoke-WebRequest -Uri $VCRedistInstallerLoc -OutFile $VCRedistInstaller
    $exists = Test-Path -Path $VCRedistInstaller -PathType Leaf
    if(!$exists) {
        Write-Host "Failed to download Visual C++ installer"
        exit
    }

    Write-Host "Installing Visual C++ Redist, click 'Yes' if prompted for permission"
    Start-Process -FilePath $VCRedistInstaller -Verb runAs -ArgumentList "/install /q /norestart" -Wait
    Remove-Item $VCRedistInstaller

    if($LastExitCode) {
        "Failed to install Visual C++ Redist: $LastExitCode"
        exit
    }
}

# download Vtracer
RunAndCheck "Invoke-WebRequest -useb (https://sh.rustup.rs) | Invoke-Expression - Command { $input | sh -s -- -y }"
RunAndCheck "cargo install vtracer" "install vtracer" 


RunAndCheck "git clone -q https://github.com/MLDSAI/OpenAdapt.git" "clone git repo"

Set-Location .\OpenAdapt

$python = GetPythonCMD

RunAndCheck "$python -m venv .venv" "create python virtual environment"
RunAndCheck ".venv\Scripts\Activate.ps1" "enable python virtual environment"
RunAndCheck "pip install wheel" "pip install wheel"
RunAndCheck "pip install -r requirements.txt" "pip install -r requirements.txt"
RunAndCheck "pip install -e ." "pip install -e ."
RunAndCheck "alembic upgrade head" "alembic upgrade head"
RunAndCheck "python -m spacy download en_core_web_trf" "python -m spacy download en_core_web_trf"
RunAndCheck "pytest" "run OpenAdapt tests"
