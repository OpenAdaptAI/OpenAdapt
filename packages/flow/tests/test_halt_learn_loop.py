"""End-to-end proof of the HALT -> LEARN -> RESOLVE loop (governed, one scenario).

The scenario is the hybrid/MockMed drift mode ``modal-once``: an unexpected
survey modal intercepts the workflow ONCE, mid-run. A compiled workflow that was
never demonstrated to handle it must HALT (refuse rather than guess). This test
closes the loop on that ONE scenario, end to end, with ZERO model calls and no
live browser (the SAME FakeBackend/FakeVision the Phase-2 tests use):

  before  -> a real ``Replayer.run`` HALTS on the modal and EMITS a learnable
             ``RunReport.halt`` naming the observed unexpected state;
  learn   -> the operator's dismiss-then-continue correction is captured as a
             demonstration and fed to the GOVERNED learn/promote loop, which
             induces the resolution as a guarded conditional branch on the
             program graph, GATES it (identity/effect/risk may not regress), and
             promotes only after held-out coverage improves without regression;
  after   -> replaying the SAME modal-once scenario through the promoted program
             now dismisses the modal and completes WITHOUT halting, while a clean
             run and a DIFFERENT unexpected modal still behave exactly as before.

Plus the safety proofs: the loop REFUSES an underdetermined correction (no
derivable branch condition) and the regression GATE BLOCKS a correction that
would weaken an armed step's identity band -- in both cases the workflow stays
halting (no ungoverned learning).
"""

from __future__ import annotations

from typing import Optional

import pytest

from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    HaltObservation,
    ProgramGraph,
    RunReport,
    State,
    StateKind,
    Step,
    Transition,
    Workflow,
)
from openadapt_flow.learning import (
    ExecutionTrace,
    SkillLibrary,
    TraceStep,
    execution_trace_from_halt,
    learn_from_halt,
    program_regression_gate,
    promoted_workflow,
    resolution_demonstration,
)
from openadapt_flow.learning.synth_stream import (
    INTENT_OPEN,
    StructuralDiffInducer,
    mockmed_base_program,
)
from openadapt_flow.runtime.replayer import Replayer
from openadapt_flow.vision.ocr import OcrLine
from tests.test_replayer import FakeBackend, FakeVision, Match, make_png

# -- the modal-once scenario --------------------------------------------------

SKILL_ID = "mockmed-save-encounter"
INTENT_SAVE = "Save encounter"
INTENT_VERIFY = "Confirm saved"
INTENT_DISMISS = "Dismiss survey modal"
MODAL_FACT = "Survey Required"
OTHER_MODAL = "Session Timeout"

VERIFY_ORIGIN = (400, 300)
DISMISS_POINT = (100, 100)


def modal_once_base_program() -> ProgramGraph:
    """The naive compiled ``save encounter`` skill: press Save, then click the
    post-save confirmation banner. It knows NOTHING about a survey modal -- so on
    ``modal-once`` the modal covers the banner and the confirm click can't resolve
    -> the run HALTS (the "before")."""
    return ProgramGraph(
        entry="s_save",
        states={
            "s_save": State(
                id="s_save",
                kind=StateKind.ACTION,
                step=Step(
                    id="s_save", intent=INTENT_SAVE, action=ActionKind.KEY, key="S"
                ),
                transitions=[Transition(target="s_verify")],
            ),
            "s_verify": State(
                id="s_verify",
                kind=StateKind.ACTION,
                step=Step(
                    id="s_verify",
                    intent=INTENT_VERIFY,
                    action=ActionKind.CLICK,
                    anchor=Anchor(
                        template="templates/verify.png",
                        region=(*VERIFY_ORIGIN, 50, 20),
                        click_point=(410, 305),
                    ),
                    timeout_s=0.1,  # keep the halt-path resolution retry fast
                ),
                transitions=[Transition(target="__end__")],
            ),
            "__end__": State(id="__end__", kind=StateKind.TERMINAL, outcome="success"),
        },
    )


