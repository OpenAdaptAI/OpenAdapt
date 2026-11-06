"""Exercise generated PowerShell guards with synthetic scheduler primitives only.

No COM object, task, helper, registry, native API or credential is used. The
command/type AST check is a narrow test boundary, not a general PowerShell sandbox.
"""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
from pathlib import Path
from runpy import run_path
from types import SimpleNamespace

import pytest

from openadapt_flow.qualify import qualification_pixel_fault_task as owner

LabResult = run_path(
    str(Path(__file__).resolve().parents[1] / "scripts/desktop/winrm_lab.py")
)["Result"]

POWERSHELL = Path("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
NONCE = "a" * 32
REQUEST = "b" * 64
SESSION = "c" * 64
HELPER = "d" * 64
GUID = "12345678-1234-1234-1234-123456789012"
FOREIGN = "87654321-4321-4321-4321-210987654321"
COMMAND = '"C:\\Synthetic\\helper.exe" --synthetic'

HARNESS = r"""
$ErrorActionPreference = 'Stop'
Import-Module Microsoft.PowerShell.Utility -ErrorAction Stop
$PSModuleAutoLoadingPreference = 'None'
$payload = [Console]::In.ReadToEnd() | ConvertFrom-Json
$script:state = $payload.state
$script:trace = [System.Collections.Generic.List[string]]::new()
$script:failure = $null
$script:output = ''
$script:outputLines = [System.Collections.Generic.List[string]]::new()
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseInput([string]$payload.script, [ref]$tokens, [ref]$errors)
if (@($errors).Count -ne 0) { throw 'boundary: invalid PowerShell syntax' }
$allowed = @('Get-ScheduledTask','New-ScheduledTaskAction','New-ScheduledTaskPrincipal',
'New-ScheduledTaskSettingsSet','New-TimeSpan','Register-ScheduledTask','Out-Null',
'Start-ScheduledTask','New-Object','Start-Sleep','ConvertTo-Json','Unregister-ScheduledTask',
'Test-Path','Get-Content','Write-Output')
$commands = $ast.FindAll({param($node) $node -is [System.Management.Automation.Language.CommandAst]}, $true)
foreach ($command in $commands) {
    if ($command.InvocationOperator -ne [System.Management.Automation.Language.TokenKind]::Unknown -or
        $null -eq $command.GetCommandName() -or $command.GetCommandName() -cnotin $allowed) {
        throw 'boundary: unrecognized or dynamic command'
    }
}
$types = $ast.FindAll({param($node) $node -is [System.Management.Automation.Language.TypeExpressionAst]}, $true)
foreach ($type in $types) {
    if ($type.TypeName.FullName -cnotin @('string','char','DateTime','pscustomobject',
      'System.Security.Cryptography.SHA256','System.Text.Encoding','BitConverter')) {
        throw 'boundary: unrecognized type expression'
    }
}
$members = $ast.FindAll({param($node) $node -is [System.Management.Automation.Language.InvokeMemberExpressionAst]}, $true)
foreach ($member in $members) {
    if ($member.Member -isnot [System.Management.Automation.Language.StringConstantExpressionAst] -or
        $member.Member.Value -cnotin @('ToString','Connect','GetFolder','Concat','AddSeconds',
          'GetTask','GetInstances','IsNullOrEmpty','Create','ComputeHash','GetBytes','Replace',
          'ToLowerInvariant','Dispose','Stop')) {
        throw 'boundary: unrecognized member invocation'
    }
}
function Record([string]$value) { $script:trace.Add($value) }
function Get-ScheduledTask {
    [CmdletBinding()]param([string]$TaskName)
    Record ('get-definition:' + $TaskName)
    $script:state.definition_reads++
    if ($script:state.mode -eq 'readback_fail' -and $script:state.definition_reads -ge 2) {
        throw 'synthetic definition readback failure'
    }
    if ($null -eq $script:state.name) { $script:state.name = $TaskName }
    if ($TaskName -cne $script:state.name) { throw 'synthetic: wrong task name' }
    if ($script:state.definition) { $script:state.definition }
}
function New-ScheduledTaskAction {
    param([string]$Execute,[string]$Argument)
    if ($script:state.mode -eq 'action_factory_fail') { throw 'synthetic action factory failure' }
    [pscustomobject]@{Execute=$Execute;Arguments=$Argument}
}
function New-ScheduledTaskPrincipal {
    param([string]$UserId,[string]$LogonType,[string]$RunLevel)
    if ($script:state.mode -eq 'principal_factory_fail') { throw 'synthetic principal factory failure' }
    [pscustomobject]@{UserId=$UserId;LogonType=$LogonType;RunLevel=$RunLevel}
}
function New-ScheduledTaskSettingsSet {
    param([switch]$AllowStartIfOnBatteries,[switch]$DontStopIfGoingOnBatteries,$ExecutionTimeLimit)
    if ($script:state.mode -eq 'settings_factory_fail') { throw 'synthetic settings factory failure' }
    [pscustomobject]@{ExecutionTimeLimit=$ExecutionTimeLimit}
}
function Register-ScheduledTask {
    [CmdletBinding()]param([string]$TaskName,$Action,$Principal,$Settings,[string]$Description)
    Record 'register'
    if ($script:state.definition) { throw 'synthetic: register cannot replace' }
    if ($script:state.mode -eq 'register_fail_no_effect') { throw 'synthetic register failure' }
    $script:state.definition = [pscustomobject]@{TaskName=$TaskName;Description=$Description;Actions=@($Action);Principal=$Principal}
    switch ($script:state.mode) {
        'created_description_swap' { $script:state.definition.Description = 'foreign' }
        'created_action_count_swap' { $script:state.definition.Actions += $Action }
        'created_executable_swap' { $script:state.definition.Actions[0].Execute = 'foreign' }
        'created_action_swap' { $script:state.definition.Actions[0].Arguments = 'foreign' }
        'created_user_swap' { $script:state.definition.Principal.UserId = 'foreign' }
        'created_logon_swap' { $script:state.definition.Principal.LogonType = 'ServiceAccount' }
        'created_runlevel_swap' { $script:state.definition.Principal.RunLevel = 'Highest' }
    }
    if ($script:state.mode -eq 'register_fail_after_create') { throw 'synthetic register failure' }
}
function Start-ScheduledTask {
    [CmdletBinding()]param([string]$TaskName)
    Record 'start'
    if ($TaskName -cne $script:state.name) { throw 'synthetic: wrong start name' }
    if ($script:state.mode -eq 'start_fail_empty') { throw 'synthetic start failure' }
    if ($script:state.mode -eq 'start_fail_live') {
        $script:state.instances = @('87654321-4321-4321-4321-210987654321')
        throw 'synthetic start failure'
    }
    $script:state.instances = @('12345678-1234-1234-1234-123456789012')
    if ($script:state.mode -eq 'multiple_created') { $script:state.instances += '87654321-4321-4321-4321-210987654321' }
    if ($script:state.mode -eq 'invalid_guid') { $script:state.instances = @('invalid') }
}
function Unregister-ScheduledTask {
    [CmdletBinding()]param([string]$TaskName,[switch]$Confirm)
    Record 'unregister'
    if ($TaskName -cne $script:state.name) { throw 'synthetic: wrong unregister name' }
    $script:state.definition = $null
}
function Start-Sleep { param([int]$Milliseconds) Record 'sleep' }
function Test-Path {
    param([string]$LiteralPath)
    if ($LiteralPath -cin $script:state.existing) { return $true }
    return ($LiteralPath.EndsWith('.status') -and $null -ne $script:state.status)
}
function Get-Content { param([switch]$Raw,[string]$LiteralPath) $script:state.status }
# This function has no fallback to the native New-Object cmdlet.
function New-Object {
    [CmdletBinding()]param([string]$ComObject)
    if ($ComObject -cne 'Schedule.Service') { throw 'boundary: unrecognized COM request' }
    if ($script:state.mode -eq 'service_fail') { throw 'synthetic service failure' }
    Record 'synthetic-service'
    $service = [pscustomobject]@{}
    $service | Add-Member ScriptMethod Connect { Record 'connect' }
    $service | Add-Member ScriptMethod GetFolder {
        param($path)
        if ($path -cne '\') { throw 'synthetic: wrong folder' }
        Record 'get-root-folder'
        $folder = [pscustomobject]@{}
        $folder | Add-Member ScriptMethod GetTask {
            param($path)
            if ($path -cne ('\' + $script:state.name)) { throw 'synthetic: wrong task path' }
            Record ('get-instance-task:' + $path)
            $task = [pscustomobject]@{}
            $task | Add-Member ScriptMethod GetInstances {
                param($flags)
                if ($flags -ne 0) { throw 'synthetic: wrong instance flags' }
                Record 'get-instances'
                foreach ($guid in $script:state.instances) {
                    $instance = [pscustomobject]@{InstanceGuid=[string]$guid}
                    $instance | Add-Member ScriptMethod Stop {
                        param($flags)
                        if ($flags -ne 0) { throw 'synthetic: wrong stop flags' }
                        Record ('stop:' + $this.InstanceGuid)
                        $script:state.instances = @()
                        if ($script:state.mode -eq 'restart_foreign') {
                            $script:state.instances = @('87654321-4321-4321-4321-210987654321')
                        }
                    }
                    $instance
                }
            }
            $task
        }
        $folder
    }
    $service
}
try {
    & ([scriptblock]::Create([string]$payload.script)) | ForEach-Object {
        $script:outputLines.Add([string]$_)
    }
} catch {
    $script:failure = $_.Exception.Message
} finally {
    $script:output = $script:outputLines -join "`n"
    [pscustomobject]@{state=$script:state;trace=@($script:trace.ToArray());output=$script:output;error=$script:failure} | ConvertTo-Json -Depth 12 -Compress
}
"""


class SyntheticLab:
    user = "synthetic-user"

    def __init__(self, mode: str = "normal") -> None:
        self.state = {
            "name": None,
            "definition": None,
            "instances": [],
            "definition_reads": 0,
            "mode": mode,
            "status": None,
            "existing": [],
        }
        self.records: list[dict] = []
        self.scripts: list[str] = []
        self.pushed: dict[str, bytes] = {}

    def push_file(self, path: Path, destination: str) -> None:
        self.pushed[destination] = Path(path).read_bytes()

    def run_ps(self, script: str, **kwargs):
        self.scripts.append(script)
        if not POWERSHELL.is_file():
            pytest.skip("existing Windows PowerShell executable unavailable")
        process = subprocess.run(
            [
                str(POWERSHELL),
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                base64.b64encode(HARNESS.encode("utf-16le")).decode(),
            ],
            input=json.dumps({"script": script, "state": self.state}).encode(),
            capture_output=True,
            timeout=30,
        )
        assert process.returncode == 0, process.stderr.decode(errors="replace")
        lines = process.stdout.decode("utf-8-sig").splitlines()
        result = json.loads(lines[-1])
        self.records.append(result)
        self.state = result["state"]
        if result["error"] and kwargs.get("check", True):
            raise RuntimeError(result["error"])
        response = LabResult(
            status=1 if result["error"] else 0,
            stdout=result["output"],
            stderr=result["error"] or "",
        )
        assert not hasattr(response, "returncode")
        return response


def launch(lab: SyntheticLab):
    return owner.launch_owned_interactive_task(
        lab,
        COMMAND,
        nonce=NONCE,
        request_sha256=REQUEST,
        session_binding_sha256=SESSION,
        helper_sha256=HELPER,
    )


def test_task_diagnostic_parser_accepts_only_the_fixed_schema():
    prefix = owner._DIAGNOSTIC_PREFIX
    valid = prefix + json.dumps(
        {
            "schema": "openadapt.owned-task-diagnostic/v1",
            "phase": "start_instance",
            "registration": "verified_owned",
            "cleanup": "left_running_instance_unproven",
        }
    )
    assert owner._task_launch_diagnostic(valid, 1) == owner.TaskLaunchDiagnostic(
        phase="start_instance",
        registration="verified_owned",
        cleanup="left_running_instance_unproven",
        exit_status=1,
    )

    for untrusted in (
        prefix
        + json.dumps(
            {
                "schema": "openadapt.owned-task-diagnostic/v1",
                "phase": "start_instance",
                "registration": "verified_owned",
                "cleanup": "left_running_instance_unproven",
                "stderr": "do not retain",
            }
        ),
        prefix
        + json.dumps(
            {
                "schema": "openadapt.owned-task-diagnostic/v1",
                "phase": [],
                "registration": "verified_owned",
                "cleanup": "left_running_instance_unproven",
            }
        ),
        prefix
        + json.dumps(
            {
                "schema": "openadapt.owned-task-diagnostic/v1",
                "phase": "start_instance",
                "registration": {},
                "cleanup": "left_running_instance_unproven",
            }
        ),
        prefix
        + json.dumps(
            {
                "schema": "openadapt.owned-task-diagnostic/v1",
                "phase": "start_instance",
                "registration": "verified_owned",
                "cleanup": [],
            }
        ),
        prefix
        + json.dumps(
            {
                "schema": "openadapt.owned-task-diagnostic/v1",
                "phase": "untrusted command content",
                "registration": "verified_owned",
                "cleanup": "left_running_instance_unproven",
            }
        ),
        valid + "\n" + valid,
    ):
        assert owner._task_launch_diagnostic(
            untrusted, 1
        ) == owner.TaskLaunchDiagnostic(
            phase="unknown",
            registration="unknown",
            cleanup="unknown",
            exit_status=1,
        )


def test_hosted_summary_accepts_only_bounded_task_diagnostic_fields():
    from tests.e2e.test_windows_hosted_campaign import _safe_task_diagnostic

    valid = {
        "phase": "observe_instance",
        "registration": "started_owned",
        "cleanup": "left_running_instance_unproven",
        "exit_status": 1,
    }
    assert _safe_task_diagnostic(valid) == valid
    assert _safe_task_diagnostic({**valid, "stderr": "never retain"}) is None
    assert _safe_task_diagnostic({**valid, "phase": ["untrusted"]}) is None
    assert _safe_task_diagnostic({**valid, "exit_status": True}) is None


def destructive_calls(lab: SyntheticLab) -> list[str]:
    return [
        x
        for row in lab.records
        for x in row["trace"]
        if x == "unregister" or x.startswith("stop:")
    ]


def test_actual_create_then_close_preserves_exact_binding():
    lab = SyntheticLab()
    receipt = launch(lab)
    assert receipt.instance_guid == GUID
    assert receipt.request_sha256 == REQUEST
    assert receipt.helper_sha256 == HELPER
    assert receipt.session_binding_sha256 == SESSION
    definition = lab.state["definition"]
    assert (
        definition["Description"]
        == f"openadapt.qualification-owned-task/v1:{NONCE}:{REQUEST}:{HELPER}:{SESSION}"
    )
    assert definition["Principal"] == {
        "UserId": lab.user,
        "LogonType": "Interactive",
        "RunLevel": "Limited",
    }
    assert definition["Actions"] == [
        {"Execute": r"C:\Synthetic\helper.exe", "Arguments": "--synthetic"}
    ]
    owner.close_owned_interactive_task(lab, receipt)
    assert destructive_calls(lab) == [f"stop:{GUID}", "unregister"]
    assert lab.state["definition"] is None
    assert lab.state["instances"] == []


def test_preexisting_name_is_not_replaced():
    lab = SyntheticLab()
    lab.state["definition"] = {"foreign": True}
    with pytest.raises(RuntimeError, match="already exists"):
        launch(lab)
    assert lab.records[-1]["trace"] == [
        f"get-definition:OAFlowQualificationPixelFault-{NONCE}"
    ]
    assert lab.state["definition"] == {"foreign": True}


@pytest.mark.parametrize(
    "mode,expected_unregister",
    [
        ("start_fail_empty", True),
        ("start_fail_live", False),
        ("created_description_swap", False),
        ("created_action_count_swap", False),
        ("created_executable_swap", False),
        ("created_action_swap", False),
        ("created_user_swap", False),
        ("created_logon_swap", False),
        ("created_runlevel_swap", False),
        ("multiple_created", False),
    ],
)
def test_failed_create_cleanup_requires_exact_empty_definition(
    mode, expected_unregister
):
    lab = SyntheticLab(mode)
    with pytest.raises(RuntimeError):
        launch(lab)
    assert destructive_calls(lab) == (["unregister"] if expected_unregister else [])
    if not expected_unregister:
        assert lab.state["definition"] is not None


@pytest.mark.parametrize(
    "mode,expected_phase,expected_registration,expected_cleanup,expected_calls",
    [
        (
            "register_fail_after_create",
            "register_definition",
            "absent",
            "removed_exact_owned_definition",
            ["unregister"],
        ),
        (
            "register_fail_no_effect",
            "register_definition",
            "unknown",
            "unknown",
            [],
        ),
        (
            "start_fail_empty",
            "start_instance",
            "absent",
            "removed_exact_owned_definition",
            ["unregister"],
        ),
        (
            "start_fail_live",
            "start_instance",
            "verified_owned",
            "left_running_instance_unproven",
            [],
        ),
        (
            "multiple_created",
            "observe_instance",
            "verified_owned",
            "left_ambiguous_instances",
            [],
        ),
        (
            "service_fail",
            "observe_instance",
            "verified_owned",
            "failed_or_unknown",
            [],
        ),
        (
            "readback_fail",
            "verify_definition_count",
            "unknown",
            "failed_or_unknown",
            [],
        ),
        (
            "action_factory_fail",
            "build_action",
            "not_attempted",
            "not_needed",
            [],
        ),
        (
            "principal_factory_fail",
            "build_principal",
            "not_attempted",
            "not_needed",
            [],
        ),
        (
            "settings_factory_fail",
            "build_settings",
            "not_attempted",
            "not_needed",
            [],
        ),
    ],
)
def test_failed_creation_emits_only_fixed_phase_and_cleanup_metadata(
    mode, expected_phase, expected_registration, expected_cleanup, expected_calls
):
    lab = SyntheticLab(mode)
    with pytest.raises(owner.TaskLaunchError) as failure:
        launch(lab)
    diagnostic = failure.value.diagnostic
    assert diagnostic == owner.TaskLaunchDiagnostic(
        phase=expected_phase,
        registration=expected_registration,
        cleanup=expected_cleanup,
        exit_status=1,
    )
    assert "synthetic" not in str(failure.value).lower()
    assert destructive_calls(lab) == expected_calls
    assert all(
        "command" not in row and "stdout" not in row and "stderr" not in row
        for row in lab.records
    )


@pytest.mark.parametrize(
    "mode,expected_phase",
    [
        ("created_description_swap", "verify_description"),
        ("created_action_count_swap", "verify_action_count"),
        ("created_executable_swap", "verify_action_executable"),
        ("created_action_swap", "verify_action_arguments"),
        ("created_user_swap", "verify_principal_user"),
        ("created_logon_swap", "verify_principal_logon"),
        ("created_runlevel_swap", "verify_principal_runlevel"),
    ],
)
def test_definition_mismatch_phases_are_bounded_and_never_cleanup_foreign_task(
    mode, expected_phase
):
    lab = SyntheticLab(mode)
    with pytest.raises(owner.TaskLaunchError) as failure:
        launch(lab)
    assert failure.value.diagnostic == owner.TaskLaunchDiagnostic(
        phase=expected_phase,
        registration="mismatch",
        cleanup="left_mismatched_definition",
        exit_status=1,
    )
    assert destructive_calls(lab) == []


def test_preexisting_task_diagnostic_does_not_cleanup_foreign_definition():
    lab = SyntheticLab()
    lab.state["definition"] = {"foreign": True}
    with pytest.raises(owner.TaskLaunchError) as failure:
        launch(lab)
    assert failure.value.diagnostic == owner.TaskLaunchDiagnostic(
        phase="preflight",
        registration="preexisting",
        cleanup="not_attempted",
        exit_status=1,
    )
    assert destructive_calls(lab) == []
    assert lab.state["definition"] == {"foreign": True}


@pytest.mark.parametrize(
    "swap",
    ["description", "action", "principal", "foreign_instance", "multiple_instances"],
)
def test_changed_proof_refuses_without_stop_or_unregister(swap):
    lab = SyntheticLab()
    receipt = launch(lab)
    lab.records.clear()
    if swap == "description":
        lab.state["definition"]["Description"] = "foreign"
    elif swap == "action":
        lab.state["definition"]["Actions"][0]["Arguments"] = "foreign"
    elif swap == "principal":
        lab.state["definition"]["Principal"]["UserId"] = "foreign"
    elif swap == "foreign_instance":
        lab.state["instances"] = [FOREIGN]
    else:
        lab.state["instances"] = [GUID, FOREIGN]
    with pytest.raises(RuntimeError):
        owner.close_owned_interactive_task(lab, receipt)
    assert destructive_calls(lab) == []
    assert lab.state["definition"] is not None


def test_restart_after_owned_stop_does_not_unregister_foreign_instance():
    lab = SyntheticLab()
    receipt = launch(lab)
    lab.records.clear()
    lab.state["mode"] = "restart_foreign"
    with pytest.raises(RuntimeError, match="identity changed"):
        owner.close_owned_interactive_task(lab, receipt)
    assert destructive_calls(lab) == [f"stop:{GUID}"]
    assert lab.state["instances"] == [FOREIGN]
    assert lab.state["definition"] is not None


def test_malformed_native_instance_proof_refused():
    with pytest.raises(RuntimeError, match="receipt is invalid"):
        launch(SyntheticLab("invalid_guid"))


def test_malformed_creation_receipt_refused():
    class MalformedLab(SyntheticLab):
        def run_ps(self, script, **kwargs):
            super().run_ps(script, **kwargs)
            return LabResult(status=0, stdout="{", stderr="")

    with pytest.raises(RuntimeError, match="receipt is invalid"):
        launch(MalformedLab())


@pytest.mark.parametrize(
    "status",
    ["matching", "wrong_session", "wrong_nonce", "malformed", "refused", "missing"],
)
def test_owned_handle_readiness_requires_available_nonce_and_session(status):
    lab = SyntheticLab()
    tasks = owner.OwnedPixelTasks(
        lab, guest_dir=r"C:\Synthetic", session_binding_sha256=SESSION
    )
    request = {
        "schema": "openadapt.qualification-pixel-fault/v1",
        "nonce": NONCE,
        "session_identity_sha256": SESSION,
    }
    handle = tasks.launch(request)
    assert len(lab.pushed) == 2
    payload = next(data for name, data in lab.pushed.items() if name.endswith(".json"))
    helper = next(data for name, data in lab.pushed.items() if name.endswith(".ps1"))
    assert handle.request_sha256 == hashlib.sha256(payload).hexdigest()
    assert handle.helper_sha256 == hashlib.sha256(helper).hexdigest()
    lab.state["status"] = {
        "matching": f"ready:{NONCE}:{SESSION}",
        "wrong_session": f"ready:{NONCE}:{HELPER}",
        "wrong_nonce": f"ready:{'f' * 32}:{SESSION}",
        "malformed": f"ready:{NONCE}:short",
        "refused": f"refused:{NONCE}",
        "missing": None,
    }[status]
    result = tasks.wait_ready(handle, timeout_s=0.01)
    assert result == (f"ready:{NONCE}:{SESSION}" if status == "matching" else None)
    assert handle.ready_status == result


@pytest.mark.parametrize("schema", ["fault", "pairing"])
@pytest.mark.parametrize(
    "reported_status,expected_status",
    [(7, 7), (True, None), (False, None), (None, None), ("missing", None)],
)
def test_matching_ready_stdout_with_invalid_status_is_refused(
    schema, reported_status, expected_status
):
    class NonzeroReadinessLab(SyntheticLab):
        def run_ps(self, script, **kwargs):
            result = super().run_ps(script, **kwargs)
            if kwargs.get("operation") == "qualification pixel task status":
                if reported_status == "missing":
                    return SimpleNamespace(stdout=result.stdout)
                return LabResult(
                    status=reported_status,
                    stdout=result.stdout,
                    stderr="suppressed",
                )
            return result

    lab = NonzeroReadinessLab()
    tasks = owner.OwnedPixelTasks(
        lab, guest_dir=r"C:\Synthetic", session_binding_sha256=SESSION
    )
    if schema == "fault":
        request = {
            "schema": "openadapt.qualification-pixel-fault/v1",
            "nonce": NONCE,
            "session_identity_sha256": SESSION,
        }
        ready = f"ready:{NONCE}:{SESSION}"
    else:
        request = {
            "schema": "openadapt.rdp-pairing/v1",
            "operation": "pairing_witness",
            "nonce": NONCE,
            "connection_digest": SESSION,
        }
        ready = f"ready:{NONCE}:{HELPER}"
    handle = tasks.launch(request)
    lab.state["status"] = ready

    assert tasks.wait_ready(handle, timeout_s=0.01) is None
    assert handle.ready_status is None
    assert tasks.last_failure_diagnostic == {
        "phase": "readiness_poll",
        "registration": "owned_handle",
        "cleanup": "not_attempted",
        "exit_status": expected_status,
    }


@pytest.mark.parametrize(
    "script",
    [
        "Get-Process",
        '& "Get-ScheduledTask"',
        "if ($true) { Get-Process }",
        "New-Object -ComObject 'Unrecognized.Service'",
        "Register-ScheduledTask -TaskName 'synthetic' -Force",
    ],
)
def test_boundary_refuses_unrecognized_or_dynamic_invocation(script):
    lab = SyntheticLab()
    with pytest.raises((RuntimeError, AssertionError)):
        lab.run_ps(script)
    assert not destructive_calls(lab)


@pytest.mark.parametrize("kind", ["helper", "request", "status"])
def test_preexisting_request_path_refused_before_push_or_task(kind):
    lab = SyntheticLab()
    names = {
        "helper": f"qualification_pixel_fault_overlay_{NONCE}.ps1",
        "request": f"qualification_pixel_fault_{NONCE}.json",
        "status": f"qualification_pixel_fault_{NONCE}.status",
    }
    lab.state["existing"] = [r"C:\Synthetic" + "\\" + names[kind]]
    tasks = owner.OwnedPixelTasks(
        lab, guest_dir=r"C:\Synthetic", session_binding_sha256=SESSION
    )
    with pytest.raises(
        RuntimeError, match="qualification helper path preflight failed"
    ):
        tasks.launch(
            {
                "schema": "openadapt.qualification-pixel-fault/v1",
                "nonce": NONCE,
                "session_identity_sha256": SESSION,
            }
        )
    assert lab.pushed == {}
    assert all(not row["trace"] for row in lab.records)


def test_actual_lab_status_nonzero_refuses_before_push_or_task():
    class FailedPreflightLab(SyntheticLab):
        def run_ps(self, script, **kwargs):
            result = super().run_ps(script, **kwargs)
            if kwargs.get("operation") == "qualification helper path preflight":
                return LabResult(
                    status=7, stdout=result.stdout, stderr="synthetic failure"
                )
            return result

    lab = FailedPreflightLab()
    tasks = owner.OwnedPixelTasks(
        lab, guest_dir=r"C:\Synthetic", session_binding_sha256=SESSION
    )
    with pytest.raises(RuntimeError, match="preflight failed"):
        tasks.launch(
            {
                "schema": "openadapt.qualification-pixel-fault/v1",
                "nonce": NONCE,
                "session_identity_sha256": SESSION,
            }
        )
    assert lab.pushed == {}
    assert all(not row["trace"] for row in lab.records)
