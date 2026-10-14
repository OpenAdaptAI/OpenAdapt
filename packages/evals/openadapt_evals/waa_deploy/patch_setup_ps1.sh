#!/bin/sh
# Patch WAA setup.ps1 to try pre-downloaded LibreOffice MSI before downloading.
#
# Usage: sh patch_setup_ps1.sh /oem/setup.ps1
#
# Replaces the Python version (patch_setup_ps1.py) which required python3
# in the Docker image. This shell version has no dependencies.

set -e

SETUP_PS1="${1:?Usage: $0 <path-to-setup.ps1>}"

if [ ! -f "$SETUP_PS1" ]; then
    echo "WARNING: $SETUP_PS1 not found, skipping patch"
    exit 0
fi

# Check if already patched (idempotent)
if grep -q "pre-downloaded MSI from Samba share" "$SETUP_PS1"; then
    echo "$SETUP_PS1 already patched (skipping)"
    exit 0
fi

# Check for marker line
MARKER='$libreOfficeInstallerFilePath = "$env:TEMP\\libreOffice_installer.exe"'
if ! grep -qF "$MARKER" "$SETUP_PS1"; then
    echo "WARNING: Could not patch $SETUP_PS1 (marker pattern not found)"
    exit 0
fi

# Create the patch block
PATCH_BLOCK='    # Try pre-downloaded MSI from Samba share first (baked into Docker image)
    $predownloadedMsi = Get-ChildItem "\\\\host.lan\\Data\\LibreOffice_*_Win_x86-64.msi" -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName
    if ($predownloadedMsi) {
        Write-Host "Installing LibreOffice from pre-downloaded MSI: $predownloadedMsi"
        Start-Process "msiexec.exe" -ArgumentList "/i `\\"$predownloadedMsi`\\" /quiet" -Wait -NoNewWindow
        Write-Host "LibreOffice installed from pre-downloaded MSI."
        Add-ToEnvPath -NewPath "C:\\Program Files\\LibreOffice\\program"
    } else {'

# Insert patch block after the marker line using sed
# Use a temp file to avoid sed -i portability issues
TMP="$(mktemp)"
awk -v patch="$PATCH_BLOCK" '
    /\$libreOfficeInstallerFilePath = "\$env:TEMP\\\\libreOffice_installer\.exe"/ {
        print
        print patch
        next
    }
    { print }
' "$SETUP_PS1" > "$TMP"

# Add closing brace after the last Add-ToEnvPath for LibreOffice
# Find the last occurrence and add "    }" after the next "}"
awk '
    /Add-ToEnvPath -NewPath "C:\\\\Program Files\\\\LibreOffice\\\\program"/ {
        found = NR
    }
    {
        lines[NR] = $0
    }
    END {
        closed = 0
        for (i = 1; i <= NR; i++) {
            print lines[i]
            if (!closed && i > found && lines[i] ~ /^[[:space:]]*\}/) {
                print "    }"
                closed = 1
            }
        }
    }
' "$TMP" > "${TMP}.2"

mv "${TMP}.2" "$SETUP_PS1"
rm -f "$TMP"

echo "Patched $SETUP_PS1 with pre-downloaded MSI fallback"
