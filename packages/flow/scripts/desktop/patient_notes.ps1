# Patient Notes -- Benchmark Harness (WinForms).
#
# A deliberately real WinForms app standing in for OpenDental's chart+note
# workflow (list-select -> edit note -> save). It is the vision-replay TARGET
# and the UIA-arm target: real WinForms controls expose a genuine (and, for
# the DataGridView rows, deliberately partial) UIA tree -- the "WinForms a11y
# is often broken" finding the spike wants measured, not asserted.
#
# All persistence goes through pn_db.py (SQLite), so the benchmark judge reads
# ground truth from the DB, never from OCR. DPI scaling is honored
# (AutoScaleMode=Dpi) so the DPI drift condition actually moves pixels.
#
# Usage:  powershell -STA -ExecutionPolicy Bypass -File patient_notes.ps1

param(
    [string]$Python = "C:\Program Files\Python312-arm64\python.exe",
    [string]$DbCli  = "C:\oa\pn_db.py"
)

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

# --- drift knobs (read from C:\oa\pn_env.json, written by the harness) ------
# font_scale : render-scale proxy for DPI drift (1.0 / 1.25 / 1.5). Real
#   per-monitor DPI change needs a session logoff (see LIMITS.md); scaling the
#   base font reproduces the same class of rendering shift -- larger glyphs,
#   moved targets -- that defeats pixel-template matching, the effect under test.
# theme      : "dark" flips to a dark colour scheme.
# window     : "maximized" (default) or "windowed"; size "WxH" forces windowed.
# A JSON config file is used instead of env vars because the harness launches
# the app in session 1 via CreateProcessAsUser, which does not inherit the
# caller's environment.
$cfg = @{ font_scale = 1.0; theme = ""; window = "maximized"; size = "" }
$cfgPath = "C:\oa\pn_env.json"
if (Test-Path $cfgPath) {
    try {
        $j = Get-Content $cfgPath -Raw | ConvertFrom-Json
        foreach ($k in @("font_scale", "theme", "window", "size")) {
            if ($null -ne $j.$k) { $cfg[$k] = $j.$k }
        }
    } catch {}
}
$fontScale = [double]$cfg["font_scale"]
$baseFont = New-Object System.Drawing.Font("Segoe UI", (9.0 * $fontScale))
$theme = [string]$cfg["theme"]
$bgColor = [System.Drawing.Color]::White
$fgColor = [System.Drawing.Color]::Black
if ($theme -eq "dark") {
    $bgColor = [System.Drawing.Color]::FromArgb(32, 32, 32)
    $fgColor = [System.Drawing.Color]::FromArgb(230, 230, 230)
}

