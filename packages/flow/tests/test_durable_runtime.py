"""Durable tiered runtime (RFC §5, Tier 3): checkpoint + pause + resume.

Drives the REAL Replayer with a faked backend/vision (as in ``test_replayer``)
and a scripted in-memory ``EffectVerifier`` -- no Playwright, no OCR stack, no
network, no model call. The theses these pin:

* a clean run writes one checkpoint per verified step and completes;
* a run that HALTs mid-way (a REFUTED effect) writes a ``PendingEscalation``
  plus checkpoints for the PRIOR verified steps, and does not die silently;
* ``resume`` continues from the LAST verified checkpoint -- it re-executes only
  the paused step onward and NEVER re-runs an already-confirmed step (no
  re-performed / double write);
* escalation pauses deterministically for an operator; it never hands the
  remaining workflow to a free-form agent.
"""

from __future__ import annotations

# Reuse the scripted fakes from the main replayer unit tests (pytest's prepend
# import mode puts tests/ on sys.path).
from tests.test_replayer import FakeBackend, FakeVision, Match

from openadapt_flow.ir import (
    ActionKind,
    Postcondition,
    PostconditionKind,
    Step,
    Workflow,
)
from openadapt_flow.runtime.durable import (
    ApprovalRecord,
    CheckpointStore,
    bundle_version,
    resume,
    resume_point,
)
from openadapt_flow.runtime.effects import (
    Effect,
    EffectKind,
    EffectState,
    EffectVerdict,
    Verdict,
)
from openadapt_flow.runtime.replayer import Replayer

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

    def capture_pre_state(self, context=None) -> EffectState:
        self.capture_calls += 1
        return EffectState(substrate=self.substrate, reachable=True, records=[])

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
        return EffectVerdict(
            verdict=Verdict.CONFIRMED,
            kind=expected.kind,
            substrate=self.substrate,
            reason="exactly the intended record is present",
        )


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


def _approval(bundle) -> ApprovalRecord:
    """An authenticated approval for the given bundle (P0-5): resume now REQUIRES
    one, so every legitimate resume in these tests carries an operator identity,
    a chosen resolution, and the bundle version it was granted against."""
    return ApprovalRecord(
        approver="operator@example.com",
        resolution="verified the system of record; approve resume",
        bundle_version=bundle_version(bundle),
    )


def _three_step_workflow(*, with_effects: bool) -> Workflow:
    steps = []
    for step_id, key in (("s0", "A"), ("s1", "B"), ("s2", "C")):
        effect = _effect(step_id) if with_effects else None
        steps.append(_key_step(step_id, key, effect=effect))
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


# -- resume is idempotent w.r.t. already-confirmed steps (no double write) ---


def test_resume_does_not_reverify_confirmed_steps(tmp_path):
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

    assert resumed.success is True
    # Idempotency: the already-confirmed steps' effects are NOT re-verified on
    # resume (their writes are never re-performed); only s2 is verified again.
    verified_steps = [call["step"] for call in verifier.verify_calls]
    assert verified_steps == ["s2"]
    # And the resumed report still accounts for the whole workflow.
    assert [r.step_id for r in resumed.results] == ["s0", "s1", "s2"]
    assert all(r.ok for r in resumed.results)


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
