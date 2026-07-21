"""A submission is fully reproducible: its headline is recomputable from the raw
rows, and a tampered claim is rejected."""

from __future__ import annotations

from effectbench.adapter import ScreenOnlySUT
from effectbench.leaderboard import (
    build_submission,
    pack_fingerprint,
    reproducibility_manifest,
    score_submission,
)
from effectbench.runner import evaluate


def _submission(trials: int = 3) -> dict:
    episodes = evaluate(ScreenOnlySUT(), trials=trials)
    return build_submission(
        system_name="screen_only", episodes=episodes, trials=trials
    )


def test_submission_round_trips() -> None:
    result = score_submission(_submission())
    assert result["ok"], result["errors"]


def test_submission_carries_reproducibility_manifest() -> None:
    sub = _submission()
    repro = sub["reproducibility"]
    assert repro["effectbench_version"]
    assert repro["pack_fingerprint"] == pack_fingerprint()
    assert repro["seeds"] == [0, 1, 2]


def test_tampered_claim_is_rejected() -> None:
    sub = _submission()
    sub["results"]["swer"]["numerator"] = 0  # lie: claim zero silent-wrong
    result = score_submission(sub)
    assert not result["ok"]
    assert any("swer" in e for e in result["errors"])


def test_wrong_pack_fingerprint_is_rejected() -> None:
    sub = _submission()
    sub["reproducibility"]["pack_fingerprint"] = "sha256:deadbeef"
    result = score_submission(sub)
    assert not result["ok"]


def test_submission_without_rows_cannot_be_reproduced() -> None:
    sub = _submission()
    sub["episodes"] = []
    result = score_submission(sub)
    assert not result["ok"]


def test_reproducibility_manifest_pins_versions_and_seeds() -> None:
    repro = reproducibility_manifest(trials=5)
    assert repro["trials_per_task"] == 5
    assert repro["seeds"] == [0, 1, 2, 3, 4]
    assert "pydantic" in repro["dependencies"]
