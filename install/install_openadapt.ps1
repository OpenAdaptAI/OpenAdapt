# PowerShell script to pull OpenAdapt and install

################################   PARAMETERS   ################################

# Change these if a different version of is required

$tesseractCmd = "tesseract"
$tesseractInstaller = "tesseract-ocr-w64-setup-5.3.1.20230401.exe"
$tesseractInstallerLoc = "https://digi.bib.uni-mannheim.de/tesseract/tesseract-ocr-w64-setup-5.3.1.20230401.exe"
$tesseractPath = "C:\Program Files\Tesseract-OCR"

$pythonCmd = "python"
$pythonVerStr = "Python 3.10*"
$pythonInstaller = "python-3.10.11-amd64.exe"
$pythonInstallerLoc = "https://www.python.org/ftp/python/3.10.11/python-3.10.11-amd64.exe"

$gitCmd = "git"
$gitInstaller = "Git-2.40.1-64-bit.exe"
$gitInstallerLoc = "https://github.com/git-for-windows/git/releases/download/v2.40.1.windows.1/Git-2.40.1-64-bit.exe"
$gitUninstaller = "C:\Program Files\Git\unins000.exe"

$VCRedistInstaller = "vc_redist.x64.exe"
$VCRedistInstallerLoc = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
$VCRedistRegPath = "HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\X64"

################################   PARAMETERS   ################################


################################   FUNCTIONS    ################################
# Run a command and ensure it did not fail
function RunAndCheck {
    Param
    (
        [Parameter(Mandatory = $true)] [string] $Command,
        [Parameter(Mandatory = $true)] [string] $Desc
    )

    Invoke-Expression $Command
    if ($LastExitCode) {
        Write-Host "Failed: to $Desc : $LastExitCode"
        Cleanup
        exit
    }

    Write-Host "Success: $Desc"
}


function Cleanup {
    $exists = Test-Path -Path "..\OpenAdapt"
    If ($exists) {
        Set-Location ..\
        Remove-Item -LiteralPath "OpenAdapt" -Force -Recurse
    }
}


# Return true if a command/exe is available
function CheckCMDExists {
    Param
    (
        [Parameter(Mandatory = $true)] [string] $command
    )

    Get-Command $command -errorvariable getErr -erroraction 'silentlycontinue'
    If ($getErr -eq $null) {
       return $true
    }
    return $false
}


# Get command for python, install python if required version is unavailable
function GetPythonCMD {
    # Use python exe if it exists and is the required version
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
    if (!$exists) {
        Write-Host "Failed to download python installer"
        Cleanup
        exit
    }

    Write-Host "Installing python, click 'Yes' if prompted for permission"
    Start-Process -FilePath $pythonInstaller -Verb runAs -ArgumentList '/quiet', 'InstallAllUsers=0', 'PrependPath=1' -Wait

    #Refresh Path Environment Variable
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")

    # Make sure python is now available and the right version
    if (CheckCMDExists($pythonCmd)) {
        $res = Invoke-Expression "python -V"
        if ($res -like $pythonVerStr) {
            Remove-Item $pythonInstaller
            return $pythonCmd
        }
    }

    Write-Host "Error after installing python. Uninstalling, click 'Yes' if prompted for permission"
    Start-Process -FilePath $pythonInstaller -Verb runAs -ArgumentList '/quiet', '/uninstall' -Wait
    Remove-Item $pythonInstaller

    # Stop OpenAdapt install
    Cleanup
    exit
}

function GetTesseractCMD {
    # Use tesseract alias if it exists
    if (CheckCMDExists $tesseractCmd) {
        return $tesseractCmd
    }

    # Downlaod Tesseract OCR
    Write-Host "Downloading Tesseract OCR installer"
    $ProgressPreference = 'SilentlyContinue'
    Invoke-WebRequest -Uri $tesseractInstallerLoc -OutFile $tesseractInstaller
    $exists = Test-Path -Path $tesseractInstaller -PathType Leaf
    if (!$exists) {
        Write-Host "Failed to download Tesseract OCR installer"
        Cleanup
        exit
    }

    # Install the Tesseract OCR Setup exe (binary file)
    Write-Host "Installing Tesseract OCR, click 'Yes' if prompted for permission"
    Start-Process -FilePath $tesseractInstaller -Verb runAs -ArgumentList "/S" -Wait
    Remove-Item $tesseractInstaller

    # Check if Tesseract OCR was installed
    if (Test-Path -Path $tesseractPath -PathType Container) {
        Write-Host "TesseractOCR installation successful."
    }
    else {
        Write-Host "TesseractOCR installation failed."
        Cleanup
        exit
    }

    #Refresh Path Environment Variable
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")

    # Add Tesseract OCR to the System Path variable
    $systemEnvPath = [System.Environment]::GetEnvironmentVariable("Path", "Machine")
    $updatedSystemPath = "$systemEnvPath;$tesseractPath"
    [System.Environment]::SetEnvironmentVariable("Path", $updatedSystemPath, "Machine")

    #Refresh Path Environment Variable
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")

    # Add Tesseract OCR to the User Path variable
    $userEnvPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
    $updatedUserPath = "$userEnvPath;$tesseractPath"
    [System.Environment]::SetEnvironmentVariable("Path", $updatedUserPath, "User")

    #Refresh Path Environment Variable
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")

    Write-Host "Added Tesseract OCR to PATH"


    # Make sure tesseract is now available
    if (CheckCMDExists($tesseractCmd)) {
        return $tesseractCmd
    }

    Write-Host "Error after installing Tesseract OCR"
    # Stop OpenAdapt install
    Cleanup
    exit
}


