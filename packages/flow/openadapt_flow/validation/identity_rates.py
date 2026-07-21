"""Data-agnostic scoring for identity-verification decisions.

This module is a public mechanism, not an evaluation corpus or operating-point
recipe. Callers supply labeled pairs and may supply any verifier with the same
``(recorded, observed) -> status`` contract. No examples, thresholds, target
recipes, or deployment-derived tuning live here.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Callable, Iterable, Literal, Protocol

from openadapt_flow.runtime.identity import verify_target_identity

IdentityLabel = Literal["same_entity", "different_entity", "indistinguishable"]
IdentityStatus = Literal["verified", "mismatch", "abstain", "unreadable"]


class IdentityDecision(Protocol):
    """Minimal verifier result consumed by :func:`score_identity_pairs`."""

    @property
    def status(self) -> str: ...


IdentityVerifier = Callable[[str, str], IdentityDecision]


@dataclass(frozen=True)
class IdentityPair:
    """One caller-provided labeled comparison."""

    recorded: str
    observed: str
    label: IdentityLabel


@dataclass(frozen=True)
class IdentityRates:
    """Counts and rates with explicit denominators.

    ``indistinguishable`` means the available evidence cannot safely separate
    two entities. Verifying such a pair is a false accept; refusing it is a
    justified abort rather than an availability error.
    """

    n_pairs: int
    false_accept_count: int
    false_accept_denominator: int
    false_accept_rate: float | None
    false_abort_count: int
    false_abort_denominator: int
    false_abort_rate: float | None
    justified_abort_count: int
    justified_abort_denominator: int
    justified_abort_rate: float | None
    verdict_counts: dict[str, int]


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _runtime_identity_verifier(recorded: str, observed: str) -> IdentityDecision:
    """Expose the runtime verifier through the scorer's two-argument contract."""
    return verify_target_identity(recorded, observed)


def score_identity_pairs(
    pairs: Iterable[IdentityPair],
    *,
    verifier: IdentityVerifier = _runtime_identity_verifier,
) -> IdentityRates:
    """Score caller-supplied pairs without embedding a corpus or thresholds."""
    rows = list(pairs)
    verdicts: Counter[str] = Counter()
    false_accepts = 0
    false_aborts = 0
    justified_aborts = 0
    unsafe_denominator = 0
    same_denominator = 0
    indistinguishable_denominator = 0

    for pair in rows:
        status = verifier(pair.recorded, pair.observed).status
        if status not in {"verified", "mismatch", "abstain", "unreadable"}:
            raise ValueError(f"identity verifier returned unknown status: {status!r}")
        verdicts[status] += 1
        if pair.label == "same_entity":
            same_denominator += 1
            false_aborts += status != "verified"
        else:
            unsafe_denominator += 1
            false_accepts += status == "verified"
            if pair.label == "indistinguishable":
                indistinguishable_denominator += 1
                justified_aborts += status != "verified"

    return IdentityRates(
        n_pairs=len(rows),
        false_accept_count=false_accepts,
        false_accept_denominator=unsafe_denominator,
        false_accept_rate=_rate(false_accepts, unsafe_denominator),
        false_abort_count=false_aborts,
        false_abort_denominator=same_denominator,
        false_abort_rate=_rate(false_aborts, same_denominator),
        justified_abort_count=justified_aborts,
        justified_abort_denominator=indistinguishable_denominator,
        justified_abort_rate=_rate(justified_aborts, indistinguishable_denominator),
        verdict_counts=dict(sorted(verdicts.items())),
    )
