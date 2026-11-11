"""Reward consumes the real aggregate receipt from an owned attempt."""

from __future__ import annotations

from pathlib import Path

import pytest

from openadapt_flow import verify_run as verifier_module
from openadapt_flow.rl_env import reward
from tests._owned_attempt_consumer_support import (
    PARAMS,
    check_matching_read,
    exercise_external_write,
    observe_receipts,
    setup_store,
)


@pytest.mark.parametrize("owned", [False, True], ids=["observation", "owned"])
def test_matching_external_write_earns_only_observation_credit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owned: bool
) -> None:
    path = tmp_path / "store.sqlite"
    cfg = setup_store(path)
    receipts = observe_receipts(monkeypatch, reward, verifier_module, owned=owned)
    judge = reward.EpisodeJudge(cfg, dict(PARAMS), episode_id="synthetic-episode")
    try:
        assert judge.baseline_error is None
        exercise_external_write(path)
        result = judge.judge("success", feasible=True, attempted=True)
    finally:
        judge.close()

    (readback,) = receipts
    check_matching_read(readback, owned=owned)
    assert result.error is None
    assert result.outcome == readback["receipt"]["outcome"]
    assert result.verdict == readback["receipt"]["effect_verdict"]
    assert result.reward == (0.0 if owned else 1.0)
    assert result.vector(steps=1, fault=None)["completion"] is (not owned)
    assert not result.silent_wrong_effect
