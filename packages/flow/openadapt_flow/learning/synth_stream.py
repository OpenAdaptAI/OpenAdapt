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

from collections.abc import Mapping
from enum import Enum
from typing import Optional

from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    Guard,
    PostconditionKind,
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

#: Postconditions about what the screen shows, which stay meaningful when
#: checked after a later action (unlike "the URL changed since this step began").
_SCREEN_STATE_CHECKS = frozenset(
    {
        PostconditionKind.TEXT_PRESENT,
        PostconditionKind.TEXT_ABSENT,
        PostconditionKind.REGION_STABLE,
    }
)


class StructuralDiffInducer:
    """Deterministic reference inducer: splice a newly-observed step in.

    A stand-in for the real multi-trace-induction PR. It diffs the fit traces
    against a base program, finds an intent that occurs in SOME successful
    traces but is not an action in the base program, and splices it in, keyed
    on the screen fact that co-occurs with it. Two placements are supported,
    decided by what the traces show:

    - **Before a step** (the obstruction appeared before the next step could
      act): the new step goes after the base step it consistently follows, as
      a GUARDED optional step (a Phase-1 ``skip`` guard on the fact).
    - **After a failed step** (the step was delivered, then an obstruction
      appeared in response, so its postcondition failed): the trace shows the
      step marked ``failed`` followed by the new step. The failed step's state
      gets an ``on_exception`` edge to the new step, which halts unless the
      fact is present. When the trace then repeats the failed step (the
      dialog stopped it from taking effect), a retry of that step follows the
      new step. Otherwise the step took effect and the dialog only covered
      the result: the new step also checks the failed step's own expected
      screen, then continues to its successor. A run where the step succeeds
      never visits these states.

    The new step's on-screen target comes from ``recorded_steps`` (intent ->
    the compiled :class:`~openadapt_flow.ir.Step` of the operator's fix
    recording, with its real anchor) when one is given. Without it the step
    gets a placeholder anchor, which only the synthetic stream uses; a caller
    replaying the result on a real screen must supply recorded steps.

    The inducer invents no identity, effect, or risk evidence, so the
    regression gate keeps the final say. Anything else (a renamed field, a new
    loop, an ambiguous splice point) yields the base program UNCHANGED, so the
    loop's canary finds the novelty still uncovered and refuses it.
    """

    def __init__(self, recorded_steps: Optional[Mapping[str, Step]] = None) -> None:
        self._recorded: dict[str, Step] = dict(recorded_steps or {})

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
        placement = self._placement(successes, new_intent)
        if placement in ("after_failed_retry", "after_failed_continue"):
            return self._splice_exception_handler(
                base,
                new_intent,
                predecessor,
                fact,
                retry=placement == "after_failed_retry",
            )
        if placement == "before":
            return self._splice_optional_step(base, new_intent, predecessor, fact)
        return base.model_copy(deep=True)

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
    def _placement(successes: list[ExecutionTrace], new_intent: str) -> Optional[str]:
        """Where the traces put the new step.

        ``"after_failed_retry"``: right after a FAILED step and followed by that
        same step again (dismiss, then retry). ``"after_failed_continue"``:
        right after a FAILED step, then on to other steps. ``"before"``: the
        preceding step did not fail. ``None`` when the traces disagree (an
        ambiguous splice is not attempted)."""
        kinds: set[str] = set()
        for trace in successes:
            steps = trace.steps
            for i, step in enumerate(steps):
                if step.intent != new_intent:
                    continue
                prev = steps[i - 1] if i > 0 else None
                nxt = steps[i + 1] if i + 1 < len(steps) else None
                if prev is not None and prev.failed:
                    retried = nxt is not None and nxt.intent == prev.intent
                    kinds.add(
                        "after_failed_retry" if retried else "after_failed_continue"
                    )
                else:
                    kinds.add("before")
                break
        return next(iter(kinds)) if len(kinds) == 1 else None

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
    def _unique_action_state(graph: ProgramGraph, intent: str) -> Optional[State]:
        """The ONE action state performing ``intent``; None when absent or
        when several states share the intent (the splice point is ambiguous)."""
        matches = [
            s
            for s in graph.states.values()
            if s.kind is StateKind.ACTION
            and s.step is not None
            and s.step.intent == intent
        ]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _fresh_id(graph: ProgramGraph, wanted: str) -> str:
        sid, n = wanted, 2
        while sid in graph.states:
            sid, n = f"{wanted}_{n}", n + 1
        return sid

    def _new_state(self, sid: str, intent: str, target: str, guard: Guard) -> State:
        """The spliced action state: the operator's recorded step when one was
        supplied for ``intent``, else the placeholder the synthetic stream uses."""
        recorded = self._recorded.get(intent)
        if recorded is None:
            return _action_state(sid, intent, target, guard=guard)
        step = recorded.model_copy(deep=True, update={"id": sid, "guard": guard})
        return State(
            id=sid,
            kind=StateKind.ACTION,
            step=step,
            transitions=[Transition(target=target, label="")],
        )

    @staticmethod
    def _fact_guard(fact: str, on_unmet: str) -> Guard:
        return Guard(
            predicate=Predicate(
                kind=PredicateKind.TEXT_PRESENT, text=fact, intent=f"{fact} present"
            ),
            on_unmet=on_unmet,  # type: ignore[arg-type]
        )

    def _splice_optional_step(
        self, base: ProgramGraph, new_intent: str, predecessor: str, fact: str
    ) -> ProgramGraph:
        graph = base.model_copy(deep=True)
        # Locate the predecessor action state and its current successor.
        pred_state = self._unique_action_state(graph, predecessor)
        if pred_state is None:
            return graph
        old_target = (
            pred_state.transitions[0].target if pred_state.transitions else "__end__"
        )
        new_sid = self._fresh_id(
            graph, "s_" + new_intent.lower().replace(" ", "_")[:24]
        )
        graph.states[new_sid] = self._new_state(
            new_sid, new_intent, old_target, self._fact_guard(fact, "skip")
        )
        pred_state.transitions = [Transition(target=new_sid, label="")]
        return graph

    def _splice_exception_handler(
        self,
        base: ProgramGraph,
        new_intent: str,
        failed_intent: str,
        fact: str,
        *,
        retry: bool,
    ) -> ProgramGraph:
        graph = base.model_copy(deep=True)
        failed_state = self._unique_action_state(graph, failed_intent)
        if (
            failed_state is None
            or failed_state.step is None
            or failed_state.on_exception is not None
        ):
            return graph  # ambiguous, or an authored handler already exists
        failed_step = failed_state.step
        successors = [t.model_copy(deep=True) for t in failed_state.transitions]
        new_sid = self._fresh_id(
            graph, "s_" + new_intent.lower().replace(" ", "_")[:24]
        )
        # The handler runs only after the step failed; it halts unless the
        # obstruction the operator resolved is on screen.
        handler = self._new_state(
            new_sid, new_intent, "__end__", self._fact_guard(fact, "halt")
        )
        assert handler.step is not None
        if retry:
            retry_sid = self._fresh_id(graph, f"{failed_state.id}__retry")
            handler.transitions = [Transition(target=retry_sid, label="")]
            graph.states[retry_sid] = State(
                id=retry_sid,
                kind=StateKind.ACTION,
                step=failed_step.model_copy(
                    deep=True, update={"id": f"{failed_step.id}__retry"}
                ),
                transitions=successors,
            )
        else:
            # The failed step took effect; the dialog only covered the result.
            # After the dismissal the screen must show what the failed step was
            # expected to produce. Checks relative to the step's own starting
            # screen (URL or title changed, a new tab) are not repeated.
            handler.step.expect = list(handler.step.expect) + [
                p.model_copy(deep=True)
                for p in failed_step.expect
                if p.kind in _SCREEN_STATE_CHECKS
            ]
            handler.transitions = successors
        graph.states[new_sid] = handler
        failed_state.on_exception = new_sid
        return graph
