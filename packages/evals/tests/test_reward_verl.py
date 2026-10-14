"""verl reward manager: unscored drop by uid, development_only refusal, expiry logging."""

from __future__ import annotations

import logging
import sys
import types

import numpy as np
import pytest
from openadapt_types.reward import RewardEvidenceReceiptV1, RewardOutcomeV1

from openadapt_evals.reward import (
    CallableRewardSource,
    EpisodeDescriptor,
    OracleIdentityError,
    UncertifiedRewardError,
)
from openadapt_evals.reward.verl import (
    REWARD_MANAGER_NAME,
    CertifiedRewardManager,
    register_with_verl,
)
from tests.reward_fixtures import (
    CERTIFICATE,
    CONTRACT,
    POLICY_CHECKPOINT,
    certificate,
    identities_for,
    identity_for,
    receipts_by_episode,
)


def _manager(receipts: dict[str, RewardEvidenceReceiptV1], **kwargs) -> CertifiedRewardManager:
    def fetch(descriptor: EpisodeDescriptor) -> RewardEvidenceReceiptV1:
        return receipts[descriptor.episode_id]

    options = {
        "reward_contract_digest": CONTRACT.digest,
        "policy_checkpoint_id": POLICY_CHECKPOINT,
        "certificate": CERTIFICATE,
    }
    options.update(kwargs)
    return CertifiedRewardManager(
        tokenizer=None, num_examine=0, source=CallableRewardSource(fetch), **options
    )


def _infos(episodes: list[str], *, identity: bool = True) -> list[dict]:
    infos = [{"episode_id": item, "task_id": "task.test.0001"} for item in episodes]
    if identity:
        for info in infos:
            info["oracle_identity"] = identity_for(info["episode_id"])
    return infos


# -- unscored drop by uid ----------------------------------------------------------------------


def test_unscored_sample_gets_its_uid_group_mean() -> None:
    episodes = ["vep.0001.a", "vep.0001.b", "vep.0001.c", "vep.0001.d"]
    receipts = receipts_by_episode(
        {
            episodes[0]: RewardOutcomeV1.VERIFIED,
            episodes[1]: RewardOutcomeV1.RECONCILIATION_REQUIRED,
            episodes[2]: RewardOutcomeV1.VERIFIED,
            episodes[3]: RewardOutcomeV1.WRONG_EFFECT,
        }
    )
    manager = _manager(receipts)
    # Two uid groups: (a, b) and (c, d). b is unscored and takes a's reward.
    rewards, extra, scored = manager.score_batch(_infos(episodes), ["u1", "u1", "u2", "u2"], 3)
    assert rewards == [1.0, 1.0, 1.0, -1.0]
    assert extra["reward_unscored"] == [False, True, False, False]
    assert extra["reward_group_unscored"] == [False] * 4
    assert extra["reward_certified"] == [True] * 4
    assert extra["reward_calibration_scope"] == ["synthetic"] * 4
    assert extra["reward_outcome"][1] == "reconciliation_required"
    assert scored[1].unscored


def test_all_unscored_group_is_flagged_and_zeroed_in_tensor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    episodes = ["vep.0002.a", "vep.0002.b", "vep.0002.c", "vep.0002.d"]
    receipts = receipts_by_episode(
        {
            episodes[0]: RewardOutcomeV1.FAILED_PLATFORM,
            episodes[1]: RewardOutcomeV1.FAILED_PLATFORM,
            episodes[2]: RewardOutcomeV1.VERIFIED,
            episodes[3]: RewardOutcomeV1.HALTED_BEFORE_EFFECT,
        }
    )
    manager = _manager(receipts)
    rewards, extra, _ = manager.score_batch(_infos(episodes), ["u1", "u1", "u2", "u2"], 3)
    assert rewards == [None, None, 1.0, 0.0]
    assert extra["reward_group_unscored"] == [True, True, False, False]

    # The documented DataProto shape, with a numpy stand-in for torch so the
    # test does not install a multi-gigabyte package.
    fake_torch = types.SimpleNamespace(
        zeros_like=lambda x, dtype=None: np.zeros(x.shape, dtype=np.float32),
        float32=np.float32,
    )
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    prompts = np.ones((4, 2), dtype=np.int64)
    responses = np.ones((4, 3), dtype=np.int64)
    attention_mask = np.array(
        [[1, 1, 1, 1, 0], [1, 1, 1, 0, 0], [1, 1, 1, 1, 1], [1, 1, 1, 1, 0]], dtype=np.int64
    )
    data = types.SimpleNamespace(
        batch={"prompts": prompts, "responses": responses, "attention_mask": attention_mask},
        non_tensor_batch={
            "uid": np.array(["u1", "u1", "u2", "u2"]),
            "extra_info": np.array(_infos(episodes)),
        },
        meta_info={"global_steps": 3},
    )
    out = manager(data, return_dict=True)
    tensor = out["reward_tensor"]
    assert tensor.shape == responses.shape
    # Reward sits at each response's last valid token; unscored group is 0.
    assert tensor.tolist() == [
        [0.0, 0.0, 0.0],
        [0.0, 0.0, 0.0],
        [0.0, 0.0, 1.0],
        [0.0, 1.0 * 0.0, 0.0],
    ]
    assert out["reward_extra_info"]["reward_group_unscored"] == [True, True, False, False]


