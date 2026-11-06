param(
    [Parameter(Mandatory = $true)][string]$RequestPath,
    [Parameter(Mandatory = $true)][string]$StatusPath
)

$ErrorActionPreference = 'Stop'
$nonce = ''
$forms = @()
try {
    $request = Get-Content -Raw -LiteralPath $RequestPath | ConvertFrom-Json
    if ($request.schema -ne 'openadapt.qualification-pixel-fault/v1' -or
        $request.kind -notin @('ambiguity', 'wrong_identity', 'stale_identity') -or
        $request.nonce -notmatch '^[a-f0-9]{32}$') {
        throw 'invalid request'
    }
    $nonce = [string]$request.nonce
    $x = [int]$request.target.x
    $y = [int]$request.target.y
    $width = [int]$request.target.width
    $height = [int]$request.target.height
    $screenWidth = [int]$request.screen.width
    $screenHeight = [int]$request.screen.height
    if ($screenWidth -le 0 -or $screenHeight -le 0 -or
        $width -le 0 -or $height -le 0 -or
        $x -lt 0 -or $y -lt 0 -or
        ($x + $width) -gt $screenWidth -or
        ($y + $height) -gt $screenHeight -or
        ($width * $height) -gt (($screenWidth * $screenHeight) / 2)) {
        throw 'invalid geometry'
    }

    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    [System.Windows.Forms.Application]::EnableVisualStyles()

    $makeOverlay = {
        param([int]$Left, [int]$Top, [int]$BoxWidth, [int]$BoxHeight, [string]$Label)
        $form = New-Object System.Windows.Forms.Form
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

    if ($request.kind -eq 'ambiguity') {
        $firstWidth = [Math]::Max(1, [int][Math]::Floor($width / 2))
        $secondWidth = $width - $firstWidth
        if ($secondWidth -le 0) { throw 'target too small' }
        $forms += & $makeOverlay $x $y $firstWidth $height 'Choose'
        $forms += & $makeOverlay ($x + $firstWidth) $y $secondWidth $height 'Choose'
    }
    elseif ($request.kind -eq 'wrong_identity') {
        $forms += & $makeOverlay $x $y $width $height 'Other record'
    }
    else {
        $forms += & $makeOverlay $x $y $width $height 'Refresh required'
    }

    [System.Windows.Forms.Application]::DoEvents()
    [System.IO.File]::WriteAllText($StatusPath, "ready:$nonce")
    Start-Sleep -Seconds 8
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
}

exit 0
