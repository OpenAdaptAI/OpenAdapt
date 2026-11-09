"""The continuous-learning LOOP: cluster -> revise -> validate -> promote/reject.

This is the orchestration the review's item 7 asks for -- "cluster successful and
failed traces, update the inferred state machine, test candidate revisions on
held-out executions, and promote only verified versions" -- assembled from parts
that already exist:

- CLUSTER: :func:`openadapt_flow.learning.clustering.cluster_traces` partitions
  the batch into successes / failures / novel variants (novelty = a successful
  trace the active program cannot reproduce).
- COVERAGE CHECK: if the active program already reproduces every successful trace
  (:func:`openadapt_flow.learning.interpreter.program_reproduces`) and nothing is
  novel, there is NOTHING to learn -- the loop stays stable (no churn on noise).
- REVISE: when a novel structure appears, an injected :class:`Inducer` (the
  sibling multi-trace-induction PR, stubbed in tests) produces a CANDIDATE
  revised :class:`~openadapt_flow.ir.ProgramGraph` over the fit traces.
- VALIDATE + PROMOTE/REJECT: the candidate runs through PR #70's promotion
  posture, lifted to a whole program: a deterministic GATE
  (:func:`openadapt_flow.learning.gate.program_regression_gate`, which reuses
  PR #70's ``RegressionGate`` per surviving step -- identity / effect / risk may
  not regress) followed by a CANARY (held-out coverage must IMPROVE without
  regressing, plus any injected perturbation veto). A candidate is promoted to
  active ONLY if it passes BOTH; otherwise the active version is retained and the
  candidate is quarantined with the reason -- never a silent adoption of an
  unverified revision.

Everything is deterministic and ``$0``; no model calls at runtime. The inducer is
the only component that might, in production, be model-backed -- and it is
deliberately behind a Protocol so the runtime loop never calls it directly.

Calibration note: the CLUSTERING thresholds (how many novel traces justify a
revision -- ``min_variant_support``) and the held-out SPLIT ratio
(``holdout_fraction``) are the parameters a real deployment must tune on real
data; the synthetic defaults here are permissive so the mechanism is exercised.
"""

from __future__ import annotations

from typing import Callable, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from openadapt_flow.ir import ProgramGraph
from openadapt_flow.learning.clustering import TraceClusters, cluster_traces
from openadapt_flow.learning.gate import ProgramGateReport, program_regression_gate
from openadapt_flow.learning.interpreter import program_reproduces
from openadapt_flow.learning.library import Provenance, SkillLibrary
from openadapt_flow.learning.trace import ExecutionTrace
from openadapt_flow.runtime.healing.governance import RegressionGate


@runtime_checkable
class Inducer(Protocol):
    """Thin seam onto multi-trace INDUCTION (a sibling PR).

    ``induce`` generalises a set of successful traces (old + new) into a revised
    program graph, optionally starting from the current ``base``. The runtime
    loop depends ONLY on this Protocol -- the real inducer (which may be
    model-backed and is therefore never on the ``$0`` runtime hot path) is wired
    in at merge; tests supply deterministic stubs.
    """

    def induce(
        self,
        traces: list[ExecutionTrace],
        *,
        base: Optional[ProgramGraph] = None,
    ) -> ProgramGraph: ...


class CanaryContext(BaseModel):
    """What an injected canary sees to (optionally) veto a gate-passing
    candidate -- mirrors PR #70's canary callable, at program grain."""

    model_config = {"arbitrary_types_allowed": True}

    active: ProgramGraph
    candidate: ProgramGraph
    subflows: dict[str, ProgramGraph] = Field(default_factory=dict)
    holdout: list[ExecutionTrace] = Field(default_factory=list)
    novel: list[ExecutionTrace] = Field(default_factory=list)


#: An injected apply-and-monitor step that can still veto a gate-passing
#: candidate (e.g. a perturbation-harness run). Returns ``(ok, reason)``.
ProgramCanaryFn = Callable[[CanaryContext], "tuple[bool, str]"]