class ModalOnceVision(FakeVision):
    """A faithful ``modal-once`` scenario: a single modal (``modal_text``) covers
    the confirm banner until its dismiss button is clicked ONCE. The confirm
    banner resolves ONLY once nothing blocks it -- so the naive program halts, and
    a program that dismisses first succeeds, using ONE vision behaviour for both.

    ``modal_text=None`` models a clean (no-drift) run; a different ``modal_text``
    models a DIFFERENT unexpected modal the learned branch must not swallow.
    """

    def __init__(self, backend: FakeBackend, *, modal_text: Optional[str] = None):
        super().__init__()
        self.backend = backend
        self.modal_text = modal_text

    def _dismissed(self) -> bool:
        return any(
            a[0] == "click"
            and abs(a[1] - DISMISS_POINT[0]) <= 15
            and abs(a[2] - DISMISS_POINT[1]) <= 15
            for a in self.backend.actions
        )

    def _modal_up(self) -> bool:
        return self.modal_text is not None and not self._dismissed()

    def find_template(
        self,
        screen_png,
        template_png,
        *,
        search_region=None,
        prefer_near=None,
        scales=(0.85, 1.0, 1.18),
        threshold=0.82,
    ):
        self.template_calls.append(search_region)
        if (
            prefer_near is not None
            and abs(prefer_near[0] - VERIFY_ORIGIN[0]) <= 5
            and abs(prefer_near[1] - VERIFY_ORIGIN[1]) <= 5
        ):
            # The confirm banner: reachable only when no modal blocks it.
            if self._modal_up():
                return None
            return Match(
                point=(410, 305), region=(*VERIFY_ORIGIN, 50, 20), confidence=0.95
            )
        # The modal's dismiss button: present only while the modal is up.
        if self._modal_up():
            return Match(point=DISMISS_POINT, region=(90, 90, 20, 20), confidence=0.95)
        return None

    def find_text(
        self,
        screen_png,
        text,
        *,
        region=None,
        min_ratio=0.8,
        raise_on_ambiguity=False,
    ):
        del raise_on_ambiguity
        self.text_calls.append(text)
        if self._modal_up() and text == self.modal_text:
            return Match(point=(50, 10), region=(30, 5, 160, 16), confidence=0.9)
        return None

    def ocr(self, screen_png, *, region=None):
        if self._modal_up():
            return [
                OcrLine(text=self.modal_text, region=(30, 5, 160, 16), confidence=0.9)
            ]
        return []


@pytest.fixture()
def bundle(tmp_path):
    bundle_dir = tmp_path / "bundle"
    (bundle_dir / "templates").mkdir(parents=True)
    # Both anchors the scenario resolves: the confirm banner and the modal's
    # dismiss button (the reference inducer splices a click on ``templates/x.png``).
    (bundle_dir / "templates" / "verify.png").write_bytes(make_png((50, 20)))
    (bundle_dir / "templates" / "x.png").write_bytes(make_png((20, 20)))
    return bundle_dir


@pytest.fixture()
def run_dir(tmp_path):
    return tmp_path / "run"


def _replay(
    program_wf: Workflow, *, modal_text, bundle, run_dir
) -> tuple[RunReport, FakeBackend]:
    backend = FakeBackend()
    vision = ModalOnceVision(backend, modal_text=modal_text)
    report = Replayer(backend, vision=vision, poll_interval_s=0.01).run(
        program_wf, bundle_dir=bundle, run_dir=run_dir
    )
    return report, backend


def _base_workflow() -> Workflow:
    return Workflow(name=SKILL_ID, program=modal_once_base_program())


def _clean_success_trace() -> ExecutionTrace:
    """A prior clean run of the skill (no modal) -- the deployment already has
    these; the loop needs one to isolate the branch-guard fact."""
    return ExecutionTrace(
        trace_id=f"{SKILL_ID}-clean",
        outcome="success",
        steps=[TraceStep(intent=INTENT_SAVE), TraceStep(intent=INTENT_VERIFY)],
    )


# ===========================================================================
# BEFORE: the naive workflow halts on modal-once and emits a learnable trace
# ===========================================================================


def test_before_modal_once_halts_and_emits_learnable_trace(bundle, run_dir):
    report, backend = _replay(
        _base_workflow(), modal_text=MODAL_FACT, bundle=bundle, run_dir=run_dir
    )

    assert report.success is False
    assert report.terminal_outcome == "halt"
    # It got as far as Save, then stalled on the blocked confirm click.
    assert backend.actions == [("press", "S")]

    # Item 1: the halt EMITTED a structured, learnable record.
    halt = report.halt
    assert halt is not None
    assert halt.intent == INTENT_VERIFY
    assert halt.completed_intents == [INTENT_SAVE]
    assert MODAL_FACT in halt.observed_texts  # the observed unexpected state

    # ... and it lifts cleanly into the trace corpus (the SAME learning type).
    trace = execution_trace_from_halt(report, trace_id="t")
    assert trace.outcome == "failure"
    assert trace.facts.get(MODAL_FACT) is True
    assert [s.intent for s in trace.steps] == [INTENT_SAVE]


