"""Deterministic clustering of a batch of execution traces.

The first stage of the learn loop: partition incoming traces into
SUCCESSES / FAILURES / NOVEL-VARIANTS so the loop can decide whether there is
anything to learn. Clustering is entirely deterministic and model-free -- it
keys on the trace's OUTCOME and its structural SIGNATURE (the ordered tuple of
action intents, :attr:`ExecutionTrace.signature`), plus, for novelty, whether
the current active :class:`~openadapt_flow.ir.ProgramGraph` can REPRODUCE the
trace (:func:`openadapt_flow.learning.interpreter.program_reproduces`).

A NOVEL variant is a SUCCESSFUL trace the active program cannot reproduce -- the
signal that the world drifted (a new dialog, a renamed field, a longer loop) and
the program may need a revision. Failures are surfaced separately: a cluster of
failures with no accompanying novel success is NOISE, not a mandate to change
the program (the loop must stay stable, not chase every red run).

Thresholds here (how many novel traces justify attempting a revision) are the
first thing a real deployment must CALIBRATE on real data -- see
``min_variant_support`` and the module note in ``learning.loop``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from openadapt_flow.ir import ProgramGraph
from openadapt_flow.learning.interpreter import ReproResult, program_reproduces
from openadapt_flow.learning.trace import ExecutionTrace


@dataclass
class TraceClusters:
    """The deterministic partition of a batch of traces."""

    successes: list[ExecutionTrace] = field(default_factory=list)
    failures: list[ExecutionTrace] = field(default_factory=list)
    #: Successful traces the active program cannot reproduce, grouped by
    #: structural signature -> the traces sharing it.
    novel_variants: dict[str, list[ExecutionTrace]] = field(default_factory=dict)
    #: Per-novel-signature, WHY the active program failed to reproduce it
    #: (the interpreter's first gap reason) -- the audit trail for a revision.
    novel_reasons: dict[str, str] = field(default_factory=dict)

    @property
    def has_novelty(self) -> bool:
        return bool(self.novel_variants)

    def novel_traces(self) -> list[ExecutionTrace]:
        return [t for group in self.novel_variants.values() for t in group]

    def summary(self) -> str:
        return (
            f"{len(self.successes)} success / {len(self.failures)} failure; "
            f"{len(self.novel_variants)} novel signature(s) "
            f"covering {len(self.novel_traces())} trace(s)"
        )


def cluster_traces(
    traces: list[ExecutionTrace],
    active: Optional[ProgramGraph],
    *,
    subflows: Optional[dict[str, ProgramGraph]] = None,
    min_variant_support: int = 1,
) -> TraceClusters:
    """Partition ``traces`` into successes / failures / novel variants.

    A successful trace is NOVEL when the ``active`` program does not reproduce
    it (or when there is no active program yet). Novel traces are grouped by
    signature; a signature is only reported as a variant when at least
    ``min_variant_support`` traces share it -- a single fluke execution should
    not, on its own, trigger a program revision on a well-calibrated deployment
    (the default of 1 is the permissive setting used by the synthetic tests;
    real data calibrates it up).
    """
    clusters = TraceClusters()
    novel_by_sig: dict[str, list[ExecutionTrace]] = {}
    novel_reason_by_sig: dict[str, str] = {}
    for trace in traces:
        if not trace.succeeded:
            clusters.failures.append(trace)
            continue
        clusters.successes.append(trace)
        if active is None:
            repro = ReproResult(
                reproduced=False,
                consumed=0,
                total=len(trace.steps),
                reason="no active program yet",
            )
        else:
            repro = program_reproduces(active, trace, subflows=subflows)
        if not repro.reproduced:
            novel_by_sig.setdefault(trace.signature, []).append(trace)
            novel_reason_by_sig.setdefault(trace.signature, repro.reason)

    for sig, group in novel_by_sig.items():
        if len(group) >= min_variant_support:
            clusters.novel_variants[sig] = group
            clusters.novel_reasons[sig] = novel_reason_by_sig[sig]
    return clusters
