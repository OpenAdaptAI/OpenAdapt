# Offline control-flow proof only: no WinForms imports, windows, or input.
param([string]$Fixture, [string]$Python, [string]$DbCli, [string]$FailCli)
$ErrorActionPreference = "Stop"
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Fixture, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
$names = @("Invoke-Db", "ConvertTo-RecordId", "Clear-CurrentRecord", "Get-SelectedRecordId", "Update-CurrentRecord", "Load-Patients")
foreach ($name in $names) {
    $fn = @($ast.FindAll({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst]}, $true) | Where-Object Name -eq $name)
    if ($fn.Count -ne 1) { throw "Missing or duplicated function $name" }
    . ([scriptblock]::Create($fn[0].Extent.Text))
}
$realInvokeDb = (Get-Command Invoke-Db).ScriptBlock
$currentRecordId = [pscustomobject]@{Text=""}
$noteBox = [pscustomobject]@{Text=""}
$patientHeader = [pscustomobject]@{Text=""}
$status = [pscustomobject]@{Text=""}
$grid = [pscustomobject]@{SelectedRows=@(); DataSource=$null; Columns=@{id=[pscustomobject]@{Visible=$false}}}
$grid | Add-Member -MemberType ScriptMethod -Name ClearSelection -Value { $this.SelectedRows = @() }
$script:recordIdentityLoading = $false
$script:mode = "normal"
$script:clearObservedBeforeList = $false
$cases = [Collections.Generic.List[string]]::new()

function Select-Id($Id) {
    $grid.SelectedRows = @([pscustomobject]@{Cells=@{id=[pscustomobject]@{Value=$Id}}})
}
function Assert-Cleared([string]$Case) {
    if ($currentRecordId.Text -cne "" -or $noteBox.Text -cne "" -or $patientHeader.Text -cne "") {
        throw "Stale identity/value in $Case"
    }
    if ($script:recordIdentityLoading) { throw "Load guard stuck in $Case" }
    $cases.Add($Case)
}
function Prime-Record {
    $script:mode = "normal"
    Select-Id "1"
    Update-CurrentRecord
    if ($currentRecordId.Text -cne "1" -or $noteBox.Text -cne "seed-note-1") { throw "Real SQLite id/note was not loaded" }
}
function Invoke-Db {
    param([string[]]$DbArgs, [switch]$RequireSuccess)
    if ($DbArgs[0] -eq "list") {
        if ($currentRecordId.Text -cne "" -or $noteBox.Text -cne "") { throw "List saw stale identity" }
        $script:clearObservedBeforeList = $true
        if ($script:mode -eq "list-error") { throw "Planted list failure" }
        if ($script:mode -eq "invalid-list") { return '[{"id":"01"}]' }
        if ($script:mode -eq "object-list") { return '{"id":1}' }
        if ($script:mode -eq "invalid-json") { return '{invalid' }
    }
    if ($DbArgs[0] -eq "get") {
        if ($script:mode -eq "wrong-row") { return & $realInvokeDb -DbArgs @("get", "2") -RequireSuccess:$RequireSuccess }
        if ($script:mode -eq "missing-row") { return "null" }
        if ($script:mode -eq "ambiguous-row") { return '[{"id":1},{"id":2}]' }
        if ($script:mode -eq "singleton-array-row") { return '[{"id":1}]' }
        if ($script:mode -eq "invalid-loaded-id") { return '{"id":"01"}' }
        if ($script:mode -eq "get-error") { throw "Planted get failure" }
        if ($script:mode -eq "selection-replaced") {
            $result = & $realInvokeDb -DbArgs $DbArgs -RequireSuccess:$RequireSuccess
            Select-Id "2"
            return $result
        }
    }
    return & $realInvokeDb -DbArgs $DbArgs -RequireSuccess:$RequireSuccess
}

# Canonical domain covers SQLite integer boundaries, not only the seed's id1.
foreach ($id in @("-9223372036854775808", "0", "1", "2", "9223372036854775807")) {
    Select-Id $id
    Update-CurrentRecord
    if ($currentRecordId.Text -cne $id) { throw "Wrong canonical id $id" }
    $db = (& $Python $DbCli get $id) | ConvertFrom-Json
    if ((ConvertTo-RecordId $db.id) -cne $currentRecordId.Text) { throw "DB mapping mismatch" }
}
$cases.Add("actual-sqlite-get-domain")

Prime-Record
$grid.ClearSelection()
Update-CurrentRecord
Assert-Cleared "selection-reset"
Prime-Record
$grid.SelectedRows = @($grid.SelectedRows[0], $grid.SelectedRows[0])
Update-CurrentRecord
Assert-Cleared "ambiguous-selection"
foreach ($bad in @("01", "+1", " 1", "1.0", "9223372036854775808", $null, $true, 1.5)) {
    Prime-Record
    Select-Id $bad
    Update-CurrentRecord
    Assert-Cleared ("invalid-selected-id-" + $cases.Count)
}
foreach ($caseMode in @("wrong-row", "missing-row", "ambiguous-row", "singleton-array-row", "invalid-loaded-id", "get-error", "selection-replaced")) {
    Prime-Record
    $script:mode = $caseMode
    Update-CurrentRecord
    Assert-Cleared $caseMode
}
Prime-Record
$script:recordIdentityLoading = $true
Update-CurrentRecord
if ($currentRecordId.Text -cne "") { throw "Identity published during load" }
$script:recordIdentityLoading = $false
$cases.Add("loading-suppresses-publication")

foreach ($caseMode in @("list-error", "invalid-list", "object-list", "invalid-json")) {
    Prime-Record
    $script:mode = $caseMode
    Load-Patients -Filter "Neil"
    Assert-Cleared $caseMode
    if ($grid.SelectedRows.Count -ne 0 -or $null -ne $grid.DataSource) { throw "Failed list retained selection" }
}
Prime-Record
$script:clearObservedBeforeList = $false
Load-Patients -Filter "Neil"
if (-not $script:clearObservedBeforeList -or $currentRecordId.Text -cne "1") { throw "Reload failed" }
$cases.Add("load-clears-before-read-then-rebinds")

# Nonzero actual child-process exits must not become cached identity.
Prime-Record
$originalCli = $DbCli
$DbCli = $FailCli
Update-CurrentRecord
Assert-Cleared "native-process-get-failure"
Load-Patients -Filter "Neil"
Assert-Cleared "native-process-list-failure"
$DbCli = $originalCli

@{scope="offline-generated-functions-fake-controls-real-sqlite"; cases=$cases; count=$cases.Count; native_validation=$false; powershell=$PSVersionTable.PSVersion.ToString()} | ConvertTo-Json -Depth 5
