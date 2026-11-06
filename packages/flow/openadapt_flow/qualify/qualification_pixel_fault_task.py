"""Create and dispose one uniquely owned Windows interactive task.

This helper is limited to qualification controllers. It never replaces an
existing task and only stops a task when the registered definition and the
exact running instance still match the receipt returned at creation.
"""

from __future__ import annotations

import base64
import hashlib
import json
import ntpath
import re
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_HEX32 = re.compile(r"^[a-f0-9]{32}$")
_HEX64 = re.compile(r"^[a-f0-9]{64}$")
_TASK_PREFIX = "OAFlowQualificationPixelFault"
_SAFE_GUEST_PATH = re.compile(r"^[A-Za-z0-9_ .:\\-]+$")
_HELPER_NAME = "qualification_pixel_fault_overlay.ps1"
_STATUS_TIMEOUT_S = 15.0
_DIAGNOSTIC_PREFIX = "OPENADAPT_TASK_DIAGNOSTIC:"
_PS_PRINCIPAL_SID_FUNCTIONS = r"""
function Get-PrincipalSidObservation {
    param([Parameter(Mandatory=$true)][string]$PrincipalUserId)
    $sid = $null
    if ($PrincipalUserId -match '^S-1-[0-9]+(-[0-9]+)+$') {
        $sid = [System.Security.Principal.SecurityIdentifier]::new($PrincipalUserId)
    } else {
        $account = [System.Security.Principal.NTAccount]::new($PrincipalUserId)
        $sid = $account.Translate([System.Security.Principal.SecurityIdentifier])
    }
    $canonicalSid = $sid.Value
    $rows = @(Get-CimInstance -ClassName Win32_UserAccount `
        -Filter ("SID = '{0}'" -f $canonicalSid) -OperationTimeoutSec 5 -ErrorAction Stop)
    $sidType = $null
    $resolved = $false
    if ($rows.Count -eq 1) {
        $rowSid = [System.Security.Principal.SecurityIdentifier]::new([string]$rows[0].SID)
        $sidType = $rows[0].SIDType
        $resolved = $rowSid.Value -ceq $canonicalSid
    }
    [pscustomobject]@{
        resolved = [bool]$resolved
        count = [int]$rows.Count
        sid_type = $sidType
        canonical_sid = [string]$canonicalSid
    }
}

function Test-ExactInteger {
    param($Value, [Parameter(Mandatory=$true)][long]$Expected)
    if ($null -eq $Value) { return $false }
    $integerTypeNames = @(
        'System.Byte', 'System.SByte', 'System.Int16', 'System.UInt16',
        'System.Int32', 'System.UInt32', 'System.Int64', 'System.UInt64'
    )
    if ($Value.GetType().FullName -notin $integerTypeNames) { return $false }
    return ($Value -eq $Expected)
}

function Resolve-CanonicalUserSid {
    param([Parameter(Mandatory=$true)][string]$PrincipalUserId)
    $observation = Get-PrincipalSidObservation -PrincipalUserId $PrincipalUserId
    if ($null -eq $observation) { return $null }
    $propertyNames = @($observation.PSObject.Properties | ForEach-Object { $_.Name })
    $expectedNames = @('resolved', 'count', 'sid_type', 'canonical_sid')
    if ($propertyNames.Count -ne $expectedNames.Count -or
        @($propertyNames | Where-Object { $_ -cnotin $expectedNames }).Count -ne 0 -or
        @($expectedNames | Where-Object { $_ -cnotin $propertyNames }).Count -ne 0) {
        return $null
    }
    if ($observation.resolved -isnot [bool] -or -not $observation.resolved -or
        -not (Test-ExactInteger $observation.count 1) -or
        -not (Test-ExactInteger $observation.sid_type 1) -or
        $observation.canonical_sid -isnot [string] -or
        $observation.canonical_sid -notmatch '^S-1-[0-9]+(-[0-9]+)+$') {
        return $null
    }
    try {
        $canonicalSid = [System.Security.Principal.SecurityIdentifier]::new(
            $observation.canonical_sid
        )
    } catch {
        return $null
    }
    if ($canonicalSid.Value -cne $observation.canonical_sid) { return $null }
    return $canonicalSid.Value
}

function Test-EmptyPrincipalGroupId {
    param($Value)
    return ($null -eq $Value -or ($Value -is [string] -and $Value.Length -eq 0))
}
"""
_LAUNCH_PHASES = frozenset(
    {
        "preflight",
        "build_action",
        "build_principal",
        "build_settings",
        "register_definition",
        "verify_definition_count",
        "verify_description",
        "verify_action_count",
        "verify_action_executable",
        "verify_action_arguments",
        "verify_principal_user",
        "verify_principal_logon",
        "verify_principal_runlevel",
        "start_instance",
        "observe_instance",
        "readiness_poll",
        "transport_result",
        "unknown",
    }
)
_REGISTRATION_STATES = frozenset(
    {
        "not_attempted",
        "preexisting",
        "unknown",
        "absent",
        "ambiguous",
        "mismatch",
        "registered_unverified",
        "verified_owned",
        "started_owned",
        "owned_handle",
    }
)
_CLEANUP_STATES = frozenset(
    {
        "not_attempted",
        "not_needed",
        "unknown",
        "removed_exact_owned_definition",
        "left_running_instance_unproven",
        "left_ambiguous_instances",
        "left_mismatched_definition",
        "failed_or_unknown",
    }
)