# -- development_only refusal ----------------------------------------------------------------


def test_development_only_halts_when_certification_required() -> None:
    episodes = ["vep.0003.a", "vep.0003.b"]
    receipts = receipts_by_episode({item: RewardOutcomeV1.VERIFIED for item in episodes}, tier=1)
    with pytest.raises(UncertifiedRewardError, match="oracle_tier=1"):
        _manager(receipts, certificate=None).score_batch(_infos(episodes), ["u1", "u1"], 3)


def test_development_only_is_never_certified(caplog: pytest.LogCaptureFixture) -> None:
    episodes = ["vep.0004.a", "vep.0004.b"]
    receipts = receipts_by_episode({item: RewardOutcomeV1.VERIFIED for item in episodes}, tier=0)
    with caplog.at_level(logging.INFO, logger="openadapt_evals.reward.receipts"):
        rewards, extra, _ = _manager(
            receipts, certificate=None, require_certified=False
        ).score_batch(_infos(episodes), ["u1", "u1"], 3)
    assert rewards == [1.0, 1.0]
    assert extra["reward_certified"] == [False, False]
    assert extra["reward_development_only"] == [True, True]
    assert extra["reward_certificate_state"] == ["absent", "absent"]
    assert any("development_only" in record.getMessage() for record in caplog.records)


# -- expiry logging ---------------------------------------------------------------------------


def test_expiry_is_logged_at_the_trainers_step(caplog: pytest.LogCaptureFixture) -> None:
    episodes = ["vep.0005.a", "vep.0005.b"]
    short = certificate(issued_at_policy_update=5, expiry_policy_updates=3)
    receipts = receipts_by_episode(
        {item: RewardOutcomeV1.VERIFIED for item in episodes}, certificate=short, policy_update=6
    )
    manager = _manager(receipts, certificate=short, require_certified=False)
    with caplog.at_level(logging.WARNING, logger="openadapt_evals.reward.receipts"):
        _, current, _ = manager.score_batch(_infos(episodes), ["u1", "u1"], 7)
        _, expired, _ = manager.score_batch(_infos(episodes), ["u1", "u1"], 8)
    assert current["reward_certificate_state"] == ["current", "current"]
    assert expired["reward_certificate_state"] == ["expired", "expired"]
    assert expired["reward_certified"] == [False, False]
    messages = [
        record.getMessage() for record in caplog.records if "expired" in record.getMessage()
    ]
    assert len(messages) == 2
    assert all("expires_at=8" in message for message in messages)
    with pytest.raises(UncertifiedRewardError):
        _manager(receipts, certificate=short).score_batch(_infos(episodes), ["u1", "u1"], 8)


# -- oracle identity ----------------------------------------------------------------------------


def _recording_manager(receipts, seen: list[EpisodeDescriptor], **kwargs) -> CertifiedRewardManager:
    def fetch(descriptor: EpisodeDescriptor) -> RewardEvidenceReceiptV1:
        seen.append(descriptor)
        return receipts[descriptor.episode_id]

    options = {
        "reward_contract_digest": CONTRACT.digest,
        "policy_checkpoint_id": POLICY_CHECKPOINT,
        "certificate": CERTIFICATE,
    }
    options.update(kwargs)
    return CertifiedRewardManager(source=CallableRewardSource(fetch), **options)


def test_identity_from_non_tensor_batch_column_wins_over_extra_info() -> None:
    episodes = ["vep.0006.a", "vep.0006.b"]
    receipts = receipts_by_episode({item: RewardOutcomeV1.VERIFIED for item in episodes})
    seen: list[EpisodeDescriptor] = []
    manager = _recording_manager(receipts, seen)
    column = [{"record_id": 7}, {"record_id": 8}]
    rewards, _, _ = manager.score_batch(
        _infos(episodes), ["u1", "u1"], 3, identities=column, runtime_signals=["completed", None]
    )
    assert rewards == [1.0, 1.0]
    assert [d.oracle_identity for d in seen] == [{"record_id": "7"}, {"record_id": "8"}]
    assert seen[0].as_payload()["metadata"] == {
        "oracle_identity": {"record_id": "7"},
        "runtime_signal": "completed",
    }
    assert seen[1].as_payload()["metadata"] == {"oracle_identity": {"record_id": "8"}}


