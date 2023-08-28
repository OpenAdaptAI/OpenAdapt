# PowerShell script to pull OpenAdapt and install

################################   PARAMETERS   ################################
# Change these if a different version is required

$setupdir = "C:/OpenAdaptSetup"

$tesseractCmd = "tesseract"
$tesseractInstaller = "tesseract.exe"
$tesseractInstallerLoc = "https://digi.bib.uni-mannheim.de/tesseract/tesseract-ocr-w64-setup-5.3.1.20230401.exe"
$tesseractPath = "C:\Program Files\Tesseract-OCR"

$pythonCmd = "python"
$pythonMinVersion = "3.10.0" # Change this if a different Lower version are supported by OpenAdapt
$pythonMaxVersion = "3.10.12" # Change this if a different Higher version are supported by OpenAdapt
$pythonInstaller = "python-3.10.11-amd64.exe"
$pythonInstallerLoc = "https://www.python.org/ftp/python/3.10.11/python-3.10.11-amd64.exe"

$gitCmd = "git"
$gitInstaller = "Git-2.40.1-64-bit.exe"
$gitInstallerLoc = "https://github.com/git-for-windows/git/releases/download/v2.40.1.windows.1/Git-2.40.1-64-bit.exe"
$gitUninstaller = "C:\Program Files\Git\unins000.exe"
################################   PARAMETERS   ################################


################################   FUNCTIONS    ################################
# Run a command and ensure it did not fail
function RunAndCheck {
    Param
    (
        [Parameter(Mandatory = $true)]
        [string] $Command,

        [Parameter(Mandatory = $true)]
        [string] $Desc,

        [Parameter(Mandatory = $false)]
        [switch] $SkipCleanup = $false
    )

    Invoke-Expression $Command
    if ($LastExitCode) {
        Write-Host "Failed: $Desc - Exit code: $LastExitCode" -ForegroundColor Red
        if (!$SkipCleanup) {
            Cleanup
            exit
        }
    }
    else {
        Write-Host "Success: $Desc" -ForegroundColor Green
    }
}

# Cleanup function to delete the setup directory
function Cleanup {
    $exists = Test-Path -Path $setupdir
    if ($exists) {
        Set-Location $setupdir
        Set-Location ../
        Remove-Item -LiteralPath $setupdir -Force -Recurse
    }
}


# Return true if a command/exe is available
function CheckCMDExists {
    Param
    (
        [Parameter(Mandatory = $true)] [string] $command
    )

    $result = Get-Command $command -errorvariable getErr -erroraction 'silentlycontinue'
    if ($null -eq $result) {
        return $false
    }
    return $true
}


# Return the current user's PATH variable
function GetUserPath {
    $userEnvPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
    return $userEnvPath
}


# Return the system's PATH variable
function GetSystemPath {
    $systemEnvPath = [System.Environment]::GetEnvironmentVariable("Path", "Machine")
    return $systemEnvPath
}


# Refresh Path Environment Variable for both Curent User and System
function RefreshPathVariables {
    $env:Path = GetUserPath + ";" + GetSystemPath
}


# Return true if a command/exe is available
function GetTesseractCMD {
    # Use tesseract alias if it exists
    if (CheckCMDExists $tesseractCmd) {
        return $tesseractCmd
    }

    # Check if tesseractPath exists and delete it if it does
    if (Test-Path -Path $tesseractPath -PathType Container) {
        Write-Host "Found Existing Old TesseractOCR, Deleting existing TesseractOCR folder"
        # Delete the whole folder
        Remove-Item $tesseractPath -Force -Recurse
    }

    # Downlaod Tesseract OCR
    Write-Host "Downloading Tesseract OCR installer"
    $ProgressPreference = 'SilentlyContinue'
    Invoke-WebRequest -Uri $tesseractInstallerLoc -OutFile $tesseractInstaller
    $exists = Test-Path -Path $tesseractInstaller -PathType Leaf
    if (!$exists) {
        Write-Host "Failed to download Tesseract OCR installer" -ForegroundColor Red
        Cleanup
        exit
    }

    # Install the Tesseract OCR Setup exe (binary file)
    Write-Host "Installing Tesseract OCR..."
    Start-Process -FilePath $tesseractInstaller -Verb runAs -ArgumentList "/S" -Wait
    Remove-Item $tesseractInstaller

    # Check if Tesseract OCR was installed
    if (Test-Path -Path $tesseractPath -PathType Container) {
        Write-Host "TesseractOCR installation successful." -ForegroundColor Green
    }
    else {
        Write-Host "TesseractOCR installation failed." -ForegroundColor Red
        Cleanup
        exit
    }

    RefreshPathVariables

    # Add Tesseract OCR to the System Path variable
    $systemEnvPath = GetSystemPath
    $updatedSystemPath = "$systemEnvPath;$tesseractPath"
    [System.Environment]::SetEnvironmentVariable("Path", $updatedSystemPath, "Machine")

    RefreshPathVariables

    # Add Tesseract OCR to the User Path variable
    $userEnvPath = GetUserPath
    $updatedUserPath = "$userEnvPath;$tesseractPath"
    [System.Environment]::SetEnvironmentVariable("Path", $updatedUserPath, "User")

    Write-Host "Added Tesseract OCR to PATH." -ForegroundColor Green

    # Make sure tesseract is now available
    if (CheckCMDExists($tesseractCmd)) {
        return $tesseractCmd
    }

    Write-Host "Error after installing Tesseract OCR."
    # Stop OpenAdapt install
    Cleanup
    exit
}


