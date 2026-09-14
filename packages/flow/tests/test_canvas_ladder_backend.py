"""Fast contracts for the no-DOM canvas backend's remote actuation lease."""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path

import pytest
from PIL import Image

from openadapt_flow.backend import (
    PreparedPointerActuationBackend,
    RemoteActuationBackend,
)

HARNESS = (
    Path(__file__).resolve().parents[1]
    / "benchmark"
    / "canvas_ladder"
    / "run_canvas_ladder_qualification.py"
)


def _module():
    spec = importlib.util.spec_from_file_location("canvas_ladder_backend", HARNESS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _png(color: tuple[int, int, int]) -> bytes:
    image = Image.new("RGB", (1280, 800), color)
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


class _Mouse:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    def move(self, x, y) -> None:
        self.events.append(("move", x, y))

    def click(self, x, y, *, click_count=1) -> None:
        self.events.append(("click", x, y, click_count))

    def down(self, *, button, click_count) -> None:
        self.events.append(("down", button, click_count))

    def up(self, *, button, click_count) -> None:
        self.events.append(("up", button, click_count))


class _Keyboard:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    def type(self, text, *, delay) -> None:
        self.events.append(("type", text, delay))

    def press(self, key) -> None:
        self.events.append(("press", key))


class _Canvas:
    def __init__(self) -> None:
        self.png = _png((245, 246, 248))

    def bounding_box(self):
        return {"x": 12.0, "y": 18.0, "width": 1280.0, "height": 800.0}

    def screenshot(self):
        return self.png


class _Locator:
    def __init__(self, canvas) -> None:
        self.first = canvas


class _Page:
    def __init__(self) -> None:
        self.canvas = _Canvas()
        self.mouse = _Mouse()
        self.keyboard = _Keyboard()

    def locator(self, selector):
        assert selector == "canvas"
        return _Locator(self.canvas)

    def wait_for_timeout(self, milliseconds) -> None:
        del milliseconds


def test_canvas_backend_binds_fresh_frame_to_prepared_click():
    module = _module()
    page = _Page()
    backend = module.CanvasBrowserBackend(page)

    assert isinstance(backend, RemoteActuationBackend)
    assert isinstance(backend, PreparedPointerActuationBackend)

    backend.prepare_pointer_actuation(*module.SAVE_BUTTON)
    backend.acquire_actuation_frame()
    backend.click(*module.SAVE_BUTTON)

    assert page.mouse.events == [
        ("move", 922.0, 666.0),
        ("down", "left", 1),
        ("up", "left", 1),
    ]


def test_canvas_backend_refuses_changed_frame_before_input():
    module = _module()
    page = _Page()
    backend = module.CanvasBrowserBackend(page)

    backend.prepare_pointer_actuation(*module.SAVE_BUTTON)
    backend.acquire_actuation_frame()
    page.canvas.png = _png((20, 30, 40))

    with pytest.raises(RuntimeError, match="content changed"):
        backend.click(*module.SAVE_BUTTON)

    assert not any(event[0] in {"down", "up", "click"} for event in page.mouse.events)


def test_drift_wrapper_preserves_remote_actuation_protocol():
    module = _module()
    backend = module._DriftBackend.moderate(module.CanvasBrowserBackend(_Page()))

    assert isinstance(backend, RemoteActuationBackend)
    assert isinstance(backend, PreparedPointerActuationBackend)


def test_canvas_backend_maps_portable_select_all_to_remote_control():
    module = _module()
    page = _Page()

    module.CanvasBrowserBackend(page).press("ControlOrMeta+a")

    assert page.keyboard.events == [("press", "Control+a")]


def _run_qualification_reports(module, tmp_path, monkeypatch, reports, saved_notes):
    from contextlib import nullcontext
    from unittest.mock import Mock

    reports = iter(reports)
    saved_notes = iter(saved_notes)
    monkeypatch.setattr(
        "playwright.sync_api.sync_playwright", lambda: nullcontext(None)
    )
    monkeypatch.setattr("openadapt_flow.recorder.Recorder", Mock())
    monkeypatch.setattr("openadapt_flow.compiler.compile_recording", Mock())
    monkeypatch.setattr(
        "openadapt_flow.runtime.replayer.Replayer",
        lambda *args, **kwargs: Mock(run=lambda *args, **kwargs: next(reports)),
    )
    monkeypatch.setattr(module, "_reset_kiosk", lambda *args: None)
    monkeypatch.setattr(module, "_read_saved_note", lambda *args: next(saved_notes))
    monkeypatch.setattr(module, "_new_page", lambda *args: (Mock(), None, Mock()))
    return module.run_qualification(
        "unused-test-container", out_dir=tmp_path, base_url="unused", port=0
    )


def test_moderate_over_halt_retains_native_reason_without_report_payloads(
    tmp_path, monkeypatch
):
    """The scheduled result must diagnose a refusal without exporting text."""
    import json

    from openadapt_flow.ir import (
        IdentityCheck,
        Resolution,
        RunReport,
        SafetyRefusalEvidence,
        StepResult,
    )

    module = _module()
    secret = "SECRET-CANARY-DO-NOT-EXPORT" * 1000
    healthy = RunReport(
        workflow_name=secret,
        started_at="2026-09-14T00:00:00Z",
        success=True,
        rung_counts={"template": 2, "ocr": 1},
        results=[StepResult(step_id=secret, intent=secret, ok=True)],
    )
    moderate = RunReport(
        workflow_name=secret,
        started_at="2026-09-14T00:00:00Z",
        execution_outcome="HALTED",
        transaction_outcome="HALTED_BEFORE_EFFECT",
        params={"note": secret},
        results=[
            StepResult(step_id=secret, intent=secret, ok=True),
            StepResult(
                step_id=secret,
                intent=secret,
                ok=False,
                error=secret,
                before_png=secret,
                after_png=secret,
                failure_category="governed_refusal",
                delivery_attempted=False,
                resolution=Resolution(
                    rung="ocr", point=(910, 648), confidence=1.0, elapsed_ms=1.0
                ),
                identity=IdentityCheck(
                    status="unreadable",
                    mode="context",
                    coverage=0.24,
                    expected=secret,
                    observed=secret,
                    param=secret,
                ),
                safety_refusal_evidence=SafetyRefusalEvidence(
                    stage="identity_verification",
                    code="identity_unverifiable",
                    detector_input_sha256="a" * 64,
                ),
            ),
        ],
    )
    severe = moderate.model_copy(deep=True)
    evidence = _run_qualification_reports(
        module,
        tmp_path,
        monkeypatch,
        [healthy, moderate, severe],
        [f"{module.EXPECTED_MRN}\t{module.NOTE_VALUE}", None, None],
    )
    trial = evidence["trials"][1]
    assert trial["failure_class"] == "moderate_drift_over_halt"
    assert trial["passed"] is False
    assert evidence["accepted"] is False
    assert evidence["successes"] == 2
    diagnostic = trial["native_diagnostics"]
    assert diagnostic["transaction_outcome"] == "HALTED_BEFORE_EFFECT"
    failed = diagnostic["first_failed_step"]
    assert failed["result_index"] == 2
    assert failed["refusal_stage"] == "identity_verification"
    assert failed["refusal_code"] == "identity_unverifiable"
    assert failed["delivery_attempted"] is False
    assert failed["resolution"] == {"rung": "ocr", "confidence": 1.0}
    assert failed["identity"] == {
        "status": "unreadable",
        "mode": "context",
        "coverage": 0.24,
    }
    encoded = json.dumps(evidence, allow_nan=False)
    assert "SECRET-CANARY" not in encoded
    assert len(json.dumps(diagnostic)) < 4096
    assert all("native_diagnostics" in row for row in evidence["trials"])
    # No frame, native report or path sweep is part of this projection.
    assert list(tmp_path.rglob("*")) == [tmp_path / "work"]


def test_native_diagnostics_bounds_the_scan_and_omits_nonfinite_metrics():
    import json

    from openadapt_flow.ir import IdentityCheck, RunReport, StepResult

    module = _module()
    result = StepResult(
        step_id="private-step",
        intent="private-intent",
        ok=False,
        identity=IdentityCheck(status="abstain", coverage=float("nan")),
    )
    report = RunReport(
        workflow_name="private-name",
        started_at="2026-09-14T00:00:00Z",
        results=[result] * 1000,
    )
    diagnostic = module._native_diagnostics(report)
    assert diagnostic["examined_step_count"] == 64
    assert diagnostic["step_scan_truncated"] is True
    assert diagnostic["first_failed_step"]["refusal_code"] is None
    assert diagnostic["first_failed_step"]["identity"]["coverage"] is None
    assert len(json.dumps(diagnostic, allow_nan=False)) < 4096


@pytest.mark.parametrize("saved_note", [None, "", "WRONG-MRN\twrong note", "expected"])
def test_severe_halt_rejects_every_persisted_note(tmp_path, monkeypatch, saved_note):
    from openadapt_flow.ir import RunReport, StepResult

    module = _module()
    expected = f"{module.EXPECTED_MRN}\t{module.NOTE_VALUE}"
    if saved_note == "expected":
        saved_note = expected
    healthy = RunReport(
        workflow_name="fixture",
        started_at="2026-09-14T00:00:00Z",
        success=True,
        rung_counts={"template": 2, "ocr": 1},
        results=[StepResult(step_id="fixture-step", intent="fixture", ok=True)],
    )
    halted = RunReport(
        workflow_name="fixture",
        started_at="2026-09-14T00:00:00Z",
        execution_outcome="HALTED",
        transaction_outcome="HALTED_BEFORE_EFFECT",
        results=[StepResult(step_id="fixture-step", intent="fixture", ok=False)],
    )
    evidence = _run_qualification_reports(
        module,
        tmp_path,
        monkeypatch,
        [healthy, healthy, halted],
        [expected, expected, saved_note],
    )
    severe = evidence["trials"][2]
    absent = saved_note is None
    assert severe["effect_after_drift"] == saved_note
    assert severe["silent_write"] is not absent
    assert severe["passed"] is absent
    assert severe["failure_class"] == (None if absent else "drift_not_safely_halted")
    assert evidence["accepted"] is absent


@pytest.mark.parametrize("content", [None, b"", b"\n", b"\t\n", b"wrong note\n"])
def test_saved_note_probe_distinguishes_absence_from_existing_rows(
    tmp_path, monkeypatch, content
):
    module = _module()
    note = tmp_path / "saved-note.txt"
    if content is not None:
        note.write_bytes(content)
    real_run = module.subprocess.run

    def local_probe(command, **kwargs):
        assert command[:4] == ["docker", "exec", "fixture", "python3"]
        assert command[-1] == module.SAVE_PATH
        return real_run([sys.executable, "-S", *command[4:-1], str(note)], **kwargs)

    monkeypatch.setattr(module.subprocess, "run", local_probe)
    observed = module._read_saved_note("fixture")
    assert observed == (None if content is None else content.decode().strip())


@pytest.mark.parametrize("returncode", [1, 125, 126, 127, 137])
def test_saved_note_probe_failure_cannot_prove_no_write(monkeypatch, returncode):
    from subprocess import CompletedProcess

    module = _module()
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: CompletedProcess(
            args=args, returncode=returncode, stdout=b"", stderr=b"private detail"
        ),
    )
    with pytest.raises(
        RuntimeError, match=rf"^Canvas saved-note probe failed \(exit {returncode}\)$"
    ):
        module._read_saved_note("fixture")
