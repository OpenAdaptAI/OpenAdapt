"""Metrics are pure aggregations with honest small-N counts and intervals."""

from __future__ import annotations

from effectbench.metrics import pass_hat_k, rate, summarize, wilson_interval
from effectbench.schema import (
    AgentReport,
    DivergenceCategory,
    EpisodeRecord,
    OracleVerdict,
    OutcomeLabel,
    Substrate,
    SwerVariant,
)
from effectbench.effect import EffectKind, Verdict


def _episode(outcome: OutcomeLabel, *, reported: bool, task: str = "t") -> EpisodeRecord:
    variant = (
        SwerVariant.WRONG_WRITE
        if outcome is OutcomeLabel.SILENT_WRONG_EFFECT
        else SwerVariant.NONE
    )
    return EpisodeRecord(
        episode_id=f"{task}-{outcome.value}-{reported}",
        task_id=task,
        arm="a",
        trial=0,
        substrate=Substrate.WEB,
        category=DivergenceCategory.C1_PARTIAL_SAVE,
        agent=AgentReport(reported_success=reported),
        oracle=OracleVerdict(verdict=Verdict.REFUTED, kind=EffectKind.RECORD_WRITTEN),
        outcome=outcome,
        swer_variant=variant,
    )


def test_swer_is_silent_wrong_over_n() -> None:
    eps = [
        _episode(OutcomeLabel.SILENT_WRONG_EFFECT, reported=True),
        _episode(OutcomeLabel.SUCCESS, reported=True),
        _episode(OutcomeLabel.OVER_HALT, reported=False),
        _episode(OutcomeLabel.SAFE_HALT, reported=False),
    ]
    s = summarize(eps, arm="a")
    assert s.swer.numerator == 1
    assert s.swer.denominator == 4
    assert s.over_halt.numerator == 1


def test_success_effect_gap_is_screen_minus_effect() -> None:
    eps = [
        _episode(OutcomeLabel.SILENT_WRONG_EFFECT, reported=True),
        _episode(OutcomeLabel.SUCCESS, reported=True),
    ]
    s = summarize(eps, arm="a")
    # screen_success = 2/2, task_success = 1/2 -> gap 0.5
    assert s.screen_success.numerator == 2
    assert s.task_success.numerator == 1
    assert abs(s.success_effect_gap - 0.5) < 1e-9


def test_cells_are_always_present() -> None:
    s = summarize([_episode(OutcomeLabel.SUCCESS, reported=True)], arm="a")
    assert s.cells  # never an aggregate-only report


def test_wilson_interval_brackets_the_rate() -> None:
    itv = wilson_interval(1, 10)
    assert itv.lo <= 0.1 <= itv.hi
    assert 0.0 <= itv.lo <= itv.hi <= 1.0


def test_rate_carries_raw_counts() -> None:
    r = rate(3, 7)
    assert r.numerator == 3 and r.denominator == 7


def test_pass_hat_k() -> None:
    # A task with 3/4 trials passing: P(random 2-subset all pass) = C(3,2)/C(4,2).
    assert abs(pass_hat_k({"t": [True, True, True, False]}, 2) - (3 / 6)) < 1e-9
    assert pass_hat_k({"t": [True, True]}, 4) == 0.0  # fewer trials than k
