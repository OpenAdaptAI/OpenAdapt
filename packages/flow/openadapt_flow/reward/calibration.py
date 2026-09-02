"""Exact false-accept bounds for a synthetic-scope reward certificate.

A certificate's ``epsilon`` is a bound on the probability that the checker
accepts an episode it should have refused. The seed does not invent that
number. It runs the checker over ``n`` synthetic ExtraDup trials (one
extra record, one duplicate record, one missing record, one wrong-type
record per trial, chosen by a seeded generator), counts the false
accepts, and reports the exact one-sided Clopper-Pearson upper bound at
the stated confidence.

The bound is exact: it uses the binomial tail directly, not a normal
approximation. For zero failures it reduces to ``1 - alpha ** (1 / n)``.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from openadapt_types.reward import RewardOutcomeV1


def binomial_cdf(k: int, n: int, p: float) -> float:
    """P(X <= k) for X ~ Binomial(n, p), computed with exact coefficients."""

    if k < 0:
        return 0.0
    if k >= n:
        return 1.0
    if p <= 0.0:
        return 1.0
    if p >= 1.0:
        return 0.0
    total = 0.0
    for i in range(k + 1):
        total += math.comb(n, i) * (p**i) * ((1.0 - p) ** (n - i))
    return min(1.0, total)


def clopper_pearson_upper(
    failures: int, trials: int, *, confidence: float = 0.95
) -> float:
    """One-sided exact upper bound on a binomial proportion.

    The smallest ``p`` with ``P(X <= failures | trials, p) <= 1 - confidence``.
    """

    if trials <= 0:
        raise ValueError("trials must be positive")
    if not 0 <= failures <= trials:
        raise ValueError("failures must lie in [0, trials]")
    if not 0.0 < confidence < 1.0:
        raise ValueError("confidence must lie in (0, 1)")
    alpha = 1.0 - confidence
    if failures == trials:
        return 1.0
    if failures == 0:
        return 1.0 - alpha ** (1.0 / trials)
    low, high = 0.0, 1.0
    for _ in range(200):
        mid = (low + high) / 2.0
        if binomial_cdf(failures, trials, mid) > alpha:
            low = mid
        else:
            high = mid
    return high


@dataclass(frozen=True)
class CalibrationResult:
    """What the seed ran and what it found."""

    trials: int
    false_accepts: int
    confidence: float
    epsilon: float
    generator_seed: int

    def as_metadata(self) -> dict[str, Any]:
        return {
            "calibration_trials": self.trials,
            "calibration_false_accepts": self.false_accepts,
            "calibration_confidence": self.confidence,
            "calibration_generator_seed": self.generator_seed,
            "calibration_method": "clopper_pearson_one_sided_exact",
        }


FAULT_CLASSES: tuple[str, ...] = (
    "extra_record",
    "duplicate_record",
    "missing_record",
    "wrong_type",
    "forbidden_present",
)


def extradup_trials(
    checker: Callable[[Sequence[dict[str, Any]], dict[str, str]], RewardOutcomeV1],
    *,
    trials: int,
    generator_seed: int,
    confidence: float = 0.95,
) -> CalibrationResult:
    """Run the checker over faulted stores and bound its false-accept rate.

    ``checker(records, identity)`` returns the reward outcome the checker
    assigns when the store holds ``records`` and the episode claims the
    required effect for ``identity``. A false accept is ``VERIFIED`` on a
    faulted store.
    """

    rng = random.Random(generator_seed)
    false_accepts = 0
    for index in range(trials):
        fault = FAULT_CLASSES[rng.randrange(len(FAULT_CLASSES))]
        patient = f"patient-cal-{index:04d}"
        records = faulted_store(fault, patient, rng)
        if checker(records, {"patient_id": patient}) is RewardOutcomeV1.VERIFIED:
            false_accepts += 1
    return CalibrationResult(
        trials=trials,
        false_accepts=false_accepts,
        confidence=confidence,
        epsilon=clopper_pearson_upper(false_accepts, trials, confidence=confidence),
        generator_seed=generator_seed,
    )


def faulted_store(fault: str, patient: str, rng: random.Random) -> list[dict[str, Any]]:
    """A MockMed-shaped store carrying one fault for ``patient``."""

    noise = [
        {
            "id": 100 + i,
            "patient_id": f"patient-other-{rng.randrange(10_000):04d}",
            "type": "Triage",
            "status": "saved",
        }
        for i in range(rng.randrange(0, 4))
    ]
    intended = {"id": 1, "patient_id": patient, "type": "Triage", "status": "saved"}
    if fault == "extra_record":
        extra = {"id": 2, "patient_id": patient, "type": "Triage", "status": "saved"}
        return [*noise, intended, extra]
    if fault == "duplicate_record":
        return [*noise, intended, dict(intended, id=3)]
    if fault == "missing_record":
        return noise
    if fault == "wrong_type":
        return [*noise, dict(intended, type="Consult")]
    if fault == "forbidden_present":
        discharge = {
            "id": 4,
            "patient_id": patient,
            "type": "Discharge",
            "status": "saved",
        }
        return [*noise, intended, discharge]
    raise ValueError(f"unknown fault class {fault!r}")
