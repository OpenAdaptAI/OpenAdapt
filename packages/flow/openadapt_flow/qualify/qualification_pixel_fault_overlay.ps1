param(
    [Parameter(Mandatory = $true)][string]$RequestPath,
    [Parameter(Mandatory = $true)][string]$StatusPath
)

$ErrorActionPreference = 'Stop'
$nonce = ''
$forms = @()
$bitmaps = @()
try {
    $request = Get-Content -Raw -LiteralPath $RequestPath | ConvertFrom-Json
    $isPairing = $request.schema -ceq 'openadapt.rdp-pairing/v1'
    if ($isPairing) {
        if ($request.operation -cne 'pairing_witness' -or
            $request.nonce -notmatch '^[a-f0-9]{32}$' -or
            $request.connection_digest -notmatch '^[a-f0-9]{64}$' -or
            $request.lifetime_seconds -isnot [int] -or
            $request.lifetime_seconds -lt 1 -or $request.lifetime_seconds -gt 300 -or
            $request.viewport.Count -ne 2 -or $request.region.Count -ne 4 -or
            [int]$request.region[2] -ne 52 -or [int]$request.region[3] -ne 52) {
            throw 'invalid pairing request'
        }
        $screenWidth = [int]$request.viewport[0]
        $screenHeight = [int]$request.viewport[1]
        $x = [int]$request.region[0]
        $y = [int]$request.region[1]
        $width = 52
        $height = 52
    }
    else {
        if ($request.schema -cne 'openadapt.qualification-pixel-fault/v1' -or
            $request.kind -notin @('ambiguity', 'wrong_identity', 'stale_identity') -or
            $request.nonce -notmatch '^[a-f0-9]{32}$' -or
            $request.session_identity_sha256 -notmatch '^[a-f0-9]{64}$') {
            throw 'invalid request'
        }
        $screenWidth = [int]$request.screen.width
        $screenHeight = [int]$request.screen.height
        $x = [int]$request.target.x
        $y = [int]$request.target.y
        $width = [int]$request.target.width
        $height = [int]$request.target.height
    }
    $nonce = [string]$request.nonce
    if ($screenWidth -le 0 -or $screenHeight -le 0 -or
        $width -le 0 -or $height -le 0 -or
        $x -lt 0 -or $y -lt 0 -or
        ($x + $width) -gt $screenWidth -or
        ($y + $height) -gt $screenHeight -or
        ($width * $height) -gt (($screenWidth * $screenHeight) / 2)) {
        throw 'invalid geometry'
    }

    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using Microsoft.Win32;
public static class OAFlowQualificationSession {
    [StructLayout(LayoutKind.Sequential)]
    private struct Luid { public uint LowPart; public int HighPart; }
    [StructLayout(LayoutKind.Sequential)]
    private struct TokenStatistics {
        public Luid TokenId;
        public Luid AuthenticationId;
        public long ExpirationTime;
        public uint TokenType;
        public uint ImpersonationLevel;
        public uint DynamicCharged;
        public uint DynamicAvailable;
        public uint GroupCount;
        public uint PrivilegeCount;
        public Luid ModifiedId;
    }
    [DllImport("kernel32.dll")] private static extern IntPtr GetCurrentProcess();
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool ProcessIdToSessionId(uint processId, out uint sessionId);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr handle);
    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern bool OpenProcessToken(IntPtr process, uint access, out IntPtr token);
    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern bool GetTokenInformation(IntPtr token, int infoClass,
        out TokenStatistics info, int infoLength, out int returnedLength);
    [DllImport("kernel32.dll")]
    private static extern uint WTSGetActiveConsoleSessionId();
    [DllImport("wtsapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool WTSQuerySessionInformationW(IntPtr server, uint sessionId,
        int infoClass, out IntPtr buffer, out uint bytesReturned);
    [DllImport("wtsapi32.dll")] private static extern void WTSFreeMemory(IntPtr buffer);
    [DllImport("shcore.dll")]
    private static extern int SetProcessDpiAwareness(int awareness);
    [DllImport("shcore.dll")]
    private static extern int GetProcessDpiAwareness(IntPtr process, out int awareness);
    public static bool EnablePerMonitorDpi() {
        // PROCESS_PER_MONITOR_DPI_AWARE = 2. A prior manifest setting is
        // accepted only if it already selected the same coordinate space.
        int result = SetProcessDpiAwareness(2);
        int awareness;
        return GetProcessDpiAwareness(new IntPtr(-1), out awareness) == 0 && awareness == 2;
    }
    private static bool IsSessionActive(uint sessionId) {
        if (sessionId == WTSGetActiveConsoleSessionId()) return true;
        IntPtr buffer;
        uint bytes;
        if (!WTSQuerySessionInformationW(IntPtr.Zero, sessionId, 8, out buffer, out bytes))
            return false;
        try { return buffer != IntPtr.Zero && bytes >= 4 && Marshal.ReadInt32(buffer) == 0; }
        finally { WTSFreeMemory(buffer); }
    }
    public static string CurrentDigest() {
        uint sessionId;
        uint processId = (uint)System.Diagnostics.Process.GetCurrentProcess().Id;
        if (!ProcessIdToSessionId(processId, out sessionId) || !IsSessionActive(sessionId))
            return null;
        IntPtr token;
        if (!OpenProcessToken(GetCurrentProcess(), 0x0008, out token)) return null;
        TokenStatistics stats;
        int returned;
        bool ok;
        try { ok = GetTokenInformation(token, 10, out stats,
            Marshal.SizeOf(typeof(TokenStatistics)), out returned); }
        finally { CloseHandle(token); }
        if (!ok) return null;
        string machineGuid;
        using (RegistryKey key = Registry.LocalMachine.OpenSubKey(
            @"SOFTWARE\Microsoft\Cryptography")) {
            machineGuid = key == null ? null : key.GetValue("MachineGuid") as string;
        }
        if (String.IsNullOrEmpty(machineGuid)) return null;
        string auth = stats.AuthenticationId.HighPart.ToString(
            System.Globalization.CultureInfo.InvariantCulture) + ":" +
            stats.AuthenticationId.LowPart.ToString(
                System.Globalization.CultureInfo.InvariantCulture);
        string material = "windows-session-v1\0" + machineGuid + "\0" +
            sessionId.ToString(System.Globalization.CultureInfo.InvariantCulture) + "\0" + auth;
        using (SHA256 sha = SHA256.Create()) {
            byte[] digest = sha.ComputeHash(Encoding.UTF8.GetBytes(material));
            return BitConverter.ToString(digest).Replace("-", "").ToLowerInvariant();
        }
    }
}
'@
    if (-not [OAFlowQualificationSession]::EnablePerMonitorDpi()) {
        throw 'process is not per-monitor DPI aware'
    }
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    [System.Windows.Forms.Application]::EnableVisualStyles()
    if ([System.Windows.Forms.Screen]::AllScreens.Count -ne 1 -or
        [System.Windows.Forms.Screen]::PrimaryScreen.Bounds.X -ne 0 -or
        [System.Windows.Forms.Screen]::PrimaryScreen.Bounds.Y -ne 0 -or
        [System.Windows.Forms.Screen]::PrimaryScreen.Bounds.Width -ne $screenWidth -or
        [System.Windows.Forms.Screen]::PrimaryScreen.Bounds.Height -ne $screenHeight) {
        throw 'display geometry differs from the observed capture'
    }

    Add-Type -ReferencedAssemblies System.Windows.Forms -TypeDefinition @'
using System.Windows.Forms;
public sealed class OAFlowQualificationOverlay : Form {
    protected override bool ShowWithoutActivation { get { return true; } }
    protected override CreateParams CreateParams {
        get {
            CreateParams value = base.CreateParams;
            value.ExStyle |= 0x08000000 | 0x00000080; // NOACTIVATE | TOOLWINDOW
            return value;
        }
    }
}
'@
    $guestDigest = [OAFlowQualificationSession]::CurrentDigest()
    if (-not $guestDigest) { throw 'active session identity is unavailable' }
    if (-not $isPairing -and $guestDigest -cne $request.session_identity_sha256) {
        throw 'interactive task is not in the observed application session'
    }

    $makeOverlay = {
        param([int]$Left, [int]$Top, [int]$BoxWidth, [int]$BoxHeight, [string]$Label)
        $form = New-Object OAFlowQualificationOverlay
        $form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::None
        $form.ShowInTaskbar = $false
        $form.TopMost = $true
        $form.StartPosition = [System.Windows.Forms.FormStartPosition]::Manual
        $form.Location = New-Object System.Drawing.Point($Left, $Top)
        $form.Size = New-Object System.Drawing.Size($BoxWidth, $BoxHeight)
        $form.BackColor = [System.Drawing.Color]::FromArgb(25, 70, 135)
        $form.Opacity = 0.92
        $labelControl = New-Object System.Windows.Forms.Label
        $labelControl.Dock = [System.Windows.Forms.DockStyle]::Fill
        $labelControl.TextAlign = [System.Drawing.ContentAlignment]::MiddleCenter
        $labelControl.ForeColor = [System.Drawing.Color]::White
        $labelControl.BackColor = [System.Drawing.Color]::FromArgb(25, 70, 135)
        $labelControl.Font = New-Object System.Drawing.Font('Segoe UI', 10, [System.Drawing.FontStyle]::Bold)
        $labelControl.Text = $Label
        [void]$form.Controls.Add($labelControl)
        [void]$form.Show()
        [System.Windows.Forms.Application]::DoEvents()
        return $form
    }

    if ($isPairing) {
        $region = $request.region
        if ($x -lt 0 -or $y -lt 0 -or
            ($x + $width) -gt $screenWidth -or ($y + $height) -gt $screenHeight) {
            throw 'pairing witness is outside the observed display'
        }
        $material = 'openadapt.rdp-pairing/v1' + [char]0 + $nonce + [char]0 +
            [string]$request.connection_digest + [char]0 + $guestDigest
        $sha = [System.Security.Cryptography.SHA256]::Create()
        try { $digestBytes = $sha.ComputeHash([System.Text.Encoding]::ASCII.GetBytes($material)) }
        finally { $sha.Dispose() }
        $bits = -join ($digestBytes | ForEach-Object { [Convert]::ToString([int]$_, 2).PadLeft(8, '0') })
        $bitmap = New-Object System.Drawing.Bitmap(52, 52)
        $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
        $graphics.Clear([System.Drawing.Color]::Black)
        $graphics.Dispose()
        for ($i = 0; $i -lt 256; $i++) {
            if ($bits[$i] -eq '1') {
                $cellX = 2 + (($i % 16) * 3)
                $cellY = 2 + ([Math]::Floor($i / 16) * 3)
                for ($py = 0; $py -lt 3; $py++) {
                    for ($px = 0; $px -lt 3; $px++) {
                        $bitmap.SetPixel($cellX + $px, $cellY + $py, [System.Drawing.Color]::White)
                    }
                }
            }
        }
        $form = New-Object OAFlowQualificationOverlay
        $form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::None
        $form.ShowInTaskbar = $false
        $form.TopMost = $true
        $form.StartPosition = [System.Windows.Forms.FormStartPosition]::Manual
        $form.Location = New-Object System.Drawing.Point($x, $y)
        $form.Size = New-Object System.Drawing.Size(52, 52)
        $form.BackColor = [System.Drawing.Color]::Black
        $form.BackgroundImageLayout = [System.Windows.Forms.ImageLayout]::None
        $form.BackgroundImage = $bitmap
        [void]$form.Show()
        $forms += $form
        $bitmaps += $bitmap
        [System.Windows.Forms.Application]::DoEvents()
        [System.IO.File]::WriteAllText($StatusPath, "ready:$nonce:$guestDigest")
        Start-Sleep -Seconds ([int]$request.lifetime_seconds)
    }
    elseif ($request.kind -eq 'ambiguity') {
        $duplicate = $request.duplicate
        if ($null -eq $duplicate) { throw 'duplicate region missing' }
        $pad = [int]$request.search_pad
        $dx = [int]$duplicate.x
        $dy = [int]$duplicate.y
        if ($pad -le 0 -or
            [int]$duplicate.width -ne $width -or
            [int]$duplicate.height -ne $height -or
            $dx -lt [Math]::Max(0, $x - $pad) -or
            $dy -lt [Math]::Max(0, $y - $pad) -or
            ($dx + $width) -gt [Math]::Min($screenWidth, $x + $width + $pad) -or
            ($dy + $height) -gt [Math]::Min($screenHeight, $y + $height + $pad) -or
            ($dx + $width) -gt $screenWidth -or ($dy + $height) -gt $screenHeight) {
            throw 'duplicate region is outside the recorded search area'
        }
        $overlap = ($dx -lt ($x + $width)) -and (($dx + $width) -gt $x) -and
            ($dy -lt ($y + $height)) -and (($dy + $height) -gt $y)
        if ($overlap) { throw 'duplicate region overlaps the original candidate' }
        $copy = New-Object System.Drawing.Bitmap($width, $height)
        $graphics = [System.Drawing.Graphics]::FromImage($copy)
        $graphics.CopyFromScreen($x, $y, 0, 0, $copy.Size)
        $graphics.Dispose()
        $form = New-Object OAFlowQualificationOverlay
        $form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::None
        $form.ShowInTaskbar = $false
        $form.TopMost = $true
        $form.StartPosition = [System.Windows.Forms.FormStartPosition]::Manual
        $form.Location = New-Object System.Drawing.Point([int]$duplicate.x, [int]$duplicate.y)
        $form.Size = New-Object System.Drawing.Size($width, $height)
        $form.BackgroundImage = $copy
        $form.BackgroundImageLayout = [System.Windows.Forms.ImageLayout]::Stretch
        [void]$form.Show()
        [System.Windows.Forms.Application]::DoEvents()
        $forms += $form
    }
    elseif ($request.kind -eq 'wrong_identity') {
        $identity = $request.identifier_region
        if ($null -eq $identity) { throw 'identity region missing' }
        if ([int]$identity.width -le 0 -or [int]$identity.height -le 0 -or
            [int]$identity.x -lt 0 -or [int]$identity.y -lt 0 -or
            ([int]$identity.x + [int]$identity.width) -gt $screenWidth -or
            ([int]$identity.y + [int]$identity.height) -gt $screenHeight) {
            throw 'identity region is outside the captured display'
        }
        $forms += & $makeOverlay ([int]$identity.x) ([int]$identity.y) `
            ([int]$identity.width) ([int]$identity.height) 'Different record'
    }
    else {
        $identity = $request.identifier_region
        if ($null -eq $identity) { throw 'identity region missing' }
        if ([int]$identity.width -le 0 -or [int]$identity.height -le 0 -or
            [int]$identity.x -lt 0 -or [int]$identity.y -lt 0 -or
            ([int]$identity.x + [int]$identity.width) -gt $screenWidth -or
            ([int]$identity.y + [int]$identity.height) -gt $screenHeight) {
            throw 'identity region is outside the captured display'
        }
        $forms += & $makeOverlay ([int]$identity.x) ([int]$identity.y) `
            ([int]$identity.width) ([int]$identity.height) 'Identity unavailable'
    }

    [System.Windows.Forms.Application]::DoEvents()
    if (-not $isPairing) {
        [System.IO.File]::WriteAllText($StatusPath, "ready:$nonce")
        Start-Sleep -Seconds 8
    }
}
catch {
    if ($nonce -match '^[a-f0-9]{32}$') {
        [System.IO.File]::WriteAllText($StatusPath, "refused:$nonce")
    }
    exit 2
}
finally {
    foreach ($form in $forms) {
        if ($null -ne $form) { $form.Close(); $form.Dispose() }
    }
    foreach ($bitmap in $bitmaps) {
        if ($null -ne $bitmap) { $bitmap.Dispose() }
    }
}

exit 0
