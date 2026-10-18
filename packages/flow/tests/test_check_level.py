"""The check-level ladder and every mapping onto it.

Each older scale keeps its wire numbers; these tests pin how each one maps so
a change to any mapping is a visible, reviewed change.
"""

from __future__ import annotations

import pytest

from openadapt_flow.assurance import ASSURANCE_GRADES, assurance_for
from openadapt_flow.check_level import (
    CheckLevel,
    from_oracle_tier,
    from_verification_tier,
    to_readback_label,
    weakest,
)
from openadapt_flow.contracts.oracle import OracleTier
from openadapt_flow.verification import VerificationTier


def test_ladder_names_and_numbers() -> None:
    assert {level.label: int(level) for level in CheckLevel} == {
        "unchecked": 0,
        "same_screen": 1,
        "reopened": 2,
        "separate_session": 3,
        "system_of_record": 4,
        "counterparty_confirmed": 5,
    }
    for level in CheckLevel:
        assert CheckLevel.from_label(level.label) is level
    with pytest.raises(ValueError):
        CheckLevel.from_label("verified")


def test_published_wire_values_do_not_change() -> None:
    assert {t.name: int(t) for t in VerificationTier} == {
        "INDEPENDENT_SYSTEM": 1,
        "INDEPENDENT_SESSION": 2,
        "PERSISTED_STATE_REACQUISITION": 3,
        "IMMEDIATE_SCREEN": 4,
    }
    assert {t.name: int(t) for t in OracleTier} == {
        "VISUAL": 0,
        "INDEPENDENT_SESSION": 1,
        "SYSTEM_OF_RECORD": 2,
        "COUNTERPARTY": 3,
    }
    assert ASSURANCE_GRADES == ("independent_readback", "screen_readback", "unchecked")


VERIFICATION_TIER_LEVELS = {
    VerificationTier.INDEPENDENT_SYSTEM: CheckLevel.SYSTEM_OF_RECORD,
    VerificationTier.INDEPENDENT_SESSION: CheckLevel.SEPARATE_SESSION,
    VerificationTier.PERSISTED_STATE_REACQUISITION: CheckLevel.REOPENED,
    VerificationTier.IMMEDIATE_SCREEN: CheckLevel.SAME_SCREEN,
}

ORACLE_TIER_LEVELS = {
    OracleTier.VISUAL: CheckLevel.SAME_SCREEN,
    OracleTier.INDEPENDENT_SESSION: CheckLevel.SEPARATE_SESSION,
    OracleTier.SYSTEM_OF_RECORD: CheckLevel.SYSTEM_OF_RECORD,
    OracleTier.COUNTERPARTY: CheckLevel.COUNTERPARTY_CONFIRMED,
}

READBACK_LABELS = {
    CheckLevel.UNCHECKED: "unchecked",
    CheckLevel.SAME_SCREEN: "screen_readback",
    CheckLevel.REOPENED: "screen_readback",
    CheckLevel.SEPARATE_SESSION: "screen_readback",
    CheckLevel.SYSTEM_OF_RECORD: "independent_readback",
    CheckLevel.COUNTERPARTY_CONFIRMED: "independent_readback",
}


def test_every_verification_tier_maps() -> None:
    assert set(VERIFICATION_TIER_LEVELS) == set(VerificationTier)
    for tier, level in VERIFICATION_TIER_LEVELS.items():
        assert tier.check_level is level
        assert from_verification_tier(int(tier)) is level
        assert from_verification_tier(tier, confirmed=False) is CheckLevel.UNCHECKED
    assert from_verification_tier(None) is CheckLevel.UNCHECKED
    with pytest.raises(ValueError):
        from_verification_tier(5)


def test_every_oracle_tier_maps() -> None:
    assert set(ORACLE_TIER_LEVELS) == set(OracleTier)
    for tier, level in ORACLE_TIER_LEVELS.items():
        assert tier.check_level is level
        assert from_oracle_tier(int(tier)) is level
    with pytest.raises(ValueError):
        from_oracle_tier(4)


def test_both_tiers_agree_where_they_overlap() -> None:
    from openadapt_flow.verification import oracle_tier_from_verification_tier

    for tier in (
        VerificationTier.INDEPENDENT_SYSTEM,
        VerificationTier.INDEPENDENT_SESSION,
    ):
        assert (
            OracleTier(oracle_tier_from_verification_tier(tier)).check_level
            is tier.check_level
        )


def test_every_level_rolls_up_to_one_readback_label() -> None:
    assert set(READBACK_LABELS) == set(CheckLevel)
    for level, label in READBACK_LABELS.items():
        assert to_readback_label(level) == label


def test_assurance_for_is_the_rollup_of_the_ladder() -> None:
    for tier, level in VERIFICATION_TIER_LEVELS.items():
        assert assurance_for(tier) == to_readback_label(level)
        assert assurance_for(int(tier), confirmed=False) == "unchecked"
    assert assurance_for(None) == "unchecked"
    assert assurance_for(9) == "unchecked"


def test_weakest_wins() -> None:
    assert weakest([]) is CheckLevel.UNCHECKED
    assert (
        weakest([CheckLevel.SYSTEM_OF_RECORD, CheckLevel.REOPENED])
        is CheckLevel.REOPENED
    )
    assert (
        weakest([CheckLevel.UNCHECKED, CheckLevel.COUNTERPARTY_CONFIRMED])
        is CheckLevel.UNCHECKED
    )
