# Lab fault: open Notepad as a topmost window over the target app, from the
# signed-in user's own session (a scheduled task from another session cannot
# take the foreground). Used by qualify_lab_native.py.
Add-Type -Namespace OALab -Name Win -MemberDefinition @'
[DllImport("user32.dll")] public static extern bool SetWindowPos(System.IntPtr h, System.IntPtr after, int x, int y, int cx, int cy, uint flags);
'@
Start-Process notepad.exe
# Windows 11 Notepad is a packaged app: the started process is a stub, so
# find the real window by process name.
$hwnd = [System.IntPtr]::Zero
for ($i = 0; $i -lt 75 -and $hwnd -eq [System.IntPtr]::Zero; $i++) {
    Start-Sleep -Milliseconds 200
    $w = Get-Process notepad -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
    if ($w) { $hwnd = $w.MainWindowHandle }
}
# HWND_TOPMOST = -1, SWP_SHOWWINDOW = 0x40
[void][OALab.Win]::SetWindowPos($hwnd, [System.IntPtr](-1), 0, 0, 1024, 720, 0x40)
