"""Synthetic trace-stream harness: a MockMed skill drifting over "time".

To exercise the learn/promote loop end-to-end without a live app, this module

1. builds a base MockMed skill program (``add-patient-note`` from
   ``docs/design/WORKFLOW_PROGRAM_IR.md`` section 2.5: sign in -> open the
   patient record -> new encounter -> type note -> SAVE), and
2. emits a STREAM of :class:`~openadapt_flow.learning.trace.ExecutionTrace`s over
   a logical time axis with INJECTED DRIFT at a chosen trace index -- e.g. a new
   consent dialog starts appearing mid-stream, so the active program stops
   reproducing the new successful traces and the loop must detect the novelty,
   induce a revision, validate it, and promote it.

It also provides :class:`StructuralDiffInducer`, a deterministic, model-free
REFERENCE inducer used by the harness/tests. It is a stand-in for the real
multi-trace-induction sibling PR (wired in at merge via the
:class:`~openadapt_flow.learning.loop.Inducer` Protocol): it induces exactly the
"a new optional step appeared" revision -- detect an intent present in some
successful traces but absent from the base program, and splice it in as a
GUARDED optional step (a Phase-1 ``skip`` guard keyed on the screen fact that
co-occurs with it). It does NOT invent identity/effect/risk -- a dialog
dismissal is reversible and unarmed -- so the regression gate has nothing to
refuse on the happy path, exactly as intended.

All deterministic, ``$0``, no model calls.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    Guard,
    Predicate,
    PredicateKind,
    ProgramGraph,
    State,
    StateKind,
    Step,
    Transition,
)
from openadapt_flow.learning.trace import ExecutionTrace, TraceStep
from openadapt_flow.runtime.effects.effect import Effect, EffectKind

# -- the MockMed skill's canonical intents ------------------------------------

INTENT_LOGIN = "Sign in"
INTENT_OPEN = "Open patient record"
INTENT_ENCOUNTER = "Start new encounter"
INTENT_NOTE = "Enter note"
INTENT_SAVE = "Save encounter"
INTENT_CONSENT = "Acknowledge consent notice"

#: The screen fact that co-occurs with the injected consent dialog -- the text a
#: ``TEXT_PRESENT`` guard keys on.
FACT_CONSENT = "Consent Required"

#: A representative identity band for the patient row (name + DOB) -- the
#: evidence the pre-click identity gate keys on; makes the "open patient" step
#: identity-ARMED so an identity regression is meaningful.
PATIENT_BAND = "Belford, Phil DOB 1990-01-01"


def _anchor(*, armed: bool = False, cp: tuple[int, int] = (100, 100)) -> Anchor:
    return Anchor(
        template="templates/x.png",
        region=(cp[0] - 10, cp[1] - 10, 20, 20),
        click_point=cp,
        context_text=PATIENT_BAND if armed else None,
    )


def _action_state(
    sid: str,
    intent: str,
    target: str,
    *,
    action: ActionKind = ActionKind.CLICK,
    armed: bool = False,
    risk: str = "reversible",
    effects: Optional[list[Effect]] = None,
    guard: Optional[Guard] = None,
    text: Optional[str] = None,
) -> State:
    step = Step(
        id=sid,
        intent=intent,
        action=action,
        anchor=None
        if action in (ActionKind.KEY, ActionKind.WAIT)
        else _anchor(armed=armed),
        text=text,
        risk=risk,  # type: ignore[arg-type]
        effects=effects or [],
        guard=guard,
        identity_armed=armed if action is ActionKind.CLICK else None,
    )
    return State(
        id=sid,
        kind=StateKind.ACTION,
        step=step,
        transitions=[Transition(target=target, label="")],
    )


def mockmed_base_program() -> ProgramGraph:
    """The base ``add-patient-note`` program (no consent dialog).

    A straight-line state machine: sign in -> open patient (identity-armed) ->
    new encounter -> type note -> SAVE (irreversible, with a ``record_written``
    system-of-record effect) -> success. The identity band + effect + risk on
    the sensitive steps are what a rigged revision could weaken, and what the
    regression gate protects.
    """
    save_effect = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"type": "encounter", "patient": "p1"},
        expected_count=1,
        risk="irreversible",
        probe="encounter record written for patient",
    )
    states = {
        "s_login": _action_state("s_login", INTENT_LOGIN, "s_open"),
        "s_open": _action_state("s_open", INTENT_OPEN, "s_encounter", armed=True),
        "s_encounter": _action_state("s_encounter", INTENT_ENCOUNTER, "s_note"),
        "s_note": _action_state(
            "s_note", INTENT_NOTE, "s_save", action=ActionKind.TYPE, text="Follow-up"
        ),
        "s_save": _action_state(
            "s_save",
            INTENT_SAVE,
            "__end__",
            risk="irreversible",
            effects=[save_effect],
        ),
        "__end__": State(id="__end__", kind=StateKind.TERMINAL, outcome="success"),
    }
    return ProgramGraph(entry="s_login", states=states)


# -- the drift stream ---------------------------------------------------------


class Drift(str, Enum):
    """The injected drift/variant a stream can carry from trace K onward."""

    NONE = "none"
    #: A new consent dialog appears after sign-in (a new OPTIONAL step + branch).
    CONSENT_DIALOG = "consent_dialog"
    #: The note field is renamed (the observed intent changes) -- a structural
    #: change the additive reference inducer CANNOT cover (used to show the loop
    #: correctly refuses an inadequate revision).
    FIELD_RENAME = "field_rename"


def _baseline_steps() -> list[TraceStep]:
    return [
        TraceStep(intent=INTENT_LOGIN),
        TraceStep(intent=INTENT_OPEN, identity=PATIENT_BAND),
        TraceStep(intent=INTENT_ENCOUNTER),
        TraceStep(intent=INTENT_NOTE, action=ActionKind.TYPE),
        TraceStep(intent=INTENT_SAVE),
    ]


def _drifted_steps(drift: Drift) -> tuple[list[TraceStep], dict[str, bool]]:
    steps = _baseline_steps()
    facts: dict[str, bool] = {}
    if drift is Drift.CONSENT_DIALOG:
        # The consent dialog appears right after sign-in.
        steps.insert(1, TraceStep(intent=INTENT_CONSENT))
        facts[FACT_CONSENT] = True
    elif drift is Drift.FIELD_RENAME:
        for s in steps:
            if s.intent == INTENT_NOTE:
                s.intent = "Enter clinical note"
    return steps, facts


def generate_stream(
    *,
    n_baseline: int = 6,
    n_drift: int = 6,
    drift: Drift = Drift.CONSENT_DIALOG,
    n_failures: int = 0,
    prefix: str = "mockmed",
) -> list[ExecutionTrace]:
    """Emit a deterministic stream of traces with drift injected at trace
    ``n_baseline``.

    The first ``n_baseline`` traces are clean successful runs of the base skill;
    from index ``n_baseline`` the injected ``drift`` appears in every successful
    run. ``n_failures`` failure traces (noise) are appended -- runs that halted
    partway with no novel successful structure, so a batch of them alone must NOT
    trigger a revision.
    """
    traces: list[ExecutionTrace] = []
    t = 0
    for _ in range(n_baseline):
        traces.append(
            ExecutionTrace(
                trace_id=f"{prefix}-{t:04d}",
                t=t,
                outcome="success",
                steps=_baseline_steps(),
                params={"encounter_type": "Triage"},
            )
        )
        t += 1
    for _ in range(n_drift):
        steps, facts = _drifted_steps(drift)
        traces.append(
            ExecutionTrace(
                trace_id=f"{prefix}-{t:04d}",
                t=t,
                outcome="success",
                steps=steps,
                facts=facts,
                params={"encounter_type": "Triage"},
            )
        )
        t += 1
    for _ in range(n_failures):
        # A run that halted after opening the patient (e.g. a transient
        # resolution failure): truncated, no novel successful structure.
        traces.append(
            ExecutionTrace(
                trace_id=f"{prefix}-{t:04d}",
                t=t,
                outcome="failure",
                steps=_baseline_steps()[:2],
                failure_reason="transient resolution failure on 'Start new encounter'",
                params={"encounter_type": "Triage"},
            )
        )
        t += 1
    return traces


# -- the reference (stand-in) inducer -----------------------------------------


class StructuralDiffInducer:
    """Deterministic reference inducer: splice a newly-observed OPTIONAL step in.

    A stand-in for the real multi-trace-induction PR, implementing exactly the
    "a new optional step appeared" case: it diffs the fit traces against a base
    program, finds an intent that occurs in SOME successful traces but is not an
    action in the base program, and inserts it as a GUARDED optional step (a
    Phase-1 ``skip`` guard keyed on the co-occurring screen fact) after the
    base step it consistently follows. Reversible + unarmed by construction (a
    dialog dismissal has no identity/effect), so it never fabricates the safety
    evidence the regression gate protects.

    It handles ONLY the additive-optional-step revision; any other structural
    drift (a renamed field, a new loop) yields the base program UNCHANGED, so the
    loop's canary correctly finds the candidate still fails to cover the novelty
    and refuses it -- the honest "I could not learn this" path.
    """

    def induce(
        self,
        traces: list[ExecutionTrace],
        *,
        base: Optional[ProgramGraph] = None,
    ) -> ProgramGraph:
        if base is None:
            raise ValueError("StructuralDiffInducer requires a base program")
        base_intents = {
            s.step.intent
            for s in base.states.values()
            if s.kind is StateKind.ACTION and s.step is not None
        }
        successes = [t for t in traces if t.succeeded]

        # Find a new intent, its consistent predecessor, and its guard fact.
        new_intent = self._find_new_intent(successes, base_intents)
        if new_intent is None:
            return base.model_copy(deep=True)
        predecessor = self._predecessor_intent(successes, new_intent, base_intents)
        fact = self._guard_fact(successes, new_intent)
        if predecessor is None or fact is None:
            return base.model_copy(deep=True)
        return self._splice_optional_step(base, new_intent, predecessor, fact)

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _find_new_intent(
        successes: list[ExecutionTrace], base_intents: set[str]
    ) -> Optional[str]:
        for trace in successes:
            for step in trace.steps:
                if step.intent not in base_intents:
                    return step.intent
        return None

    @staticmethod
    def _predecessor_intent(
        successes: list[ExecutionTrace], new_intent: str, base_intents: set[str]
    ) -> Optional[str]:
        """The base intent the new step consistently follows across traces."""
        preds: set[str] = set()
        for trace in successes:
            intents = [s.intent for s in trace.steps]
            if new_intent not in intents:
                continue
            idx = intents.index(new_intent)
            if idx == 0:
                return None  # inserting at the very start is out of scope
            prev = intents[idx - 1]
            if prev in base_intents:
                preds.add(prev)
        return next(iter(preds)) if len(preds) == 1 else None

    @staticmethod
    def _guard_fact(successes: list[ExecutionTrace], new_intent: str) -> Optional[str]:
        """A fact TRUE in exactly the traces that contain the new step and
        absent from those that do not -- the guard predicate's text."""
        with_step, without_step = [], []
        for trace in successes:
            (
                with_step
                if any(s.intent == new_intent for s in trace.steps)
                else without_step
            ).append(trace)
        if not with_step:
            return None
        candidate_facts = {k for t in with_step for k, v in t.facts.items() if v}
        for fact in sorted(candidate_facts):
            if all(t.facts.get(fact, False) for t in with_step) and not any(
                t.facts.get(fact, False) for t in without_step
            ):
                return fact
        return None

    @staticmethod
    def _splice_optional_step(
        base: ProgramGraph, new_intent: str, predecessor: str, fact: str
    ) -> ProgramGraph:
        graph = base.model_copy(deep=True)
        # Locate the predecessor action state and its current successor.
        pred_state = next(
            s
            for s in graph.states.values()
            if s.kind is StateKind.ACTION
            and s.step is not None
            and s.step.intent == predecessor
        )
        old_target = (
            pred_state.transitions[0].target if pred_state.transitions else "__end__"
        )
        new_sid = "s_" + new_intent.lower().replace(" ", "_")[:24]
        guard = Guard(
            predicate=Predicate(
                kind=PredicateKind.TEXT_PRESENT, text=fact, intent=f"{fact} present"
            ),
            on_unmet="skip",
        )
        new_state = _action_state(new_sid, new_intent, old_target, guard=guard)
        graph.states[new_sid] = new_state
        pred_state.transitions = [Transition(target=new_sid, label="")]
        return graph
