"""Symbolic Phase-2 coverage check: does a ``ProgramGraph`` REPRODUCE a trace?

The continuous-learning loop must answer "does the current active program still
explain the executions we just saw?" without a live app. This module walks a
:class:`~openadapt_flow.ir.ProgramGraph` deterministically over an
:class:`~openadapt_flow.learning.trace.ExecutionTrace`'s observed facts -- the
SAME control-flow rules the live Phase-2 interpreter in
:mod:`openadapt_flow.runtime.replayer` applies (guarded transitions evaluated in
order, first match wins; a guarded ``skip`` step is a no-op when its predicate
is unmet; loops bounded by ``max_iterations``) -- but instead of resolving pixels
it consumes the trace's ordered action intents and evaluates predicates against
the trace's recorded ``facts`` / ``params``.

A graph REPRODUCES a successful trace iff the walk consumes EXACTLY the trace's
observed actions, in order, and reaches a ``success`` terminal (or falls off the
graph cleanly). Anything else -- a leftover unconsumed action (the trace did
something the program has no state for), an action-state intent that does not
match, a branch with no matching edge, a ``halt`` terminal -- is a coverage GAP.

Deterministic and ``$0``: no pixels, no backend, no model calls. This is the
"replay via the Phase-2 interpreter" the design calls for, at the structural
altitude a trace stream lives at.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from openadapt_flow.ir import (
    Predicate,
    PredicateKind,
    ProgramGraph,
    State,
    StateKind,
)
from openadapt_flow.learning.trace import ExecutionTrace

# Deterministic termination guard mirroring the live interpreter's step budget
# (runtime.replayer.PROGRAM_MAX_STEPS): a graph with an authored cycle of
# always-true edges cannot spin forever during a coverage check either.
_MAX_STATE_VISITS = 10_000


@dataclass
class ReproResult:
    """Verdict of a symbolic reproduction of one trace by one graph."""

    reproduced: bool
    #: How many of the trace's ordered actions the walk consumed.
    consumed: int
    #: Total actions the trace performed (``reproduced`` requires consumed==total
    #: AND a success terminal).
    total: int
    reason: str = ""
    #: The terminal outcome the walk reached ("success" / "halt" / "escalate" /
    #: "fall_off" / ""), diagnostic.
    terminal: str = ""


def predicate_holds(pred: Optional[Predicate], trace: ExecutionTrace) -> bool:
    """Evaluate a Phase-1 :class:`Predicate` over a trace's observed facts.

    Model-free, mirroring :meth:`Replayer._predicate_holds` but reading the
    trace's structural ``facts`` / ``params`` instead of pixels:

    - ``TEXT_PRESENT`` / ``TEXT_ABSENT`` -> the text's truth in ``trace.facts``
      (a fact absent from the dict is treated as NOT present);
    - ``PARAM_EQUALS`` -> the trace's value for ``param`` equals ``value``;
    - ``ANCHOR_RESOLVES`` -> True (a recorded target is assumed locatable in a
      structural trace; a trace never encodes a resolution failure as a fact);
    - ``AND`` / ``OR`` / ``NOT`` -> boolean composition;
    - an unconditional (``None``) guard -> True;
    - an unknown kind fails safe to False (never "holds").
    """
    if pred is None:
        return True
    kind = pred.kind
    if kind is PredicateKind.TEXT_PRESENT:
        return bool(pred.text and trace.facts.get(pred.text, False))
    if kind is PredicateKind.TEXT_ABSENT:
        return not (pred.text and trace.facts.get(pred.text, False))
    if kind is PredicateKind.PARAM_EQUALS:
        return pred.param is not None and trace.params.get(pred.param) == pred.value
    if kind is PredicateKind.ANCHOR_RESOLVES:
        return True
    if kind is PredicateKind.AND:
        return all(predicate_holds(op, trace) for op in pred.operands)
    if kind is PredicateKind.OR:
        return any(predicate_holds(op, trace) for op in pred.operands)
    if kind is PredicateKind.NOT:
        return not all(predicate_holds(op, trace) for op in pred.operands)
    return False


@dataclass
class _Walk:
    """Mutable cursor threaded through a (possibly nested) graph walk."""

    trace: ExecutionTrace
    subflows: dict[str, ProgramGraph]
    pos: int = 0  # index into trace.steps consumed so far
    visits: int = 0
    fail_reason: str = ""
    terminal: str = ""

    def fail(self, reason: str) -> bool:
        if not self.fail_reason:
            self.fail_reason = reason
        return False


def _select_transition(state: State, trace: ExecutionTrace) -> Optional[str]:
    """Pick the next state id exactly as :meth:`Replayer._select_transition`:
    no transitions -> None (fall off); all unconditional -> the first; else the
    first whose guard holds; none matching -> None with a recorded reason (the
    caller treats a guarded branch with no live edge as a coverage gap)."""
    transitions = state.transitions
    if not transitions:
        return None
    if all(t.guard is None for t in transitions):
        return transitions[0].target
    for t in transitions:
        if predicate_holds(t.guard, trace):
            return t.target
    return None


def _walk_graph(graph: ProgramGraph, walk: _Walk) -> Optional[bool]:
    """Walk one (sub)graph from its entry. Returns:

    - ``True``  -> reached a ``success`` terminal (a subflow RETURNS to caller);
    - ``None``  -> fell off the graph cleanly (no outgoing edge) -- also a
      normal return for a subflow / the top program;
    - ``False`` -> a HALT / coverage gap (``walk.fail_reason`` set).
    """
    state_id: Optional[str] = graph.entry
    while state_id is not None:
        walk.visits += 1
        if walk.visits > _MAX_STATE_VISITS:
            return walk.fail(
                f"graph exceeded {_MAX_STATE_VISITS} state visits "
                "(possible non-terminating graph)"
            )
        state = graph.states.get(state_id)
        if state is None:
            return walk.fail(f"graph references undefined state '{state_id}'")

        if state.kind is StateKind.TERMINAL:
            walk.terminal = state.outcome or "success"
            if state.outcome in (None, "success"):
                return True
            return walk.fail(
                f"reached {state.outcome!r} terminal '{state.id}': {state.reason}"
            )

        result = _exec_state(graph, state, walk)
        if result is False:
            return False
        state_id = _select_transition(state, walk.trace)
        if state_id is None and state.transitions:
            # Branch/guarded state whose guards ALL failed on this trace: the
            # program has no edge for the situation the trace was in -> gap.
            return walk.fail(
                f"state '{state.id}' has transitions but none matched the "
                "trace's observed facts"
            )
    walk.terminal = walk.terminal or "fall_off"
    return None  # fell off cleanly


def _exec_state(graph: ProgramGraph, state: State, walk: _Walk) -> Optional[bool]:
    """Execute one non-terminal state's payload against the trace cursor.

    Returns ``False`` on a coverage gap / halt (``walk.fail_reason`` set),
    otherwise ``None`` (proceed to transition selection)."""
    if state.kind is StateKind.ACTION:
        return _consume_action(state, walk)
    if state.kind is StateKind.BRANCH:
        return None  # performs no action; transition selection does the work
    if state.kind is StateKind.SUBFLOW_CALL:
        return _call_subflow(state.subflow, walk)
    if state.kind is StateKind.LOOP:
        return _run_loop(state, walk)
    return walk.fail(f"state '{state.id}' has unsupported kind {state.kind!r}")


def _consume_action(state: State, walk: _Walk) -> Optional[bool]:
    """Match an ``action`` state against the trace's next action.

    Honors a Phase-1 ``skip`` guard: when the step's guard is unmet and
    ``on_unmet == "skip"`` the action is a no-op (consumes NO trace step),
    exactly as :meth:`Replayer._apply_step_gates` skips it. A ``halt`` guard
    that is unmet is a coverage gap (the live run would HALT)."""
    step = state.step
    if step is None:
        return walk.fail(f"action state '{state.id}' carries no step")
    if step.guard is not None and not predicate_holds(step.guard.predicate, walk.trace):
        if step.guard.on_unmet == "skip":
            return None  # optional step correctly skipped; consume nothing
        return walk.fail(
            f"guard for step '{step.id}' ({step.intent}) unmet on the trace "
            "(a live run would HALT here)"
        )
    trace = walk.trace
    if walk.pos >= len(trace.steps):
        return walk.fail(
            f"program expected action '{step.intent}' but the trace had no "
            "further actions"
        )
    observed = trace.steps[walk.pos]
    if observed.intent != step.intent:
        return walk.fail(
            f"action mismatch at position {walk.pos}: program expected "
            f"'{step.intent}', trace performed '{observed.intent}'"
        )
    walk.pos += 1
    return None


def _call_subflow(name: Optional[str], walk: _Walk) -> Optional[bool]:
    if not name or name not in walk.subflows:
        return walk.fail(f"subflow '{name}' is not defined")
    result = _walk_graph(walk.subflows[name], walk)
    if result is False:
        return False
    return None  # subflow returned; continue in the caller


def _run_loop(state: State, walk: _Walk) -> Optional[bool]:
    """Iterate a ``loop`` state's body subflow over the trace, greedily.

    A worklist trace does not encode row bindings, so coverage counts CONTIGUOUS
    body executions: run the body subflow against the remaining trace steps as
    long as it consumes at least one action, bounded by ``max_iterations``. A
    body iteration that consumes nothing ends the loop (the worklist is
    exhausted). This reproduces the "worklist grew" variant: the same loop body
    covers 1 row or 5 rows with no program change."""
    spec = state.loop
    if spec is None:
        return walk.fail(f"loop state '{state.id}' carries no LoopSpec")
    body = walk.subflows.get(spec.body)
    if body is None:
        return walk.fail(f"loop body subflow '{spec.body}' is not defined")
    iterations = 0
    while iterations < spec.max_iterations:
        before = walk.pos
        result = _walk_graph(body, walk)
        if result is False:
            return False
        if walk.pos == before:
            break  # body consumed no further actions: worklist exhausted
        iterations += 1
    return None


def program_reproduces(
    graph: ProgramGraph,
    trace: ExecutionTrace,
    *,
    subflows: Optional[dict[str, ProgramGraph]] = None,
) -> ReproResult:
    """Does ``graph`` symbolically reproduce ``trace``?

    Walks the graph over the trace's observed facts/params, consuming the
    trace's ordered actions. A SUCCESS trace is reproduced iff the walk reaches
    a success terminal (or falls off cleanly) with EVERY observed action
    consumed and none left over. A FAILURE trace is (by definition) never
    "reproduced" as a success -- the loop uses reproduction only to decide
    whether the successful executions are already explained.
    """
    walk = _Walk(trace=trace, subflows=subflows or {})
    result = _walk_graph(graph, walk)
    total = len(trace.steps)
    if result is False:
        return ReproResult(
            reproduced=False,
            consumed=walk.pos,
            total=total,
            reason=walk.fail_reason,
            terminal=walk.terminal,
        )
    # Reached a success terminal (True) or fell off cleanly (None).
    if walk.pos != total:
        return ReproResult(
            reproduced=False,
            consumed=walk.pos,
            total=total,
            reason=(
                f"program ended with {total - walk.pos} of {total} observed "
                "actions unconsumed (the trace did something the program has "
                "no state for)"
            ),
            terminal=walk.terminal or ("success" if result else "fall_off"),
        )
    if not trace.succeeded:
        return ReproResult(
            reproduced=False,
            consumed=walk.pos,
            total=total,
            reason="trace outcome was a failure",
            terminal=walk.terminal or ("success" if result else "fall_off"),
        )
    return ReproResult(
        reproduced=True,
        consumed=walk.pos,
        total=total,
        terminal=walk.terminal or ("success" if result else "fall_off"),
    )