function Invoke-Db {
    param([string[]]$DbArgs)
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $Python
    $psi.Arguments = (@("`"$DbCli`"") + $DbArgs) -join ' '
    $psi.RedirectStandardOutput = $true
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $p = [System.Diagnostics.Process]::Start($psi)
    $out = $p.StandardOutput.ReadToEnd()
    $p.WaitForExit()
    return $out
}

$form = New-Object System.Windows.Forms.Form
$form.Text = "Patient Notes - Benchmark Harness"
$form.Name = "patientNotesForm"
$form.Size = New-Object System.Drawing.Size(760, 560)
$form.StartPosition = "Manual"
$form.Location = New-Object System.Drawing.Point(80, 80)
$form.AutoScaleMode = [System.Windows.Forms.AutoScaleMode]::Dpi
$form.Font = $baseFont
$form.BackColor = $bgColor
$form.ForeColor = $fgColor
# Maximize by default so the app fills the screen: the captured frame is then
# entirely app content (no background window bleed), which keeps identity
# bands and REGION_STABLE postconditions deterministic across record/replay.
# The window-resize drift condition overrides this to a fixed windowed size.
if ([string]$cfg["window"] -eq "windowed") {
    $form.WindowState = "Normal"
} else {
    $form.WindowState = "Maximized"
}
if ([string]$cfg["size"]) {
    $wh = ([string]$cfg["size"]) -split 'x'
    $form.WindowState = "Normal"
    $form.Size = New-Object System.Drawing.Size([int]$wh[0], [int]$wh[1])
}
$form.TopMost = $true

$searchBox = New-Object System.Windows.Forms.TextBox
$searchBox.Name = "searchBox"
$searchBox.AccessibleName = "searchBox"
$searchBox.Location = New-Object System.Drawing.Point(20, 20)
$searchBox.Size = New-Object System.Drawing.Size(480, 28)
$form.Controls.Add($searchBox)

$searchButton = New-Object System.Windows.Forms.Button
$searchButton.Name = "searchButton"
$searchButton.AccessibleName = "searchButton"
$searchButton.Text = "Search"
$searchButton.Location = New-Object System.Drawing.Point(510, 18)
$searchButton.Size = New-Object System.Drawing.Size(100, 30)
$form.Controls.Add($searchButton)

$grid = New-Object System.Windows.Forms.DataGridView
$grid.Name = "patientGrid"
$grid.AccessibleName = "patientGrid"
$grid.Location = New-Object System.Drawing.Point(20, 60)
$grid.Size = New-Object System.Drawing.Size(590, 220)
$grid.ReadOnly = $true
$grid.AllowUserToAddRows = $false
$grid.SelectionMode = "FullRowSelect"
$grid.MultiSelect = $false
$grid.AutoSizeColumnsMode = "Fill"
$grid.RowHeadersVisible = $false
# Tall rows + header so an identity band around a row-click cleanly isolates
# that patient's values (Neil / Sorenson / dob) instead of bleeding into the
# column-header row above it.
$grid.RowTemplate.Height = [int](44 * $fontScale)
$grid.ColumnHeadersHeight = [int](40 * $fontScale)
$grid.AllowUserToResizeRows = $false
$form.Controls.Add($grid)

$noteLabel = New-Object System.Windows.Forms.Label
$noteLabel.Text = "Clinical note:"
$noteLabel.Location = New-Object System.Drawing.Point(20, 295)
$noteLabel.Size = New-Object System.Drawing.Size(200, 22)
$form.Controls.Add($noteLabel)

$noteBox = New-Object System.Windows.Forms.TextBox
$noteBox.Name = "noteBox"
$noteBox.AccessibleName = "noteBox"
$noteBox.Multiline = $true
$noteBox.Location = New-Object System.Drawing.Point(20, 320)
$noteBox.Size = New-Object System.Drawing.Size(590, 120)
$form.Controls.Add($noteBox)

$saveButton = New-Object System.Windows.Forms.Button
$saveButton.Name = "saveButton"
$saveButton.AccessibleName = "saveButton"
$saveButton.Text = "Save Note"
$saveButton.Location = New-Object System.Drawing.Point(510, 450)
$saveButton.Size = New-Object System.Drawing.Size(100, 34)
$form.Controls.Add($saveButton)

$status = New-Object System.Windows.Forms.Label
$status.Name = "statusLabel"
$status.AccessibleName = "statusLabel"
$status.Text = "Ready"
$status.Location = New-Object System.Drawing.Point(20, 458)
$status.Size = New-Object System.Drawing.Size(470, 22)
$form.Controls.Add($status)

function Load-Patients {
    param([string]$Filter)
    $json = Invoke-Db -DbArgs @("list", "`"$Filter`"")
    $rows = @()
    if ($json.Trim().Length -gt 0) { $rows = $json | ConvertFrom-Json }
    $dt = New-Object System.Data.DataTable
    [void]$dt.Columns.Add("id")
    [void]$dt.Columns.Add("first")
    [void]$dt.Columns.Add("last")
    [void]$dt.Columns.Add("dob")
    foreach ($r in $rows) {
        [void]$dt.Rows.Add($r.id, $r.first, $r.last, $r.dob)
    }
    $grid.DataSource = $dt
    if ($grid.Columns["id"]) { $grid.Columns["id"].Visible = $false }
    $status.Text = "Loaded " + $rows.Count + " patients"
}

$searchButton.Add_Click({ Load-Patients -Filter $searchBox.Text })
$searchBox.Add_KeyDown({
    if ($_.KeyCode -eq "Enter") { Load-Patients -Filter $searchBox.Text; $_.SuppressKeyPress = $true }
})

$grid.Add_SelectionChanged({
    if ($grid.SelectedRows.Count -gt 0) {
        $row = $grid.SelectedRows[0]
        $id = $row.Cells["id"].Value
        if ($id) {
            $j = Invoke-Db -DbArgs @("get", "$id")
            $p = $j | ConvertFrom-Json
            if ($p) { $noteBox.Text = $p.note }
        }
    }
})

$saveButton.Add_Click({
    if ($grid.SelectedRows.Count -lt 1) { $status.Text = "No patient selected"; return }
    $id = $grid.SelectedRows[0].Cells["id"].Value
    $bytes = [Text.Encoding]::UTF8.GetBytes($noteBox.Text)
    $b64 = [Convert]::ToBase64String($bytes)
    $res = Invoke-Db -DbArgs @("save", "$id", "$b64")
    $status.Text = "Saved note for patient $id"
})

if ($theme -eq "dark") {
    foreach ($c in @($searchBox, $noteBox)) {
        $c.BackColor = [System.Drawing.Color]::FromArgb(50, 50, 50)
        $c.ForeColor = $fgColor
    }
    $grid.BackgroundColor = [System.Drawing.Color]::FromArgb(45, 45, 45)
    $grid.DefaultCellStyle.BackColor = [System.Drawing.Color]::FromArgb(45, 45, 45)
    $grid.DefaultCellStyle.ForeColor = $fgColor
    $grid.ColumnHeadersDefaultCellStyle.BackColor = [System.Drawing.Color]::FromArgb(60, 60, 60)
    $grid.ColumnHeadersDefaultCellStyle.ForeColor = $fgColor
    $grid.EnableHeadersVisualStyles = $false
}

Load-Patients -Filter ""
[void]$form.ShowDialog()