@dataclass(frozen=True)
class TaskLaunchDiagnostic:
    """Allowlisted task-launch failure metadata; contains no command or output."""

    phase: str
    registration: str
    cleanup: str
    exit_status: int


class TaskLaunchError(RuntimeError):
    """Preserve a nonzero task-launch result with its safe diagnostic."""

    def __init__(self, diagnostic: TaskLaunchDiagnostic) -> None:
        message = (
            "owned task name already exists"
            if diagnostic.registration == "preexisting"
            else "create owned qualification task failed "
            f"with status {diagnostic.exit_status}"
        )
        super().__init__(message)
        self.diagnostic = diagnostic


def _valid_guest_directory(value: str) -> bool:
    if not isinstance(value, str) or not _SAFE_GUEST_PATH.fullmatch(value):
        return False
    drive, tail = ntpath.splitdrive(value)
    if len(drive) != 2 or not drive[0].isalpha() or not tail.startswith("\\"):
        return False
    if tail == "\\" or ntpath.normpath(value) != value:
        return False
    return all(part not in {"", ".", ".."} for part in tail[1:].split("\\"))


@dataclass(frozen=True)
class OwnedInteractiveTask:
    """Bounded identity of one task created by this qualification session."""

    task_name: str
    nonce: str
    request_sha256: str
    session_binding_sha256: str
    helper_sha256: str
    action_sha256: str
    principal_sid_sha256: str
    instance_guid: str


@dataclass(frozen=True)
class OwnedPixelTaskHandle:
    """One helper request plus the exact interactive task instance."""

    nonce: str
    helper_path: str
    request_path: str
    status_path: str
    request_sha256: str
    session_binding_sha256: str
    task: OwnedInteractiveTask
    schema: str
    helper_sha256: str
    ready_status: str | None = None


def _ps_quote(value: str) -> str:
    if any(char in value for char in "\r\n\x00"):
        raise ValueError("interactive task value contains a control character")
    return "'" + value.replace("'", "''") + "'"


def _split_command(command_line: str) -> tuple[str, str]:
    if not command_line or any(char in command_line for char in "\r\n\x00"):
        raise ValueError("interactive task command is invalid")
    if command_line.startswith('"'):
        executable, separator, arguments = command_line[1:].partition('" ')
        if not separator or not executable:
            raise ValueError("interactive task executable is invalid")
    else:
        executable, separator, arguments = command_line.partition(" ")
        if not executable:
            raise ValueError("interactive task executable is invalid")
    return executable, arguments if separator else ""


def _task_launch_diagnostic(stdout: str, status: int) -> TaskLaunchDiagnostic:
    """Parse only one fixed, allowlisted diagnostic emitted by the PS guard."""

    if type(status) is not int or status == 0:
        raise ValueError("task launch diagnostic requires a nonzero exit status")
    matches = [
        line[len(_DIAGNOSTIC_PREFIX) :]
        for line in stdout.splitlines()
        if line.startswith(_DIAGNOSTIC_PREFIX)
    ]
    if len(matches) == 1:
        try:
            value = json.loads(matches[0])
        except (TypeError, json.JSONDecodeError):
            value = None
        if (
            isinstance(value, dict)
            and set(value) == {"schema", "phase", "registration", "cleanup"}
            and value.get("schema") == "openadapt.owned-task-diagnostic/v1"
            and isinstance(value.get("phase"), str)
            and value["phase"] in _LAUNCH_PHASES
            and isinstance(value.get("registration"), str)
            and value["registration"] in _REGISTRATION_STATES
            and isinstance(value.get("cleanup"), str)
            and value["cleanup"] in _CLEANUP_STATES
        ):
            return TaskLaunchDiagnostic(
                phase=value["phase"],
                registration=value["registration"],
                cleanup=value["cleanup"],
                exit_status=status,
            )
    return TaskLaunchDiagnostic(
        phase="unknown",
        registration="unknown",
        cleanup="unknown",
        exit_status=status,
    )


