"""Real, authenticated Windows keyboard leases over a synthetic WinForms record.

No qualification driver or fabricated input receipt participates. The field's
UIA value and the app's independent SQLite record decide what changed. The
transport observer delegates every request unchanged; it can discard one real
acknowledgement to test the uncertainty boundary.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import platform
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from openadapt_flow.backend import (
    ActionDeliveryUncertain,
    KeyboardFrameChangedBeforeInput,
    StructuralResolutionRefused,
)
from openadapt_flow.ir import ActionDeliveryReceipt, StructuralLocator
from tests.e2e.test_windows_hosted_replay import RUN_E2E, _Host, _points

pytestmark = [
    pytest.mark.skipif(
        not RUN_E2E,
        reason="opt-in hosted Windows e2e; set OAFLOW_WINDOWS_HOSTED_E2E=1 on Windows",
    ),
    pytest.mark.timeout(300),
]

EVIDENCE_ENV = "OAFLOW_NATIVE_KEYBOARD_LEASE_EVIDENCE"
REPOSITORY = Path(__file__).resolve().parents[4]

# This hook belongs only to the copied synthetic fixture. It recreates the
# focused textbox in the same form, retaining its visible and stable locator
# attributes while the old native handle remains alive but detached. An old
# HWND therefore cannot be immediately reused for the replacement control.
REPLACE_HOOK = r"""
$retiredLeaseControls = New-Object System.Collections.ArrayList
$leaseTimer = New-Object System.Windows.Forms.Timer
$leaseTimer.Interval = 40
$leaseTimer.Add_Tick({
    $commandPath = Join-Path $PSScriptRoot "lease-command"
    if (-not (Test-Path $commandPath)) { return }
    $command = [IO.File]::ReadAllText($commandPath)
    Remove-Item $commandPath
    if ($command -ne "replace-note") { throw "Unknown lease fixture command" }
    $old = $script:noteBox
    $oldHandle = $old.Handle.ToInt64()
    $new = New-Object System.Windows.Forms.TextBox
    $new.Name = $old.Name
    $new.AccessibleName = $old.AccessibleName
    $new.Multiline = $old.Multiline
    $new.Location = $old.Location
    $new.Size = $old.Size
    $new.Font = $old.Font
    $new.BackColor = $old.BackColor
    $new.ForeColor = $old.ForeColor
    $new.TabIndex = $old.TabIndex
    $new.Text = $old.Text
    $form.Controls.Add($new)
    $newHandle = $new.Handle.ToInt64()
    $old.Name = "retiredLeaseTextbox"
    $old.AccessibleName = "retiredLeaseTextbox"
    $form.Controls.Remove($old)
    $old.Hide()
    [void]$script:retiredLeaseControls.Add($old)
    $script:noteBox = $new
    $new.BringToFront()
    [void]$new.Focus()
    $new.SelectionStart = $new.TextLength
    $ack = @{ old_handle=$oldHandle; new_handle=$newHandle;
              form_handle=$form.Handle.ToInt64(); name=$new.Name;
              value=$new.Text; focused=$new.Focused }
    $ack | ConvertTo-Json | Set-Content (Join-Path $PSScriptRoot "lease-replaced.json")
})
$leaseTimer.Start()
"""


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _redacted(value: Any) -> Any:
    """Keep raw technical envelopes, excluding reusable lease tokens/frames."""
    result = copy.deepcopy(value)
    if isinstance(result, dict):
        token = result.pop("lease_id", None)
        if isinstance(token, str):
            result["lease_id_sha256"] = _sha(token.encode())
        frame = result.pop("frame_png", None)
        if isinstance(frame, str):
            result["frame_png_retained_separately"] = True
    return result


class _Transport:
    def __init__(self, backend, monkeypatch, evidence: dict[str, Any]) -> None:
        original = backend._session.post
        self.last_input: dict[str, Any] | None = None
        self.drop_next_ack = False
        self.inputs = 0
        self.before_keyboard_input = None
        self.lease_evidence_dir: Path | None = None
        self.events = evidence["transport"] = []

        def post(url, **kwargs):
            started = time.monotonic()
            path = url.removeprefix(backend.server_url)
            planted = None
            if path == "/keyboard/input" and self.before_keyboard_input is not None:
                planted = self.before_keyboard_input(copy.deepcopy(kwargs["json"]))
            response = original(url, **kwargs)
            if path in {
                "/keyboard/lease",
                "/keyboard/input",
                "/uia/editable-value",
                "/uia/act",
            }:
                request = kwargs["json"]
                event = {
                    "path": path,
                    "request": _redacted(request),
                    "http_status": response.status_code,
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                    "response": _redacted(response.json()),
                }
                if planted is not None:
                    event["planted_fixture_change"] = planted
                if (
                    path == "/keyboard/lease"
                    and response.status_code == 200
                    and self.lease_evidence_dir is not None
                ):
                    self.lease_evidence_dir.mkdir(parents=True, exist_ok=True)
                    destination = (
                        self.lease_evidence_dir / f"lease-{len(self.events):04d}.png"
                    )
                    png = base64.b64decode(response.json()["frame_png"], validate=True)
                    destination.write_bytes(png)
                    event["retained_frame"] = str(destination)
                    event["retained_frame_sha256"] = _sha(png)
                self.events.append(event)
                if path == "/keyboard/input":
                    self.inputs += 1
                    self.last_input = copy.deepcopy(request)
                    if self.drop_next_ack and response.status_code == 200:
                        from requests import ConnectionError

                        self.drop_next_ack = False
                        event["injected_fault"] = "discard_actual_acknowledgement"
                        raise ConnectionError("synthetic acknowledgement loss")
            return response

        monkeypatch.setattr(backend._session, "post", post)


def _read(backend, automation_id: str) -> str:
    result = backend.read_structural(StructuralLocator(automation_id=automation_id))
    if result is None:
        pytest.fail("UIA structural read unavailable", pytrace=False)
    match, count = result["match"], result["candidate_count"]
    if match != "unique" or count != 1:
        # WindowsBackend validated the match and count. Keep provider text,
        # previews and unrecognized reason strings out of the failure report.
        reason = "not_unique" if result["reason"] == "not_unique" else "unclassified"
        pytest.fail(
            f"UIA census refused (match={match}, candidate_count={count}, reason={reason})",
            pytrace=False,
        )
    if result["complete"] is not True or not isinstance(result["text"], str):
        reason = (
            result.get("reason")
            if isinstance(result.get("reason"), str)
            else "unclassified"
        )
        pytest.fail(
            f"UIA value proof incomplete after a unique match (reason={reason})",
            pytrace=False,
        )
    return result["text"]


def _read_label(backend, automation_id: str) -> str:
    """Read a non-value display label (preview_text only; complete stays False)."""
    result = backend.read_structural(StructuralLocator(automation_id=automation_id))
    if result is None:
        pytest.fail("UIA structural read unavailable", pytrace=False)
    match, count = result["match"], result["candidate_count"]
    if match != "unique" or count != 1:
        reason = "not_unique" if result["reason"] == "not_unique" else "unclassified"
        pytest.fail(
            f"UIA census refused (match={match}, candidate_count={count}, reason={reason})",
            pytrace=False,
        )
    if result["complete"] is not False or result["text"] is not None:
        pytest.fail(
            "UIA display label unexpectedly claimed complete value proof", pytrace=False
        )
    preview = result.get("preview_text")
    if not isinstance(preview, str):
        pytest.fail(
            "UIA display label preview_text unavailable after unique match",
            pytrace=False,
        )
    return preview


def _values(backend) -> dict[str, str]:
    return {aid: _read(backend, aid) for aid in ("searchBox", "noteBox")}


def _wait_value(backend, automation_id: str, expected: str) -> None:
    deadline = time.monotonic() + 15
    while _read(backend, automation_id) != expected:
        assert time.monotonic() < deadline, (
            f"UIA {automation_id} did not reach {expected!r}"
        )
        time.sleep(0.05)


def _wait_label(backend, automation_id: str, expected: str) -> None:
    deadline = time.monotonic() + 15
    while _read_label(backend, automation_id) != expected:
        assert time.monotonic() < deadline, (
            f"UIA label {automation_id} did not reach {expected!r}"
        )
        time.sleep(0.05)


def _validate(backend, host, baseline, header, context, expected) -> dict[str, Any]:
    """Resolve anew and independently validate all bindings, including retries."""
    handle = backend.locate_structural(StructuralLocator(automation_id="noteBox"))
    assert handle is not None and handle.candidate_count == 1
    observation = backend.focus_identity()
    assert observation is not None
    target = observation["native_focus"]
    assert target["locator"]["automation_id"] == "noteBox"
    assert target["target_fingerprint"] == handle.target_fingerprint
    assert tuple(target["point"]) == tuple(handle.point)
    assert _read_label(backend, "patientHeader") == header
    assert backend._guard_context() == context
    assert _values(backend) == expected
    assert host.rows() == baseline
    return {
        "target": target,
        "context": context,
        "record_header": header,
        "values": expected,
        "rows_unchanged": True,
    }


def _lease(backend, host, root, name, baseline, header, context, expected):
    before = _validate(backend, host, baseline, header, context, expected)
    png = backend.guarded_keyboard_frame()
    (root / f"{name}.png").write_bytes(png)
    # Re-verify live focus, guard context, and DB rows without running three
    # full-desktop strict UIA census walks that would exhaust the lease TTL.
    observation = backend.focus_identity()
    assert observation is not None
    assert observation["native_focus"] == before["target"]
    assert backend._guard_context() == context
    assert host.rows() == baseline
    backend.arm_guarded_keyboard(*before["target"]["point"])
    return _sha(png), before


def _deliver(
    backend,
    host,
    root,
    evidence,
    name,
    action,
    argument,
    baseline,
    header,
    context,
    before,
    after,
):
    """Only typed, zero-input stale frames can reacquire a complete lease."""
    attempts = []
    case = {"case": name, "attempts": attempts}
    evidence["cases"].append(case)
    for attempt in range(6):
        digest, validation = _lease(
            backend, host, root, f"{name}-{attempt}", baseline, header, context, before
        )
        row = {"fresh_validation": validation, "frame_sha256": digest}
        attempts.append(row)
        try:
            receipt = (
                backend.type_text_guarded(argument, expected_frame_sha256=digest)
                if action == "type"
                else backend.press_guarded(argument, expected_frame_sha256=digest)
            )
        except KeyboardFrameChangedBeforeInput as exc:
            row["zero_keyboard_input_refusal"] = {
                "type": type(exc).__name__,
                "operation": exc.operation,
                "changed_pixel_count": exc.changed_pixel_count,
                "changed_bbox": exc.changed_bbox,
                "frame_size": exc.frame_size,
                "lease_sha256": exc.lease_sha256,
                "target_fingerprint": exc.target_fingerprint,
            }
            # No focusing click or keyboard preparation is repeated. The next
            # attempt rechecks real focus, target, record, context and DB, then
            # obtains and fully revalidates an entirely new leased frame.
            assert _values(backend) == before and host.rows() == baseline
            continue
        assert receipt.status == "delivered" and receipt.native is False
        assert receipt.outcome_verified is False
        assert receipt.target_fingerprint == validation["target"]["target_fingerprint"]
        row["actual_receipt"] = receipt.model_dump(mode="json")
        _wait_value(backend, "noteBox", after["noteBox"])
        assert _values(backend) == after and host.rows() == baseline
        case["after"] = {"values": _values(backend), "rows": host.rows()}
        return receipt
    raise AssertionError(f"{name}: exact desktop frame changed on all six fresh leases")


def _refuse(
    backend,
    host,
    transport,
    evidence,
    name,
    digest,
    expected,
    baseline,
    reason,
    *,
    typed_frame=False,
):
    case: dict[str, Any] = {"case": name, "before": expected}
    evidence["cases"].append(case)
    previous = transport.inputs
    error_type = (
        KeyboardFrameChangedBeforeInput if typed_frame else StructuralResolutionRefused
    )
    with pytest.raises(error_type) as error:
        backend.type_text_guarded("UNEXPECTED INPUT", expected_frame_sha256=digest)
    assert transport.inputs == previous + 1
    raw = transport.events[-1]
    assert raw["response"]["reason"] == reason
    assert raw["response"]["no_keyboard_input_delivered"] is True
    assert _values(backend) == expected and host.rows() == baseline
    case.update(
        {
            "exception_type": type(error.value).__name__,
            "refusal": raw,
            "after": {"values": _values(backend), "rows": host.rows()},
        }
    )


def test_native_keyboard_lease_actual_delivery_and_refusal(tmp_path, monkeypatch):
    evidence: dict[str, Any] = {
        "schema": "openadapt.native-keyboard-lease-proof/v1",
        "platform": platform.platform(),
        "cases": [],
        "head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True
        ).strip(),
        "tree": subprocess.check_output(
            ["git", "rev-parse", "HEAD^{tree}"], cwd=REPOSITORY, text=True
        ).strip(),
        "expected_source": os.environ.get("EXPECTED_SOURCE_SHA"),
        "run_id": os.environ.get("GITHUB_RUN_ID"),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
    }
    host = _Host(tmp_path)
    app = tmp_path / "patient_notes.ps1"
    source = app.read_text()
    assert source.count("[void]$form.ShowDialog()") == 1
    app.write_text(
        source.replace(
            "[void]$form.ShowDialog()", REPLACE_HOOK + "\n[void]$form.ShowDialog()"
        )
    )
    evidence["fixture_sha256"] = _sha(app.read_bytes())
    try:
        with monkeypatch.context() as agent_env:
            agent_env.setenv("OAFLOW_AGENT_TRACE_CENSUS", "1")
            backend = host.start_agent()
        capabilities = backend.agent_capabilities()
        evidence["capabilities"] = sorted(capabilities)
        assert {
            "native_focus_v1",
            "native_keyboard_lease_v1",
            "context_environment_v1",
        } <= capabilities
        transport = _Transport(backend, monkeypatch, evidence)
        host.launch_app()
        points = _points(backend)
        baseline = evidence["rows_before"] = host.rows()
        target = next(row for row in baseline if row["id"] == 1)
        header = f"Patient: {target['first']} {target['last']} ({target['dob']})"
        backend.click(*points["searchBox"])
        backend.type_text(target["first"])
        backend.press("enter")
        _wait_label(backend, "patientHeader", header)
        backend.click(*points["noteBox"])
        context = backend._guard_context()
        before = {"searchBox": target["first"], "noteBox": ""}
        typed = {**before, "noteBox": "lease proof"}
        _deliver(
            backend,
            host,
            tmp_path,
            evidence,
            "type",
            "type",
            "lease proof",
            baseline,
            header,
            context,
            before,
            typed,
        )
        # Keep the exact real request in memory to test a consumed token later.
        replay = copy.deepcopy(transport.last_input)
        assert replay is not None
        newline = {**typed, "noteBox": "lease proof\r\n"}
        _deliver(
            backend,
            host,
            tmp_path,
            evidence,
            "key",
            "press",
            "enter",
            baseline,
            header,
            context,
            typed,
            newline,
        )
        backend.press("ctrl+a")  # explicit synthetic setup, not a leased retry
        _deliver(
            backend,
            host,
            tmp_path,
            evidence,
            "hotkey",
            "press",
            "ctrl+x",
            baseline,
            header,
            context,
            newline,
            before,
        )

        # A visible value change after observation keeps the same native focus.
        digest, observed = _lease(
            backend, host, tmp_path, "planted-frame", baseline, header, context, before
        )
        backend.type_text("external change")
        changed = {**before, "noteBox": "external change"}
        _wait_value(backend, "noteBox", changed["noteBox"])
        deadline = time.monotonic() + 5
        while True:
            planted_png = backend.screenshot()
            if _sha(planted_png) != digest:
                (tmp_path / "planted-frame-after.png").write_bytes(planted_png)
                break
            assert time.monotonic() < deadline, (
                "planted input never changed visible pixels"
            )
            time.sleep(0.05)
        assert (
            backend.focus_identity()["native_focus"]["target_fingerprint"]
            == observed["target"]["target_fingerprint"]
        )
        _refuse(
            backend,
            host,
            transport,
            evidence,
            "stale_frame",
            digest,
            changed,
            baseline,
            "stale_frame",
            typed_frame=True,
        )
        # Reacquiring after this typed refusal performs the entire validation
        # sequence; the new input is visibly appended exactly once.
        retry_after = {**changed, "noteBox": "external change verified"}
        _deliver(
            backend,
            host,
            tmp_path,
            evidence,
            "fresh_after_zero_input",
            "type",
            " verified",
            baseline,
            header,
            context,
            changed,
            retry_after,
        )

        digest, _ = _lease(
            backend,
            host,
            tmp_path,
            "planted-focus",
            baseline,
            header,
            context,
            retry_after,
        )
        backend.click(*points["searchBox"])
        _refuse(
            backend,
            host,
            transport,
            evidence,
            "stale_focus",
            digest,
            retry_after,
            baseline,
            "stale_focus",
        )
        backend.click(*points["noteBox"])

        digest, observed = _lease(
            backend,
            host,
            tmp_path,
            "planted-replacement",
            baseline,
            header,
            context,
            retry_after,
        )
        (tmp_path / "lease-command").write_text("replace-note")
        acknowledgement = tmp_path / "lease-replaced.json"
        deadline = time.monotonic() + 10
        while not acknowledgement.is_file():
            assert time.monotonic() < deadline, "fixture did not replace its textbox"
            time.sleep(0.05)
        ack = json.loads(acknowledgement.read_text(encoding="utf-8-sig"))
        assert ack["old_handle"] != ack["new_handle"] and ack["focused"] is True
        replaced = backend.focus_identity()["native_focus"]
        assert replaced["locator"] == observed["target"]["locator"]
        assert replaced["region"] == observed["target"]["region"]
        assert (
            replaced["target_fingerprint"] != observed["target"]["target_fingerprint"]
        )
        evidence["replacement"] = {
            "fixture": ack,
            "before": observed["target"],
            "after": replaced,
        }
        _refuse(
            backend,
            host,
            transport,
            evidence,
            "replaced_target",
            digest,
            retry_after,
            baseline,
            "stale_focus",
        )

        # A consumed token is tested at the real endpoint, not by re-arming a
        # client object. Unknown lease failures carry no zero-input contract.
        previous = transport.inputs
        response = backend._session.post(
            backend.server_url + "/keyboard/input",
            json=replay,
            **backend._request_kwargs(),
        )
        assert transport.inputs == previous + 1
        assert (
            response.status_code == 409
            and response.json()["code"] == "keyboard_lease_unavailable"
        )
        assert "no_keyboard_input_delivered" not in response.json()
        assert _values(backend) == retry_after and host.rows() == baseline
        evidence["cases"].append(
            {
                "case": "consumed_lease",
                "response": transport.events[-1],
                "after": {"values": _values(backend), "rows": host.rows()},
            }
        )

        # Lose a real successful acknowledgement. The client must surface
        # uncertainty after one request and must never retry the input.
        transport.drop_next_ack = True
        uncertain = {"case": "uncertain_ack", "attempts": []}
        evidence["cases"].append(uncertain)
        for attempt in range(6):
            digest, validation = _lease(
                backend,
                host,
                tmp_path,
                f"lost-ack-{attempt}",
                baseline,
                header,
                context,
                retry_after,
            )
            previous = transport.inputs
            try:
                backend.type_text_guarded(" uncertain", expected_frame_sha256=digest)
            except KeyboardFrameChangedBeforeInput:
                assert (
                    transport.events[-1]["response"]["no_keyboard_input_delivered"]
                    is True
                )
                assert _values(backend) == retry_after and host.rows() == baseline
                uncertain["attempts"].append(
                    {
                        "fresh_validation": validation,
                        "zero_input_refusal": transport.events[-1],
                    }
                )
                continue
            except ActionDeliveryUncertain:
                # Only this discarded 200 acknowledgement can explain this
                # positive case. No catch or retry follows an uncertain input.
                assert transport.events[-1]["http_status"] == 200
                assert (
                    transport.events[-1]["injected_fault"]
                    == "discard_actual_acknowledgement"
                )
                assert transport.inputs == previous + 1
                raw_receipt = ActionDeliveryReceipt.model_validate(
                    transport.events[-1]["response"]["receipt"]
                )
                assert raw_receipt.operation == "physical_type_text"
                assert (
                    raw_receipt.target_fingerprint
                    == validation["target"]["target_fingerprint"]
                )
                assert (
                    raw_receipt.native is False
                    and raw_receipt.outcome_verified is False
                )
                uncertain["attempts"].append(
                    {
                        "fresh_validation": validation,
                        "actual_response_discarded": transport.events[-1],
                    }
                )
                break
            raise AssertionError("lost acknowledgement was reported as delivered")
        else:
            raise AssertionError(
                "lost-ack case never acquired an unchanged exact frame"
            )
        _wait_value(backend, "noteBox", retry_after["noteBox"] + " uncertain")
        assert host.rows() == baseline
        uncertain["after"] = {"values": _values(backend), "rows": host.rows()}
        evidence["passed"] = True
    finally:
        try:
            evidence["final_values"] = _values(backend)
            evidence["final_context"] = backend._guard_context()
            (tmp_path / "final.png").write_bytes(backend.screenshot())
        except Exception as exc:
            evidence["final_field_observation_error"] = type(exc).__name__
        try:
            evidence["rows_after"] = (
                host.rows() if (tmp_path / "patients.db").exists() else None
            )
        except Exception as exc:
            evidence["final_oracle_error"] = type(exc).__name__
        fixture_process = host.app
        try:
            host.close()
        finally:
            evidence["agent_terminated"] = (
                host.agent is None or host.agent.poll() is not None
            )
            evidence["fixture_terminated"] = (
                fixture_process is None or fixture_process.poll() is not None
            )
            for destination in (
                tmp_path / "evidence.json",
                Path(
                    os.environ.get(EVIDENCE_ENV, tmp_path / "published-evidence.json")
                ),
            ):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(json.dumps(evidence, indent=2) + "\n")


# Additional controls and repaint commands exist only in this proof's copied
# synthetic app. None carries a save handler or changes the SQLite record.
OBSERVATION_HOOK = r"""
$form.Size = New-Object System.Drawing.Size(960, 560)
$leaseFields = @{}
function Add-LeaseField($id, $y, $text, $password, $readOnly) {
    $box = New-Object System.Windows.Forms.TextBox
    $box.Name = $id
    $box.AccessibleName = $id
    $box.Location = New-Object System.Drawing.Point(650, $y)
    $box.Size = New-Object System.Drawing.Size(180, 24)
    $box.MaxLength = 100000
    $box.UseSystemPasswordChar = $password
    $box.ReadOnly = $readOnly
    $box.Text = $text
    $form.Controls.Add($box)
    $script:leaseFields[$id + '-' + $y] = $box
}
Add-LeaseField "leasePassword" 100 "synthetic password fixture" $true $false
Add-LeaseField "leaseLong" 140 (('x' * 8192) + '-END') $false $false
Add-LeaseField "leaseOversized" 180 ('x' * 65537) $false $false
Add-LeaseField "leaseReadOnly" 220 "read only synthetic fixture" $false $true
Add-LeaseField "leaseDuplicate" 260 "first duplicate" $false $false
Add-LeaseField "leaseDuplicate" 300 "second duplicate" $false $false
$leaseButton = New-Object System.Windows.Forms.Button
$leaseButton.Name = "leaseNotEditable"
$leaseButton.AccessibleName = "Display name must not become a value"
$leaseButton.Text = "Display name must not become a value"
$leaseButton.Location = New-Object System.Drawing.Point(650, 340)
$leaseButton.Size = New-Object System.Drawing.Size(250, 30)
$form.Controls.Add($leaseButton)
$leaseFields['button'] = $leaseButton
function Get-LeaseStoredValues {
    $controls = @{ noteBox=$script:noteBox; leaseLong=$script:leaseFields['leaseLong-140'];
                   leaseOversized=$script:leaseFields['leaseOversized-180'] }
    $values = @{}
    foreach ($id in $controls.Keys) {
        $control = $controls[$id]
        $text = [string]$control.Text
        $bytes = [Text.Encoding]::UTF8.GetBytes($text)
        $hasher = [Security.Cryptography.SHA256]::Create()
        try { $hash = ([BitConverter]::ToString($hasher.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant() }
        finally { $hasher.Dispose() }
        $point = $control.PointToScreen([Drawing.Point]::new([int]($control.Width / 2), [int]($control.Height / 2)))
        $values[$id] = @{ live_length=$text.Length; sha256=$hash;
            suffix=$text.Substring([Math]::Max(0, $text.Length - 16));
            hwnd=$control.Handle.ToInt64(); pid=$PID; point=@($point.X, $point.Y) }
    }
    return $values
}
$form.Add_Shown({
    $points = @{}
    foreach ($key in $script:leaseFields.Keys) {
        $control = $script:leaseFields[$key]
        $p = $control.PointToScreen((New-Object System.Drawing.Point([int]($control.Width / 2), [int]($control.Height / 2))))
        $points[$key] = @($p.X, $p.Y)
    }
    $temporary = Join-Path $PSScriptRoot 'lease-observation-points.tmp'
    [IO.File]::WriteAllText($temporary, ($points | ConvertTo-Json))
    [IO.File]::Move($temporary, (Join-Path $PSScriptRoot 'lease-observation-points.json'))
    $temporary = Join-Path $PSScriptRoot 'lease-stored-initial.tmp'
    [IO.File]::WriteAllText($temporary, ((Get-LeaseStoredValues) | ConvertTo-Json -Depth 5))
    [IO.File]::Move($temporary, (Join-Path $PSScriptRoot 'lease-stored-initial.json'))
})
$leaseObservationTimer = New-Object System.Windows.Forms.Timer
$leaseObservationTimer.Interval = 40
$leaseObservationTimer.Add_Tick({
    $commandPath = Join-Path $PSScriptRoot 'lease-observation-command'
    if (-not (Test-Path $commandPath)) { return }
    $command = [IO.File]::ReadAllText($commandPath)
    Remove-Item $commandPath
    switch ($command) {
        'clear-note' { $script:noteBox.Text = ''; [void]$script:noteBox.Focus(); $script:noteBox.SelectionStart = 0 }
        'paint-success' { $script:status.Text = 'Synthetic lease repaint success' }
        'paint-uncertain' { $script:status.Text = 'Synthetic lease repaint uncertain' }
        'snapshot-observation' { }
        default { throw 'Unknown observation fixture command' }
    }
    $ack = @{ command=$command; note_value=$script:noteBox.Text; status=$script:status.Text;
              form_handle=$form.Handle.ToInt64(); note_handle=$script:noteBox.Handle.ToInt64();
              stored_values=(Get-LeaseStoredValues) }
    $temporary = Join-Path $PSScriptRoot 'lease-observation-ack.tmp'
    [IO.File]::WriteAllText($temporary, ($ack | ConvertTo-Json -Depth 5))
    [IO.File]::Move($temporary, (Join-Path $PSScriptRoot 'lease-observation-ack.json'))
})
$leaseObservationTimer.Start()
"""


def _fixture_command(root: Path, command: str) -> dict[str, Any]:
    acknowledgement = root / "lease-observation-ack.json"
    acknowledgement.unlink(missing_ok=True)
    temporary = root / "lease-observation-command.tmp"
    temporary.write_text(command)
    temporary.replace(root / "lease-observation-command")
    deadline = time.monotonic() + 10
    while not acknowledgement.is_file():
        assert time.monotonic() < deadline, f"fixture command {command} timed out"
        time.sleep(0.05)
    result = json.loads(acknowledgement.read_text(encoding="utf-8-sig"))
    assert result["command"] == command
    return result


def _stored_snapshot(root: Path, aid: str, expected: str) -> dict[str, Any]:
    observed = _fixture_command(root, "snapshot-observation")["stored_values"][aid]
    assert observed["live_length"] == len(expected.encode("utf-16-le")) // 2
    assert observed["suffix"] == expected[-16:]
    assert observed["sha256"] == _sha(expected.encode("utf-8"))
    assert type(observed["hwnd"]) is int and observed["hwnd"] > 0
    assert type(observed["pid"]) is int and observed["pid"] > 0
    return observed


def _editable_positive(backend, transport, root, aid, point, expected):
    backend.click(*point)
    managed_before = _stored_snapshot(root, aid, expected)
    handle = backend.locate_structural(StructuralLocator(automation_id=aid))
    assert handle is not None and handle.candidate_count == 1
    observed = backend.editable_value_observation(*point)
    assert observed is not None
    proof = observed["completeness_proof"]
    assert type(proof["thread"]) is int and proof["thread"] > 0
    assert observed == {
        "status": "ok",
        "schema": "openadapt.native-editable-value/v2",
        "complete": True,
        "value": expected,
        "target_fingerprint": handle.target_fingerprint,
        "bounds": [
            handle.region[0],
            handle.region[1],
            handle.region[0] + handle.region[2],
            handle.region[1] + handle.region[3],
        ],
        "point": list(handle.point),
        "completeness_proof": {
            "schema": "openadapt.native-value-completeness/v3",
            "method": "uia-value+wm-gettext",
            "hwnd": managed_before["hwnd"],
            "pid": managed_before["pid"],
            "thread": proof["thread"],
            "utf16_units": len(expected.encode("utf-16-le")) // 2,
            "sha256_utf16le": _sha(expected.encode("utf-16-le")),
            "native_reads": 2,
            "uia_reads": 2,
        },
    }
    assert backend.text_value_at(*point) == expected
    assert backend.focused_text_value() == expected
    wire = [
        event for event in transport.events if event["path"] == "/uia/editable-value"
    ][-3:]
    assert len(wire) == 3 and all(event["response"] == observed for event in wire)
    managed_after = _stored_snapshot(root, aid, expected)
    assert managed_before == managed_after
    return {
        "case": aid,
        "length": len(expected),
        "value_sha256": _sha(expected.encode()),
        "managed_before": managed_before,
        "managed_after": managed_after,
        "observation": observed,
    }


def test_native_editable_values_and_runtime_type_retry(tmp_path, monkeypatch):
    """Exact values enable only a fully revalidated, zero-input TYPE retry."""
    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.ir import Workflow
    from openadapt_flow.recorder import Recorder
    from openadapt_flow.runtime import Replayer

    evidence: dict[str, Any] = {
        "schema": "openadapt.native-editable-runtime-proof/v1",
        "head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True
        ).strip(),
        "tree": subprocess.check_output(
            ["git", "rev-parse", "HEAD^{tree}"], cwd=REPOSITORY, text=True
        ).strip(),
        "run_id": os.environ.get("GITHUB_RUN_ID"),
        "observations": [],
        "runtime": [],
    }
    host = _Host(tmp_path)
    app = tmp_path / "patient_notes.ps1"
    source = app.read_text()
    assert source.count("[void]$form.ShowDialog()") == 1
    app.write_text(
        source.replace(
            "[void]$form.ShowDialog()", OBSERVATION_HOOK + "\n[void]$form.ShowDialog()"
        )
    )
    evidence["fixture_sha256"] = _sha(app.read_bytes())
    try:
        backend = host.start_agent()
        evidence["capabilities"] = sorted(backend.agent_capabilities())
        assert "native_editable_value_v2" in evidence["capabilities"]
        transport = _Transport(backend, monkeypatch, evidence)
        transport.lease_evidence_dir = tmp_path / "runtime-lease-frames"
        host.launch_app()
        points = _points(backend)
        positions = tmp_path / "lease-observation-points.json"
        deadline = time.monotonic() + 15
        while not positions.is_file():
            assert time.monotonic() < deadline, "observation fixture did not start"
            time.sleep(0.05)
        extras = json.loads(positions.read_text(encoding="utf-8-sig"))
        baseline = evidence["rows_before"] = host.rows()
        target = next(row for row in baseline if row["id"] == 1)
        header = f"Patient: {target['first']} {target['last']} ({target['dob']})"
        backend.click(*points["searchBox"])
        backend.type_text(target["first"])
        backend.press("enter")
        _wait_label(backend, "patientHeader", header)
        evidence["observations"].append(
            _editable_positive(
                backend, transport, tmp_path, "noteBox", points["noteBox"], ""
            )
        )
        exact = "  exact café value  "
        backend.type_text(exact)
        _wait_value(backend, "noteBox", exact)
        evidence["observations"].append(
            _editable_positive(
                backend, transport, tmp_path, "noteBox", points["noteBox"], exact
            )
        )
        initial_values = tmp_path / "lease-stored-initial.json"
        deadline = time.monotonic() + 10
        while not initial_values.is_file():
            assert time.monotonic() < deadline, "managed initial snapshot unavailable"
            time.sleep(0.05)
        evidence["managed_initial"] = json.loads(
            initial_values.read_text(encoding="utf-8-sig")
        )
        unavailable_reasons = {
            "leaseLong-140": "provider_native_value_disagreement",
            "leasePassword-100": "password_or_password_status_unavailable",
            "leaseOversized-180": "value_exceeds_limit",
            "leaseReadOnly-220": "read_only_or_read_only_status_unavailable",
            "leaseDuplicate-260": "focused_target_unavailable_or_ambiguous",
            "button": "not_editable_value_control",
        }
        for name, reason in unavailable_reasons.items():
            point = extras[name]
            backend.click(*point)
            stored = None
            if name in {"leaseLong-140", "leaseOversized-180"}:
                aid = "leaseLong" if name == "leaseLong-140" else "leaseOversized"
                expected = (
                    "x" * 8192 + "-END" if name == "leaseLong-140" else "x" * 65537
                )
                stored = _stored_snapshot(tmp_path, aid, expected)
                assert stored == evidence["managed_initial"][aid]
            assert backend.editable_value_observation(*point) is None
            assert backend.text_value_at(*point) is None
            assert backend.focused_text_value() is None
            wire = [
                event
                for event in transport.events
                if event["path"] == "/uia/editable-value"
            ][-3:]
            assert len(wire) == 3
            for event in wire:
                raw = event["response"]
                assert event["http_status"] == 200
                assert set(raw) == {"status", "complete", "reason"}
                assert raw["status"] == "unavailable" and raw["complete"] is False
                assert raw["reason"] == reason
            observation = {"case": name, "unavailable": wire}
            if stored is not None:
                stored_after = _stored_snapshot(tmp_path, aid, expected)
                assert stored == stored_after
                observation.update(managed_before=stored, managed_after=stored_after)
            evidence["observations"].append(observation)
        assert host.rows() == baseline

        # This is a real observed demonstration. The operator-selected record
        # header comes from its actual UIA bounds on the captured before frame.
        _fixture_command(tmp_path, "clear-note")
        header_handle = backend.locate_structural(
            StructuralLocator(automation_id="patientHeader")
        )
        assert header_handle is not None
        assert _read_label(backend, "patientHeader") == header
        text = "lease runtime"
        recorder = Recorder(backend, tmp_path / "runtime-recording")
        before_png = backend.screenshot()
        before_focus = backend.focus_identity()
        assert before_focus is not None
        backend.type_text(text)
        _wait_value(backend, "noteBox", text)
        after_focus = backend.focus_identity()
        recorder.record_observed(
            {
                "kind": "type",
                "text": text,
                "focus_before": before_focus,
                "focus_after": after_focus,
                "identifier_region": list(header_handle.region),
            },
            before_png=before_png,
            after_png=backend.screenshot(),
            structural_before={},
        )
        recording = recorder.finish()
        bundle = tmp_path / "runtime-bundle"
        workflow = compile_recording(recording, bundle, name="native-editable-runtime")
        assert len(workflow.steps) == 1
        step = workflow.steps[0]
        assert step.action.value == "type" and step.risk == "reversible"
        assert (
            step.anchor is not None
            and step.identity_armed
            and step.anchor.identifier_crop
        )
        assert step.anchor.structural.automation_id == "noteBox"
        assert not step.effects and step.api_binding is None

        for outcome in ("success", "uncertain"):
            _fixture_command(tmp_path, "clear-note")
            assert backend.text_value_at(*points["noteBox"]) == ""
            assert backend.focused_text_value() == ""
            assert (
                _read_label(backend, "patientHeader") == header
                and host.rows() == baseline
            )
            case: dict[str, Any] = {"case": outcome, "planted": []}
            evidence["runtime"].append(case)
            start = len(transport.events)

            def repaint_once(request):
                if case["planted"]:
                    return None
                expected = request["expected_frame_sha256"]
                ack = _fixture_command(tmp_path, "paint-" + outcome)
                assert ack["note_value"] == ""
                planted_png = backend.screenshot()
                assert _sha(planted_png) != expected
                destination = tmp_path / f"runtime-{outcome}-planted.png"
                destination.write_bytes(planted_png)
                planted = {
                    "ack": ack,
                    "frame_sha256": _sha(planted_png),
                    "frame": str(destination),
                }
                case["planted"].append(planted)
                return planted

            transport.before_keyboard_input = repaint_once
            transport.drop_next_ack = outcome == "uncertain"
            run_dir = tmp_path / f"runtime-{outcome}"
            report = Replayer(backend, use_structural=True).run(
                Workflow.load(bundle),
                bundle_dir=bundle,
                run_dir=run_dir,
            )
            transport.before_keyboard_input = None
            case["report"] = report.model_dump(mode="json")
            assert len(report.results) == 1
            result = report.results[0]
            events = transport.events[start:]
            inputs = [event for event in events if event["path"] == "/keyboard/input"]
            case["input_attempts"] = inputs
            # Retain genuine focus acknowledgement as non-authorizing raw
            # transport evidence, separately from the current keyboard result.
            preparation = [
                event
                for event in events
                if event["path"] == "/uia/act"
                and event["response"].get("receipt", {}).get("operation") == "uia_focus"
            ]
            assert len(preparation) == 1
            case["preparation_events"] = preparation
            assert 2 <= len(inputs) <= 3
            assert inputs[0]["http_status"] == 409
            assert inputs[0]["response"]["reason"] == "stale_frame"
            assert inputs[0]["response"]["no_keyboard_input_delivered"] is True
            assert inputs[0]["planted_fixture_change"] == case["planted"][0]
            accepted = [event for event in inputs if event["http_status"] == 200]
            assert len(accepted) == 1 and inputs[-1] is accepted[0]
            for refused in inputs[:-1]:
                assert refused["response"]["no_keyboard_input_delivered"] is True
            assert result.fresh_actuation_events
            assert all(
                event.phase == "keyboard" and event.retried
                for event in result.fresh_actuation_events
            )
            actual = accepted[0]
            receipt = ActionDeliveryReceipt.model_validate(
                actual["response"]["receipt"]
            )
            assert receipt.operation == "physical_type_text" and receipt.native is False
            assert backend.text_value_at(*points["noteBox"]) == text
            assert backend.focused_text_value() == text
            assert (
                _read_label(backend, "patientHeader") == header
                and host.rows() == baseline
            )
            assert result.before_png is not None
            retained = run_dir / result.before_png
            assert retained.is_file()
            assert (
                _sha(retained.read_bytes())
                == actual["request"]["expected_frame_sha256"]
            )
            frames = [
                event
                for event in transport.events
                if event["path"] == "/keyboard/lease"
                and event.get("retained_frame_sha256")
                == actual["request"]["expected_frame_sha256"]
            ]
            assert (
                frames
                and Path(frames[-1]["retained_frame"]).read_bytes()
                == retained.read_bytes()
            )
            rejected_sha = inputs[0]["request"]["expected_frame_sha256"]
            assert any(
                event.get("retained_frame_sha256") == rejected_sha
                and Path(event["retained_frame"]).is_file()
                for event in events
                if event["path"] == "/keyboard/lease"
            )
            case["retained_attempt_frame_sha256"] = _sha(retained.read_bytes())
            case["before_png"] = str(retained)
            case["rows_after"] = host.rows()
            if outcome == "success":
                assert report.success is True, result.error
                assert result.delivery_receipt == receipt
                assert result.delivery_uncertainty is None
            else:
                assert report.success is False
                assert result.delivery_uncertainty is not None
                assert result.delivery_uncertainty.operation == "physical_type_text"
                assert result.delivery_uncertainty.retried is False
                assert result.delivery_receipt is None
                assert actual["injected_fault"] == "discard_actual_acknowledgement"
        evidence["passed"] = True
    finally:
        try:
            evidence["rows_after"] = (
                host.rows() if (tmp_path / "patients.db").exists() else None
            )
        except Exception as exc:
            evidence["final_oracle_error"] = type(exc).__name__
        fixture_process = host.app
        try:
            host.close()
        finally:
            evidence["agent_terminated"] = (
                host.agent is None or host.agent.poll() is not None
            )
            evidence["fixture_terminated"] = (
                fixture_process is None or fixture_process.poll() is not None
            )
            for destination in (
                tmp_path / "editable-value-evidence.json",
                Path(
                    os.environ.get(
                        "OAFLOW_NATIVE_EDITABLE_VALUE_EVIDENCE",
                        tmp_path / "published-editable-evidence.json",
                    )
                ),
            ):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(json.dumps(evidence, indent=2) + "\n")
