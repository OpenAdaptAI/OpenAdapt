"""An identity halt caused by the app's own window over the band (#1724).

Rule tests for ``self_repair._covered_band_halt`` over a small linear
workflow; the backend is a stand-in whose ``owned_window_over`` says whether
the app's own window covers a region.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest

from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    IdentityCheck,
    Resolution,
    RunReport,
    Step,
    StepResult,
    Workflow,
)
from openadapt_flow.repair.self_repair import _covered_band_halt
from openadapt_flow.runtime.effects import Effect, EffectKind
from openadapt_flow.runtime.identity_template import build_identity_template

BAND = (200, 300, 220, 30)
RECORDED = "Patient: Neil Sorenson (1984-03-12)"
#: What the identity check read with the session notice over the band's right.
READ_BESIDE_DIALOG = "Patient: Neil Sorenson (1984-"
TEMPLATE = build_identity_template(RECORDED)


def _halt(report, workflow, backend, recorded=RECORDED):
    return _covered_band_halt(
        report, workflow, backend, recorded_band=lambda step: recorded
    )


def _anchor(click=(100, 100), band=BAND) -> Anchor:
    return Anchor(
        template="templates/t.png",
        region=(click[0] - 20, click[1] - 10, 40, 20),
        click_point=click,
        identity_template=TEMPLATE,
        identifier_region=band,
    )


def _workflow(*, search_saves: bool = False) -> Workflow:
    return Workflow(
        name="notes",
        steps=[
            Step(
                id="search",
                intent="click 'Search'",
                action=ActionKind.CLICK,
                anchor=_anchor((500, 50)),
                risk="irreversible" if search_saves else "reversible",
            ),
            Step(
                id="note",
                intent="click the note field",
                action=ActionKind.CLICK,
                anchor=_anchor((300, 350)),
                risk="reversible",
            ),
            Step(
                id="save",
                intent="click 'Save Note'",
                action=ActionKind.CLICK,
                anchor=_anchor((100, 450)),
                risk="irreversible",
                effects=[Effect(kind=EffectKind.RECORD_WRITTEN, match={"id": "1"})],
            ),
        ],
    )


def _structural(point=(300, 350)) -> Resolution:
    return Resolution(rung="structural", point=point, confidence=1.0, elapsed_ms=1)


def _report(
    *,
    identity: str = "mismatch",
    halted_delivered: bool = False,
    search_rung: str = "structural",
    search_ok: bool = True,
    point=(300, 350),
    observed: str = READ_BESIDE_DIALOG,
    search_verified: bool = True,
    outcome: str = "REJECTED_POLICY",
) -> RunReport:
    from openadapt_flow.ir import HaltObservation

    return RunReport(
        workflow_name="notes",
        started_at="2026-11-03T00:00:00Z",
        halt=HaltObservation(state_id="note", reason="Identity check failed"),
        transaction_outcome=outcome,
        results=[
            StepResult(
                step_id="search",
                intent="click 'Search'",
                ok=search_ok,
                delivery_attempted=True,
                identity=IdentityCheck(status="verified", mode="context")
                if search_verified
                else None,
                resolution=Resolution(
                    rung=search_rung, point=(500, 50), confidence=1.0, elapsed_ms=1
                ),
            ),
            StepResult(
                step_id="note",
                intent="click the note field",
                ok=False,
                delivery_attempted=halted_delivered,
                resolution=_structural(point),
                identity=IdentityCheck(
                    status=identity, mode="context", observed=observed, coverage=0.62
                ),
                error="Identity check failed",
            ),
        ],
    )


class Backend:
    def __init__(self, covered: Any = True) -> None:
        self.covered = covered
        self.regions: list[tuple] = []
        self.pinned: list[str] = []

    def owned_window_over(self, region, *, pinned_step_id) -> Optional[dict]:
        self.regions.append(tuple(region))
        self.pinned.append(pinned_step_id)
        if isinstance(self.covered, Exception):
            raise self.covered
        return {"kind": "dialog", "title": "Session notice"} if self.covered else None


def test_a_covered_band_is_repaired_after_the_previous_click() -> None:
    backend = Backend()
    found = _halt(_report(), _workflow(), backend)
    assert found is not None
    step, mode, result = found
    assert (step.id, mode, result.step_id) == ("search", "after", "search")
    assert backend.regions == [BAND]
    assert backend.pinned == ["search"]  # the last identity-verified step


def test_no_identity_verified_step_means_no_pinned_window() -> None:
    backend = Backend()
    assert _halt(_report(search_verified=False), _workflow(), backend) is None
    assert backend.regions == []


def test_the_band_moves_with_the_resolved_point() -> None:
    backend = Backend()
    _halt(_report(point=(310, 345)), _workflow(), backend)
    assert backend.regions == [(BAND[0] + 10, BAND[1] - 5, BAND[2], BAND[3])]


@pytest.mark.parametrize(
    "backend, report, workflow",
    [
        (Backend(False), _report(), _workflow()),  # nothing covers the band
        (Backend(RuntimeError("AX")), _report(), _workflow()),  # no proof
        (object(), _report(), _workflow()),  # a backend without the hook
        (Backend(), _report(identity="verified"), _workflow()),
        (Backend(), _report(halted_delivered=True), _workflow()),  # input sent
        (Backend(), _report(search_rung="geometry"), _workflow()),
        (Backend(), _report(search_ok=False), _workflow()),
        (Backend(), _report(), _workflow(search_saves=True)),  # a save took input
    ],
)
def test_anything_else_keeps_its_usual_routing(backend, report, workflow) -> None:
    assert _halt(report, workflow, backend) is None


def test_an_observed_effect_is_never_reclassified() -> None:
    from openadapt_flow.ir import EffectVerificationEvidence

    report = _report()
    report.results[0].effect_evidence = [
        EffectVerificationEvidence.model_construct(observed_effect="present")
    ]
    assert _halt(report, _workflow(), Backend()) is None


@pytest.mark.parametrize(
    "observed",
    [
        "Patient: Nell Sorensen (1986-",  # the look-alike sibling behind it
        "Patient: Maria Alvarez (1979-",  # another patient behind it
        "Patient: Neil Sorensen (1983-",  # a one-letter surname and DOB change
        "Patient: Neil Sorenson Maria",  # a name the record can't explain
        "Patient: Neil",  # too little of the band to tell
        "",  # nothing read
    ],
)
def test_another_patient_behind_the_dialog_stays_an_identity_halt(observed) -> None:
    report = _report(observed=observed)
    assert _halt(report, _workflow(), Backend()) is None


def test_no_template_is_doubt() -> None:
    workflow = _workflow()
    workflow.steps[1].anchor.identity_template = None
    assert _halt(_report(), workflow, Backend()) is None


@pytest.mark.parametrize(
    "observed",
    [
        "Patient: Neil Sorenson (1991-",  # a different birth year shows
        "Patient: Sorenson",  # the first name is not the record's
        "Patient: Neil Sorenson (1984-03-13)",  # a full, different date
        "Neil Sorenson (1984-",  # does not start where the band starts
    ],
)
def test_every_word_read_must_be_the_records(observed) -> None:
    assert _halt(_report(observed=observed), _workflow(), Backend()) is None


def test_a_same_name_patient_with_a_visible_other_dob_is_refused() -> None:
    from openadapt_flow.repair.self_repair import _uncovered_band_matches

    recorded = "Patient: Christopherson, Maximilian DOB 1/2/80 MRN 4471"
    step = _workflow().steps[1]
    step.anchor.identity_template = build_identity_template(recorded)
    assert not _uncovered_band_matches(
        step, "Patient: Christopherson, Maximilian DOB 3/9/91", recorded
    )
    assert _uncovered_band_matches(
        step, "Patient: Christopherson, Maximilian DOB 1/2/8", recorded
    )


def test_no_recorded_band_is_doubt() -> None:
    assert _halt(_report(), _workflow(), Backend(), recorded=None) is None


def test_a_parameterised_band_is_never_reclassified() -> None:
    workflow = _workflow()
    template = workflow.steps[1].anchor.identity_template
    workflow.steps[1].anchor.identity_template = template.model_copy(
        update={"param_token_indices": {"patient": [2]}}
    )
    assert _halt(_report(), workflow, Backend()) is None


@pytest.mark.parametrize(
    "outcome", ["CANCELED", "RECONCILIATION_REQUIRED", "FAILED_PLATFORM", "ROLLED_BACK"]
)
def test_only_an_identity_refusal_before_effect_qualifies(outcome) -> None:
    assert _halt(_report(outcome=outcome), _workflow(), Backend()) is None


def test_a_structured_identity_mismatch_is_a_real_mismatch() -> None:
    report = _report()
    report.results[1].identity = report.results[1].identity.model_copy(
        update={"mode": "structured"}
    )
    assert _halt(report, _workflow(), Backend()) is None


def test_uncertain_delivery_is_never_reclassified() -> None:
    from openadapt_flow.ir import ActionDeliveryUncertainty

    report = _report()
    report.results[0].delivery_uncertainty = ActionDeliveryUncertainty.model_construct(
        operation="click"
    )
    assert _halt(report, _workflow(), Backend()) is None


def test_the_recorded_band_comes_from_the_crop_and_must_meet_the_template(tmp_path):
    from types import SimpleNamespace

    from openadapt_flow.repair.self_repair import _recorded_band_text

    workflow = _workflow()
    step = workflow.steps[1]
    step.anchor.identifier_crop = "templates/identifiers/note.png"
    crop = tmp_path / step.anchor.identifier_crop
    crop.parent.mkdir(parents=True)
    crop.write_bytes(b"png")

    def vision(text):
        line = SimpleNamespace(text=text, region=(0, 0, 10, 10))
        return SimpleNamespace(ocr=lambda png: [line])

    assert _recorded_band_text(workflow, tmp_path, step, vision(RECORDED)) == RECORDED
    assert (
        _recorded_band_text(workflow, tmp_path, step, vision("Patient: Neil")) is None
    )
    other = vision("Patient: Nell Sorensen (1986-07-09)")
    assert _recorded_band_text(workflow, tmp_path, step, other) is None


@pytest.mark.parametrize(
    "observed",
    [
        "Patient: Neil Sorens",  # the dialog hides where Sorenson/Sorensen differ
        "Patient: Neil Soren",
        "Patient: Neil So",
    ],
)
def test_a_dialog_hiding_the_difference_below_the_floor_stays_a_halt(
    observed,
) -> None:
    assert _halt(_report(observed=observed), _workflow(), Backend()) is None


def test_a_hidden_birth_date_is_reclassified_only_for_the_probe_to_recheck() -> None:
    # The whole name shows and the dialog hides the birth date, so a
    # same-name patient could be behind it. Reclassifying only routes the
    # stop to an ``after`` repair: the probe replays this step with its own
    # identity check on the uncovered band, which refuses another birth date.
    found = _halt(
        _report(observed="Patient: Neil Sorenson (19"), _workflow(), Backend()
    )
    assert found is not None and found[1] == "after"
    assert found[0].id == "search"  # the note step itself is replayed unchanged
