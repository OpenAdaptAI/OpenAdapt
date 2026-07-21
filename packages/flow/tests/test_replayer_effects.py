"""Live-local tests for EffectVerifier wiring in the Replayer.

These drive the REAL Replayer against a REAL system of record (the in-process
MockMed ``fault_server``) via the ``RestRecordVerifier`` from PR #63 -- the
same verifier the fault-model study judges by. The backend and vision are
faked (as in ``test_replayer``), but the write lands in the fault server's
ground-truth DB, so the effect check reads the actual record, never the
screen. No model calls, no network beyond localhost -- runs in CI.

The thesis these pin: a step whose SCREEN postcondition PASSES (the app
painted "Saved") is still HALTED when the system of record disagrees
(phantom / duplicate / partial write), and a genuinely correct write is
CONFIRMED and proceeds. A no-effects bundle behaves exactly as before, and a
step that declares effects with no verifier configured HALTS (fail-safe).
"""

from __future__ import annotations

import requests

from openadapt_flow.ir import (
    ActionKind,
    Postcondition,
    PostconditionKind,
    Step,
    Workflow,
)
from openadapt_flow.mockmed.fault_server import serve as fault_serve
from openadapt_flow.runtime.authorization import (
    GovernedRunAuthorization,
    UnverifiedWriteApproval,
    runtime_inputs_digest,
)
from openadapt_flow.runtime.effects import (
    Effect,
    EffectKind,
    RestCompensator,
    RestRecordVerifier,
)
from openadapt_flow.runtime.replayer import Replayer

# Reuse the scripted fakes from the main replayer unit tests (pytest's
# prepend import mode puts tests/ on sys.path).
from tests.test_replayer import FakeBackend, FakeVision, Match

TARGET = {"patient_id": "p1", "type": "Triage"}


class WritingBackend(FakeBackend):
    """A fake backend whose ``press`` writes to the MockMed system of record.

    Models the real app: the (consequential) keypress makes the app POST an
    encounter to its persistence boundary. ``fault`` selects the fault mode
    the server injects; ``posts`` writes more than once to model a duplicate /
    double-delivered submission. Nothing else about replay changes.
    """

    def __init__(self, sor_url, *, fault="", posts=1, viewport=(300, 200)):
        super().__init__(viewport=viewport)
        self.sor_url = sor_url.rstrip("/")
        self.fault = fault
        self.posts = posts

    def press(self, key):
        super().press(key)
        url = f"{self.sor_url}/api/encounter"
        if self.fault:
            url += f"?fault={self.fault}"
        for _ in range(self.posts):
            requests.post(
                url,
                json={"patient_id": "p1", "type": "Triage", "note": "n"},
                timeout=5,
            )


def _fault_server():
    url, db, stop = fault_serve()
    return url.rstrip("/"), db, stop


def _save_workflow(*, effects, risk="reversible"):
    """A one-step workflow: press Enter (with a screen postcondition that
    PASSES) carrying the given system-of-record effects."""
    return Workflow(
        name="save",
        steps=[
            Step(
                id="save",
                intent="save encounter",
                action=ActionKind.KEY,
                key="Enter",
                expect=[
                    Postcondition(
                        kind=PostconditionKind.TEXT_PRESENT,
                        text="Saved",
                        timeout_s=0.2,
                    )
                ],
                risk=risk,
                effects=effects,
            )
        ],
    )


def _vision_that_confirms_saved():
    vision = FakeVision()
    # The screen oracle AGREES the save happened ("Saved" banner is present) --
    # the whole point is that the effect check disagrees against the record.
    vision.text_results = {
        "Saved": Match(point=(50, 10), region=(30, 5, 40, 10), confidence=0.9)
    }
    return vision


def _dirs(tmp_path):
    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True)
    return bundle, tmp_path / "run"


def _authorize_unverified(workflow, bundle):
    workflow.save(bundle)
    workflow = Workflow.load(bundle)
    step = workflow.steps[0]
    authorization = GovernedRunAuthorization(
        bundle_content_digest=workflow.manifest.content_digest,
        runtime_inputs_digest=runtime_inputs_digest(workflow, None, None),
        admitted_policy_name="test",
        unverified_write_approvals=[
            UnverifiedWriteApproval(
                step_id=step.id,
                effect_contract_hashes=[
                    effect.contract_hash() for effect in step.effects
                ],
            )
        ],
    )
    return workflow, authorization


# -- CONFIRMED: correct write proceeds --------------------------------------


