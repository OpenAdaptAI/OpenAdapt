"""Tests for the public, corpus-free identity scoring interface."""

from dataclasses import dataclass

import pytest

from openadapt_flow.validation.identity_rates import (
    IdentityPair,
    score_identity_pairs,
)


@dataclass
class _Decision:
    status: str


def test_score_identity_pairs_reports_safety_and_availability_separately() -> None:
    statuses = iter(("verified", "abstain", "mismatch", "verified"))

    result = score_identity_pairs(
        (
            IdentityPair("a", "a", "same_entity"),
            IdentityPair("b", "b?", "same_entity"),
            IdentityPair("c", "d", "different_entity"),
            IdentityPair("e", "e?", "indistinguishable"),
        ),
        verifier=lambda _recorded, _observed: _Decision(next(statuses)),
    )

    assert result.n_pairs == 4
    assert (result.false_accept_count, result.false_accept_denominator) == (1, 2)
    assert result.false_accept_rate == 0.5
    assert (result.false_abort_count, result.false_abort_denominator) == (1, 2)
    assert result.false_abort_rate == 0.5
    assert (result.justified_abort_count, result.justified_abort_denominator) == (0, 1)
    assert result.justified_abort_rate == 0.0
    assert result.verdict_counts == {"abstain": 1, "mismatch": 1, "verified": 2}


def test_score_identity_pairs_uses_none_for_empty_denominators() -> None:
    result = score_identity_pairs(())

    assert result.n_pairs == 0
    assert result.false_accept_rate is None
    assert result.false_abort_rate is None
    assert result.justified_abort_rate is None


def test_score_identity_pairs_rejects_unknown_verdict() -> None:
    with pytest.raises(ValueError, match="unknown status"):
        score_identity_pairs(
            (IdentityPair("a", "b", "different_entity"),),
            verifier=lambda _recorded, _observed: _Decision("success"),
        )
