"""A record_written halt must not deny a partial write.

When an effect expects more than one record and only some of them land, the
verdict stays REFUTED (the run halts), but the reason has to say that some
records did land. Telling the operator that nothing landed invites a re-run
that duplicates the records that are already there.
"""

from __future__ import annotations

from openadapt_flow.runtime.effects._common import _judge_record_written
from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectKind,
    EffectState,
    Verdict,
)


def _judge(current: list[dict], expected_count: int = 2):
    effect = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"note": "x"},
        expected_count=expected_count,
    )
    before = EffectState(substrate="rest", reachable=True, records=[])
    return _judge_record_written(effect, before, current, current, "rest")


def test_partial_write_reason_says_some_records_landed() -> None:
    verdict = _judge([{"id": 1, "note": "x"}])

    assert verdict.verdict is Verdict.REFUTED
    assert verdict.observed_count == 1
    assert verdict.expected_count == 2
    assert verdict.observed_effect == "conflicting"
    assert "nothing landed" not in verdict.reason
    assert "1 of 2" in verdict.reason
    assert "partial write" in verdict.reason


def test_zero_records_still_reports_nothing_landed() -> None:
    verdict = _judge([])

    assert verdict.verdict is Verdict.REFUTED
    assert verdict.observed_count == 0
    assert verdict.observed_effect == "absent"
    assert "nothing landed" in verdict.reason
