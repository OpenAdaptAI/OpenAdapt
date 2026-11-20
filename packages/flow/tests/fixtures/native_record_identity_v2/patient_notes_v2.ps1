# Patient Notes -- Native Record Identity v2 (NEW synthetic WinForms fixture).
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
    param([string[]]$DbArgs, [switch]$RequireSuccess)
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $Python
    $psi.Arguments = (@("`"$DbCli`"") + $DbArgs) -join ' '
    $psi.RedirectStandardOutput = $true
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $p = [System.Diagnostics.Process]::Start($psi)
    $out = $p.StandardOutput.ReadToEnd()
    $p.WaitForExit()
    if ($RequireSuccess -and $p.ExitCode -ne 0) { throw "Database command failed" }
    return $out
}

$form = New-Object System.Windows.Forms.Form
$form.Text = "Patient Notes - Native Record Identity v2"
$form.Name = "patientNotesNativeIdentityV2"
$form.Size = New-Object System.Drawing.Size(900, 560)
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

# Legacy display caption only. The NEW v2 pack reads currentRecordId's complete
# native value for identity; this label's Name/preview is not identity authority.
$patientHeader = New-Object System.Windows.Forms.Label
$patientHeader.Name = "patientHeader"
$patientHeader.Text = ""
$patientHeader.Location = New-Object System.Drawing.Point(230, 295)
$patientHeader.Size = New-Object System.Drawing.Size(380, 22)
$form.Controls.Add($patientHeader)

$noteBox = New-Object System.Windows.Forms.TextBox
$noteBox.Name = "noteBox"
$noteBox.AccessibleName = "noteBox"
$noteBox.Multiline = $true
$noteBox.Location = New-Object System.Drawing.Point(20, 320)
$noteBox.Size = New-Object System.Drawing.Size(590, 120)
$form.Controls.Add($noteBox)

# NEW v2 fixture identity: a readonly standard Edit value, not a caption.
$currentRecordId = New-Object System.Windows.Forms.TextBox
$currentRecordId.Name = "currentRecordId"
$currentRecordId.AccessibleName = "currentRecordId"
$currentRecordId.ReadOnly = $true
$currentRecordId.UseSystemPasswordChar = $false
$currentRecordId.PasswordChar = [char]0
$currentRecordId.TabStop = $false
$currentRecordId.Location = New-Object System.Drawing.Point(650, 320)
$currentRecordId.Size = New-Object System.Drawing.Size(210, 28)
$currentRecordId.Text = ""
$form.Controls.Add($currentRecordId)

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

# Only the NEW fixture publishes an independently inspectable record id.
$script:recordIdentityLoading = $false

function ConvertTo-RecordId {
    param($Value)
    # SQLite INTEGER PRIMARY KEY is signed int64. Refuse aliases/coercions.
    if ($Value -isnot [string] -and $Value -isnot [int] -and $Value -isnot [long]) {
        throw "Record id is not an exact integer or canonical string"
    }
    $text = [Convert]::ToString($Value, [Globalization.CultureInfo]::InvariantCulture)
    [long]$parsed = 0
    $ok = [long]::TryParse($text, [Globalization.NumberStyles]::AllowLeadingSign,
        [Globalization.CultureInfo]::InvariantCulture, [ref]$parsed)
    if (-not $ok -or $parsed.ToString([Globalization.CultureInfo]::InvariantCulture) -cne $text) {
        throw "Record id is not canonical signed int64"
    }
    return $text
}

function Clear-CurrentRecord {
    $currentRecordId.Text = ""
    $noteBox.Text = ""
    $patientHeader.Text = ""
}

function Get-SelectedRecordId {
    if ($grid.SelectedRows.Count -ne 1) { throw "No unique selected record" }
    return ConvertTo-RecordId $grid.SelectedRows[0].Cells["id"].Value
}

function Update-CurrentRecord {
    Clear-CurrentRecord
    if ($script:recordIdentityLoading) { return }
    try {
        $selectedId = Get-SelectedRecordId
        $j = Invoke-Db -DbArgs @("get", "$selectedId") -RequireSuccess
        # Root shape is checked before PS5.1/7 pipeline enumeration.
        if (-not $j.TrimStart().StartsWith("{")) { throw "Get requires an object root" }
        $p = $j | ConvertFrom-Json -ErrorAction Stop
        if ($null -eq $p -or $p -is [array]) { throw "No unique loaded record" }
        $loadedId = ConvertTo-RecordId $p.id
        if ($loadedId -cne $selectedId) { throw "Loaded record differs from selection" }
        $noteBox.Text = $p.note
        $patientHeader.Text = "Patient: " + $p.first + " " + $p.last + " (" + $p.dob + ")"
        # A fresh selection read prevents publishing a stale in-flight load.
        if ((Get-SelectedRecordId) -cne $loadedId) { throw "Selection changed during load" }
        $currentRecordId.Text = $loadedId
    } catch {
        Clear-CurrentRecord
        $status.Text = "Record identity unavailable"
    }
}

function Load-Patients {
    param([string]$Filter)
    $script:recordIdentityLoading = $true
    Clear-CurrentRecord
    $loaded = $false
    try {
        $json = Invoke-Db -DbArgs @("list", "`"$Filter`"") -RequireSuccess
        if (-not $json.TrimStart().StartsWith("[")) { throw "List requires an array root" }
        $rows = @($json | ConvertFrom-Json -ErrorAction Stop)
        $dt = New-Object System.Data.DataTable
        [void]$dt.Columns.Add("id")
        [void]$dt.Columns.Add("first")
        [void]$dt.Columns.Add("last")
        [void]$dt.Columns.Add("dob")
        foreach ($r in $rows) {
            $id = ConvertTo-RecordId $r.id
            [void]$dt.Rows.Add($id, $r.first, $r.last, $r.dob)
        }
        $grid.DataSource = $dt
        if ($grid.Columns["id"]) { $grid.Columns["id"].Visible = $false }
        $status.Text = "Loaded " + $rows.Count + " patients"
        $loaded = $true
    } catch {
        $grid.ClearSelection()
        $grid.DataSource = $null
        Clear-CurrentRecord
        $status.Text = "Record list unavailable"
    } finally {
        $script:recordIdentityLoading = $false
    }
    if ($loaded) { Update-CurrentRecord }
}

$searchButton.Add_Click({ Load-Patients -Filter $searchBox.Text })
$searchBox.Add_KeyDown({
    if ($_.KeyCode -eq "Enter") { Load-Patients -Filter $searchBox.Text; $_.SuppressKeyPress = $true }
})

$grid.Add_SelectionChanged({ Update-CurrentRecord })

$saveButton.Add_Click({
    if ($grid.SelectedRows.Count -lt 1) { $status.Text = "No patient selected"; return }
    $id = $grid.SelectedRows[0].Cells["id"].Value
    $bytes = [Text.Encoding]::UTF8.GetBytes($noteBox.Text)
    $b64 = [Convert]::ToBase64String($bytes)
    $res = Invoke-Db -DbArgs @("save", "$id", "$b64")
    $status.Text = "Saved note for patient $id"
})

if ($theme -eq "dark") {
    foreach ($c in @($searchBox, $noteBox, $currentRecordId)) {
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
