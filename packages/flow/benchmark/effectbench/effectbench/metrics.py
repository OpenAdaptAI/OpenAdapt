"""EffectBench metrics -- SWER and its co-metrics, with confidence intervals.

Every headline is a pure aggregation over a list of
:class:`~effectbench.schema.EpisodeRecord` rows, so a run's numbers can be
recomputed from the published raw rows by anyone. No single mean is reported
alone: :func:`summarize` always returns the per (category x substrate)
decomposition alongside the overall rate -- a single aggregate mean is the thing
the benchmark critiques.

Metrics:

- **SWER** -- Silent Wrong-Effect Rate = ``|SILENT_WRONG_EFFECT| / N``; split
  into wrong-write vs phantom.
- **Over-halt rate** -- ``|OVER_HALT| / N``; the availability cost. Reported
  JOINTLY with SWER (an agent reaches SWER=0 by halting on everything ->
  over-halt=100%).
- **Task success** (effect-verified) -- ``|SUCCESS| / N``; the honest number.
- **Screen success** -- ``|reported_success| / N``; what a screen-only oracle
  would have claimed.
- **Success-effect gap** -- ``screen_success - task_success``.
- **Cost / latency** -- mean + total, off the recorded model calls.
- **``pass^k``** -- fraction of tasks whose k sampled trials ALL succeeded.

Intervals: Wilson 95% for every binomial rate; an optional bootstrap for the
gap. Small-N studies report the counts too (estimates carry ``k`` / ``n``).
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from typing import Callable, Iterable, Mapping, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field

from effectbench.schema import EpisodeRecord, OutcomeLabel, SwerVariant


class Interval(BaseModel):
    """A confidence interval [lo, hi] on a rate, with the method that made it."""

    model_config = ConfigDict(extra="forbid")

    lo: float
    hi: float
    method: str = "wilson"
    confidence: float = 0.95


class RateEstimate(BaseModel):
    """A binomial rate ``k/n`` with its confidence interval and raw counts."""

    model_config = ConfigDict(extra="forbid")

    numerator: int
    denominator: int
    rate: float
    ci: Interval

    @property
    def as_tuple(self) -> tuple[int, int, float]:
        return self.numerator, self.denominator, self.rate


def wilson_interval(k: int, n: int, *, z: float = 1.959963984540054) -> Interval:
    """Wilson score 95% interval for a binomial proportion ``k/n``."""
    if n <= 0:
        return Interval(lo=0.0, hi=1.0, method="wilson")
    phat = k / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (phat + z2 / (2 * n)) / denom
    margin = (z * math.sqrt((phat * (1 - phat) + z2 / (4 * n)) / n)) / denom
    lo = max(0.0, center - margin)
    hi = min(1.0, center + margin)
    return Interval(lo=lo, hi=hi, method="wilson")


def rate(k: int, n: int) -> RateEstimate:
    """A :class:`RateEstimate` for ``k/n`` with a Wilson interval."""
    return RateEstimate(
        numerator=k,
        denominator=n,
        rate=(k / n) if n else 0.0,
        ci=wilson_interval(k, n),
    )


def bootstrap_ci(
    values: Sequence[float],
    *,
    statistic: Callable[[Sequence[float]], float] = lambda xs: sum(xs) / len(xs),
    n_resamples: int = 10000,
    confidence: float = 0.95,
    seed: int = 0,
) -> Interval:
    """Nonparametric bootstrap percentile interval (deterministic given ``seed``)."""
    xs = list(values)
    if not xs:
        return Interval(lo=0.0, hi=1.0, method="bootstrap", confidence=confidence)
    rng = random.Random(seed)
    n = len(xs)
    stats: list[float] = []
    for _ in range(n_resamples):
        sample = [xs[rng.randrange(n)] for _ in range(n)]
        stats.append(statistic(sample))
    stats.sort()
    alpha = (1.0 - confidence) / 2.0
    lo = stats[int(alpha * (n_resamples - 1))]
    hi = stats[int((1.0 - alpha) * (n_resamples - 1))]
    return Interval(lo=lo, hi=hi, method="bootstrap", confidence=confidence)


def pass_hat_k(per_task_trials: Mapping[str, Sequence[bool]], k: int) -> float:
    """``pass^k``: expected fraction of tasks whose k sampled trials ALL pass."""
    if k <= 0:
        return 1.0
    per_task: list[float] = []
    for trials in per_task_trials.values():
        n = len(trials)
        if n < k:
            continue
        c = sum(1 for t in trials if t)
        per_task.append(math.comb(c, k) / math.comb(n, k) if c >= k else 0.0)
    return sum(per_task) / len(per_task) if per_task else 0.0


class CellSummary(BaseModel):
    """Metrics for one (category x substrate) cell -- the decomposition unit."""

    model_config = ConfigDict(extra="forbid")

    category: str
    substrate: str
    n: int
    swer: RateEstimate
    swer_wrong_write: RateEstimate
    swer_phantom: RateEstimate
    over_halt: RateEstimate
    task_success: RateEstimate
    screen_success: RateEstimate
    success_effect_gap: float


class BenchmarkSummary(BaseModel):
    """The full, decomposed summary of a set of episodes (one arm, or filtered).

    ``cells`` is the mandatory per (category x substrate) breakdown; the overall
    rates are provided for the abstract but never in isolation from ``cells``.
    """

    model_config = ConfigDict(extra="forbid")

    arm: str = ""
    n_episodes: int
    n_tasks: int
    arms: list[str] = Field(default_factory=list)

    swer: RateEstimate
    swer_wrong_write: RateEstimate
    swer_phantom: RateEstimate
    over_halt: RateEstimate
    task_success: RateEstimate
    screen_success: RateEstimate
    success_effect_gap: float
    success_effect_gap_ci: Interval

    total_cost_usd: float
    mean_cost_usd: float
    mean_latency_s: float

    pass_hat_k: dict[str, float] = Field(default_factory=dict)
    cells: list[CellSummary] = Field(default_factory=list)
    outcome_counts: dict[str, int] = Field(default_factory=dict)


def _cell(category: str, substrate: str, eps: Sequence[EpisodeRecord]) -> CellSummary:
    n = len(eps)
    swer_n = sum(1 for e in eps if e.outcome is OutcomeLabel.SILENT_WRONG_EFFECT)
    ww = sum(1 for e in eps if e.swer_variant is SwerVariant.WRONG_WRITE)
    ph = sum(1 for e in eps if e.swer_variant is SwerVariant.PHANTOM)
    over = sum(1 for e in eps if e.outcome is OutcomeLabel.OVER_HALT)
    succ = sum(1 for e in eps if e.outcome is OutcomeLabel.SUCCESS)
    screen = sum(1 for e in eps if e.reported_success)
    return CellSummary(
        category=category,
        substrate=substrate,
        n=n,
        swer=rate(swer_n, n),
        swer_wrong_write=rate(ww, n),
        swer_phantom=rate(ph, n),
        over_halt=rate(over, n),
        task_success=rate(succ, n),
        screen_success=rate(screen, n),
        success_effect_gap=((screen - succ) / n) if n else 0.0,
    )


def summarize(
    episodes: Iterable[EpisodeRecord],
    *,
    arm: Optional[str] = None,
    pass_k_values: Sequence[int] = (1, 2, 4, 8),
) -> BenchmarkSummary:
    """Aggregate episodes into the full decomposed :class:`BenchmarkSummary`.

    Args:
        episodes: The result rows to summarize.
        arm: If given, only episodes with this ``arm`` are aggregated.
        pass_k_values: The ``k`` values to report ``pass^k`` for.
    """
    eps = [e for e in episodes if arm is None or e.arm == arm]
    n = len(eps)

    swer_n = sum(1 for e in eps if e.outcome is OutcomeLabel.SILENT_WRONG_EFFECT)
    ww = sum(1 for e in eps if e.swer_variant is SwerVariant.WRONG_WRITE)
    ph = sum(1 for e in eps if e.swer_variant is SwerVariant.PHANTOM)
    over = sum(1 for e in eps if e.outcome is OutcomeLabel.OVER_HALT)
    succ = sum(1 for e in eps if e.outcome is OutcomeLabel.SUCCESS)
    screen = sum(1 for e in eps if e.reported_success)

    gap_values = [
        (1.0 if e.reported_success else 0.0)
        - (1.0 if e.outcome is OutcomeLabel.SUCCESS else 0.0)
        for e in eps
    ]
    gap = (sum(gap_values) / n) if n else 0.0

    by_task: dict[str, list[bool]] = defaultdict(list)
    for e in eps:
        by_task[e.task_id].append(e.outcome is OutcomeLabel.SUCCESS)
    pass_k = {str(k): pass_hat_k(by_task, k) for k in pass_k_values}

    grouped: dict[tuple[str, str], list[EpisodeRecord]] = defaultdict(list)
    for e in eps:
        grouped[(e.category.value, e.substrate.value)].append(e)
    cells = [
        _cell(cat, sub, cell_eps) for (cat, sub), cell_eps in sorted(grouped.items())
    ]

    outcome_counts: dict[str, int] = defaultdict(int)
    for e in eps:
        outcome_counts[e.outcome.value] += 1

    total_cost = sum(e.cost_usd for e in eps)
    return BenchmarkSummary(
        arm=arm or "",
        n_episodes=n,
        n_tasks=len(by_task),
        arms=sorted({e.arm for e in eps}),
        swer=rate(swer_n, n),
        swer_wrong_write=rate(ww, n),
        swer_phantom=rate(ph, n),
        over_halt=rate(over, n),
        task_success=rate(succ, n),
        screen_success=rate(screen, n),
        success_effect_gap=gap,
        success_effect_gap_ci=bootstrap_ci(gap_values),
        total_cost_usd=total_cost,
        mean_cost_usd=(total_cost / n) if n else 0.0,
        mean_latency_s=(sum(e.latency_s for e in eps) / n) if n else 0.0,
        pass_hat_k=pass_k,
        cells=cells,
        outcome_counts=dict(outcome_counts),
    )
