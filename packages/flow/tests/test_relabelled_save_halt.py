"""A relabelled Save behind a scroll halts on the Save, ready for repair (#1331).

Synthetic fixture shaped like the openIMIS claim program: a key press and an
unlabelled picker click whose risk the compiler could not classify, then a
closed-loop scroll back up to an icon-only Save. The vendor relabels the Save,
so nothing on the deterministic ladder finds it.

* The halt names the Save, not the scroll that was looking for it, and no
  input reaches the Save.
* While the key press and the picker click are unreviewed, a write may have
  happened, so the run ends RECONCILIATION_REQUIRED and self-repair refuses.
* Once a person reviews both as reversible (``qualify set-risk``), the same
  halt is HALTED_BEFORE_EFFECT and self-repair takes the Save.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openadapt_flow.ir import ActionKind, Anchor, Step, Workflow
from openadapt_flow.qualification import (
    ActionRiskClass,
    ActionRiskClassification,
    EnvironmentBoundary,
    init_project,
    set_action_classification,
)
from openadapt_flow.repair.self_repair import _not_before_effect, _repairable
from openadapt_flow.runtime.replayer import Replayer
from openadapt_flow.transaction import (
    TransactionOutcome,
    classify_transaction_outcome,
    steps_requiring_reconciliation,
)
from tests.test_replayer import FakeBackend, FakeVision, Match, make_png

PICKER_PNG = make_png((40, 30))
SAVE_PNG = make_png((24, 24))
PICKER = Match(point=(210, 305), region=(190, 290, 40, 30), confidence=0.95)


class RelabelledSaveVision(FakeVision):
    """The picker is where it was; the recorded Save is on no frame."""

    def find_template(self, screen_png, template_png, **kwargs):
        self.template_calls.append(kwargs.get("search_region"))
        return PICKER if template_png == PICKER_PNG else None


def _claim_program() -> Workflow:
    return Workflow(
        name="claim",
        steps=[
            Step(
                id="step_002",
                intent="press Tab to look up the insurance number",
                action=ActionKind.KEY,
                key="Tab",
                risk_review_required=True,
                risk_explanation="special key may navigate, edit, or invoke a shortcut",
            ),
            Step(
                id="step_012",
                intent="click the service picker",
                action=ActionKind.CLICK,
                anchor=Anchor(
                    template="templates/picker.png",
                    region=(190, 290, 40, 30),
                    click_point=(210, 305),
                ),
                risk_review_required=True,
                risk_explanation="unlabelled pointer control",
            ),
            Step(
                id="step_014",
                intent="scroll by (0, -600)",
                action=ActionKind.SCROLL,
                scroll_dx=0,
                scroll_dy=-600,
            ),
            Step(
                id="step_015",
                intent="click the icon-only Save",
                action=ActionKind.CLICK,
                anchor=Anchor(
                    template="templates/save.png",
                    region=(600, 40, 24, 24),
                    click_point=(612, 52),
                ),
                risk="irreversible",
                timeout_s=0.2,
            ),
        ],
    )


def _review_as_reversible(workflow: Workflow) -> None:
    init_project(
        workflow,
        environment=EnvironmentBoundary(
            target_kind="web",
            application="synthetic-claims",
            application_version="1",
            environment_digest="0" * 64,
            runtime_version="test",
        ),
    )
    for step_id in ("step_002", "step_012"):
        set_action_classification(
            workflow,
            ActionRiskClassification(
                step_id=step_id,
                classification=ActionRiskClass.STATE_CHANGING,
                explanation="moves focus or opens a list; saves nothing",
                operator_confirmed=True,
            ),
        )


@pytest.fixture()
def bundle(tmp_path) -> Path:
    bundle_dir = tmp_path / "bundle"
    (bundle_dir / "templates").mkdir(parents=True)
    (bundle_dir / "templates" / "picker.png").write_bytes(PICKER_PNG)
    (bundle_dir / "templates" / "save.png").write_bytes(SAVE_PNG)
    return bundle_dir


def _run(workflow: Workflow, bundle: Path, run_dir: Path):
    backend = FakeBackend()
    report = Replayer(backend, vision=RelabelledSaveVision(), poll_interval_s=0.01).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )
    return backend, report


def test_the_halt_names_the_save_not_the_scroll(bundle, tmp_path) -> None:
    backend, report = _run(_claim_program(), bundle, tmp_path / "run")

    assert report.success is False
    assert report.halt is not None and report.halt.state_id == "step_015"
    by_id = {r.step_id: r for r in report.results}
    assert by_id["step_014"].ok
    save = by_id["step_015"]
    assert save.delivery_attempted is False
    assert save.resolution is None
    assert save.error.startswith("Could not resolve target for step 'step_015'")
    assert "step_014" in save.error
    # Tab, the picker, two upward wheel edges (2.5x budget), and no Save click.
    assert backend.actions == [
        ("press", "Tab"),
        ("click", 210, 305, False),
        ("scroll", 0, -600),
        ("scroll", 0, -600),
    ]


def test_unreviewed_risk_keeps_the_run_in_reconciliation(bundle, tmp_path) -> None:
    workflow = _claim_program()
    _, report = _run(workflow, bundle, tmp_path / "run")

    # A key press or click whose risk nobody reviewed may have written, and no
    # verifier read says it did not: fail closed.
    assert (
        classify_transaction_outcome(report)
        is TransactionOutcome.RECONCILIATION_REQUIRED
    )
    assert steps_requiring_reconciliation(report) == ["step_002", "step_012"]
    # Self-repair refuses, and says which steps and who can settle them.
    report.transaction_outcome = "RECONCILIATION_REQUIRED"
    reason = _not_before_effect(report)
    assert "effect not settled at step_002, step_012" in reason
    assert "qualify set-risk" in reason


def test_reviewed_risk_makes_the_halt_repairable(bundle, tmp_path) -> None:
    workflow = _claim_program()
    _review_as_reversible(workflow)
    _, report = _run(workflow, bundle, tmp_path / "run")

    assert report.halt is not None and report.halt.state_id == "step_015"
    assert (
        classify_transaction_outcome(report) is TransactionOutcome.HALTED_BEFORE_EFFECT
    )
    assert steps_requiring_reconciliation(report) == []
    found = _repairable(report, workflow)
    assert not isinstance(found, str), found
    step, index, halted = found
    assert (step.id, index, halted.step_id) == ("step_015", 3, "step_015")