def launch_owned_interactive_task(
    lab: Any,
    command_line: str,
    *,
    nonce: str,
    request_sha256: str,
    session_binding_sha256: str,
    helper_sha256: str,
) -> OwnedInteractiveTask:
    """Create one no-clobber interactive task and return its exact instance.

    The command stays inside a PowerShell script sent through the existing lab
    transport. Its executable and argument digests, unique task description,
    request digest, session digest and running instance GUID form the bounded
    creation receipt. No prior task or process is stopped.
    """

    if not _HEX32.fullmatch(nonce):
        raise ValueError("interactive task nonce is invalid")
    if not all(
        _HEX64.fullmatch(value)
        for value in (request_sha256, session_binding_sha256, helper_sha256)
    ):
        raise ValueError("interactive task binding digests are invalid")
    user = getattr(lab, "user", None)
    if not isinstance(user, str) or not user:
        raise ValueError("interactive task has no session owner")
    executable, arguments = _split_command(command_line)
    task_name = f"{_TASK_PREFIX}-{nonce}"
    description = (
        f"openadapt.qualification-owned-task/v1:{nonce}:"
        f"{request_sha256}:{helper_sha256}:{session_binding_sha256}"
    )
    action_sha256 = hashlib.sha256(
        (executable + "\0" + arguments).encode("utf-8")
    ).hexdigest()
    script = f"""
{_PS_PRINCIPAL_SID_FUNCTIONS}
$taskName = {_ps_quote(task_name)}
$description = {_ps_quote(description)}
$expectedExe = {_ps_quote(executable)}
$expectedArgs = {_ps_quote(arguments)}
$expectedUser = {_ps_quote(user)}
$expectedPrincipalSid = $null
$phase = 'preflight'
$registration = 'not_attempted'
$cleanup = 'not_attempted'
$registrationAttempted = $false
try {{
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($null -ne $existing) {{
    $registration = 'preexisting'
    throw 'owned task name already exists'
}}
$phase = 'build_action'
$action = New-ScheduledTaskAction -Execute $expectedExe -Argument $expectedArgs
$phase = 'build_principal'
$principal = New-ScheduledTaskPrincipal -UserId $expectedUser -LogonType Interactive -RunLevel Limited
$phase = 'build_settings'
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
$phase = 'verify_principal_user'
$expectedPrincipalSid = Resolve-CanonicalUserSid -PrincipalUserId $expectedUser
if ($null -eq $expectedPrincipalSid) {{ throw 'expected task principal is not a uniquely resolved user' }}
$phase = 'register_definition'
$registration = 'unknown'
$registrationAttempted = $true
Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Settings $settings -Description $description | Out-Null
$registration = 'registered_unverified'
$phase = 'verify_definition_count'
$createdSet = @(Get-ScheduledTask -TaskName $taskName -ErrorAction Stop)
if ($createdSet.Count -ne 1) {{ throw 'created task name is ambiguous' }}
$created = $createdSet[0]
$phase = 'verify_description'
if ($created.Description -cne $description) {{ throw 'created task description does not match request' }}
$phase = 'verify_action_count'
if (@($created.Actions).Count -ne 1) {{ throw 'created task action count does not match request' }}
$phase = 'verify_action_executable'
if ($created.Actions[0].Execute -cne $expectedExe) {{ throw 'created task executable does not match request' }}
$phase = 'verify_action_arguments'
if ($created.Actions[0].Arguments -cne $expectedArgs) {{ throw 'created task arguments do not match request' }}
$phase = 'verify_principal_user'
if (-not (Test-EmptyPrincipalGroupId $created.Principal.GroupId)) {{ throw 'created task principal is a group' }}
$createdPrincipalSid = Resolve-CanonicalUserSid -PrincipalUserId ([string]$created.Principal.UserId)
if ($null -eq $createdPrincipalSid -or $createdPrincipalSid -cne $expectedPrincipalSid) {{
    throw 'created task principal does not match request'
}}
$phase = 'verify_principal_logon'
if ($created.Principal.LogonType.ToString() -cne 'Interactive') {{ throw 'created task logon type does not match request' }}
$phase = 'verify_principal_runlevel'
if ($created.Principal.RunLevel.ToString() -cne 'Limited') {{ throw 'created task run level does not match request' }}
$registration = 'verified_owned'
$phase = 'start_instance'
Start-ScheduledTask -TaskName $taskName -ErrorAction Stop
$registration = 'started_owned'
$phase = 'observe_instance'
$service = New-Object -ComObject 'Schedule.Service'
$service.Connect()
$folder = $service.GetFolder([string]::Concat([char]92))
$taskPath = [string]::Concat([char]92, $taskName)
$deadline = [DateTime]::UtcNow.AddSeconds(10)
$instance = $null
while ([DateTime]::UtcNow -lt $deadline -and $null -eq $instance) {{
    $matches = @($folder.GetTask($taskPath).GetInstances(0))
    if ($matches.Count -eq 1) {{ $instance = $matches[0] }}
    elseif ($matches.Count -gt 1) {{ throw 'task has multiple running instances' }}
    else {{ Start-Sleep -Milliseconds 100 }}
}}
if ($null -eq $instance -or [string]::IsNullOrEmpty($instance.InstanceGuid)) {{
    throw 'created task has no exact running instance'
}}
$sha = [System.Security.Cryptography.SHA256]::Create()
try {{
    $principalSidBytes = $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($createdPrincipalSid))
    $principalSidSha = [BitConverter]::ToString($principalSidBytes).Replace('-', '').ToLowerInvariant()
}} finally {{ $sha.Dispose() }}
[pscustomobject]@{{
    task_name = $taskName
    description = $description
    instance_guid = [string]$instance.InstanceGuid
    principal_sid_sha256 = $principalSidSha
}} | ConvertTo-Json -Compress
}} catch {{
    $creationError = $_
    try {{
        if ($registration -eq 'preexisting') {{
            $cleanup = 'not_attempted'
        }} else {{
            if ($registrationAttempted) {{ $registration = 'unknown' }}
            $partialSet = @(Get-ScheduledTask -TaskName $taskName -ErrorAction Stop)
            if ($partialSet.Count -eq 0) {{
                if ($registrationAttempted) {{
                    $registration = 'unknown'
                    $cleanup = 'unknown'
                }} else {{
                    $registration = 'not_attempted'
                    $cleanup = 'not_needed'
                }}
            }} elseif ($partialSet.Count -gt 1) {{
                $registration = 'ambiguous'
                $cleanup = 'not_attempted'
            }} else {{
                $partial = $partialSet[0]
                if ($null -ne $expectedPrincipalSid -and
                    $partial.Description -ceq $description -and
                    @($partial.Actions).Count -eq 1 -and
                    $partial.Actions[0].Execute -ceq $expectedExe -and
                    $partial.Actions[0].Arguments -ceq $expectedArgs -and
                    (Test-EmptyPrincipalGroupId $partial.Principal.GroupId) -and
                    (Resolve-CanonicalUserSid -PrincipalUserId ([string]$partial.Principal.UserId)) -ceq $expectedPrincipalSid -and
                    $partial.Principal.LogonType.ToString() -ceq 'Interactive' -and
                    $partial.Principal.RunLevel.ToString() -ceq 'Limited') {{
                    $registration = 'verified_owned'
                    $partialService = New-Object -ComObject 'Schedule.Service'
                    $partialService.Connect()
                    $partialFolder = $partialService.GetFolder([string]::Concat([char]92))
                    $partialPath = [string]::Concat([char]92, $taskName)
                    $partialInstances = @($partialFolder.GetTask($partialPath).GetInstances(0))
                    if ($partialInstances.Count -eq 0) {{
                        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Stop
                        $registration = 'absent'
                        $cleanup = 'removed_exact_owned_definition'
                    }} elseif ($partialInstances.Count -eq 1) {{
                        $cleanup = 'left_running_instance_unproven'
                    }} else {{
                        $cleanup = 'left_ambiguous_instances'
                    }}
                }} else {{
                    $registration = 'mismatch'
                    $cleanup = 'left_mismatched_definition'
                }}
            }}
        }}
    }} catch {{
        if ($registrationAttempted -and $registration -ne 'verified_owned' -and
            $registration -ne 'started_owned') {{
            $registration = 'unknown'
        }}
        $cleanup = 'failed_or_unknown'
    }}
    $diagnostic = @{{
        schema = 'openadapt.owned-task-diagnostic/v1'
        phase = $phase
        registration = $registration
        cleanup = $cleanup
    }}
    Write-Output ('{_DIAGNOSTIC_PREFIX}' + ($diagnostic | ConvertTo-Json -Compress))
    throw $creationError
}}
"""
    result = lab.run_ps(
        script,
        check=False,
        timeout_s=20.0,
        operation="create owned qualification task",
    )
    exit_status = getattr(result, "status", None)
    if type(exit_status) is not int:
        raise RuntimeError("owned task launch status is unavailable")
    if exit_status != 0:
        stdout = getattr(result, "stdout", "")
        raise TaskLaunchError(
            _task_launch_diagnostic(
                stdout if isinstance(stdout, str) else "", exit_status
            )
        )
    output = result.stdout.strip()
    try:
        receipt = json.loads(output)
        instance_guid = str(uuid.UUID(receipt["instance_guid"]))
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("owned task creation receipt is invalid") from exc
    principal_sid_sha256 = receipt.get("principal_sid_sha256")
    if (
        set(receipt) != {"task_name", "description", "instance_guid", "principal_sid_sha256"}
        or not isinstance(principal_sid_sha256, str)
        or not _HEX64.fullmatch(principal_sid_sha256)
        or
        receipt.get("task_name") != task_name
        or receipt.get("description") != description
    ):
        raise RuntimeError("owned task creation receipt binding changed")
    return OwnedInteractiveTask(
        task_name=task_name,
        nonce=nonce,
        request_sha256=request_sha256,
        session_binding_sha256=session_binding_sha256,
        helper_sha256=helper_sha256,
        action_sha256=action_sha256,
        principal_sid_sha256=principal_sid_sha256,
        instance_guid=instance_guid,
    )


