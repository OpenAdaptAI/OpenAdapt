"""Close the HALT -> LEARN -> RESOLVE loop for ONE governed scenario.

Today a HALT is a dead stop: the Replayer refuses rather than guessing on an
unhandled state, but nothing learns from the operator's post-halt correction.
The continuous-learning scaffold (:mod:`openadapt_flow.learning.loop`,
:mod:`~openadapt_flow.learning.gate`, :mod:`~openadapt_flow.learning.library`)
and the multi-trace inducer (:mod:`openadapt_flow.compiler.induction`,
:mod:`openadapt_flow.learning.synth_stream`) already EXIST but no real run feeds
them. This module is the thin BRIDGE that makes the loop real:

1. **A halt emits a learnable trace.** ``Replayer.run`` now populates
   ``RunReport.halt`` (:class:`~openadapt_flow.ir.HaltObservation`) with the halt
   point, the observed unexpected on-screen text, and the completed pre-context.
   :func:`execution_trace_from_halt` lifts that record into an
   :class:`~openadapt_flow.learning.trace.ExecutionTrace` — the SAME type the
   loop already consumes (no parallel system).

2. **The operator correction is a demonstration.**
   :func:`resolution_demonstration` extends the halt's pre-context with the
   operator's resolving actions (dismiss the modal, then continue), carrying the
   observed screen fact forward — the same shape a recording produces.

3. **Induce + compile through the GOVERNED path.** :func:`learn_from_halt` feeds
   the demonstration to the UNCHANGED :func:`~openadapt_flow.learning.loop.learn_from_traces`,
   which clusters, induces a candidate :class:`~openadapt_flow.ir.ProgramGraph`
   (the resolution compiled as a guarded conditional branch on the program
   graph), GATES it with PR #70's :class:`RegressionGate` lifted per step, and
   validates it on held-out coverage before promoting. If the single correction
   underdetermines the generalization the inducer returns the base unchanged, the
   canary sees the novelty still uncovered, and the loop REFUSES to promote — the
   workflow stays halting, same discipline as multi-trace induction.

Deterministic and ``$0`` — no model calls. Scenario-agnostic: the modal-once
proof lives in the tests; this module carries no scenario specifics.
"""

from __future__ import annotations

from typing import Optional

from openadapt_flow.ir import ProgramGraph, RunReport, Workflow
from openadapt_flow.learning.library import SkillLibrary
from openadapt_flow.learning.loop import (
    Inducer,
    LearnOutcome,
    ProgramCanaryFn,
    learn_from_traces,
)
from openadapt_flow.learning.trace import ExecutionTrace, TraceStep
from openadapt_flow.runtime.healing.governance import RegressionGate


def execution_trace_from_halt(
    report: RunReport,
    *,
    trace_id: str,
    params: Optional[dict[str, str]] = None,
) -> ExecutionTrace:
    """Lift a halted :class:`RunReport` into a FAILURE
    :class:`ExecutionTrace` — the learnable record item 1 emits.

    The completed pre-context becomes the trace's ordered actions; the observed
    unexpected on-screen text becomes ``facts`` (each present); the halt reason is
    kept for the audit trail. A failure trace never drives induction on its own
    (the loop learns from the SUCCESS demonstration that resolves it) — it records
    WHAT was unhandled and is the anchor the demonstration extends.
    """
    halt = report.halt
    if halt is None:
        raise ValueError(
            "report has no halt observation to learn from (run did not halt, "
            "or predates halt emission)"
        )
    steps = [TraceStep(intent=i) for i in halt.completed_intents]
    facts = {text: True for text in halt.observed_texts}
    return ExecutionTrace(
        trace_id=trace_id,
        outcome="failure",
        steps=steps,
        facts=facts,
        params=dict(params or report.params),
        failure_reason=halt.reason,
    )


def resolution_demonstration(
    halt_trace: ExecutionTrace,
    *,
    resolution_steps: list[TraceStep],
    tail_intents: tuple[str, ...] = (),
    trace_id: str,
    params: Optional[dict[str, str]] = None,
) -> ExecutionTrace:
    """Build the operator-correction demonstration (item 2) as a SUCCESS trace.

    Extends the halt's completed pre-context with the operator's ``resolution_steps``
    (e.g. dismiss the modal) and the ``tail_intents`` that then complete (the
    originally-blocked step and any remainder), carrying the halt's observed
    ``facts`` forward so the induced branch guard keys on the SAME screen fact the
    halt named. This is the same shape a demonstration recording produces, so it
    flows through the ordinary induction path — not a bespoke shortcut.
    """
    steps = (
        list(halt_trace.steps)
        + list(resolution_steps)
        + [TraceStep(intent=i) for i in tail_intents]
    )
    return ExecutionTrace(
        trace_id=trace_id,
        outcome="success",
        steps=steps,
        facts=dict(halt_trace.facts),
        params=dict(params or halt_trace.params),
    )


def learn_from_halt(
    library: SkillLibrary,
    skill_id: str,
    *,
    halt_report: RunReport,
    correction: ExecutionTrace,
    inducer: Inducer,
    gate: Optional[RegressionGate] = None,
    baseline: Optional[list[ExecutionTrace]] = None,
    min_variant_support: int = 1,
    holdout_fraction: float = 0.5,
    canary: Optional[ProgramCanaryFn] = None,
) -> tuple[LearnOutcome, ExecutionTrace]:
    """Run the governed halt->learn cycle for ``skill_id`` (item 3 + item 4).

    Emits the halt trace (recorded in the corpus for provenance), seeds any prior
    clean ``baseline`` successes (a real deployment already has these), then runs
    the UNCHANGED :func:`learn_from_traces` over the operator ``correction``: it
    induces the resolution as a guarded branch, gates it (identity/effect/risk may
    not regress), and promotes ONLY if held-out coverage improves without
    regression. Returns the :class:`LearnOutcome` and the emitted halt trace.
    """
    halt_trace = execution_trace_from_halt(
        halt_report, trace_id=f"{skill_id}-halt-{len(library.get(skill_id).corpus)}"
    )
    # Record the halt in the corpus for provenance (failure traces are excluded
    # from the success sets induction / coverage use, so this is audit-only).
    library.extend_corpus(skill_id, [halt_trace])
    if baseline:
        library.extend_corpus(skill_id, baseline)
    outcome = learn_from_traces(
        library,
        skill_id,
        [correction],
        inducer=inducer,
        gate=gate or RegressionGate(),
        min_variant_support=min_variant_support,
        holdout_fraction=holdout_fraction,
        canary=canary,
    )
    return outcome, halt_trace


def promoted_workflow(library: SkillLibrary, skill_id: str, *, name: str) -> Workflow:
    """Materialize the skill's ACTIVE version as a replayable ``Workflow``.

    The promoted :class:`ProgramGraph` becomes ``Workflow.program`` (with its
    subflows) — the first real thing to set ``.program`` from a learned revision,
    replayed by the UNCHANGED Phase-2 interpreter. A skill whose candidate was
    refused still materializes its (unchanged) active version, so the workflow
    stays halting exactly as before promotion was attempted.
    """
    active = library.active_version(skill_id)
    if active is None:
        raise ValueError(f"skill {skill_id!r} has no active version")
    graph: ProgramGraph = active.graph
    return Workflow(name=name, program=graph, subflows=dict(active.subflows))
