"""The classifier is a total function whose SWER numerator is exactly the
SILENT_WRONG_EFFECT label."""

from __future__ import annotations

import pytest

from effectbench.oracle import TrueEffectState, classify_outcome
from effectbench.schema import OutcomeLabel, SwerVariant


def test_reported_success_over_wrong_record_is_silent_wrong_write() -> None:
    label, variant, _ = classify_outcome(
        reported_success=True,
        true_state=TrueEffectState.WRONG_PERSISTED,
        correct_action_available=False,
    )
    assert label is OutcomeLabel.SILENT_WRONG_EFFECT
    assert variant is SwerVariant.WRONG_WRITE


def test_reported_success_over_absent_record_is_phantom() -> None:
    label, variant, _ = classify_outcome(
        reported_success=True,
        true_state=TrueEffectState.ABSENT,
        correct_action_available=True,
    )
    assert label is OutcomeLabel.SILENT_WRONG_EFFECT
    assert variant is SwerVariant.PHANTOM


def test_over_halt_vs_safe_halt_splits_on_availability() -> None:
    over, _, _ = classify_outcome(
        reported_success=False,
        true_state=TrueEffectState.ABSENT,
        correct_action_available=True,
    )
    safe, _, _ = classify_outcome(
        reported_success=False,
        true_state=TrueEffectState.ABSENT,
        correct_action_available=False,
    )
    assert over is OutcomeLabel.OVER_HALT
    assert safe is OutcomeLabel.SAFE_HALT


def test_correct_effect_reported_success_is_success() -> None:
    label, _, _ = classify_outcome(
        reported_success=True,
        true_state=TrueEffectState.CORRECT,
        correct_action_available=True,
    )
    assert label is OutcomeLabel.SUCCESS


def test_unreadable_oracle_is_not_scoreable() -> None:
    with pytest.raises(ValueError):
        classify_outcome(
            reported_success=True,
            true_state=TrueEffectState.UNREADABLE,
            correct_action_available=True,
        )


def test_labels_partition_every_input() -> None:
    seen = set()
    for reported in (True, False):
        for state in (
            TrueEffectState.CORRECT,
            TrueEffectState.WRONG_PERSISTED,
            TrueEffectState.ABSENT,
        ):
            for avail in (True, False):
                label, _, _ = classify_outcome(
                    reported_success=reported,
                    true_state=state,
                    correct_action_available=avail,
                )
                seen.add(label)
    assert seen == set(OutcomeLabel)