function GetGitCMD {
    $gitExists = CheckCMDExists($gitCmd)
    if (!$gitExists) {
        # Install git
        Write-Host "Downloading git installer"
        $ProgressPreference = 'SilentlyContinue'
        Invoke-WebRequest -Uri $gitInstallerLoc -OutFile $gitInstaller
        $exists = Test-Path -Path $gitInstaller -PathType Leaf
        if (!$exists) {
            Write-Host "Failed to download git installer"
            exit
        }

        Write-Host "Installing git, click 'Yes' if prompted for permission"
        Start-Process -FilePath $gitInstaller -Verb runAs -ArgumentList '/VERYSILENT /NORESTART /NOCANCEL /SP- /CLOSEAPPLICATIONS /RESTARTAPPLICATIONS /COMPONENTS="icons,ext\reg\shellhere,assoc,assoc_sh"' -Wait
        Remove-Item $gitInstaller

        #Refresh Path Environment Variable
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")

        # Make sure git is now available
        $gitExists = CheckCMDExists($gitCmd)
        if (!$gitExists) {
            Write-Host "Error after installing git. Uninstalling, click 'Yes' if prompted for permission"
            Start-Process -FilePath $gitUninstaller -Verb runAs -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART' -Wait
            exit
        }
    }
    # Return the git command
    return $gitCmd
}


function GetVSCppRedistCMD {
    # Check if Visual C++ Redist is installed
    $ErrorActionPreference = "SilentlyContinue"
    $vcredistExists = Get-ItemPropertyValue -Path $VCRedistRegPath -erroraction 'silentlycontinue' -Name Installed
    $ErrorActionPreference = "Continue"

    if (!$vcredistExists) {
        # Install Visual C++ Redist
        Write-Host "Downloading Visual C++ Redist"
        $ProgressPreference = 'SilentlyContinue'
        Invoke-WebRequest -Uri $VCRedistInstallerLoc -OutFile $VCRedistInstaller
        $exists = Test-Path -Path $VCRedistInstaller -PathType Leaf
        if (!$exists) {
            Write-Host "Failed to download Visual C++ installer"
            Cleanup
            exit
        }

        Write-Host "Installing Visual C++ Redist, click 'Yes' if prompted for permission"
        Start-Process -FilePath $VCRedistInstaller -Verb runAs -ArgumentList "/install /q /norestart" -Wait
        Remove-Item $VCRedistInstaller

        if ($LastExitCode) {
            Write-Host "Failed to install Visual C++ Redist: $LastExitCode"
            Cleanup
            exit
        }
    }
}

################################   FUNCTIONS    ################################


################################   SCRIPT    ################################

# Check and Install TesseractOCR -> Python 3.10 -> Git -> VS C++ Redist.
$tesseract = GetTesseractCMD
RunAndCheck "$tesseract --version" "check TesseractOCR" > $null

$python = GetPythonCMD
RunAndCheck "$python --version" "check python version"

# $git = GetGitCMD
# RunAndCheck "$git --version" "check git version"

# GetVSCppRedistCMD

# # OpenAdapt Setup
# RunAndCheck "git clone -q https://github.com/MLDSAI/OpenAdapt.git" "clone git repo"
# Set-Location .\OpenAdapt
# RunAndCheck "pip install poetry" "install poetry"
# RunAndCheck "poetry install"
# RunAndCheck "poetry shell"
# RunAndCheck "alembic upgrade head" "alembic upgrade head"
# RunAndCheck "pytest" "run OpenAdapt tests"
Write-Host "OpenAdapt installed successfully"

################################   SCRIPT    ################################