# ===========================================================================
# LEARN: the governed loop induces + gates + promotes a dismiss branch
# ===========================================================================


def _learn(library: SkillLibrary, halt_report: RunReport):
    halt_trace = execution_trace_from_halt(halt_report, trace_id="probe")
    correction = resolution_demonstration(
        halt_trace,
        resolution_steps=[TraceStep(intent=INTENT_DISMISS, action=ActionKind.CLICK)],
        tail_intents=(INTENT_VERIFY,),
        trace_id=f"{SKILL_ID}-correction",
    )
    return learn_from_halt(
        library,
        SKILL_ID,
        halt_report=halt_report,
        correction=correction,
        inducer=StructuralDiffInducer(),
        baseline=[_clean_success_trace()],
    )


def test_learn_promotes_a_guarded_dismiss_branch(bundle, run_dir, tmp_path):
    halt_report, _ = _replay(
        _base_workflow(), modal_text=MODAL_FACT, bundle=bundle, run_dir=run_dir
    )
    library = SkillLibrary(tmp_path / "skills")
    library.create_skill(SKILL_ID, modal_once_base_program())

    outcome, _ = _learn(library, halt_report)

    assert outcome.action == "promoted", outcome.reason
    assert outcome.coverage_before < outcome.coverage_after == 1.0
    assert outcome.gate is not None and outcome.gate.passed

    # Item 3: the promotion set a real ``.program`` with a guarded conditional
    # branch (a skip-guarded dismiss step keyed on the observed modal fact).
    active = library.active_version(SKILL_ID)
    assert active.version == 2
    dismiss = next(
        s
        for s in active.graph.states.values()
        if s.kind is StateKind.ACTION and s.step and s.step.intent == INTENT_DISMISS
    )
    assert dismiss.step.guard is not None
    assert dismiss.step.guard.on_unmet == "skip"
    assert dismiss.step.guard.predicate.text == MODAL_FACT
    # Version history is auditable: v1 retired to superseded, never deleted.
    statuses = {v.version: v.status for v in library.get(SKILL_ID).versions}
    assert statuses == {1: "superseded", 2: "active"}


# ===========================================================================
# AFTER: the promoted program resolves modal-once; no regression elsewhere
# ===========================================================================


def test_after_loop_resolves_modal_once_without_regression(bundle, run_dir, tmp_path):
    halt_report, _ = _replay(
        _base_workflow(),
        modal_text=MODAL_FACT,
        bundle=bundle,
        run_dir=run_dir / "before",
    )
    library = SkillLibrary(tmp_path / "skills")
    library.create_skill(SKILL_ID, modal_once_base_program())
    outcome, _ = _learn(library, halt_report)
    assert outcome.promoted
    learned = promoted_workflow(library, SKILL_ID, name=SKILL_ID)

    # (a) SAME modal-once scenario -> now dismisses the modal and completes.
    r_modal, b_modal = _replay(
        learned, modal_text=MODAL_FACT, bundle=bundle, run_dir=run_dir / "modal"
    )
    assert r_modal.success is True
    assert r_modal.halt is None
    # It pressed Save, clicked the modal's dismiss button, then confirmed.
    assert b_modal.actions == [
        ("press", "S"),
        ("click", *DISMISS_POINT, False),
        ("click", 410, 305, False),
    ]

    # (b) clean (no-drift) run -> the dismiss branch is skipped; unchanged.
    r_clean, b_clean = _replay(
        learned, modal_text=None, bundle=bundle, run_dir=run_dir / "clean"
    )
    assert r_clean.success is True
    assert b_clean.actions == [("press", "S"), ("click", 410, 305, False)]

    # (c) a DIFFERENT unexpected modal -> the learned branch does NOT swallow it;
    # the run still HALTS (no over-generalization, and no regression vs base:
    # the base halts here too).
    r_other, _ = _replay(
        learned, modal_text=OTHER_MODAL, bundle=bundle, run_dir=run_dir / "other"
    )
    assert r_other.success is False
    assert r_other.terminal_outcome == "halt"
    r_base_other, _ = _replay(
        _base_workflow(),
        modal_text=OTHER_MODAL,
        bundle=bundle,
        run_dir=run_dir / "base_other",
    )
    assert r_base_other.success is False  # base behaviour is unchanged


# ===========================================================================
# GATE: the loop refuses bad / underdetermined corrections (no ungoverned learn)
# ===========================================================================