def test_effect_confirmed_proceeds(tmp_path):
    url, _db, stop = _fault_server()
    try:
        backend = WritingBackend(url)  # one clean write
        vision = _vision_that_confirms_saved()
        workflow = _save_workflow(
            effects=[
                Effect(
                    kind=EffectKind.RECORD_WRITTEN,
                    match=TARGET,
                    expected_count=1,
                    timeout_s=2.0,
                )
            ]
        )
        bundle, run_dir = _dirs(tmp_path)
        replayer = Replayer(
            backend,
            vision=vision,
            effect_verifier=RestRecordVerifier(url),
            poll_interval_s=0.01,
        )
        report = replayer.run(workflow, bundle_dir=bundle, run_dir=run_dir)

        assert report.success is True
        r = report.results[0]
        assert r.postconditions_ok is True
        assert r.effect_verified is True
        assert any("CONFIRMED" in line for line in r.effect_results)
        # $0 guarantee: effect verification adds no model calls / cost.
        assert report.model_calls == 0
        assert report.est_model_cost_usd == 0.0
    finally:
        stop()


# -- REFUTED: screen says success, record is empty -> HALT ------------------


def test_effect_refuted_halts_despite_green_screen(tmp_path):
    url, db, stop = _fault_server()
    try:
        # Optimistic-UI fault: the app painted "Saved" but the server REJECTED
        # the write, so the record is empty. The screen postcondition passes;
        # the effect check must refute and halt.
        backend = WritingBackend(url, fault="optimistic")
        vision = _vision_that_confirms_saved()
        workflow = _save_workflow(
            effects=[
                Effect(
                    kind=EffectKind.RECORD_WRITTEN,
                    match=TARGET,
                    expected_count=1,
                    timeout_s=2.0,
                )
            ]
        )
        bundle, run_dir = _dirs(tmp_path)
        replayer = Replayer(
            backend,
            vision=vision,
            effect_verifier=RestRecordVerifier(url),
            poll_interval_s=0.01,
        )
        report = replayer.run(workflow, bundle_dir=bundle, run_dir=run_dir)

        assert report.success is False
        r = report.results[0]
        # The SCREEN oracle was satisfied -- the record oracle was not.
        assert r.postconditions_ok is True
        assert r.effect_verified is False
        assert r.ok is False
        assert "refuted" in (r.error or "").lower()
        assert "system of record" in (r.error or "")
        # Nothing landed in the ground-truth store.
        assert db.snapshot()["records"] == []
    finally:
        stop()


# -- REFUTED duplicate on an irreversible effect -> reconcile -> proceed ----


def test_effect_duplicate_irreversible_reconciles_and_proceeds(tmp_path):
    url, db, stop = _fault_server()
    try:
        # Two non-idempotent submissions -> two rows (a double-delivered
        # click). The effect refutes (2 != 1); the compensator deletes the
        # extra and re-verification confirms exactly one -> the run proceeds.
        backend = WritingBackend(url, posts=2)
        vision = _vision_that_confirms_saved()
        workflow = _save_workflow(
            risk="irreversible",
            effects=[
                Effect(
                    kind=EffectKind.RECORD_WRITTEN,
                    match=TARGET,
                    expected_count=1,
                    risk="irreversible",
                    timeout_s=2.0,
                )
            ],
        )
        bundle, run_dir = _dirs(tmp_path)
        replayer = Replayer(
            backend,
            vision=vision,
            effect_verifier=RestRecordVerifier(url),
            effect_compensator=RestCompensator(url),
            poll_interval_s=0.01,
        )
        report = replayer.run(workflow, bundle_dir=bundle, run_dir=run_dir)

        assert report.success is True
        r = report.results[0]
        assert r.effect_verified is True
        assert any("RECONCILED" in line for line in r.effect_results)
        # The system of record now holds exactly one row.
        assert len(db.snapshot()["records"]) == 1
    finally:
        stop()


# -- REFUTED duplicate, NO compensator -> HALT ------------------------------


def test_effect_duplicate_irreversible_without_compensator_halts(tmp_path):
    url, db, stop = _fault_server()
    try:
        backend = WritingBackend(url, posts=2)
        vision = _vision_that_confirms_saved()
        workflow = _save_workflow(
            risk="irreversible",
            effects=[
                Effect(
                    kind=EffectKind.RECORD_WRITTEN,
                    match=TARGET,
                    expected_count=1,
                    risk="irreversible",
                    timeout_s=2.0,
                )
            ],
        )
        bundle, run_dir = _dirs(tmp_path)
        replayer = Replayer(
            backend,
            vision=vision,
            effect_verifier=RestRecordVerifier(url),
            # no compensator configured
            poll_interval_s=0.01,
        )
        report = replayer.run(workflow, bundle_dir=bundle, run_dir=run_dir)

        assert report.success is False
        r = report.results[0]
        assert r.effect_verified is False
        assert "escalated" in (r.error or "").lower()
        # Both duplicate rows remain (nothing was reconciled).
        assert len(db.snapshot()["records"]) == 2
    finally:
        stop()


