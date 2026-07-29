"""Durable tiered runtime (RFC §5, Tier 3): checkpoint + pause + resume.

Drives the REAL Replayer with a faked backend/vision (as in ``test_replayer``)
and a scripted in-memory ``EffectVerifier`` -- no Playwright, no OCR stack, no
network, no model call. The theses these pin:

* a clean run writes one checkpoint per verified step and completes;
* a run that HALTs mid-way (a REFUTED effect) writes a ``PendingEscalation``
  plus checkpoints for the PRIOR verified steps, and does not die silently;
* ``resume`` continues from the LAST verified checkpoint -- it re-executes only
  the paused step onward, rechecks retained effects without re-performing their
  actions, and never creates a double write;
* escalation pauses deterministically for an operator; it never hands the
  remaining workflow to a free-form agent.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

import pytest

from openadapt_flow.ir import (
    ActionDeliveryReceipt,
    ActionKind,
    FreshActuationEvent,
    Postcondition,
    PostconditionKind,
    Resolution,
    RunReport,
    Step,
    StepResult,
    Workflow,
)
from openadapt_flow.runtime.durable import (
    ApprovalRecord,
    ApprovalRequired,
    CheckpointStore,
    RunCheckpoint,
    StateDiverged,
    bundle_version,
    issue_resume_approval,
    resume,
    resume_point,
)
from openadapt_flow.runtime.durable.approval import approval_pause_digest
from openadapt_flow.runtime.durable.authority import DurableAuthority
from openadapt_flow.runtime.durable.checkpoint import RunManifest
from openadapt_flow.runtime.durable.controller import DurableRun, resumed_step_results
from openadapt_flow.runtime.durable.resume import _validate_retained_step_proof
from openadapt_flow.runtime.effects import (
    Effect,
    EffectKind,
    EffectState,
    EffectVerdict,
    Verdict,
)
from openadapt_flow.runtime.replayer import Replayer
from openadapt_flow.transaction import IdempotencyLedger

# Reuse the scripted fakes from the main replayer unit tests (pytest's prepend
# import mode puts tests/ on sys.path).
from tests.test_replayer import FakeBackend, FakeVision, Match

# -- fakes -------------------------------------------------------------------


class FakeSoRVerifier:
    """A scripted EffectVerifier over an in-memory system of record.

    ``refute`` holds the ``match`` selectors (as sorted-item tuples) the
    verifier should REFUTE; everything else CONFIRMS. ``verify_calls`` records
    every match verified so a test can prove a step was (not) re-verified on
    resume. Duck-types the ``EffectVerifier`` protocol -- no I/O, no model.
    """

    substrate = "fake"

    def __init__(self) -> None:
        self.refute: set[tuple] = set()
        self.verify_calls: list[dict] = []
        self.capture_calls = 0
        self.records: list[dict] = []

    def capture_pre_state(self, context=None) -> EffectState:
        self.capture_calls += 1
        return EffectState(
            substrate=self.substrate,
            reachable=True,
            records=[dict(record) for record in self.records],
        )

    def verify(self, expected: Effect, before: EffectState, context=None):
        self.verify_calls.append(dict(expected.match))
        key = tuple(sorted(expected.match.items()))
        if key in self.refute:
            return EffectVerdict(
                verdict=Verdict.REFUTED,
                kind=expected.kind,
                substrate=self.substrate,
                reason="the intended record is missing from the system of record",
                observed_count=0,
                expected_count=expected.expected_count,
            )
        record = dict(expected.match)
        if record not in self.records:
            self.records.append(record)
        return EffectVerdict(
            verdict=Verdict.CONFIRMED,
            kind=expected.kind,
            substrate=self.substrate,
            reason="exactly the intended record is present",
        )


class _ResumeProjectionFailingLedger(IdempotencyLedger):
    """Fail the only terminal projection after a restartable durable pause."""

    def __init__(self, path):
        super().__init__(path)

    def record_outcome(self, key, outcome, *, run_id):
        del key, outcome, run_id
        raise OSError("terminal ledger projection unavailable")


def _vision_ok() -> FakeVision:
    vision = FakeVision()
    # The screen oracle passes for every step (the point is that the DURABLE
    # layer, not the screen, decides checkpoint vs. pause).
    vision.text_results = {
        "OK": Match(point=(50, 10), region=(30, 5, 40, 10), confidence=0.9)
    }
    return vision


def _key_step(step_id: str, key: str, *, effect=None, risk="reversible") -> Step:
    return Step(
        id=step_id,
        intent=f"press {key}",
        action=ActionKind.KEY,
        key=key,
        expect=[
            Postcondition(kind=PostconditionKind.TEXT_PRESENT, text="OK", timeout_s=0.2)
        ],
        risk=risk,
        effects=[effect] if effect is not None else [],
    )


def _effect(step_id: str, *, needs_operator=False) -> Effect:
    return Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"step": step_id},
        expected_count=1,
        timeout_s=0.5,
        needs_operator_confirmation=needs_operator,
    )


def _dirs(tmp_path):
    bundle = tmp_path / "bundle"
    return bundle, tmp_path / "run"


def _approval(bundle, run_dir=None) -> ApprovalRecord:
    """An authenticated approval for the given bundle (P0-5): resume now REQUIRES
    one, so every legitimate resume in these tests carries an operator identity,
    a chosen resolution, and the bundle version it was granted against."""
    run_dir = run_dir or bundle.parent / "run"
    store = CheckpointStore(run_dir)
    manifest = store.read_manifest()
    pending = store.read_pending()
    assert manifest is not None and pending is not None
    return issue_resume_approval(
        pending,
        approver="operator@example.com",
        resolution="verified the system of record; approve resume",
        bundle_version=bundle_version(bundle),
        run_id=manifest.run_id,
        workflow_name=manifest.workflow_name,
        run_dir=run_dir,
    )


def _three_step_workflow(*, with_effects: bool) -> Workflow:
    steps = []
    for step_id, key in (("s0", "A"), ("s1", "B"), ("s2", "C")):
        effect = _effect(step_id) if with_effects else None
        steps.append(
            _key_step(
                step_id,
                key,
                effect=effect,
                risk="irreversible" if with_effects else "reversible",
            )
        )
    return Workflow(name="durable-demo", steps=steps)


# -- clean run: one checkpoint per verified step, no pause -------------------


def test_clean_run_checkpoints_each_step_and_completes(tmp_path):
    backend = FakeBackend()
    workflow = _three_step_workflow(with_effects=False)
    bundle, run_dir = _dirs(tmp_path)
    workflow.save(bundle)

    replayer = Replayer(
        backend, vision=_vision_ok(), durable=True, poll_interval_s=0.01
    )
    report = replayer.run(workflow, bundle_dir=bundle, run_dir=run_dir)

    assert report.success is True
    store = CheckpointStore(run_dir)
    checkpoints = store.checkpoints()
    # One checkpoint per verified step, in order, each pointing at its successor.
    assert [c.step_index for c in checkpoints] == [0, 1, 2]
    assert [c.step_id for c in checkpoints] == ["s0", "s1", "s2"]
    assert [c.next_step_index for c in checkpoints] == [1, 2, 3]
    # No pause on a clean run.
    assert store.read_pending() is None
    # $0: no model calls.
    assert report.model_calls == 0


def test_linear_resume_never_synthesizes_missing_verified_history(tmp_path):
    workflow = _three_step_workflow(with_effects=False)

    with pytest.raises(StateDiverged, match="exact verified checkpoint"):
        resumed_step_results(tmp_path / "empty-run", workflow, 1)


# -- halt mid-way: pending escalation + prior checkpoints -------------------


def _run_to_halt(tmp_path):
    """A 3-step effectful run whose THIRD step (s2) is REFUTED -> halt.

    Returns (report, run_dir, bundle, verifier)."""
    verifier = FakeSoRVerifier()
    verifier.refute.add((("step", "s2"),))  # the third step's effect refutes
    backend = FakeBackend()
    workflow = _three_step_workflow(with_effects=True)
    bundle, run_dir = _dirs(tmp_path)
    workflow.save(bundle)

    replayer = Replayer(
        backend,
        vision=_vision_ok(),
        effect_verifier=verifier,
        durable=True,
        poll_interval_s=0.01,
    )
    report = replayer.run(
        workflow, params={"who": "alice"}, bundle_dir=bundle, run_dir=run_dir
    )
    return report, run_dir, bundle, backend, verifier


def test_halt_midway_writes_pending_and_checkpoints_prior(tmp_path):
    report, run_dir, _bundle, _backend, _verifier = _run_to_halt(tmp_path)

    assert report.success is False
    store = CheckpointStore(run_dir)

    # Only the two PRIOR verified steps are checkpointed; the halted step is not.
    checkpoints = store.checkpoints()
    assert [c.step_index for c in checkpoints] == [0, 1]
    assert all(c.effect_verified is True for c in checkpoints)

    # A durable pending escalation was written (the run paused, did not just die).
    pending = store.read_pending()
    assert pending is not None
    assert pending.step_index == 2
    assert pending.step_id == "s2"
    assert pending.category == "effect_refuted"
    assert "refuted" in pending.reason.lower()
    # It proposes operator options and points at the last verified checkpoint.
    assert pending.proposed_options  # non-empty
    assert any("RESUME" in opt for opt in pending.proposed_options)
    assert pending.resume_from_index == 2  # continue after s1 (index 1)
    assert pending.resume_from_step_id == "s1"
    # The run's parameter bindings are captured for an identical re-bind.
    assert pending.params == {"who": "alice"}
    # And the resume point helper agrees.
    assert resume_point(run_dir) == 2


# -- resume: continue from the last checkpoint, not from step 0 -------------


def test_resume_continues_from_last_checkpoint(tmp_path):
    report, run_dir, bundle, _orig_backend, verifier = _run_to_halt(tmp_path)
    assert report.success is False

    # The operator resolves the cause (the record now exists): s2 will confirm.
    verifier.refute.clear()
    # A FRESH backend for the resumed leg (a new run against the live system);
    # its recorded actions reveal exactly which steps re-executed.
    resume_backend = FakeBackend()
    resume_replayer = Replayer(
        resume_backend,
        vision=_vision_ok(),
        effect_verifier=verifier,
        poll_interval_s=0.01,
    )

    resumed = resume(run_dir, resume_replayer, approval=_approval(bundle))

    assert resumed.success is True
    # The already-verified steps were NOT re-executed on the resumed backend...
    assert ("press", "A") not in resume_backend.actions
    assert ("press", "B") not in resume_backend.actions
    # ...only the paused step onward was.
    assert ("press", "C") in resume_backend.actions

    # The checkpoint set now covers the whole workflow (s2 advanced).
    store = CheckpointStore(run_dir)
    assert [c.step_index for c in store.checkpoints()] == [0, 1, 2]
    # The pending escalation was cleared when the resume started.
    assert store.read_pending() is None


def test_durable_resume_projection_failure_keeps_fail_closed_terminal_evidence(
    tmp_path,
):
    """The resumed action runs once when its terminal ledger projection fails."""

    ledger = _ResumeProjectionFailingLedger(tmp_path / "ledger.sqlite")
    verifier = FakeSoRVerifier()
    verifier.refute.add((("step", "s2"),))
    workflow = _three_step_workflow(with_effects=True)
    bundle, run_dir = _dirs(tmp_path)
    workflow.save(bundle)
    initial = Replayer(
        FakeBackend(),
        vision=_vision_ok(),
        effect_verifier=verifier,
        idempotency_ledger=ledger,
        durable=True,
        poll_interval_s=0.01,
    ).run(
        workflow,
        params={"who": "alice"},
        bundle_dir=bundle,
        run_dir=run_dir,
        idempotency_key="durable-projection-key",
    )
    assert initial.success is False
    pending = CheckpointStore(run_dir).read_pending()
    assert pending is not None

    verifier.refute.clear()
    resumed_backend = FakeBackend()
    resumed = resume(
        run_dir,
        Replayer(
            resumed_backend,
            vision=_vision_ok(),
            effect_verifier=verifier,
            idempotency_ledger=ledger,
            poll_interval_s=0.01,
        ),
        approval=_approval(bundle),
    )

    # The resumed action is delivered once. The failed projection does not
    # dispatch it again and does not erase its retained report/effect evidence.
    assert resumed_backend.actions == [("press", "C")]
    assert resumed.success is False
    assert resumed.execution_outcome == "FAILED"
    assert resumed.transaction_outcome == "RECONCILIATION_REQUIRED"
    persisted = RunReport.model_validate_json((run_dir / "report.json").read_text())
    assert persisted.execution_outcome == "FAILED"
    assert persisted.transaction_outcome == "RECONCILIATION_REQUIRED"
    assert persisted.results[-1].step_id == "<idempotency>"
    assert persisted.results[-1].failure_category == "runtime_failure"

    store = CheckpointStore(run_dir)
    manifest = store.read_manifest()
    assert manifest is not None
    authority = DurableAuthority(run_dir, store)
    with authority._transaction() as connection:  # noqa: SLF001 - exact authority proof
        record = authority._read(connection)  # noqa: SLF001 - exact authority proof
    assert record is not None
    assert record.terminal_projection_state == "corrected"
    assert (
        record.report_sha256
        == "sha256:"
        + hashlib.sha256((run_dir / "report.json").read_bytes()).hexdigest()
    )
    assert authority.prove_executor_outcome(
        manifest,
        attempt_id="irrelevant-after-completion",
        owner_nonce_sha256="irrelevant-after-completion",
        source_pause_binding=approval_pause_digest(pending),
    ) == ("halted", False)


def test_durable_pause_defers_ledger_outcome_until_resume_verifies(tmp_path):
    """A restartable halt does not consume the logical terminal outcome."""

    ledger = IdempotencyLedger(tmp_path / "ledger.sqlite")
    verifier = FakeSoRVerifier()
    verifier.refute.add((("step", "s2"),))
    workflow = _three_step_workflow(with_effects=True)
    bundle, run_dir = _dirs(tmp_path)
    workflow.save(bundle)
    Replayer(
        FakeBackend(),
        vision=_vision_ok(),
        effect_verifier=verifier,
        idempotency_ledger=ledger,
        durable=True,
        poll_interval_s=0.01,
    ).run(
        workflow,
        params={"who": "alice"},
        bundle_dir=bundle,
        run_dir=run_dir,
        idempotency_key="durable-final-key",
    )
    assert ledger.lookup("durable-final-key")["outcome"] is None

    verifier.refute.clear()
    resumed = resume(
        run_dir,
        Replayer(
            FakeBackend(),
            vision=_vision_ok(),
            effect_verifier=verifier,
            idempotency_ledger=ledger,
            poll_interval_s=0.01,
        ),
        approval=_approval(bundle),
    )
    assert resumed.success is True
    # This fixture uses the Demo profile, so its correct terminal taxonomy is
    # COMPLETED_UNVERIFIED. The important invariant is that the ledger receives
    # that one final logical outcome, not the earlier restartable halt.
    assert ledger.lookup("durable-final-key")["outcome"] == (
        resumed.transaction_outcome
    )


def test_indeterminate_durable_state_fails_closed_without_ledger_projection(
    tmp_path, monkeypatch
):
    """A failed durable-state read cannot retain a success-shaped report."""

    ledger = IdempotencyLedger(tmp_path / "ledger.sqlite")
    workflow = _three_step_workflow(with_effects=False)
    bundle, run_dir = _dirs(tmp_path)
    workflow.save(bundle)
    replayer = Replayer(
        FakeBackend(),
        vision=_vision_ok(),
        idempotency_ledger=ledger,
        durable=True,
        poll_interval_s=0.01,
    )
    monkeypatch.setattr(
        replayer,
        "_durable_terminal_state",
        lambda _run_dir: "indeterminate",
    )

    report = replayer.run(
        workflow,
        bundle_dir=bundle,
        run_dir=run_dir,
        idempotency_key="indeterminate-durable-state-key",
    )

    assert report.success is False
    assert report.execution_outcome == "FAILED"
    assert report.results[-1].step_id == "<idempotency>"
    assert report.results[-1].failure_category == "runtime_failure"
    assert "durable terminal state persistence failed" in (
        report.results[-1].error or ""
    )
    persisted = RunReport.model_validate_json((run_dir / "report.json").read_text())
    assert persisted.execution_outcome == "FAILED"
    assert ledger.lookup("indeterminate-durable-state-key")["outcome"] is None


def test_resume_requires_the_active_pause_and_exact_inputs(tmp_path):
    _report, run_dir, bundle, _backend, verifier = _run_to_halt(tmp_path)
    replayer = Replayer(FakeBackend(), vision=_vision_ok(), effect_verifier=verifier)

    with pytest.raises(StateDiverged, match="parameters differ"):
        resume(
            run_dir,
            replayer,
            approval=_approval(bundle),
            params={"who": "different"},
        )

    store = CheckpointStore(run_dir)
    approval = _approval(bundle)
    store.clear_pending()
    with pytest.raises(ApprovalRequired, match="no active durable pause"):
        resume(run_dir, replayer, approval=approval)
    assert replayer.backend.actions == []


def test_linear_checkpoint_retains_exact_delivery_proof(tmp_path):
    workflow = Workflow(
        name="delivery-proof",
        steps=[Step(id="submit", intent="Submit", action=ActionKind.CLICK)],
    )
    resolution = Resolution(
        rung="template",
        point=(8, 8),
        confidence=0.99,
        elapsed_ms=1.0,
    )
    drag_end = Resolution(
        rung="template",
        point=(15, 15),
        confidence=0.98,
        elapsed_ms=1.1,
    )
    receipt = ActionDeliveryReceipt(
        receipt_id="receipt-1",
        operation="guarded_coordinate_click",
        native=False,
        delivered_at="2026-07-28T00:00:01+00:00",
    )
    event = FreshActuationEvent(
        attempt=1,
        operation="click",
        changed_pixel_count=4,
        changed_bbox=(1, 1, 2, 2),
        frame_size=(20, 20),
        retried=True,
    )
    checkpoint = RunCheckpoint(
        run_id="run-1",
        workflow_name=workflow.name,
        step_index=0,
        step_id="submit",
        next_step_index=1,
        resolution=resolution,
        drag_end_resolution=drag_end,
        delivery_attempted=True,
        delivery_receipt=receipt,
        fresh_actuation_events=[event],
        before_png="steps/submit_before.png",
    )

    restored = resumed_step_results(
        tmp_path,
        workflow,
        1,
        checkpoints=[RunCheckpoint.model_validate_json(checkpoint.model_dump_json())],
        run_id="run-1",
    )[0]

    assert restored.delivery_receipt == receipt
    assert restored.drag_end_resolution == drag_end
    assert restored.fresh_actuation_events == [event]
    assert restored.before_png == "steps/submit_before.png"


def test_checkpoint_creation_rejects_impossible_action_evidence(tmp_path):
    step = Step(
        id="submit",
        intent="Submit",
        action=ActionKind.CLICK,
        anchor={
            "template": "submit.png",
            "region": (0, 0, 10, 10),
            "click_point": (5, 5),
        },
    )
    workflow = Workflow(name="checkpoint-action-proof", steps=[step])
    bundle, run_dir = _dirs(tmp_path)
    workflow.save(bundle)
    durable = DurableRun(
        run_dir,
        run_id="run-action-proof",
        workflow_name=workflow.name,
        bundle_dir=bundle,
        params={},
        worklists={},
    )
    impossible = StepResult(
        step_id=step.id,
        intent=step.intent,
        ok=True,
        actuation="guarded_coordinate",
        starting_state_settled=True,
        delivery_attempted=True,
        resolution=Resolution(
            rung="template",
            point=(5, 5),
            confidence=0.99,
            elapsed_ms=1.0,
        ),
        delivery_receipt=ActionDeliveryReceipt(
            receipt_id="wrong-operation",
            operation="physical_press",
            native=False,
            delivered_at="2026-07-28T00:00:01+00:00",
        ),
    )

    with pytest.raises(StateDiverged, match="invalid action evidence"):
        durable.record(0, step, impossible, {})
    assert durable.store.checkpoints() == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("input_verified", True),
        ("starting_state_settled", False),
        ("delivery_attempted", False),
        (
            "delivery_receipt",
            ActionDeliveryReceipt(
                receipt_id="wrong-operation",
                operation="physical_press",
                native=False,
                delivered_at="2026-07-28T00:00:01+00:00",
            ),
        ),
        (
            "resolution",
            Resolution(
                rung="ocr",
                point=(5, 5),
                confidence=0.99,
                elapsed_ms=1.0,
            ),
        ),
        (
            "fresh_actuation_events",
            [
                FreshActuationEvent(
                    attempt=1,
                    operation="click",
                    changed_pixel_count=1,
                    changed_bbox=(0, 0, 1, 1),
                    frame_size=(10, 10),
                    retried=False,
                )
            ],
        ),
    ],
)
def test_resume_admission_rejects_invalid_retained_action_evidence(field, value):
    step = Step(
        id="submit",
        intent="Submit",
        action=ActionKind.CLICK,
        anchor={
            "template": "submit.png",
            "region": (0, 0, 10, 10),
            "click_point": (5, 5),
        },
    )
    workflow = Workflow(name="retained-action-proof", steps=[step])
    manifest = RunManifest(
        run_id="run-action-proof",
        workflow_name=workflow.name,
        bundle_dir="/tmp/retained-action-proof",
        params={},
    )
    proof = {
        "input_verified": None,
        "starting_state_settled": True,
        "delivery_attempted": True,
        "delivery_receipt": ActionDeliveryReceipt(
            receipt_id="click-receipt",
            operation="guarded_coordinate_click",
            native=False,
            delivered_at="2026-07-28T00:00:01+00:00",
        ),
        "resolution": Resolution(
            rung="template",
            point=(5, 5),
            confidence=0.99,
            elapsed_ms=1.0,
        ),
        "fresh_actuation_events": [],
    }
    proof[field] = value

    with pytest.raises(StateDiverged, match="action evidence is invalid"):
        _validate_retained_step_proof(
            step=step,
            params={},
            run_id=manifest.run_id,
            skipped=False,
            actuation="guarded_coordinate",
            effect_verified=None,
            effect_approved_unverified=False,
            effect_contract_hashes=[],
            effect_evidence=[],
            stored_effects=None,
            identity=None,
            input_verified=proof["input_verified"],
            starting_state_settled=proof["starting_state_settled"],
            delivery_attempted=proof["delivery_attempted"],
            delivery_receipt=proof["delivery_receipt"],
            resolution=proof["resolution"],
            drag_end_resolution=None,
            fresh_actuation_events=proof["fresh_actuation_events"],
            postconditions_ok=None,
            delivery_uncertainty=None,
            governed_authorization_id=None,
            governed_approval_source=None,
            manifest=manifest,
            workflow=workflow,
        )


def test_resume_refuses_a_checkpoint_added_after_the_pause(tmp_path):
    _report, run_dir, bundle, _backend, verifier = _run_to_halt(tmp_path)
    store = CheckpointStore(run_dir)
    pending = store.read_pending()
    assert pending is not None
    store.write_checkpoint(
        RunCheckpoint(
            run_id=pending.run_id,
            bundle_version=bundle_version(bundle),
            workflow_name="durable-demo",
            step_index=2,
            step_id="s2",
            next_step_index=3,
            params={"who": "alice"},
        )
    )
    backend = FakeBackend()
    with pytest.raises(
        StateDiverged,
        match="checkpoint history|monotonic authority",
    ):
        resume(
            run_dir,
            Replayer(backend, vision=_vision_ok(), effect_verifier=verifier),
            approval=_approval(bundle),
        )
    assert backend.actions == []


def test_attended_checkpoint_must_follow_the_active_pause(tmp_path):
    _report, run_dir, bundle, _backend, verifier = _run_to_halt(tmp_path)
    store = CheckpointStore(run_dir)
    pending = store.read_pending()
    assert pending is not None
    before_pause = (
        datetime.fromisoformat(pending.created_at) - timedelta(hours=1)
    ).isoformat()
    store.write_checkpoint(
        RunCheckpoint(
            run_id=pending.run_id,
            bundle_version=bundle_version(bundle),
            workflow_name="durable-demo",
            step_index=2,
            step_id="s2",
            next_step_index=3,
            params={"who": "alice"},
            actuation="human_attended",
            created_at=before_pause,
        )
    )
    store.write_pending(pending.model_copy(update={"status": "approved"}))
    backend = FakeBackend()
    with pytest.raises(
        StateDiverged,
        match="history changed|monotonic authority",
    ):
        resume(
            run_dir,
            Replayer(backend, vision=_vision_ok(), effect_verifier=verifier),
            approval=_approval(bundle),
        )
    assert backend.actions == []


# -- resume is idempotent w.r.t. already-confirmed steps (no double write) ---


def test_resume_revalidates_but_does_not_repeat_confirmed_steps(tmp_path):
    report, run_dir, bundle, _orig_backend, verifier = _run_to_halt(tmp_path)
    assert report.success is False

    verifier.refute.clear()
    verifier.verify_calls.clear()  # count only the resumed leg

    resume_backend = FakeBackend()
    resume_replayer = Replayer(
        resume_backend,
        vision=_vision_ok(),
        effect_verifier=verifier,
        poll_interval_s=0.01,
    )
    resumed = resume(run_dir, resume_replayer, approval=_approval(bundle))

    manifest = CheckpointStore(run_dir).read_manifest()
    assert manifest is not None
    assert report.started_at == manifest.created_at
    assert resumed.started_at == manifest.created_at

    assert resumed.success is True
    # Resume re-reads the retained effects before it continues. It does not
    # repeat their actions; only s2 is actuated on this leg.
    verified_steps = [call["step"] for call in verifier.verify_calls]
    assert verified_steps == ["s2"]
    assert verifier.capture_calls >= 1
    # And the resumed report still accounts for the whole workflow.
    assert [r.step_id for r in resumed.results] == ["s0", "s1", "s2"]
    assert all(r.ok for r in resumed.results)
    assert [r.risk for r in resumed.results[:2]] == ["irreversible", "irreversible"]


# -- halt on the FIRST step resumes from zero -------------------------------


def test_halt_on_first_step_resumes_from_zero(tmp_path):
    verifier = FakeSoRVerifier()
    verifier.refute.add((("step", "s0"),))  # the first step refutes
    backend = FakeBackend()
    workflow = _three_step_workflow(with_effects=True)
    bundle, run_dir = _dirs(tmp_path)
    workflow.save(bundle)

    replayer = Replayer(
        backend,
        vision=_vision_ok(),
        effect_verifier=verifier,
        durable=True,
        poll_interval_s=0.01,
    )
    report = replayer.run(workflow, bundle_dir=bundle, run_dir=run_dir)

    assert report.success is False
    store = CheckpointStore(run_dir)
    assert store.checkpoints() == []  # nothing verified yet
    pending = store.read_pending()
    assert pending is not None
    assert pending.step_index == 0
    assert pending.resume_from_index == 0
    assert pending.resume_from_step_id is None
    assert resume_point(run_dir) == 0

    # Resume from zero re-runs the first (paused) step.
    verifier.refute.clear()
    resume_backend = FakeBackend()
    resume_replayer = Replayer(
        resume_backend,
        vision=_vision_ok(),
        effect_verifier=verifier,
        poll_interval_s=0.01,
    )
    resumed = resume(run_dir, resume_replayer, approval=_approval(bundle))
    assert resumed.success is True
    assert ("press", "A") in resume_backend.actions
    assert [c.step_index for c in store.checkpoints()] == [0, 1, 2]


# -- placeholder effect halt is classified for the operator -----------------


def test_placeholder_effect_pause_is_classified(tmp_path):
    # A consequential write whose system-of-record binding the compiler could
    # not derive (needs_operator_confirmation) HALTs fail-safe; the durable
    # pause must name it a placeholder so the operator knows to complete it.
    verifier = FakeSoRVerifier()
    backend = FakeBackend()
    workflow = Workflow(
        name="placeholder-demo",
        steps=[
            _key_step("s0", "A", effect=_effect("s0")),
            _key_step("s1", "B", effect=_effect("s1", needs_operator=True)),
        ],
    )
    bundle, run_dir = _dirs(tmp_path)
    workflow.save(bundle)

    replayer = Replayer(
        backend,
        vision=_vision_ok(),
        effect_verifier=verifier,
        durable=True,
        poll_interval_s=0.01,
    )
    report = replayer.run(workflow, bundle_dir=bundle, run_dir=run_dir)

    assert report.success is False
    store = CheckpointStore(run_dir)
    assert [c.step_index for c in store.checkpoints()] == [0]  # s0 verified
    pending = store.read_pending()
    assert pending is not None
    assert pending.step_index == 1
    assert pending.category == "placeholder_effect"
    assert any("binding" in opt.lower() for opt in pending.proposed_options)
    assert pending.resume_from_index == 1


# -- back-compat: durability OFF leaves no durable artifacts ----------------


def test_non_durable_run_writes_no_durable_artifacts(tmp_path):
    backend = FakeBackend()
    workflow = _three_step_workflow(with_effects=False)
    bundle, run_dir = _dirs(tmp_path)
    workflow.save(bundle)

    replayer = Replayer(backend, vision=_vision_ok(), poll_interval_s=0.01)
    report = replayer.run(workflow, bundle_dir=bundle, run_dir=run_dir)

    assert report.success is True
    store = CheckpointStore(run_dir)
    assert store.checkpoints() == []
    assert store.read_manifest() is None
    assert store.read_pending() is None


def test_fresh_durable_run_refuses_an_owned_run_directory_before_actuation(tmp_path):
    workflow = _three_step_workflow(with_effects=False)
    bundle, run_dir = _dirs(tmp_path)
    workflow.save(bundle)
    first = Replayer(FakeBackend(), vision=_vision_ok(), durable=True)
    assert first.run(workflow, bundle_dir=bundle, run_dir=run_dir).success is True

    second_backend = FakeBackend()
    with pytest.raises(StateDiverged, match="already contains a durable run"):
        Replayer(second_backend, vision=_vision_ok(), durable=True).run(
            workflow, bundle_dir=bundle, run_dir=run_dir
        )
    assert second_backend.actions == []


def test_resume_approval_is_bound_to_the_exact_active_pause(tmp_path):
    _report, run_dir, bundle, _backend, _verifier = _run_to_halt(tmp_path)
    approval = _approval(bundle)
    store = CheckpointStore(run_dir)
    pending = store.read_pending()
    assert pending is not None
    changed = pending.model_copy(
        update={"reason": "a later pause needs a new approval"}
    )
    authority = DurableAuthority(run_dir, store)
    record = authority.validate(store.read_manifest())
    store.write_pending(changed)
    authority.advance(
        store.read_manifest(),
        expected_progress_digest=record.progress_digest,
        phase="paused",
        pause_binding_sha256=approval_pause_digest(changed),
    )

    backend = FakeBackend()
    with pytest.raises(ApprovalRequired, match="exact active"):
        resume(
            run_dir,
            Replayer(backend, vision=_vision_ok(), effect_verifier=FakeSoRVerifier()),
            approval=approval,
        )
    assert backend.actions == []