def test_loop_refuses_underdetermined_correction(bundle, run_dir, tmp_path):
    """A halt whose observed state gave NO discriminating fact (the modal label
    was not OCR-readable) underdetermines the branch condition: the reference
    inducer cannot derive a guard, so the canary sees the novelty still uncovered
    and REFUSES to promote -- the workflow stays halting."""
    library = SkillLibrary(tmp_path / "skills")
    library.create_skill(SKILL_ID, modal_once_base_program())

    # A halt that observed no readable dialog text -> no fact to key a guard on.
    blind_halt = RunReport(workflow_name=SKILL_ID, started_at="t")
    blind_halt.halt = HaltObservation(
        state_id="s_verify",
        intent=INTENT_VERIFY,
        reason="confirm banner could not be resolved (unknown overlay)",
        outcome="halt",
        observed_texts=[],  # nothing discriminating observed
        completed_intents=[INTENT_SAVE],
    )
    halt_trace = execution_trace_from_halt(blind_halt, trace_id="probe")
    correction = resolution_demonstration(
        halt_trace,
        resolution_steps=[TraceStep(intent=INTENT_DISMISS, action=ActionKind.CLICK)],
        tail_intents=(INTENT_VERIFY,),
        trace_id="corr",
    )  # carries NO fact (halt observed none)

    outcome, _ = learn_from_halt(
        library,
        SKILL_ID,
        halt_report=blind_halt,
        correction=correction,
        inducer=StructuralDiffInducer(),
        baseline=[_clean_success_trace()],
    )

    assert outcome.action == "quarantined"
    assert "canary" in outcome.reason.lower()
    # The active version is untouched -> the workflow still halts on modal-once.
    assert library.active_version(SKILL_ID).version == 1
    wf = promoted_workflow(library, SKILL_ID, name=SKILL_ID)
    r, _ = _replay(wf, modal_text=MODAL_FACT, bundle=bundle, run_dir=run_dir)
    assert r.success is False and r.terminal_outcome == "halt"


class _IdentityWeakeningInducer:
    """A rigged inducer that 'resolves' the halt by SILENTLY dropping an armed
    step's recorded identity band -- the exact regression PR #70's gate exists to
    catch. Stands in for a bad revision the loop must never promote."""

    def induce(self, traces, *, base: Optional[ProgramGraph] = None) -> ProgramGraph:
        assert base is not None
        graph = base.model_copy(deep=True)
        for s in graph.states.values():
            if (
                s.kind is StateKind.ACTION
                and s.step is not None
                and s.step.intent == INTENT_OPEN
                and s.step.anchor is not None
            ):
                s.step.anchor.context_text = None  # weaken identity
        return graph


def test_regression_gate_blocks_identity_weakening_correction(tmp_path):
    """The RegressionGate arm of the loop: a candidate that would weaken an armed
    step's identity band is REFUSED before any canary, and quarantined."""
    library = SkillLibrary(tmp_path / "skills")
    library.create_skill(SKILL_ID, mockmed_base_program())

    # A novel successful correction (a new consent dialog step) so the loop
    # proceeds to induce + gate; the rigged inducer returns the weakened program.
    correction = ExecutionTrace(
        trace_id="corr",
        outcome="success",
        steps=[
            TraceStep(intent="Sign in"),
            TraceStep(intent="Acknowledge consent notice"),
            TraceStep(intent=INTENT_OPEN),
            TraceStep(intent="Start new encounter"),
            TraceStep(intent="Enter note", action=ActionKind.TYPE),
            TraceStep(intent="Save encounter"),
        ],
        facts={"Consent Required": True},
    )
    halt_report = RunReport(workflow_name=SKILL_ID, started_at="t")
    halt_report.halt = HaltObservation(
        intent="Start new encounter",
        reason="unexpected consent dialog",
        observed_texts=["Consent Required"],
        completed_intents=["Sign in", INTENT_OPEN],
    )

    outcome, _ = learn_from_halt(
        library,
        SKILL_ID,
        halt_report=halt_report,
        correction=correction,
        inducer=_IdentityWeakeningInducer(),
    )

    assert outcome.action == "quarantined"
    assert outcome.gate is not None and not outcome.gate.passed
    assert "regression gate" in outcome.reason.lower()
    assert library.active_version(SKILL_ID).version == 1  # active retained


def test_program_regression_gate_unit_flags_dropped_band():
    """Direct unit check the loop relies on: dropping an armed step's identity
    band trips the program-level regression gate."""
    base = mockmed_base_program()
    weakened = _IdentityWeakeningInducer().induce([], base=base)
    report = program_regression_gate(base, weakened)
    assert not report.passed
    assert any("s_open" in f for f in report.failures)