def close_owned_interactive_task(lab: Any, task: OwnedInteractiveTask) -> None:
    """Stop only the exact running instance and task definition in ``task``."""

    if not isinstance(task, OwnedInteractiveTask):
        raise TypeError("owned task identity is required")
    if not _HEX64.fullmatch(task.principal_sid_sha256):
        raise ValueError("owned task principal identity is invalid")
    description = (
        f"openadapt.qualification-owned-task/v1:{task.nonce}:"
        f"{task.request_sha256}:{task.helper_sha256}:{task.session_binding_sha256}"
    )
    script = f"""
$taskName = {_ps_quote(task.task_name)}
$description = {_ps_quote(description)}
$expectedInstance = {_ps_quote(task.instance_guid)}
$expectedActionSha = {_ps_quote(task.action_sha256)}
{_PS_PRINCIPAL_SID_FUNCTIONS}
$expectedPrincipalSidSha = {_ps_quote(task.principal_sid_sha256)}
$definitionSet = @(Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)
if ($definitionSet.Count -gt 1) {{ throw 'owned task name is ambiguous' }}
$definition = if ($definitionSet.Count -eq 1) {{ $definitionSet[0] }} else {{ $null }}
if ($null -eq $definition) {{ 'missing'; exit 0 }}
if ($definition.Description -cne $description -or @($definition.Actions).Count -ne 1 -or
    -not (Test-EmptyPrincipalGroupId $definition.Principal.GroupId) -or
    $definition.Principal.LogonType.ToString() -cne 'Interactive' -or
    $definition.Principal.RunLevel.ToString() -cne 'Limited') {{
    throw 'task definition no longer matches owned receipt'
}}
$sha = [System.Security.Cryptography.SHA256]::Create()
try {{
    $actionText = [string]$definition.Actions[0].Execute + [char]0 + [string]$definition.Actions[0].Arguments
    $actionBytes = $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($actionText))
    $actionSha = [BitConverter]::ToString($actionBytes).Replace('-', '').ToLowerInvariant()
    $principalSid = Resolve-CanonicalUserSid -PrincipalUserId ([string]$definition.Principal.UserId)
    if ($null -eq $principalSid) {{ throw 'task principal is not a uniquely resolved user' }}
    $principalSidBytes = $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($principalSid))
    $principalSidSha = [BitConverter]::ToString($principalSidBytes).Replace('-', '').ToLowerInvariant()
}} finally {{ $sha.Dispose() }}
if ($actionSha -cne $expectedActionSha -or $principalSidSha -cne $expectedPrincipalSidSha) {{
    throw 'task command or principal changed before disposal'
}}
$service = New-Object -ComObject 'Schedule.Service'
$service.Connect()
$folder = $service.GetFolder([string]::Concat([char]92))
$taskPath = [string]::Concat([char]92, $taskName)
$running = @($folder.GetTask($taskPath).GetInstances(0))
if ($running.Count -gt 1) {{ throw 'owned task has multiple running instances' }}
if ($running.Count -eq 1) {{
    if ([string]$running[0].InstanceGuid -cne $expectedInstance) {{
        throw 'running task instance changed before disposal'
    }}
    $running[0].Stop(0)
}}
$deadline = [DateTime]::UtcNow.AddSeconds(5)
while ([DateTime]::UtcNow -lt $deadline) {{
    $stillRunning = @($folder.GetTask($taskPath).GetInstances(0))
    if ($stillRunning.Count -eq 0) {{ break }}
    if ($stillRunning.Count -ne 1 -or [string]$stillRunning[0].InstanceGuid -cne $expectedInstance) {{
        throw 'running task identity changed during disposal'
    }}
    Start-Sleep -Milliseconds 100
}}
if ($stillRunning.Count -ne 0) {{ throw 'owned task did not stop within its bound' }}
$againSet = @(Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)
if ($againSet.Count -gt 1) {{ throw 'task name became ambiguous before unregister' }}
$again = if ($againSet.Count -eq 1) {{ $againSet[0] }} else {{ $null }}
if ($null -ne $again) {{
    if ($again.Description -cne $description -or @($again.Actions).Count -ne 1 -or
        -not (Test-EmptyPrincipalGroupId $again.Principal.GroupId) -or
        $again.Principal.LogonType.ToString() -cne 'Interactive' -or
        $again.Principal.RunLevel.ToString() -cne 'Limited') {{
        throw 'task definition changed before disposal'
    }}
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {{
        $actionText = [string]$again.Actions[0].Execute + [char]0 + [string]$again.Actions[0].Arguments
        $actionBytes = $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($actionText))
        $againActionSha = [BitConverter]::ToString($actionBytes).Replace('-', '').ToLowerInvariant()
        $againPrincipalSid = Resolve-CanonicalUserSid -PrincipalUserId ([string]$again.Principal.UserId)
        if ($null -eq $againPrincipalSid) {{ throw 'task principal is not a uniquely resolved user' }}
        $againPrincipalSidBytes = $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($againPrincipalSid))
        $againPrincipalSidSha = [BitConverter]::ToString($againPrincipalSidBytes).Replace('-', '').ToLowerInvariant()
    }} finally {{ $sha.Dispose() }}
    if ($againActionSha -cne $expectedActionSha -or $againPrincipalSidSha -cne $expectedPrincipalSidSha) {{
        throw 'task command or principal changed before unregister'
    }}
    $finalRunning = @($folder.GetTask($taskPath).GetInstances(0))
    if ($finalRunning.Count -ne 0) {{ throw 'task restarted before unregister' }}
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Stop
}}
'closed'
"""
    result = lab.run_ps(
        script,
        timeout_s=20.0,
        operation="dispose owned qualification task",
    ).stdout.strip()
    if result not in {"closed", "missing"}:
        raise RuntimeError("owned task disposal receipt is invalid")


