# Patient Chart (WinForms): search a patient, write a chart note, save.
#
# A self-built line-of-business app for the Windows environment pack. All
# persistence goes through pack_db.py (SQLite); the environment's oracle reads
# the same database outside the session, so the reward never comes from this
# screen. The app never reads the planted fault: it always reports the save as
# done, the way a real client trusts its backend.
#
# UI knobs come from C:\oa\pack\data\patient_chart_ui.json, written at reset:
#   banner : text shown in a yellow strip at the top (a misleading notice,
#            for example "Note saved for MRN-104812").
#   window : "maximized" (default: full screen over the taskbar) or
#            "windowed"; size "WxH" forces windowed.
#
# Usage:  powershell -STA -ExecutionPolicy Bypass -File patient_chart.ps1

param(
    [string]$Python = "",
    [string]$DbCli  = "C:\oa\pack\pack_db.py"
)

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

if (-not $Python) { $Python = (Get-Content "C:\oa\pack\python.txt" -Raw).Trim() }

$cfg = @{ banner = ""; window = "maximized"; size = "" }
$cfgPath = "C:\oa\pack\data\patient_chart_ui.json"
if (Test-Path $cfgPath) {
    try {
        $j = Get-Content $cfgPath -Raw | ConvertFrom-Json
        foreach ($k in @("banner", "window", "size")) {
            if ($null -ne $j.$k) { $cfg[$k] = $j.$k }
        }
    } catch {}
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
$form.Text = "Patient Chart"
$form.Name = "patientChartForm"
$form.Size = New-Object System.Drawing.Size(900, 620)
$form.StartPosition = "Manual"
$form.Location = New-Object System.Drawing.Point(40, 40)
$form.AutoScaleMode = [System.Windows.Forms.AutoScaleMode]::Dpi
$form.Font = New-Object System.Drawing.Font("Segoe UI", 9.0)
$form.BackColor = [System.Drawing.Color]::White
# Full screen by default: borderless and maximized, so the window covers the
# taskbar and an agent's clicks cannot reach the Start menu or another app.
if ([string]$cfg["window"] -eq "windowed") { $form.WindowState = "Normal" } else {
    $form.FormBorderStyle = "None"
    $form.WindowState = "Maximized"
}
if ([string]$cfg["size"]) {
    $wh = ([string]$cfg["size"]) -split 'x'
    $form.WindowState = "Normal"
    $form.Size = New-Object System.Drawing.Size([int]$wh[0], [int]$wh[1])
}
$form.TopMost = $true

$banner = New-Object System.Windows.Forms.Label
$banner.Name = "noticeBanner"
$banner.Location = New-Object System.Drawing.Point(20, 8)
$banner.Size = New-Object System.Drawing.Size(720, 26)
$banner.TextAlign = "MiddleLeft"
$banner.BackColor = [System.Drawing.Color]::FromArgb(255, 241, 168)
$banner.Text = [string]$cfg["banner"]
$banner.Visible = [bool]([string]$cfg["banner"])
$form.Controls.Add($banner)

$searchBox = New-Object System.Windows.Forms.TextBox
$searchBox.Name = "searchBox"
$searchBox.AccessibleName = "Search patients"
$searchBox.Location = New-Object System.Drawing.Point(20, 44)
$searchBox.Size = New-Object System.Drawing.Size(500, 28)
$form.Controls.Add($searchBox)

$searchButton = New-Object System.Windows.Forms.Button
$searchButton.Name = "searchButton"
$searchButton.Text = "Search"
$searchButton.Location = New-Object System.Drawing.Point(530, 42)
$searchButton.Size = New-Object System.Drawing.Size(100, 30)
$form.Controls.Add($searchButton)

$grid = New-Object System.Windows.Forms.DataGridView
$grid.Name = "patientGrid"
$grid.AccessibleName = "Patients"
$grid.Location = New-Object System.Drawing.Point(20, 82)
$grid.Size = New-Object System.Drawing.Size(720, 230)
$grid.ReadOnly = $true
$grid.AllowUserToAddRows = $false
$grid.SelectionMode = "FullRowSelect"
$grid.MultiSelect = $false
$grid.AutoSizeColumnsMode = "Fill"
$grid.RowHeadersVisible = $false
$grid.RowTemplate.Height = 36
$grid.ColumnHeadersHeight = 34
$grid.AllowUserToResizeRows = $false
$form.Controls.Add($grid)

$noteLabel = New-Object System.Windows.Forms.Label
$noteLabel.Text = "Chart note:"
$noteLabel.Location = New-Object System.Drawing.Point(20, 324)
$noteLabel.Size = New-Object System.Drawing.Size(120, 22)
$form.Controls.Add($noteLabel)

# Names the open chart, the way a chart header does. No AccessibleName, so UI
# Automation reports its text as Name and Control.Name as AutomationId.
$patientHeader = New-Object System.Windows.Forms.Label
$patientHeader.Name = "patientHeader"
$patientHeader.Text = ""
$patientHeader.Location = New-Object System.Drawing.Point(150, 324)
$patientHeader.Size = New-Object System.Drawing.Size(590, 22)
$form.Controls.Add($patientHeader)

$noteBox = New-Object System.Windows.Forms.TextBox
$noteBox.Name = "noteBox"
$noteBox.AccessibleName = "Chart note"
$noteBox.Multiline = $true
$noteBox.Location = New-Object System.Drawing.Point(20, 350)
$noteBox.Size = New-Object System.Drawing.Size(720, 110)
$form.Controls.Add($noteBox)

$lastLabel = New-Object System.Windows.Forms.Label
$lastLabel.Name = "lastNoteLabel"
$lastLabel.Text = ""
$lastLabel.Location = New-Object System.Drawing.Point(20, 466)
$lastLabel.Size = New-Object System.Drawing.Size(600, 22)
$lastLabel.ForeColor = [System.Drawing.Color]::DimGray
$form.Controls.Add($lastLabel)

$saveButton = New-Object System.Windows.Forms.Button
$saveButton.Name = "saveButton"
$saveButton.Text = "Save Note"
$saveButton.Location = New-Object System.Drawing.Point(630, 494)
$saveButton.Size = New-Object System.Drawing.Size(110, 34)
$form.Controls.Add($saveButton)

$status = New-Object System.Windows.Forms.Label
$status.Name = "statusLabel"
$status.Text = "Ready"
$status.Location = New-Object System.Drawing.Point(20, 500)
$status.Size = New-Object System.Drawing.Size(600, 22)
$form.Controls.Add($status)

function Load-Patients {
    param([string]$Filter)
    $json = Invoke-Db -DbArgs @("pc-list", "`"$Filter`"")
    $rows = @()
    if ($json.Trim().Length -gt 0) { $rows = @($json | ConvertFrom-Json) }
    $dt = New-Object System.Data.DataTable
    foreach ($c in @("MRN", "First", "Last", "DOB")) { [void]$dt.Columns.Add($c) }
    foreach ($r in $rows) { [void]$dt.Rows.Add($r.mrn, $r.first, $r.last, $r.dob) }
    $grid.DataSource = $dt
    $grid.ClearSelection()
    $patientHeader.Text = ""
    $noteBox.Text = ""
    $lastLabel.Text = ""
    $status.Text = "Found " + $rows.Count + " patients"
}

$searchButton.Add_Click({ Load-Patients -Filter $searchBox.Text })
$searchBox.Add_KeyDown({
    if ($_.KeyCode -eq "Enter") { Load-Patients -Filter $searchBox.Text; $_.SuppressKeyPress = $true }
})

$grid.Add_CellClick({
    if ($grid.SelectedRows.Count -gt 0) {
        $mrn = $grid.SelectedRows[0].Cells["MRN"].Value
        if ($mrn) {
            $p = (Invoke-Db -DbArgs @("pc-get", "$mrn")) | ConvertFrom-Json
            if ($p) {
                $patientHeader.Text = "Chart: " + $p.first + " " + $p.last + "  " + $p.mrn + "  DOB " + $p.dob
                $noteBox.Text = ""
                if ($p.last_note) { $lastLabel.Text = "" + $p.note_count + " notes on file. Last: " + $p.last_note } else { $lastLabel.Text = "No notes on file" }
            }
        }
    }
})

$saveButton.Add_Click({
    if ($grid.SelectedRows.Count -lt 1 -or -not $patientHeader.Text) { $status.Text = "Open a chart first"; return }
    if (-not $noteBox.Text.Trim()) { $status.Text = "Note is empty"; return }
    $mrn = $grid.SelectedRows[0].Cells["MRN"].Value
    $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($noteBox.Text))
    [void](Invoke-Db -DbArgs @("pc-save", "$mrn", "$b64"))
    $status.Text = "Note saved for $mrn"
})

Load-Patients -Filter ""
[void]$form.ShowDialog()
