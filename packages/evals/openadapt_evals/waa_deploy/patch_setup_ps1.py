#!/usr/bin/env python3
"""Patch WAA setup.ps1 to try a pre-downloaded LibreOffice MSI before downloading.

The WAA setup.ps1 downloads LibreOffice from internet mirrors at runtime,
which frequently fails (timeouts, 404s when versions rotate).  Our Dockerfile
pre-downloads the MSI into /oem so it's available on the Samba share.

This script inserts a PowerShell block that checks for the pre-downloaded MSI
at \\\\host.lan\\Data\\ first and only falls through to the internet download
if the local copy isn't found.

Usage:
    python3 patch_setup_ps1.py /oem/setup.ps1
"""

import sys


def patch_setup_ps1(path: str) -> bool:
    """Patch setup.ps1 with pre-downloaded MSI fallback.

    Returns True if patched, False if marker not found (non-fatal).
    """
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()

    # Marker: the line that sets the download path for LibreOffice installer.
    # We insert our MSI-first block immediately after this line.
    marker = '$libreOfficeInstallerFilePath = "$env:TEMP\\libreOffice_installer.exe"'
    if marker not in text:
        print(f"WARNING: Could not patch {path} (marker pattern not found)")
        return False

    # Check if already patched (idempotent).
    if "pre-downloaded MSI from Samba share" in text:
        print(f"{path} already patched (skipping)")
        return True

    # The PowerShell block to insert: try local MSI, wrap original in else.
    patch_block = """\
    # Try pre-downloaded MSI from Samba share first (baked into Docker image)
    $predownloadedMsi = Get-ChildItem "\\\\host.lan\\Data\\LibreOffice_*_Win_x86-64.msi" -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName
    if ($predownloadedMsi) {
        Write-Host "Installing LibreOffice from pre-downloaded MSI: $predownloadedMsi"
        Start-Process "msiexec.exe" -ArgumentList "/i `\\"$predownloadedMsi`\\" /quiet" -Wait -NoNewWindow
        Write-Host "LibreOffice installed from pre-downloaded MSI."
        Add-ToEnvPath -NewPath "C:\\Program Files\\LibreOffice\\program"
    } else {
"""

    # Find the marker and insert after its line
    marker_idx = text.index(marker)
    line_end = text.index("\n", marker_idx) + 1
    text = text[:line_end] + patch_block + text[line_end:]

    # Now find the ORIGINAL Add-ToEnvPath for LibreOffice (the one from the
    # original setup.ps1, NOT the one we just inserted).  It's the LAST
    # occurrence in the file.  After it, we need to find the next closing
    # brace and add our else-closing brace.
    lo_env = 'Add-ToEnvPath -NewPath "C:\\Program Files\\LibreOffice\\program"'
    last_idx = text.rfind(lo_env)
    # The last_idx should be after our inserted block.  Find the next line
    # after this that contains only whitespace and a closing brace.
    search_start = text.index("\n", last_idx) + 1
    # Look for the next '}' that closes the original LibreOffice install block
    lines = text[search_start:].split("\n")
    offset = search_start
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "}" or stripped.startswith("}"):
            # Insert our closing brace after this one
            brace_end = offset + len(line)
            text = text[:brace_end] + "\n    }" + text[brace_end:]
            break
        offset += len(line) + 1  # +1 for the newline

    with open(path, "w", encoding="utf-8") as f:
        f.write(text)

    print(f"Patched {path} with pre-downloaded MSI fallback")
    return True


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <path-to-setup.ps1>")
        sys.exit(1)
    patch_setup_ps1(sys.argv[1])