function ComparePythonVersion($version) {
    $v = [version]::new($version)
    $min = [version]::new($pythonMinVersion)
    $max = [version]::new($pythonMaxVersion)

    return $v -ge $min -and $v -le $max
}


# Check and Istall Python and return the python command
function GetPythonCMD {
    # Use python exe if it exists and is within the required version range
    if (CheckCMDExists $pythonCmd) {
        $res = Invoke-Expression "python -V"
        $versionString = $res.Split(' ')[-1]

        if (ComparePythonVersion $versionString  $pythonMaxVersion) {
            return $pythonCmd
        }
    }

    # Install required python version
    Write-Host "Downloading python installer..."
    $ProgressPreference = 'SilentlyContinue'
    Invoke-WebRequest -Uri $pythonInstallerLoc -OutFile $pythonInstaller
    $exists = Test-Path -Path $pythonInstaller -PathType Leaf

    if (!$exists) {
        Write-Host "Failed to download python installer" -ForegroundColor Red
        Cleanup
        exit
    }

    Write-Host "Installing python..."
    Start-Process -FilePath $pythonInstaller -Verb runAs -ArgumentList '/quiet', 'InstallAllUsers=0', 'PrependPath=1' -Wait

    RefreshPathVariables

    # Make sure python is now available and within the required version range
    if (CheckCMDExists $pythonCmd) {
        $res = Invoke-Expression "python -V"
        $versionString = $res.Split(' ')[-1]

        if (ComparePythonVersion $versionString $pythonMinVersion $pythonMaxVersion) {
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


# Check and Install Git and return the git command
function GetGitCMD {
    $gitExists = CheckCMDExists $gitCmd
    if (!$gitExists) {
        # Install git
        Write-Host "Downloading git installer..."
        $ProgressPreference = 'SilentlyContinue'
        Invoke-WebRequest -Uri $gitInstallerLoc -OutFile $gitInstaller
        $exists = Test-Path -Path $gitInstaller -PathType Leaf
        if (!$exists) {
            Write-Host "Failed to download git installer" -ForegroundColor Red
            exit
        }

        Write-Host "Installing git..."
        Start-Process -FilePath $gitInstaller -Verb runAs -ArgumentList '/VERYSILENT /NORESTART /NOCANCEL /SP- /CLOSEAPPLICATIONS /RESTARTAPPLICATIONS /COMPONENTS="icons,ext\reg\shellhere,assoc,assoc_sh"' -Wait
        Remove-Item $gitInstaller

        RefreshPathVariables

        # Make sure git is now available
        $gitExists = CheckCMDExists $gitCmd
        if (!$gitExists) {
            Write-Host "Error after installing git. Uninstalling..."
            Start-Process -FilePath $gitUninstaller -Verb runAs -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART' -Wait
            Cleanup
            exit
        }
    }
    # Return the git command
    return $gitCmd
}
################################   FUNCTIONS    ################################


################################   SCRIPT    ################################

Write-Host "Install Script Started..." -ForegroundColor Yellow

# Create a new directory and run the setup from there
New-Item -ItemType Directory -Path $setupdir -Force
Set-Location -Path $setupdir
Set-ExecutionPolicy RemoteSigned -Scope Process -Force

# Check and Install the required softwares for OpenAdapt
$tesseract = GetTesseractCMD
RunAndCheck "$tesseract --version" "check TesseractOCR"

$python = GetPythonCMD
RunAndCheck "$python --version" "check Python"

$git = GetGitCMD
RunAndCheck "$git --version" "check Git"

# OpenAdapt Setup
RunAndCheck "git clone -q https://github.com/MLDSAI/OpenAdapt.git" "clone git repo"
Set-Location .\OpenAdapt
RunAndCheck "pip install poetry" "Run ``pip install poetry``"
RunAndCheck "poetry install" "Run ``poetry install``"
RunAndCheck "poetry run alembic upgrade head" "Run ``alembic upgrade head``" -SkipCleanup:$true
RunAndCheck "poetry run pytest" "Run ``Pytest``" -SkipCleanup:$true
Write-Host "OpenAdapt installed Successfully!" -ForegroundColor Green
Start-Process powershell -Verb RunAs -ArgumentList "-NoExit", "-Command", "Set-Location -Path '$pwd'; poetry shell"

################################   SCRIPT    ################################