# -- FAIL-SAFE: effects declared, no verifier configured -> HALT ------------


def test_effects_declared_but_no_verifier_is_config_error_halt(tmp_path):
    url, db, stop = _fault_server()
    try:
        backend = WritingBackend(url)
        vision = _vision_that_confirms_saved()
        workflow = _save_workflow(
            effects=[
                Effect(kind=EffectKind.RECORD_WRITTEN, match=TARGET, expected_count=1)
            ]
        )
        bundle, run_dir = _dirs(tmp_path)
        # NO effect_verifier -> a step that declares effects must HALT before
        # acting (never perform an unverifiable consequential write).
        replayer = Replayer(backend, vision=vision, poll_interval_s=0.01)
        report = replayer.run(workflow, bundle_dir=bundle, run_dir=run_dir)

        assert report.success is False
        r = report.results[0]
        assert r.effect_verified is False
        assert "no EffectVerifier" in (r.error or "")
        # The consequential action was refused -- nothing was written and the
        # keypress never fired.
        assert ("press", "Enter") not in backend.actions
        assert db.snapshot()["records"] == []
    finally:
        stop()


def test_run_bound_approval_executes_gui_write_but_never_marks_it_verified(
    tmp_path,
):
    url, db, stop = _fault_server()
    try:
        backend = WritingBackend(url)
        vision = _vision_that_confirms_saved()
        workflow = _save_workflow(
            effects=[
                Effect(
                    kind=EffectKind.RECORD_WRITTEN,
                    match=TARGET,
                    expected_count=1,
                )
            ]
        )
        bundle, run_dir = _dirs(tmp_path)
        workflow, authorization = _authorize_unverified(workflow, bundle)
        report = Replayer(
            backend,
            vision=vision,
            governed_authorization=authorization,
            poll_interval_s=0.01,
        ).run(workflow, bundle_dir=bundle, run_dir=run_dir)

        assert report.success is True
        result = report.results[0]
        assert result.postconditions_ok is True
        assert result.effect_verified is None
        assert result.effect_approved_unverified is True
        assert any("approved-unverified" in line for line in result.effect_results)
        assert len(result.effect_contract_hashes) == 1
        assert report.approved_unverified_effect_step_ids == ["save"]
        assert len(db.snapshot()["records"]) == 1
    finally:
        stop()


def test_run_bound_approval_digest_mismatch_halts_before_action(tmp_path):
    backend = FakeBackend()
    workflow = _save_workflow(
        effects=[Effect(kind=EffectKind.RECORD_WRITTEN, match=TARGET)]
    )
    bundle, run_dir = _dirs(tmp_path)
    workflow, authorization = _authorize_unverified(workflow, bundle)
    authorization = authorization.model_copy(update={"bundle_content_digest": "0" * 64})

    report = Replayer(
        backend,
        vision=_vision_that_confirms_saved(),
        governed_authorization=authorization,
    ).run(workflow, bundle_dir=bundle, run_dir=run_dir)

    assert report.success is False
    assert report.results[0].step_id == "<authorization>"
    assert "bound to bundle digest" in (report.results[0].error or "")
    assert backend.actions == []


# -- BACK-COMPAT: a no-effects bundle replays unchanged with no verifier ----


def test_no_effects_bundle_replays_unchanged(tmp_path):
    # A step with NO effects and NO verifier configured behaves exactly as
    # before this PR: it acts and passes on the screen postcondition alone.
    backend = FakeBackend()
    vision = _vision_that_confirms_saved()
    workflow = _save_workflow(effects=[])
    bundle, run_dir = _dirs(tmp_path)
    replayer = Replayer(backend, vision=vision, poll_interval_s=0.01)
    report = replayer.run(workflow, bundle_dir=bundle, run_dir=run_dir)

    assert report.success is True
    r = report.results[0]
    assert r.postconditions_ok is True
    # No effect machinery engaged.
    assert r.effect_verified is None
    assert r.effect_results == []
    assert ("press", "Enter") in backend.actions
