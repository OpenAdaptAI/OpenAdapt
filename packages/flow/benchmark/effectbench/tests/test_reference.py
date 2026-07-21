"""Pin the reference result -- the regression anchor for the whole benchmark.

If a change to the schema, classifier, judge, or metrics moves these numbers,
this test fails. The numbers match the published headline and the OpenAdapt
engine's in-tree re-expression: screen-only SWER 50/90 (wrong-write 40, phantom
10), effect-verified 0/90, 5 of 7 transactional faults silently mishandled.
"""

from __future__ import annotations

import json
from pathlib import Path

from effectbench.adapter import EffectVerifiedSUT, ScreenOnlySUT
from effectbench.metrics import summarize
from effectbench.reference import reference_result
from effectbench.runner import evaluate
from effectbench.tasks.mockmed import TRANSACTIONAL_MODES

RESULTS = Path(__file__).resolve().parent.parent / "results" / "reference.json"


def test_screen_only_swer_is_the_published_headline() -> None:
    episodes = evaluate(ScreenOnlySUT(), trials=10)
    s = summarize(episodes, arm="screen_only")
    assert s.swer.numerator == 50
    assert s.swer.denominator == 90
    assert s.swer_wrong_write.numerator == 40
    assert s.swer_phantom.numerator == 10


def test_effect_verified_swer_is_zero() -> None:
    episodes = evaluate(EffectVerifiedSUT(), trials=10)
    s = summarize(episodes, arm="effect_verified")
    assert s.swer.numerator == 0
    assert s.swer.denominator == 90


def test_five_of_seven_transactional_silently_mishandled() -> None:
    result = reference_result()
    tm = result["transactional_silently_mishandled"]
    assert (tm["silent"], tm["total"]) == (5, 7)
    assert len(TRANSACTIONAL_MODES) == 7


def test_swer_and_over_halt_reported_jointly() -> None:
    # An always-halt system reaches SWER 0 only by paying over-halt -- both must
    # be present in the summary so the trade-off is visible.
    episodes = evaluate(EffectVerifiedSUT(), trials=3)
    s = summarize(episodes, arm="effect_verified")
    assert s.swer.denominator == s.over_halt.denominator


def test_committed_reference_artifact_matches_recomputation() -> None:
    committed = json.loads(RESULTS.read_text())
    fresh = reference_result()
    for arm in ("screen_only", "effect_verified"):
        assert (
            committed["arms"][arm]["swer"]["numerator"]
            == fresh["arms"][arm]["swer"]["numerator"]
        )
        assert (
            committed["arms"][arm]["swer"]["denominator"]
            == fresh["arms"][arm]["swer"]["denominator"]
        )
