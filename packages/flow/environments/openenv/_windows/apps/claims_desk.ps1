# Claims Desk (WPF): find a claim, adjudicate it (status, allowed amount, note), save.
#
# A self-built line-of-business app for the Windows environment pack, the
# WPF counterpart of patient_chart.ps1 (WinForms). Persistence goes through
# pack_db.py (SQLite); the oracle reads the database outside the session. The
# app never reads the planted fault and always reports the save as done.
#
# UI knobs come from C:\oa\pack\data\claims_desk_ui.json, written at reset:
#   banner : text in a yellow strip at the top (a misleading notice).
#   window : "maximized" (default: full screen over the taskbar) or
#            "windowed"; size "WxH" forces windowed.
#
# Usage:  powershell -STA -ExecutionPolicy Bypass -File claims_desk.ps1

param(
    [string]$Python = "",
    [string]$DbCli  = "C:\oa\pack\pack_db.py"
)

Add-Type -AssemblyName PresentationFramework
Add-Type -AssemblyName PresentationCore
Add-Type -AssemblyName WindowsBase

if (-not $Python) { $Python = (Get-Content "C:\oa\pack\python.txt" -Raw).Trim() }

$cfg = @{ banner = ""; window = "maximized"; size = "" }
$cfgPath = "C:\oa\pack\data\claims_desk_ui.json"
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

[xml]$xaml = @"
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Claims Desk" Name="claimsDeskWindow" Width="980" Height="660"
        Left="40" Top="40" Topmost="True" FontFamily="Segoe UI" FontSize="13" Background="White">
  <Grid Margin="20,8,20,12">
    <Grid.RowDefinitions>
      <RowDefinition Height="30"/>
      <RowDefinition Height="40"/>
      <RowDefinition Height="250"/>
      <RowDefinition Height="52"/>
      <RowDefinition Height="36"/>
      <RowDefinition Height="40"/>
      <RowDefinition Height="100"/>
      <RowDefinition Height="44"/>
    </Grid.RowDefinitions>
    <TextBlock Grid.Row="0" Name="noticeBanner" Background="#FFF1A8" Padding="6,4" Visibility="Collapsed"/>
    <StackPanel Grid.Row="1" Orientation="Horizontal" Margin="0,4,0,4">
      <TextBox Name="searchBox" Width="520" Height="28" AutomationProperties.Name="Search claims"/>
      <Button Name="searchButton" Content="Search" Width="100" Height="30" Margin="10,0,0,0"/>
    </StackPanel>
    <DataGrid Grid.Row="2" Name="claimsGrid" AutoGenerateColumns="False" IsReadOnly="True"
              SelectionMode="Single" SelectionUnit="FullRow" HeadersVisibility="Column"
              RowHeight="34" ColumnHeaderHeight="32" CanUserAddRows="False"
              AutomationProperties.Name="Claims">
      <DataGrid.Columns>
        <DataGridTextColumn Header="Claim" Binding="{Binding claim_no}" Width="170"/>
        <DataGridTextColumn Header="Member" Binding="{Binding member}" Width="190"/>
        <DataGridTextColumn Header="Member ID" Binding="{Binding member_id}" Width="130"/>
        <DataGridTextColumn Header="Service date" Binding="{Binding service_date}" Width="130"/>
        <DataGridTextColumn Header="Billed" Binding="{Binding billed}" Width="100"/>
        <DataGridTextColumn Header="Status" Binding="{Binding status}" Width="*"/>
      </DataGrid.Columns>
    </DataGrid>
    <StackPanel Grid.Row="3" VerticalAlignment="Center">
      <TextBlock Name="claimHeader" FontWeight="SemiBold"/>
      <TextBlock Name="historyText" Foreground="DimGray"/>
    </StackPanel>
    <StackPanel Grid.Row="4" Orientation="Horizontal" VerticalAlignment="Center">
      <TextBlock Text="Decision:" Width="90" VerticalAlignment="Center"/>
      <RadioButton Name="statusApproved" Content="Approved" GroupName="decision" Margin="0,0,24,0" VerticalAlignment="Center"/>
      <RadioButton Name="statusDenied" Content="Denied" GroupName="decision" Margin="0,0,24,0" VerticalAlignment="Center"/>
      <RadioButton Name="statusPended" Content="Pended" GroupName="decision" VerticalAlignment="Center"/>
    </StackPanel>
    <StackPanel Grid.Row="5" Orientation="Horizontal" VerticalAlignment="Center">
      <TextBlock Text="Allowed:" Width="90" VerticalAlignment="Center"/>
      <TextBox Name="amountBox" Width="160" Height="28" AutomationProperties.Name="Allowed amount"/>
    </StackPanel>
    <TextBox Grid.Row="6" Name="noteBox" AcceptsReturn="True" TextWrapping="Wrap" AutomationProperties.Name="Adjudication note"/>
    <DockPanel Grid.Row="7" LastChildFill="False" Margin="0,8,0,0">
      <TextBlock Name="statusText" Text="Ready" DockPanel.Dock="Left" VerticalAlignment="Center"/>
      <Button Name="saveButton" Content="Save Decision" Width="130" Height="34" DockPanel.Dock="Right"/>
    </DockPanel>
  </Grid>