class LearnOutcome(BaseModel):
    """The result of one :func:`learn_from_traces` cycle."""

    skill_id: str
    action: str  # "no_change" | "promoted" | "quarantined"
    reason: str
    active_version: int
    candidate_version: Optional[int] = None
    clusters: str = ""  # cluster summary (diagnostic)
    coverage_before: float = 0.0
    coverage_after: float = 0.0
    gate: Optional[ProgramGateReport] = None
    canary_ran: bool = False
    canary_reason: str = ""

    @property
    def promoted(self) -> bool:
        return self.action == "promoted"


def _coverage(
    graph: ProgramGraph,
    traces: list[ExecutionTrace],
    subflows: dict[str, ProgramGraph],
) -> float:
    """Fraction of successful traces ``graph`` reproduces (0.0 for an empty
    set -- vacuously no coverage to compare)."""
    successes = [t for t in traces if t.succeeded]
    if not successes:
        return 0.0
    reproduced = sum(
        1
        for t in successes
        if program_reproduces(graph, t, subflows=subflows).reproduced
    )
    return reproduced / len(successes)


def _split_holdout(
    successes: list[ExecutionTrace], holdout_fraction: float
) -> tuple[list[ExecutionTrace], list[ExecutionTrace]]:
    """Deterministic per-signature fit/holdout split.

    Grouping by signature guarantees the inducer's FIT set sees every observed
    variant (so it can learn the novel one), while the HOLDOUT set holds genuine
    UNSEEN instances of any signature with more than one trace -- the executions
    a candidate is validated on but was never fitted to. A singleton signature
    contributes only to fit (it cannot be held out without hiding it from the
    inducer)."""
    by_sig: dict[str, list[ExecutionTrace]] = {}
    for t in sorted(successes, key=lambda x: x.trace_id):
        by_sig.setdefault(t.signature, []).append(t)
    fit: list[ExecutionTrace] = []
    holdout: list[ExecutionTrace] = []
    for group in by_sig.values():
        if len(group) == 1:
            fit.append(group[0])
            continue
        n_hold = max(1, int(round(len(group) * holdout_fraction)))
        n_hold = min(n_hold, len(group) - 1)  # always keep >=1 in fit
        fit.extend(group[:-n_hold])
        holdout.extend(group[-n_hold:])
    return fit, holdout


