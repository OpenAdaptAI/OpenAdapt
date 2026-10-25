"""The candidate census follows the target's teach-time role, whatever rung
resolved it.

A form field or control recorded at teach time (OpenEMR's "Contact" section
button, resolved by its pixel template) has no look-alike rows to count, so a
verified identity stands before a save. A target recorded as a row, or with no
recorded role, is still counted and halts when it cannot be. A scroll whose
next target is in view but refused by the census stops and says so instead of
spending its budget.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from openadapt_flow.ir import IdentityCheck, StructuralLocator, Workflow
from openadapt_flow.runtime import identity_template as itmpl
from openadapt_flow.runtime.identity_census import teach_time_role
from openadapt_flow.runtime.replayer import Replayer
from openadapt_flow.vision import Match
from tests.test_identity_evidence_capability import (
    _W,
    _button,
    _crop_only_step,
    _gate,
)
from tests.test_replayer import (
    FakeBackend,
    FakeVision,
    OcrLine,
    click_step,
    context_click_step,
    make_png,
    scroll_step,
)


@pytest.fixture()
def bundle(tmp_path: Path) -> Path:
    bundle_dir = tmp_path / "bundle"
    (bundle_dir / "templates").mkdir(parents=True)
    (bundle_dir / "templates" / "btn.png").write_bytes(make_png((50, 20)))
    return bundle_dir


@pytest.fixture()
def run_dir(tmp_path: Path) -> Path:
    return tmp_path / "run"


def _with_role(step, role: str | None):
    assert step.anchor is not None
    step.anchor.structural = (
        StructuralLocator(role=role, name="Contact") if role is not None else None
    )
    return step


@pytest.mark.parametrize("role", ["button", "textbox", "combobox", "checkbox"])
def test_a_pixel_resolved_form_field_before_a_save_verifies(
    tmp_path: Path, role: str
) -> None:
    """The OpenEMR case: a recorded control, verified by its pixel crop."""

    step, res = _crop_only_step(tmp_path, _button())  # irreversible
    _with_role(step, role)
    assert teach_time_role(step) == "control"
    error, check = _gate(tmp_path, step, res, _button(), pixel_verify_enabled=True)
    assert error is None
    assert check is not None and check.status == "verified"
    assert check.census_status == "not_a_row"


@pytest.mark.parametrize("role", [None, "row", "listitem", "link", "gridcell"])
def test_a_pixel_resolved_row_that_cannot_be_counted_still_halts(
    tmp_path: Path, role: str | None
) -> None:
    step, res = _crop_only_step(tmp_path, _button())
    _with_role(step, role)
    assert teach_time_role(step) == "unknown"
    error, check = _gate(tmp_path, step, res, _button(), pixel_verify_enabled=True)
    assert error is not None and "identity_uncounted" in error
    assert check is not None and check.ambiguity_halt == "identity_uncounted"


def test_a_row_recorded_at_teach_time_is_counted_even_with_a_control_role(
    tmp_path: Path,
) -> None:
    """A row's own button: teach time saw the row, so the role does not exempt it."""

    step, res = _crop_only_step(tmp_path, _button())
    _with_role(step, "button")
    step.anchor.identity_template = itmpl.build_identity_template(
        None, structured_identity="Open", structured_row=True
    )
    assert teach_time_role(step) == "row"
    error, check = _gate(tmp_path, step, res, _button(), pixel_verify_enabled=True)
    assert error is not None
    assert check is not None and check.status != "verified"


def test_a_row_with_ocr_look_alikes_halts(bundle, run_dir) -> None:
    """Two OCR rows read the recorded band: the step stops before the click."""

    band = "Jane Sample Knee pain referral High"
    vision = FakeVision()
    vision.template_results = [
        Match(point=(110, 105), region=(100, 100, 50, 20), confidence=0.99)
    ] * 4
    vision.ocr_lines = [
        OcrLine(band, region=(160, 95, 240, 20)),
        OcrLine(band, region=(160, 195, 240, 20)),
    ]
    step = context_click_step(band, risk="irreversible")
    report = Replayer(FakeBackend(), vision=vision, poll_interval_s=0.01).run(
        Workflow(name="wf", steps=[step]), bundle_dir=bundle, run_dir=run_dir
    )
    result = report.results[0]
    assert report.success is False
    assert result.identity is not None
    assert result.identity.ambiguity_halt == "identical_candidates"
    assert result.delivery_attempted is False


def test_a_scroll_stops_when_the_census_refuses_a_target_in_view(
    bundle, run_dir, monkeypatch
) -> None:
    vision = FakeVision()
    target = Match(point=(110, 105), region=(100, 100, 50, 20), confidence=0.99)
    vision.template_results = [target] * 6
    backend = FakeBackend()
    click = click_step()
    assert click.anchor is not None
    click.anchor.context_text = "Synthetic Contact Address"
    click.identity_armed = True
    workflow = Workflow(name="wf", steps=[scroll_step(), click])
    replayer = Replayer(backend, vision=vision, poll_interval_s=0.01)
    monkeypatch.setattr(
        replayer,
        "_verify_identity",
        lambda *args, **kwargs: IdentityCheck(
            status="abstain",
            mode="pixel",
            ambiguity_halt="identity_uncounted",
        ),
    )
    report = replayer.run(workflow, bundle_dir=bundle, run_dir=run_dir)
    assert report.success is False
    assert len(report.results) == 1
    halted = report.results[0]
    assert halted.safety_halt is True
    assert "is in view" in halted.error and "identity_uncounted" in halted.error
    assert "exhausted" not in halted.error
    assert backend.actions == []  # in view on the first frame: no wheel input


def test_teach_time_role_unknown_without_any_record() -> None:
    step = click_step()
    assert step.anchor is not None
    step.anchor.structural = None
    assert teach_time_role(step) == "unknown"
    assert _W > 0 and np is not None


@pytest.mark.parametrize("role", ["button", "menuitem", "tab", "checkbox"])
def test_a_control_role_never_skips_an_ocr_census_that_can_count(
    bundle, run_dir, role
) -> None:
    """A per-row "Open" button: the role does not exempt OCR look-alikes."""

    band = "Jane Sample Knee pain referral High"
    vision = FakeVision()
    vision.template_results = [
        Match(point=(110, 105), region=(100, 100, 50, 20), confidence=0.99)
    ] * 4
    vision.ocr_lines = [
        OcrLine(band, region=(160, 95, 240, 20)),
        OcrLine(band, region=(160, 195, 240, 20)),
    ]
    step = context_click_step(band, risk="irreversible")
    step.anchor.structural = StructuralLocator(role=role, name="Open")
    backend = FakeBackend()
    report = Replayer(backend, vision=vision, poll_interval_s=0.01).run(
        Workflow(name="wf", steps=[step]), bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is False
    assert backend.actions == []
    assert report.results[0].identity.ambiguity_halt == "identical_candidates"


def test_a_recorded_structured_identity_with_no_teach_census_is_not_a_control() -> None:
    """A backend with no census at teach time: the row text may be a row's."""

    step = click_step()
    _with_role(step, "button")
    step.anchor.identity_template = itmpl.build_identity_template(
        None, structured_identity="John Smith Follow-up Dr Lee"
    )
    assert teach_time_role(step) == "unknown"