</Window>
"@

$window = [Windows.Markup.XamlReader]::Load((New-Object System.Xml.XmlNodeReader $xaml))
$c = @{}
foreach ($n in @("noticeBanner", "searchBox", "searchButton", "claimsGrid", "claimHeader",
                 "statusApproved", "statusDenied", "statusPended", "amountBox", "noteBox",
                 "statusText", "saveButton", "historyText")) { $c[$n] = $window.FindName($n) }

if ([string]$cfg["banner"]) {
    $c.noticeBanner.Text = [string]$cfg["banner"]
    $c.noticeBanner.Visibility = "Visible"
}
# Full screen by default: no chrome and maximized, so the window covers the
# taskbar and an agent's clicks cannot reach the Start menu or another app.
if ([string]$cfg["window"] -ne "windowed" -and -not [string]$cfg["size"]) {
    $window.WindowStyle = "None"
    $window.ResizeMode = "NoResize"
    $window.WindowState = "Maximized"
}
if ([string]$cfg["size"]) {
    $wh = ([string]$cfg["size"]) -split 'x'
    $window.Width = [int]$wh[0]; $window.Height = [int]$wh[1]
}

$script:current = $null

function Clear-Detail {
    $script:current = $null
    $c.claimHeader.Text = ""
    $c.historyText.Text = ""
    foreach ($r in @("statusApproved", "statusDenied", "statusPended")) { $c[$r].IsChecked = $false }
    $c.amountBox.Text = ""
    $c.noteBox.Text = ""
}

function Load-Claims {
    param([string]$Filter)
    $json = Invoke-Db -DbArgs @("cd-list", "`"$Filter`"")
    $rows = @()
    if ($json.Trim().Length -gt 0) { $rows = @($json | ConvertFrom-Json) }
    $c.claimsGrid.ItemsSource = $rows
    $c.claimsGrid.UnselectAll()
    Clear-Detail
    $c.statusText.Text = "Found " + $rows.Count + " claims"
}

$c.searchButton.Add_Click({ Load-Claims -Filter $c.searchBox.Text })
$c.searchBox.Add_KeyDown({
    if ($_.Key -eq "Return") { Load-Claims -Filter $c.searchBox.Text; $_.Handled = $true }
})

$c.claimsGrid.Add_SelectionChanged({
    $item = $c.claimsGrid.SelectedItem
    if ($null -eq $item) { return }
    $p = (Invoke-Db -DbArgs @("cd-get", "$($item.claim_no)")) | ConvertFrom-Json
    if ($p) {
        Clear-Detail
        $script:current = $p.claim_no
        $c.claimHeader.Text = "Claim " + $p.claim_no + "  " + $p.member + "  " + $p.member_id + "  billed " + $p.billed + "  (" + $p.status + ")"
        if ($p.last_decision) {
            $c.historyText.Text = "" + $p.decision_count + " decisions on file. Last: " + $p.last_decision.status + " " + $p.last_decision.allowed_amount + " - " + $p.last_decision.note
        } else { $c.historyText.Text = "No decisions on file" }
    }
})

$c.saveButton.Add_Click({
    if (-not $script:current) { $c.statusText.Text = "Open a claim first"; return }
    $decision = ""
    if ($c.statusApproved.IsChecked) { $decision = "Approved" }
    elseif ($c.statusDenied.IsChecked) { $decision = "Denied" }
    elseif ($c.statusPended.IsChecked) { $decision = "Pended" }
    if (-not $decision) { $c.statusText.Text = "Choose a decision"; return }
    $amount = $c.amountBox.Text.Trim()
    if (-not ($amount -match '^\d+(\.\d{1,2})?$')) { $c.statusText.Text = "Allowed amount must be a number"; return }
    $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($c.noteBox.Text))
    [void](Invoke-Db -DbArgs @("cd-save", "$($script:current)", $decision, $amount, "$b64"))
    $c.statusText.Text = "Decision saved for $($script:current)"
})

Load-Claims -Filter ""
[void]$window.ShowDialog()