def test_identity_falls_back_to_extra_info() -> None:
    episodes = ["vep.0007.a", "vep.0007.b"]
    receipts = receipts_by_episode({item: RewardOutcomeV1.VERIFIED for item in episodes})
    seen: list[EpisodeDescriptor] = []
    manager = _recording_manager(receipts, seen)
    manager.score_batch(_infos(episodes), ["u1", "u1"], 3)
    assert [d.oracle_identity for d in seen] == identities_for(episodes)


def test_missing_identity_is_refused_before_any_fetch() -> None:
    episodes = ["vep.0008.a", "vep.0008.b"]
    receipts = receipts_by_episode({item: RewardOutcomeV1.VERIFIED for item in episodes})
    seen: list[EpisodeDescriptor] = []
    manager = _recording_manager(receipts, seen)
    with pytest.raises(OracleIdentityError, match="'oracle_identity'.*identity_missing"):
        manager.score_batch(_infos(episodes, identity=False), ["u1", "u1"], 3)
    assert seen == []
    # One bad row names its episode.
    infos = _infos(episodes)
    infos[1]["oracle_identity"] = {}
    with pytest.raises(OracleIdentityError, match="vep.0008.b"):
        manager.score_batch(infos, ["u1", "u1"], 3)
    assert seen == []
    # Disabled on purpose: nothing is sent and nothing is refused here.
    relaxed = _recording_manager(receipts, seen, oracle_identity_key=None)
    rewards, _, _ = relaxed.score_batch(_infos(episodes, identity=False), ["u1", "u1"], 3)
    assert rewards == [1.0, 1.0]
    assert all(d.oracle_identity is None for d in seen)


def test_call_reads_identity_column_from_non_tensor_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    episodes = ["vep.0009.a", "vep.0009.b"]
    receipts = receipts_by_episode({item: RewardOutcomeV1.VERIFIED for item in episodes})
    seen: list[EpisodeDescriptor] = []
    manager = _recording_manager(receipts, seen)
    fake_torch = types.SimpleNamespace(
        zeros_like=lambda x, dtype=None: np.zeros(x.shape, dtype=np.float32),
        float32=np.float32,
    )
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    data = types.SimpleNamespace(
        batch={
            "prompts": np.ones((2, 2), dtype=np.int64),
            "responses": np.ones((2, 3), dtype=np.int64),
            "attention_mask": np.ones((2, 5), dtype=np.int64),
        },
        non_tensor_batch={
            "uid": np.array(["u1", "u1"]),
            "extra_info": np.array(_infos(episodes, identity=False)),
            "oracle_identity": np.array([{"record_id": "col-a"}, {"record_id": "col-b"}]),
        },
        meta_info={"global_steps": 3},
    )
    out = manager(data, return_dict=True)
    assert out["reward_tensor"].tolist() == [[0.0, 0.0, 1.0], [0.0, 0.0, 1.0]]
    assert [d.oracle_identity for d in seen] == [{"record_id": "col-a"}, {"record_id": "col-b"}]


def test_endpoint_token_sets_the_bearer_header() -> None:
    manager = CertifiedRewardManager(
        endpoint_url="http://reward.test",
        endpoint_token="tok",
        reward_contract_digest=CONTRACT.digest,
        policy_checkpoint_id=POLICY_CHECKPOINT,
    )
    assert manager.source.headers == {"Authorization": "Bearer tok"}


# -- registration ---------------------------------------------------------------------------------


def test_register_with_verl_uses_the_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    registry: dict[str, type] = {}
    fake_pkg = types.ModuleType("verl.workers.reward_manager")

    def register(name: str):
        def wrap(cls: type) -> type:
            registry[name] = cls
            return cls

        return wrap

    fake_pkg.register = register  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "verl", types.ModuleType("verl"))
    monkeypatch.setitem(sys.modules, "verl.workers", types.ModuleType("verl.workers"))
    monkeypatch.setitem(sys.modules, "verl.workers.reward_manager", fake_pkg)
    assert register_with_verl() is True
    assert registry[REWARD_MANAGER_NAME] is CertifiedRewardManager


def test_register_without_verl_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "verl", None)
    monkeypatch.setitem(sys.modules, "verl.workers.reward_manager", None)
    assert register_with_verl() is False


def test_manager_needs_a_source() -> None:
    with pytest.raises(ValueError, match="endpoint_url"):
        CertifiedRewardManager(
            reward_contract_digest=CONTRACT.digest, policy_checkpoint_id=POLICY_CHECKPOINT
        )