class OwnedPixelTasks:
    """Session owner for nonce-bound guest pixel-fault helper tasks."""

    def __init__(
        self,
        lab: Any,
        *,
        guest_dir: str,
        session_binding_sha256: str,
    ) -> None:
        if not _valid_guest_directory(guest_dir):
            raise ValueError("guest directory is unsafe")
        if not _HEX64.fullmatch(session_binding_sha256):
            raise ValueError("session binding digest is invalid")
        self._lab = lab
        self._guest_dir = guest_dir
        self._session_binding_sha256 = session_binding_sha256
        self._helper_path = Path(__file__).with_name(_HELPER_NAME)
        self._tasks: list[OwnedPixelTaskHandle] = []
        self.last_failure_diagnostic: dict[str, Any] | None = None

    def launch(self, request: dict[str, Any]) -> OwnedPixelTaskHandle:
        """Launch one fixed helper request only when its binding is exact."""

        self.last_failure_diagnostic = None
        if not isinstance(request, dict):
            raise ValueError("pixel helper request must be an object")
        schema = request.get("schema")
        nonce = request.get("nonce")
        if not isinstance(nonce, str) or not _HEX32.fullmatch(nonce):
            raise ValueError("pixel helper request nonce is invalid")
        if schema == "openadapt.qualification-pixel-fault/v1":
            binding = request.get("session_identity_sha256")
        elif (
            schema == "openadapt.rdp-pairing/v1"
            and request.get("operation") == "pairing_witness"
        ):
            binding = request.get("connection_digest")
        else:
            raise ValueError("pixel helper request schema is unsupported")
        if binding != self._session_binding_sha256:
            raise ValueError("pixel helper request session binding changed")
        payload = json.dumps(request, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        if len(payload) > 4096 or not self._helper_path.is_file():
            raise ValueError("pixel helper request or packaged helper is unavailable")
        request_sha256 = hashlib.sha256(payload).hexdigest()
        helper_sha256 = hashlib.sha256(self._helper_path.read_bytes()).hexdigest()
        remote_helper = (
            f"{self._guest_dir}\\qualification_pixel_fault_overlay_{nonce}.ps1"
        )
        remote_request = f"{self._guest_dir}\\qualification_pixel_fault_{nonce}.json"
        remote_status = f"{self._guest_dir}\\qualification_pixel_fault_{nonce}.status"
        for remote_path in (remote_helper, remote_request, remote_status):
            result = self._lab.run_ps(
                f"if (Test-Path -LiteralPath {_ps_quote(remote_path)}) "
                "{ throw 'qualification helper path already exists' }",
                check=False,
                timeout_s=2.0,
                operation="qualification helper path preflight",
            )
            if result.status != 0:
                raise RuntimeError("qualification helper path preflight failed")
        with tempfile.TemporaryDirectory(prefix="oa-qualify-fault-") as temp_dir:
            request_path = Path(temp_dir) / "request.json"
            request_path.write_bytes(payload)
            self._lab.push_file(self._helper_path, remote_helper)
            self._lab.push_file(request_path, remote_request)
        launcher = f"""
$ErrorActionPreference = 'Stop'
$expectedHelper = {_ps_quote(helper_sha256)}
$expectedRequest = {_ps_quote(request_sha256)}
$helperPath = {_ps_quote(remote_helper)}
$requestPath = {_ps_quote(remote_request)}
$statusPath = {_ps_quote(remote_status)}
$actualHelper = (Get-FileHash -LiteralPath $helperPath -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
$actualRequest = (Get-FileHash -LiteralPath $requestPath -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
if ($actualHelper -cne $expectedHelper -or $actualRequest -cne $expectedRequest) {{ exit 2 }}
& $helperPath -RequestPath $requestPath -StatusPath $statusPath
exit $LASTEXITCODE
"""
        encoded = base64.b64encode(launcher.encode("utf-16-le")).decode("ascii")
        command = (
            '"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" '
            "-NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand "
            + encoded
        )
        self.last_failure_diagnostic = None
        try:
            task = launch_owned_interactive_task(
                self._lab,
                command,
                nonce=nonce,
                request_sha256=request_sha256,
                session_binding_sha256=self._session_binding_sha256,
                helper_sha256=helper_sha256,
            )
        except TaskLaunchError as exc:
            self.last_failure_diagnostic = {
                "phase": exc.diagnostic.phase,
                "registration": exc.diagnostic.registration,
                "cleanup": exc.diagnostic.cleanup,
                "exit_status": exc.diagnostic.exit_status,
            }
            raise
        handle = OwnedPixelTaskHandle(
            nonce=nonce,
            helper_path=remote_helper,
            request_path=remote_request,
            status_path=remote_status,
            request_sha256=request_sha256,
            session_binding_sha256=self._session_binding_sha256,
            task=task,
            schema=schema,
            helper_sha256=helper_sha256,
        )
        self._tasks.append(handle)
        return handle

    def wait_ready(
        self,
        handle: OwnedPixelTaskHandle,
        *,
        timeout_s: float = _STATUS_TIMEOUT_S,
    ) -> str | None:
        """Return only this request's bounded readiness record, or decline."""

        if not any(item is handle for item in self._tasks) or not (
            0 < timeout_s <= _STATUS_TIMEOUT_S
        ):
            return None
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            result = self._lab.run_ps(
                f"if (Test-Path -LiteralPath {_ps_quote(handle.status_path)}) "
                f"{{ Get-Content -Raw -LiteralPath {_ps_quote(handle.status_path)} }}",
                check=False,
                timeout_s=2.0,
                operation="qualification pixel task status",
            )
            result_status = getattr(result, "status", None)
            if type(result_status) is not int or result_status != 0:
                self.last_failure_diagnostic = {
                    "phase": "readiness_poll",
                    "registration": "owned_handle",
                    "cleanup": "not_attempted",
                    "exit_status": result_status
                    if type(result_status) is int
                    else None,
                }
                return None
            status = result.stdout.strip()
            if status == f"refused:{handle.nonce}":
                return None
            prefix = f"ready:{handle.nonce}:"
            if status.startswith(prefix):
                suffix = status[len(prefix) :]
                if not _HEX64.fullmatch(suffix):
                    return None
                if (
                    handle.schema == "openadapt.qualification-pixel-fault/v1"
                    and suffix != handle.session_binding_sha256
                ):
                    return None
                object.__setattr__(handle, "ready_status", status)
                return status
            time.sleep(0.2)
        return None

    def close(self) -> None:
        """Dispose each task only through its exact creation receipt."""

        errors: list[Exception] = []
        for handle in reversed(self._tasks):
            try:
                close_owned_interactive_task(self._lab, handle.task)
                self._remove_owned_files(handle)
            except Exception as exc:  # noqa: BLE001 - preserve exact ownership errors
                errors.append(exc)
        if errors:
            raise RuntimeError("owned pixel task disposal failed") from errors[0]
        self._tasks.clear()

    def _remove_owned_files(self, handle: OwnedPixelTaskHandle) -> None:
        """Remove only this request's paths after its exact task was stopped."""

        script = f"""
$helperPath = {_ps_quote(handle.helper_path)}
$requestPath = {_ps_quote(handle.request_path)}
$statusPath = {_ps_quote(handle.status_path)}
$expectedHelper = {_ps_quote(handle.helper_sha256)}
$expectedRequest = {_ps_quote(handle.request_sha256)}
$nonce = {_ps_quote(handle.nonce)}
if (Test-Path -LiteralPath $helperPath) {{
    $item = Get-Item -LiteralPath $helperPath -ErrorAction Stop
    if ($item.PSIsContainer) {{ throw 'qualification helper path changed type' }}
    $actual = (Get-FileHash -LiteralPath $helperPath -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
    if ($actual -cne $expectedHelper) {{ throw 'qualification helper content changed' }}
}}
if (Test-Path -LiteralPath $requestPath) {{
    $item = Get-Item -LiteralPath $requestPath -ErrorAction Stop
    if ($item.PSIsContainer) {{ throw 'qualification request path changed type' }}
    $actual = (Get-FileHash -LiteralPath $requestPath -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
    if ($actual -cne $expectedRequest) {{ throw 'qualification request content changed' }}
}}
if (Test-Path -LiteralPath $statusPath) {{
    $item = Get-Item -LiteralPath $statusPath -ErrorAction Stop
    if ($item.PSIsContainer) {{ throw 'qualification status path changed type' }}
    $status = [System.IO.File]::ReadAllText($statusPath).Trim()
    if ($status -cnotmatch ('^(ready:' + [regex]::Escape($nonce) + ':[a-f0-9]{{64}}|refused:' + [regex]::Escape($nonce) + ')$')) {{
        throw 'qualification status content changed'
    }}
}}
foreach ($path in @($helperPath, $requestPath, $statusPath)) {{
    if (Test-Path -LiteralPath $path) {{ Remove-Item -LiteralPath $path -ErrorAction Stop }}
}}
'removed'
"""
        result = self._lab.run_ps(
            script,
            timeout_s=5.0,
            operation="remove owned qualification helper files",
        ).stdout.strip()
        if result != "removed":
            raise RuntimeError("owned qualification file cleanup receipt is invalid")


__all__ = [
    "OwnedInteractiveTask",
    "OwnedPixelTaskHandle",
    "OwnedPixelTasks",
    "close_owned_interactive_task",
    "launch_owned_interactive_task",
]
