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
) -> RunReport:
    return RunReport(
        workflow_name="notes",
        started_at="2026-11-03T00:00:00Z",
        results=[
            StepResult(
                step_id="search",
                intent="click 'Search'",
                ok=search_ok,
                delivery_attempted=True,
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

    def owned_window_over(self, region) -> Optional[dict]:
        self.regions.append(tuple(region))
        if isinstance(self.covered, Exception):
            raise self.covered
        return {"kind": "dialog", "title": "Session notice"} if self.covered else None


def test_a_covered_band_is_repaired_after_the_previous_click() -> None:
    backend = Backend()
    found = _covered_band_halt(_report(), _workflow(), backend)
    assert found is not None
    step, mode, result = found
    assert (step.id, mode, result.step_id) == ("search", "after", "search")
    assert backend.regions == [BAND]


def test_the_band_moves_with_the_resolved_point() -> None:
    backend = Backend()
    _covered_band_halt(_report(point=(310, 345)), _workflow(), backend)
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
    assert _covered_band_halt(report, workflow, backend) is None


def test_an_observed_effect_is_never_reclassified() -> None:
    from openadapt_flow.ir import EffectVerificationEvidence

    report = _report()
    report.results[0].effect_evidence = [
        EffectVerificationEvidence.model_construct(observed_effect="present")
    ]
    assert _covered_band_halt(report, _workflow(), Backend()) is None


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
    assert _covered_band_halt(report, _workflow(), Backend()) is None


def test_no_template_is_doubt() -> None:
    workflow = _workflow()
    workflow.steps[1].anchor.identity_template = None
    assert _covered_band_halt(_report(), workflow, Backend()) is None
