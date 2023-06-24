# PowerShell script to pull OpenAdapt and install

################################   PARAMETERS   ################################
# Change these if a different version of is required

$setupdir = "C:/OpenAdaptSetup"

$tesseractCmd = "tesseract"
$tesseractInstaller = "tesseract.exe"
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


/**
 * Cleanup function to remove OpenAdapt folder created during the install process
 */
function Cleanup {
    $exists = Test-Path -Path $setupdir
    If ($exists) {
        Set-Location $setupdir
        Remove-Item -LiteralPath $setupdir -Force -Recurse
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

    # Refresh Path Environment Variable
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")

    # Add Tesseract OCR to the System Path variable
    $systemEnvPath = [System.Environment]::GetEnvironmentVariable("Path", "Machine")
    $updatedSystemPath = "$systemEnvPath;$tesseractPath"
    [System.Environment]::SetEnvironmentVariable("Path", $updatedSystemPath, "Machine")

    # Refresh Path Environment Variable
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")

    # Add Tesseract OCR to the User Path variable
    $userEnvPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
    $updatedUserPath = "$userEnvPath;$tesseractPath"
    [System.Environment]::SetEnvironmentVariable("Path", $updatedUserPath, "User")

    # Refresh Path Environment Variable
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")

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


function GetPythonCMD {
    # Use python exe if it exists and is the required version
    if (CheckCMDExists $pythonCmd) {
        $res = Invoke-Expression "python -V"
        if ($res -like $pythonVerStr) {
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

    #Refresh Path Environment Variable
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")

    # Make sure python is now available and the right version
    if (CheckCMDExists $pythonCmd) {
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

        # Refresh Path Environment Variable
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")

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

# Create a new directory and run the setup from there
New-Item -ItemType Directory -Path $setupdir -Force
Set-Location -Path $setupdir

# Check and Install the required softwares for OpenAdapt
$tesseract = GetTesseractCMD
RunAndCheck "$tesseract --version" "check TesseractOCR" > $null

$python = GetPythonCMD
RunAndCheck "$python --version" "check Python" > $null

$git = GetGitCMD
RunAndCheck "$git --version" "check Git" > $null

# OpenAdapt Setup
RunAndCheck "git clone -q https://github.com/MLDSAI/OpenAdapt.git" "clone git repo" > $null
Set-Location .\OpenAdapt
RunAndCheck "pip install poetry" "Run ``pip install poetry``" > $null
RunAndCheck "poetry install" "Run ``poetry install``" > $null
RunAndCheck "poetry run alembic upgrade head" "Run ``alembic upgrade head``" -SkipCleanup:$true > $null
RunAndCheck "poetry run pytest" "Run ``Pytest``" -SkipCleanup:$true > $null
Write-Host "OpenAdapt installed Successfully!" -ForegroundColor Green
Start-Process powershell -ArgumentList "-NoExit","-Command poetry shell"

################################   SCRIPT    ################################