def learn_from_traces(
    library: SkillLibrary,
    skill_id: str,
    new_traces: list[ExecutionTrace],
    *,
    inducer: Inducer,
    gate: Optional[RegressionGate] = None,
    min_variant_support: int = 1,
    holdout_fraction: float = 0.5,
    canary: Optional[ProgramCanaryFn] = None,
) -> LearnOutcome:
    """Run one learn/promote cycle for ``skill_id`` over ``new_traces``.

    See the module docstring for the full pipeline. Returns a
    :class:`LearnOutcome` describing what happened (no_change / promoted /
    quarantined) and why. The skill's active version is only ever REPLACED by a
    candidate that passed both the regression gate and the held-out canary.
    """
    active = library.active_version(skill_id)
    if active is None:
        raise ValueError(f"skill {skill_id!r} has no active version to learn from")
    active_graph = active.graph
    subflows = active.subflows

    # Accumulate the observed executions, then cluster THIS batch.
    library.extend_corpus(skill_id, new_traces)
    clusters: TraceClusters = cluster_traces(
        new_traces,
        active_graph,
        subflows=subflows,
        min_variant_support=min_variant_support,
    )

    # -- coverage check: nothing novel => nothing to learn (stability) --------
    if not clusters.has_novelty:
        return LearnOutcome(
            skill_id=skill_id,
            action="no_change",
            reason=(
                "active version already reproduces every successful trace in "
                f"the batch; {len(clusters.failures)} failure(s) present but no "
                "novel structure justifies a revision (stable)"
            ),
            active_version=active.version,
            clusters=clusters.summary(),
            coverage_before=1.0,
            coverage_after=1.0,
        )

    # -- revise: induce a candidate over the fit successes (old + new) --------
    corpus_successes = [t for t in library.get(skill_id).corpus if t.succeeded]
    fit, holdout = _split_holdout(corpus_successes, holdout_fraction)
    validation = holdout or corpus_successes  # fall back if nothing held out
    novel_traces = clusters.novel_traces()

    candidate_graph = inducer.induce(fit, base=active_graph)
    candidate = library.add_candidate(
        skill_id,
        candidate_graph,
        subflows=subflows,
        provenance=Provenance(
            parent_version=active.version,
            trace_ids=[t.trace_id for t in fit],
            note=(
                f"revision induced from {len(fit)} fit trace(s) after "
                f"{len(novel_traces)} novel trace(s): {clusters.summary()}"
            ),
        ),
    )

    coverage_before = _coverage(active_graph, validation, subflows)
    coverage_after = _coverage(candidate_graph, validation, subflows)

    # -- validate: GATE (reuse PR #70's RegressionGate per surviving step) ----
    gate_report = program_regression_gate(
        active_graph,
        candidate_graph,
        active_subflows=subflows,
        candidate_subflows=subflows,
        gate=gate,
    )
    if not gate_report.passed:
        reason = (
            "candidate REJECTED by the regression gate (identity/effect/risk "
            "would regress on a surviving step): " + "; ".join(gate_report.failures)
        )
        library.quarantine(skill_id, candidate.version, reason)
        return LearnOutcome(
            skill_id=skill_id,
            action="quarantined",
            reason=reason,
            active_version=active.version,
            candidate_version=candidate.version,
            clusters=clusters.summary(),
            coverage_before=coverage_before,
            coverage_after=coverage_after,
            gate=gate_report,
        )

    # -- validate: CANARY (held-out coverage must improve w/o regressing) -----
    # No held-out success the active version reproduced may become unreproduced.
    regressed = [
        t
        for t in validation
        if t.succeeded
        and program_reproduces(active_graph, t, subflows=subflows).reproduced
        and not program_reproduces(candidate_graph, t, subflows=subflows).reproduced
    ]
    # The candidate must actually cover the novel executions it was induced for.
    still_uncovered = [
        t
        for t in novel_traces
        if not program_reproduces(candidate_graph, t, subflows=subflows).reproduced
    ]

    canary_reason = ""
    canary_ok = True
    if regressed:
        canary_ok = False
        canary_reason = (
            f"{len(regressed)} held-out trace(s) the active version reproduced "
            "would no longer be reproduced by the candidate (coverage regression)"
        )
    elif still_uncovered:
        canary_ok = False
        canary_reason = (
            f"candidate still fails to reproduce {len(still_uncovered)} of the "
            "novel trace(s) it was induced for"
        )
    elif coverage_after <= coverage_before:
        canary_ok = False
        canary_reason = (
            f"candidate does not improve held-out coverage "
            f"({coverage_before:.2f} -> {coverage_after:.2f})"
        )

    # Optional injected canary (e.g. a perturbation-harness veto) -- may still
    # refuse a candidate that passed the built-in coverage canary.
    if canary_ok and canary is not None:
        ok, reason = canary(
            CanaryContext(
                active=active_graph,
                candidate=candidate_graph,
                subflows=subflows,
                holdout=validation,
                novel=novel_traces,
            )
        )
        if not ok:
            canary_ok = False
            canary_reason = f"injected canary veto: {reason}"

    if not canary_ok:
        reason = f"candidate REJECTED by the canary: {canary_reason}"
        library.quarantine(skill_id, candidate.version, reason)
        return LearnOutcome(
            skill_id=skill_id,
            action="quarantined",
            reason=reason,
            active_version=active.version,
            candidate_version=candidate.version,
            clusters=clusters.summary(),
            coverage_before=coverage_before,
            coverage_after=coverage_after,
            gate=gate_report,
            canary_ran=True,
            canary_reason=canary_reason,
        )

    # -- promote: verified revision becomes the new active version ------------
    candidate.validation_score = coverage_after
    candidate.reason = ""
    library.promote(skill_id, candidate.version)
    return LearnOutcome(
        skill_id=skill_id,
        action="promoted",
        reason=(
            "candidate PASSED the regression gate and improved held-out "
            f"coverage ({coverage_before:.2f} -> {coverage_after:.2f}) without "
            "regression; promoted to active"
        ),
        active_version=candidate.version,
        candidate_version=candidate.version,
        clusters=clusters.summary(),
        coverage_before=coverage_before,
        coverage_after=coverage_after,
        gate=gate_report,
        canary_ran=canary is not None,
    )
