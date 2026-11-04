"""A deterministic, model-free inducer: splice a newly observed step in.

:class:`StructuralDiffInducer` diffs successful traces against a base program
and splices in the one step they show that the program lacks, guarded on the
screen fact that co-occurs with it. ``openadapt-flow teach`` and the branch
teach use it; it plugs into the learn/promote loop through the
:class:`~openadapt_flow.learning.loop.Inducer` Protocol. It does not invent
identity, effects or risk, so the regression gate still decides promotion.

All deterministic, ``$0``, no model calls.
"""

from __future__ import annotations

from collections.abc import Mapping
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
from openadapt_flow.learning.trace import ExecutionTrace
from openadapt_flow.runtime.effects.effect import Effect

__all__ = ["StructuralDiffInducer"]


def _anchor(
    *, context_text: Optional[str] = None, cp: tuple[int, int] = (100, 100)
) -> Anchor:
    return Anchor(
        template="templates/x.png",
        region=(cp[0] - 10, cp[1] - 10, 20, 20),
        click_point=cp,
        context_text=context_text,
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
    context_text: Optional[str] = None,
) -> State:
    """An action state with a placeholder anchor; ``context_text`` is the
    identity band an armed step's anchor carries."""
    step = Step(
        id=sid,
        intent=intent,
        action=action,
        anchor=None
        if action in (ActionKind.KEY, ActionKind.WAIT)
        else _anchor(context_text=context_text if armed else None),
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
