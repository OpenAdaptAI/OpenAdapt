"""Native approval values, independent managed storage, and no-write refusal.

This synthetic observation variant removes the grid from the visible form so
virtual cells with no independent native value source do not obscure the
specific field contract under test. The original SQLite data and save handler
remain. It qualifies neither those virtual cells nor a complete Flow save.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import time
from pathlib import Path

import pytest
from PIL import Image

from openadapt_flow import vision
from openadapt_flow.approval_mode.form_state import approval_form_state
from openadapt_flow.approval_mode.gate import ApprovalGate, StepContext
from openadapt_flow.approval_mode.item_approval import ItemApprovalStore
from openadapt_flow.backends.win_agent import server
from openadapt_flow.backends.win_agent.native_value import valid_value_proof
from openadapt_flow.ir import ActionKind, Step, StepResult, StructuralLocator, Workflow
from openadapt_flow.runtime.effects.desktop_readback import (
    ControlRef,
    DesktopSessionFacts,
)
from openadapt_flow.runtime.effects.desktop_readback_sessions import UiaReadSurface
from tests.e2e.test_windows_hosted_replay import RUN_E2E, _Host

pytestmark = [
    pytest.mark.skipif(
        not RUN_E2E, reason="opt-in hosted Windows native approval proof"
    ),
    pytest.mark.timeout(300),
]
EVIDENCE_ENV = "OAFLOW_NATIVE_APPROVAL_EVIDENCE"
SHORT = "Synthetic Ω café 42"
HOOK = r"""
# This is a synthetic observation variant, not qualification of virtual cells.
$form.Controls.Remove($grid)
$grid.Hide()
$approvalProbe = New-Object System.Windows.Forms.TextBox
$approvalProbe.Name = 'approvalProbe'
$approvalProbe.AccessibleName = 'approvalProbe'
$approvalProbe.Location = New-Object System.Drawing.Point(20, 120)
$approvalProbe.Size = New-Object System.Drawing.Size(590, 28)
$approvalProbe.MaxLength = 131072
$form.Controls.Add($approvalProbe)
function Get-ApprovalStored {
    $values = @{}
    $controls = @{ probe=$script:approvalProbe; search=$script:searchBox; note=$script:noteBox }
    foreach ($id in $controls.Keys) {
        $control = $controls[$id]
        $text = [string]$control.Text
        $bytes = [Text.Encoding]::Unicode.GetBytes($text)
        $hasher = [Security.Cryptography.SHA256]::Create()
        try { $hash = ([BitConverter]::ToString($hasher.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant() }
        finally { $hasher.Dispose() }
        $values[$id] = @{ utf16_units=$text.Length; sha256_utf16le=$hash;
            suffix=$text.Substring([Math]::Max(0, $text.Length - 16));
            hwnd=$control.Handle.ToInt64(); pid=$PID }
    }
    return @{ fields=$values; form_hwnd=$form.Handle.ToInt64(); pid=$PID }
}
$approvalTimer = New-Object System.Windows.Forms.Timer
$approvalTimer.Interval = 40
$approvalTimer.Add_Tick({
    $commandPath = Join-Path $PSScriptRoot 'approval-command'
    if (-not (Test-Path $commandPath)) { return }
    $command = [IO.File]::ReadAllText($commandPath)
    Remove-Item $commandPath
    switch ($command) {
        'empty' { $script:approvalProbe.Text = '' }
        'short' { $script:approvalProbe.Text = 'Synthetic Ω café 42' }
        'long' { $script:approvalProbe.Text = ('x' * 8192) + '-END' }
        'snapshot' { }
        default { throw 'Unknown synthetic approval fixture command' }
    }
    [void]$script:approvalProbe.Focus()
    $snapshot = Get-ApprovalStored
    $snapshot['command'] = $command
    $temporary = Join-Path $PSScriptRoot 'approval-ack.tmp'
    [IO.File]::WriteAllText($temporary, ($snapshot | ConvertTo-Json -Depth 6))
    [IO.File]::Move($temporary, (Join-Path $PSScriptRoot 'approval-ack.json'))
})
$approvalTimer.Start()
"""


def _command(root: Path, command: str) -> dict:
    ack = root / "approval-ack.json"
    ack.unlink(missing_ok=True)
    temporary = root / "approval-command.tmp"
    temporary.write_text(command)
    temporary.replace(root / "approval-command")
    deadline = time.monotonic() + 15
    while not ack.is_file():
        assert time.monotonic() < deadline, "synthetic approval fixture timed out"
        time.sleep(0.05)
    result = json.loads(ack.read_text(encoding="utf-8-sig"))
    assert result["command"] == command
    return result


def _check_stored(snapshot: dict, expected: str) -> None:
    actual = snapshot["fields"]["probe"]
    raw = expected.encode("utf-16-le")
    assert actual["utf16_units"] == len(raw) // 2
    assert actual["sha256_utf16le"] == hashlib.sha256(raw).hexdigest()
    assert actual["suffix"] == expected[-16:]


def test_native_approval_completeness_refuses_a_provider_prefix(tmp_path, monkeypatch):
    import uiautomation as auto

    destination = Path(os.environ[EVIDENCE_ENV])
    destination.parent.mkdir(parents=True, exist_ok=True)
    frames = destination.with_suffix(".frames")
    frames.mkdir(exist_ok=True)
    evidence = {
        "schema": "openadapt.native-approval-completeness-proof/v1",
        "scope": "synthetic observation variant, not full Flow/save qualification",
        "phases": [],
        "transport": [],
    }
    repository = Path(__file__).resolve().parents[4]
    evidence["actual_checkout_commit"] = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repository, text=True
    ).strip()
    evidence["actual_checkout_tree"] = subprocess.check_output(
        ["git", "rev-parse", "HEAD^{tree}"], cwd=repository, text=True
    ).strip()
    evidence["declared_pr_or_dispatch_head"] = os.environ.get(
        "OAFLOW_NATIVE_APPROVAL_SOURCE_HEAD"
    )
    critical = (
        "packages/flow/openadapt_flow/backends/win_agent/native_value.py",
        "packages/flow/openadapt_flow/backends/win_agent/value_protocol.py",
        "packages/flow/openadapt_flow/backends/win_agent/server.py",
        "packages/flow/openadapt_flow/backends/windows_backend.py",
        "packages/flow/openadapt_flow/approval_mode/form_state.py",
        "packages/flow/openadapt_flow/approval_mode/window_fields.py",
        "packages/flow/openadapt_flow/runtime/effects/desktop_readback_sessions.py",
        "packages/flow/openadapt_flow/runtime/effects/desktop_readback.py",
        ".github/workflows/desktop-replay.yml",
    )
    evidence["source_sha256"] = {
        name: hashlib.sha256((repository / name).read_bytes()).hexdigest()
        for name in critical
    }
    host = _Host(tmp_path)
    fixture = tmp_path / "patient_notes.ps1"
    fixture.write_text(
        fixture.read_text().replace(
            "[void]$form.ShowDialog()", HOOK + "\n[void]$form.ShowDialog()"
        ),
        encoding="utf-8-sig",
    )
    try:
        backend = host.start_agent()
        original_post = backend._session.post
        writes = []

        def post(url, **kwargs):
            path = url.removeprefix(backend.server_url)
            if path in {"/input", "/input/guarded", "/uia/act", "/execute_windows"}:
                writes.append(path)
            response = original_post(url, **kwargs)
            if path in {"/window/form-fields", "/uia/read"}:
                evidence["transport"].append(
                    {
                        "path": path,
                        "request": kwargs["json"],
                        "status": response.status_code,
                        "response": response.json(),
                    }
                )
            return response

        monkeypatch.setattr(backend._session, "post", post)
        host.launch_app()
        before = host.rows()
        evidence["before_rows"] = before
        for name, expected in (
            ("empty", ""),
            ("short", SHORT),
            ("long", "x" * 8192 + "-END"),
        ):
            managed = _command(tmp_path, name)
            _check_stored(managed, expected)
            payload = {"hwnd": managed["form_hwnd"], "pid": managed["pid"]}
            # Reacquire actual UIA/runtime/native scope, using the exact engine
            # reader independently in this process as well as over HTTP.
            with auto.UIAutomationInitializerInThread():
                window = auto.ControlFromHandle(payload["hwnd"])
                control = auto.ControlFromHandle(managed["fields"]["probe"]["hwnd"])
                user32 = server._user32_factory()
                initial = server._window_operation("window-bounds", payload, user32)[
                    "window"
                ]
                assert initial is not None

                def identity():
                    return server._form_value_identity(
                        control,
                        window,
                        payload["hwnd"],
                        lambda: (
                            server._window_operation("window-bounds", payload, user32)[
                                "window"
                            ]
                            == initial
                        ),
                        auto,
                    )

                observed = server._form_field_value(control, identity)
                api = server._native_value_api()
                raw_status = control.GetPropertyValueEx(30019, 1)
                status_measurement = {
                    "source": "GetPropertyValueEx(30019,1)",
                    "kind": type(raw_status).__name__,
                    "value": raw_status if type(raw_status) is bool else None,
                }
                uia_value = None
                if type(raw_status) is bool and raw_status is False:
                    # This separate provider-prefix measurement is diagnostic
                    # only and obeys the same shared pre/post getter guard.
                    api.require_non_sensitive_value(control)
                    method = control.GetValuePattern
                    api.require_non_sensitive_value(control)
                    pattern = method()
                    api.require_non_sensitive_value(control)
                    uia_value = pattern.Value
                    api.require_non_sensitive_value(control)
                rect = control.BoundingRectangle
                point = (
                    int((rect.left + rect.right) / 2),
                    int((rect.top + rect.bottom) / 2),
                )
            assert backend.record_verified_window("probe", *point)
            fields = backend.approval_form_fields("save")
            phase = {
                "name": name,
                "managed_before": managed,
                "raw_password_status": status_measurement,
                "uia_utf16_units": len(uia_value.encode("utf-16-le")) // 2
                if uia_value is not None
                else None,
                "uia_sha256_utf16le": hashlib.sha256(
                    uia_value.encode("utf-16-le")
                ).hexdigest()
                if uia_value is not None
                else None,
                "direct_complete": observed.complete,
                "direct_reason": observed.reason,
                "fields": fields,
            }
            evidence["phases"].append(phase)
            if name == "long":
                assert (
                    not observed.complete
                    and observed.reason == "provider_native_value_disagreement"
                )
                assert phase["uia_utf16_units"] == 4096 and fields is None
            else:
                assert observed.complete and observed.value == expected
                assert observed.proof is not None and valid_value_proof(
                    expected, observed.proof.payload()
                )
                assert fields is not None
                by_hwnd = {
                    f["value_proof"]["hwnd"]: f
                    for f in fields["fields"]
                    if f["value_proof"] is not None
                }
                for stored in managed["fields"].values():
                    field = by_hwnd[stored["hwnd"]]
                    assert (
                        field["value_proof"]["sha256_utf16le"]
                        == stored["sha256_utf16le"]
                    )
                    assert field["value_proof"]["utf16_units"] == stored["utf16_units"]
                    assert field["value_proof"]["pid"] == stored["pid"]
            semantic = backend.read_structural(
                StructuralLocator(automation_id="approvalProbe")
            )
            phase["structural"] = semantic
            surface = UiaReadSurface(
                backend,
                facts=DesktopSessionFacts(True, False, False, "uia"),
                settle_s=0,
            )
            located = surface.read(ControlRef(automation_id="approvalProbe"))
            phase["structural_located"] = located.status
            assert semantic is not None
            if name == "long":
                assert semantic["complete"] is False and semantic["text"] is None
                assert semantic["reason"] == "provider_native_value_disagreement"
                assert located.status == "unavailable" and located.item is None
            else:
                assert semantic["complete"] is True and semantic["text"] == expected
                assert located.status == "ok" and located.item is not None
                assert located.item.text == expected
            frame = backend.screenshot()
            store = ItemApprovalStore(tmp_path / ("run-" + name))
            gate = ApprovalGate("each_item", approval_store=store, approval_wait_s=0)
            gate.begin_item(
                run_id="run-" + name, bundle_content_digest="a" * 64, profile="standard"
            )
            step = Step(
                id="save",
                intent="Save synthetic fixture",
                action=ActionKind.KEY,
                key="Enter",
                risk="irreversible",
            )
            result = StepResult(step_id=step.id, intent=step.intent, ok=False)
            verdict = gate.before_step(
                StepContext(
                    step=step,
                    workflow=Workflow(name="native approval", steps=[step]),
                    result=result,
                    step_index=0,
                    run_id="run-" + name,
                    frame=lambda: frame,
                    form_state=lambda png: approval_form_state(
                        backend, vision, png, "save"
                    ),
                )
            )
            assert not verdict.proceed  # nobody approved; no input may occur
            phase["gate_checks"] = [
                check.model_dump(mode="json") for check in verdict.checks
            ]
            requests = store.requests()
            phase["request_count"] = len(requests)
            if name == "long":
                assert not requests and any(
                    c.detail == "form_state_unreadable" for c in verdict.checks
                )
            else:
                assert (
                    len(requests) == 1
                    and requests[0].form_state_kind == "window_fields"
                )
                request = requests[0]
                raw = (store.run_dir / request.screenshot).read_bytes()
                assert hashlib.sha256(raw).hexdigest() == request.screenshot_sha256
                with Image.open(io.BytesIO(raw)) as image:
                    size = image.size
                    image.verify()
                (frames / (name + ".png")).write_bytes(raw)
                phase["frame"] = {
                    "file": name + ".png",
                    "sha256": request.screenshot_sha256,
                    "size": size,
                    "bytes": len(raw),
                }
            after = _command(tmp_path, "snapshot")
            _check_stored(after, expected)
            assert after["fields"] == managed["fields"]
            phase["managed_after"] = after
            phase["after_rows"] = host.rows()
            assert phase["after_rows"] == before
            assert not writes
        evidence["input_requests"] = writes
    finally:
        host.close()
        evidence["fixture_and_agent_stopped"] = host.app is None and (
            host.agent is None or host.agent.poll() is not None
        )
        destination.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
