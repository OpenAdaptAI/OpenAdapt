"""The public oracle ladder must not expose Flow's inverse legacy rank."""

from __future__ import annotations

from pathlib import Path

from openadapt_flow.verification import (
    VerificationTier,
    oracle_tier_from_verification_tier,
)


def test_legacy_verification_rank_maps_to_seal_oracle_tier() -> None:
    assert oracle_tier_from_verification_tier(VerificationTier.INDEPENDENT_SYSTEM) == 2
    assert oracle_tier_from_verification_tier(VerificationTier.INDEPENDENT_SESSION) == 1
    assert (
        oracle_tier_from_verification_tier(
            VerificationTier.PERSISTED_STATE_REACQUISITION
        )
        == 0
    )
    assert oracle_tier_from_verification_tier(VerificationTier.IMMEDIATE_SCREEN) == 0


def test_public_docs_use_the_seal_oracle_ladder() -> None:
    root = Path(__file__).parents[1]
    readme = (root / "README.md").read_text(encoding="utf-8")
    kit = (root / "docs" / "EFFECT_KIT.md").read_text(encoding="utf-8")

    assert "Seal Oracle tier 2" in readme
    assert "evidence tier 1 (independent system of record)" not in readme
    assert "tier 0 is visual evidence" in kit
    assert "tier 2 is a system-of-record read" in kit
