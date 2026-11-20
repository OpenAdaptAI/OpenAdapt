"""Real, authenticated Windows keyboard leases over a synthetic WinForms record.

No qualification driver or fabricated input receipt participates. The field's
UIA value and the app's independent SQLite record decide what changed. The
transport observer delegates every request unchanged; it can discard one real
acknowledgement to test the uncertainty boundary.
"""

from __future__ import annotations

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
        self.events = evidence["transport"] = []

        def post(url, **kwargs):
            started = time.monotonic()
            response = original(url, **kwargs)
            path = url.removeprefix(backend.server_url)
            if path in {"/keyboard/lease", "/keyboard/input"}:
                request = kwargs["json"]
                event = {
                    "path": path,
                    "request": _redacted(request),
                    "http_status": response.status_code,
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                    "response": _redacted(response.json()),
                }
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
    assert result is not None and result["match"] == "unique", result
    assert result["candidate_count"] == 1 and isinstance(result["text"], str)
    return result["text"]


def _values(backend) -> dict[str, str]:
    return {aid: _read(backend, aid) for aid in ("searchBox", "noteBox")}


def _wait_value(backend, automation_id: str, expected: str) -> None:
    deadline = time.monotonic() + 15
    while _read(backend, automation_id) != expected:
        assert time.monotonic() < deadline, (
            f"UIA {automation_id} did not reach {expected!r}"
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
    assert _read(backend, "patientHeader") == header
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
    after = _validate(backend, host, baseline, header, context, expected)
    assert before["target"] == after["target"]
    backend.arm_guarded_keyboard(*after["target"]["point"])
    return _sha(png), after


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
        _wait_value(backend, "patientHeader", header)
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
