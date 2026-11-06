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
    user_sha256: str
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
    user_sha256 = hashlib.sha256(user.encode("utf-8")).hexdigest()
    script = f"""
$taskName = {_ps_quote(task_name)}
$description = {_ps_quote(description)}
$expectedExe = {_ps_quote(executable)}
$expectedArgs = {_ps_quote(arguments)}
$expectedUser = {_ps_quote(user)}
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($null -ne $existing) {{ throw 'owned task name already exists' }}
try {{
$action = New-ScheduledTaskAction -Execute $expectedExe -Argument $expectedArgs
$principal = New-ScheduledTaskPrincipal -UserId $expectedUser -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Settings $settings -Description $description | Out-Null
$createdSet = @(Get-ScheduledTask -TaskName $taskName -ErrorAction Stop)
if ($createdSet.Count -ne 1) {{ throw 'created task name is ambiguous' }}
$created = $createdSet[0]
if ($created.Description -cne $description -or @($created.Actions).Count -ne 1 -or
    $created.Actions[0].Execute -cne $expectedExe -or
    $created.Actions[0].Arguments -cne $expectedArgs -or
    $created.Principal.UserId -cne $expectedUser -or
    $created.Principal.LogonType.ToString() -cne 'Interactive' -or
    $created.Principal.RunLevel.ToString() -cne 'Limited') {{
    throw 'created task identity does not match request'
}}
Start-ScheduledTask -TaskName $taskName -ErrorAction Stop
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
[pscustomobject]@{{
    task_name = $taskName
    description = $description
    instance_guid = [string]$instance.InstanceGuid
}} | ConvertTo-Json -Compress
}} catch {{
    $creationError = $_
    try {{
        $partialSet = @(Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)
        if ($partialSet.Count -eq 1) {{
            $partial = $partialSet[0]
        }} else {{
            $partial = $null
        }}
        if ($null -ne $partial -and $partial.Description -ceq $description -and
            @($partial.Actions).Count -eq 1 -and
            $partial.Actions[0].Execute -ceq $expectedExe -and
            $partial.Actions[0].Arguments -ceq $expectedArgs -and
            $partial.Principal.UserId -ceq $expectedUser -and
            $partial.Principal.LogonType.ToString() -ceq 'Interactive' -and
            $partial.Principal.RunLevel.ToString() -ceq 'Limited') {{
            $partialService = New-Object -ComObject 'Schedule.Service'
            $partialService.Connect()
            $partialFolder = $partialService.GetFolder([string]::Concat([char]92))
            $partialPath = [string]::Concat([char]92, $taskName)
            $partialInstances = @($partialFolder.GetTask($partialPath).GetInstances(0))
            if ($partialInstances.Count -eq 0) {{
                Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Stop
            }}
        }}
    }} catch {{
        throw 'owned task setup failed; exact partial cleanup could not be proven'
    }}
    throw $creationError
}}
"""
    output = lab.run_ps(
        script,
        timeout_s=20.0,
        operation="create owned qualification task",
    ).stdout.strip()
    try:
        receipt = json.loads(output)
        instance_guid = str(uuid.UUID(receipt["instance_guid"]))
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("owned task creation receipt is invalid") from exc
    if (
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
        user_sha256=user_sha256,
        instance_guid=instance_guid,
    )


def close_owned_interactive_task(lab: Any, task: OwnedInteractiveTask) -> None:
    """Stop only the exact running instance and task definition in ``task``."""

    if not isinstance(task, OwnedInteractiveTask):
        raise TypeError("owned task identity is required")
    description = (
        f"openadapt.qualification-owned-task/v1:{task.nonce}:"
        f"{task.request_sha256}:{task.helper_sha256}:{task.session_binding_sha256}"
    )
    script = f"""
$taskName = {_ps_quote(task.task_name)}
$description = {_ps_quote(description)}
$expectedInstance = {_ps_quote(task.instance_guid)}
$expectedActionSha = {_ps_quote(task.action_sha256)}
$expectedUserSha = {_ps_quote(task.user_sha256)}
$definitionSet = @(Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)
if ($definitionSet.Count -gt 1) {{ throw 'owned task name is ambiguous' }}
$definition = if ($definitionSet.Count -eq 1) {{ $definitionSet[0] }} else {{ $null }}
if ($null -eq $definition) {{ 'missing'; exit 0 }}
if ($definition.Description -cne $description -or @($definition.Actions).Count -ne 1 -or
    $definition.Principal.LogonType.ToString() -cne 'Interactive' -or
    $definition.Principal.RunLevel.ToString() -cne 'Limited') {{
    throw 'task definition no longer matches owned receipt'
}}
$sha = [System.Security.Cryptography.SHA256]::Create()
try {{
    $actionText = [string]$definition.Actions[0].Execute + [char]0 + [string]$definition.Actions[0].Arguments
    $actionBytes = $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($actionText))
    $actionSha = [BitConverter]::ToString($actionBytes).Replace('-', '').ToLowerInvariant()
    $userBytes = $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes([string]$definition.Principal.UserId))
    $userSha = [BitConverter]::ToString($userBytes).Replace('-', '').ToLowerInvariant()
}} finally {{ $sha.Dispose() }}
if ($actionSha -cne $expectedActionSha -or $userSha -cne $expectedUserSha) {{
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
        $again.Principal.LogonType.ToString() -cne 'Interactive' -or
        $again.Principal.RunLevel.ToString() -cne 'Limited') {{
        throw 'task definition changed before disposal'
    }}
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {{
        $actionText = [string]$again.Actions[0].Execute + [char]0 + [string]$again.Actions[0].Arguments
        $actionBytes = $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($actionText))
        $againActionSha = [BitConverter]::ToString($actionBytes).Replace('-', '').ToLowerInvariant()
        $userBytes = $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes([string]$again.Principal.UserId))
        $againUserSha = [BitConverter]::ToString($userBytes).Replace('-', '').ToLowerInvariant()
    }} finally {{ $sha.Dispose() }}
    if ($againActionSha -cne $expectedActionSha -or $againUserSha -cne $expectedUserSha) {{
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

    def launch(self, request: dict[str, Any]) -> OwnedPixelTaskHandle:
        """Launch one fixed helper request only when its binding is exact."""

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
        task = launch_owned_interactive_task(
            self._lab,
            command,
            nonce=nonce,
            request_sha256=request_sha256,
            session_binding_sha256=self._session_binding_sha256,
            helper_sha256=helper_sha256,
        )
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
            status = self._lab.run_ps(
                f"if (Test-Path -LiteralPath {_ps_quote(handle.status_path)}) "
                f"{{ Get-Content -Raw -LiteralPath {_ps_quote(handle.status_path)} }}",
                check=False,
                timeout_s=2.0,
                operation="qualification pixel task status",
            ).stdout.strip()
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
