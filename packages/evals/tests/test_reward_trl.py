"""TRL reward function: unscored drop, development_only refusal, expiry logging."""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest
from openadapt_types.reward import (
    RewardCertificationRefused,
    RewardEvidenceReceiptV1,
    RewardOutcomeV1,
)

from openadapt_evals.reward import (
    CallableRewardSource,
    EpisodeDescriptor,
    HttpRewardEndpoint,
    OracleIdentityError,
    RewardEndpointError,
    UncertifiedRewardError,
    assess_receipt,
    fill_unscored_with_group_mean,
)
from openadapt_evals.reward.receipts import ReceiptMismatchError, consecutive_groups
from openadapt_evals.reward.trl import REWARD_FUNC_NAME, CertifiedRewardFunction
from tests.reward_fixtures import (
    CERTIFICATE,
    CONTRACT,
    POLICY_CHECKPOINT,
    certificate,
    identities_for,
    identity_for,
    receipt,
    receipts_by_episode,
)


class _State:
    def __init__(self, global_step: int) -> None:
        self.global_step = global_step


def _source(receipts: dict[str, RewardEvidenceReceiptV1]) -> CallableRewardSource:
    def fetch(descriptor: EpisodeDescriptor) -> RewardEvidenceReceiptV1:
        return receipts[descriptor.episode_id]

    return CallableRewardSource(fetch)


def _reward_fn(receipts, **kwargs) -> CertifiedRewardFunction:
    options = {
        "reward_contract_digest": CONTRACT.digest,
        "policy_checkpoint_id": POLICY_CHECKPOINT,
        "num_generations": 4,
        "certificate": CERTIFICATE,
    }
    options.update(kwargs)
    return CertifiedRewardFunction(_source(receipts), **options)


def _call(
    fn: CertifiedRewardFunction,
    episodes: list[str],
    step: int = 3,
    prompts=None,
    **columns,
):
    prompts = prompts or ["p"] * len(episodes)
    columns.setdefault("oracle_identity", identities_for(episodes))
    columns = {key: value for key, value in columns.items() if value is not None}
    return fn(
        prompts, ["c"] * len(episodes), episode_id=episodes, trainer_state=_State(step), **columns
    )


# -- unscored drop --------------------------------------------------------------------------


def test_fill_unscored_pins_advantage_to_zero() -> None:
    values = [1.0, 0.0, None, 1.0]
    filled = fill_unscored_with_group_mean(values, [0, 0, 0, 0])
    scored_mean = (1.0 + 0.0 + 1.0) / 3
    assert filled == [1.0, 0.0, pytest.approx(scored_mean), 1.0]
    # The group mean is unchanged by the filled sample, so its advantage is 0.
    assert sum(filled) / len(filled) == pytest.approx(scored_mean)
    assert filled[2] - sum(filled) / len(filled) == pytest.approx(0.0)


def test_fill_unscored_keeps_none_for_all_unscored_group() -> None:
    assert fill_unscored_with_group_mean([None, None, 1.0, 0.0], [0, 0, 1, 1]) == [
        None,
        None,
        1.0,
        0.0,
    ]


def test_consecutive_groups_by_size_and_by_prompt() -> None:
    assert consecutive_groups(["a"] * 4 + ["b"] * 4, 4) == [0, 0, 0, 0, 1, 1, 1, 1]
    assert consecutive_groups(["a", "a", "b", "b", "b"], None) == [0, 0, 1, 1, 1]
    with pytest.raises(ValueError):
        consecutive_groups(["a"] * 5, 4)


def test_unscored_episode_is_dropped_not_scored_zero() -> None:
    episodes = ["ep.0001.a", "ep.0001.b", "ep.0001.c", "ep.0001.d"]
    receipts = receipts_by_episode(
        {
            episodes[0]: RewardOutcomeV1.VERIFIED,
            episodes[1]: RewardOutcomeV1.HALTED_BEFORE_EFFECT,
            episodes[2]: RewardOutcomeV1.FAILED_PLATFORM,
            episodes[3]: RewardOutcomeV1.RECONCILIATION_REQUIRED,
        }
    )
    fn = _reward_fn(receipts)
    rewards = _call(fn, episodes)
    scored_mean = (1.0 + 0.0) / 2
    assert rewards == [1.0, 0.0, pytest.approx(scored_mean), pytest.approx(scored_mean)]
    # The unscored pair took the scored mean, not 0.0, and left it unchanged.
    assert sum(rewards) / len(rewards) == pytest.approx(scored_mean)
    columns = fn.metadata_columns()
    assert columns["reward_unscored"] == [False, False, True, True]
    assert columns["reward_certified"] == [True, True, True, True]
    assert columns["reward_calibration_scope"] == ["synthetic"] * 4


def test_all_unscored_group_returns_none_for_trl() -> None:
    episodes = ["ep.0002.a", "ep.0002.b", "ep.0002.c", "ep.0002.d"]
    receipts = receipts_by_episode({item: RewardOutcomeV1.FAILED_PLATFORM for item in episodes})
    assert _call(_reward_fn(receipts), episodes) == [None] * 4


# -- development_only refusal --------------------------------------------------------------


def test_development_only_receipt_cannot_claim_certified() -> None:
    tier0 = receipt("ep.0003.a", RewardOutcomeV1.VERIFIED, tier=0)
    assert tier0.development_only and not tier0.certified
    with pytest.raises(ValueError):
        RewardEvidenceReceiptV1.model_validate({**tier0.model_dump(mode="json"), "certified": True})
    forged = tier0.model_copy(update={"certified": True})
    with pytest.raises(RewardCertificationRefused):
        assess_receipt(forged, policy_update=3)


def test_require_certified_halts_on_development_only() -> None:
    episodes = ["ep.0004.a", "ep.0004.b", "ep.0004.c", "ep.0004.d"]
    receipts = receipts_by_episode({item: RewardOutcomeV1.VERIFIED for item in episodes}, tier=0)
    with pytest.raises(UncertifiedRewardError, match="development_only=True"):
        _call(_reward_fn(receipts, certificate=None), episodes)


def test_development_run_scores_but_never_certifies(caplog: pytest.LogCaptureFixture) -> None:
    episodes = ["ep.0005.a", "ep.0005.b", "ep.0005.c", "ep.0005.d"]
    receipts = receipts_by_episode({item: RewardOutcomeV1.VERIFIED for item in episodes}, tier=0)
    with caplog.at_level(logging.INFO, logger="openadapt_evals.reward.receipts"):
        fn = _reward_fn(receipts, certificate=None, require_certified=False)
        rewards = _call(fn, episodes)
    assert rewards == [1.0] * 4
    assert fn.metadata_columns()["reward_certified"] == [False] * 4
    assert fn.metadata_columns()["reward_development_only"] == [True] * 4
    assert any("development_only" in record.getMessage() for record in caplog.records)


# -- certificate expiry --------------------------------------------------------------------


def test_expired_certificate_is_logged_and_halts(caplog: pytest.LogCaptureFixture) -> None:
    episodes = ["ep.0006.a", "ep.0006.b", "ep.0006.c", "ep.0006.d"]
    short = certificate(issued_at_policy_update=0, expiry_policy_updates=10)
    receipts = receipts_by_episode(
        {item: RewardOutcomeV1.VERIFIED for item in episodes}, certificate=short, policy_update=3
    )
    fn = _reward_fn(receipts, certificate=short)
    assert _call(fn, episodes, step=9) == [1.0] * 4
    with caplog.at_level(logging.WARNING, logger="openadapt_evals.reward.receipts"):
        with pytest.raises(UncertifiedRewardError, match="certificate_state=expired"):
            _call(fn, episodes, step=10)
    messages = [record.getMessage() for record in caplog.records]
    assert any("expired" in message and short.certificate_id in message for message in messages)
    assert any("expires_at=10" in message for message in messages)


def test_expired_certificate_without_require_is_uncertified(
    caplog: pytest.LogCaptureFixture,
) -> None:
    episodes = ["ep.0007.a", "ep.0007.b", "ep.0007.c", "ep.0007.d"]
    short = certificate(issued_at_policy_update=0, expiry_policy_updates=10)
    receipts = receipts_by_episode(
        {item: RewardOutcomeV1.VERIFIED for item in episodes}, certificate=short, policy_update=3
    )
    fn = _reward_fn(receipts, certificate=short, require_certified=False)
    with caplog.at_level(logging.WARNING, logger="openadapt_evals.reward.receipts"):
        rewards = _call(fn, episodes, step=25)
    assert rewards == [1.0] * 4
    assert fn.metadata_columns()["reward_certified"] == [False] * 4
    assert fn.metadata_columns()["reward_certificate_state"] == ["expired"] * 4
    assert sum("expired" in record.getMessage() for record in caplog.records) == 4


# -- binding and transport ------------------------------------------------------------------


def test_receipt_for_another_contract_is_refused() -> None:
    episodes = ["ep.0008.a", "ep.0008.b", "ep.0008.c", "ep.0008.d"]
    receipts = receipts_by_episode({item: RewardOutcomeV1.VERIFIED for item in episodes})
    fn = _reward_fn(receipts, reward_contract_digest="sha256:" + "0" * 64)
    with pytest.raises(ReceiptMismatchError, match="binds contract"):
        _call(fn, episodes)


def test_async_path_matches_sync(caplog: pytest.LogCaptureFixture) -> None:
    episodes = ["ep.0009.a", "ep.0009.b", "ep.0009.c", "ep.0009.d"]
    receipts = receipts_by_episode(
        {
            episodes[0]: RewardOutcomeV1.VERIFIED,
            episodes[1]: RewardOutcomeV1.HALTED_BEFORE_EFFECT,
            episodes[2]: RewardOutcomeV1.VERIFIED,
            episodes[3]: RewardOutcomeV1.FAILED_PLATFORM,
        }
    )
    fn = _reward_fn(receipts, require_certified=False)
    coroutine_fn = fn.as_async()
    assert asyncio.iscoroutinefunction(coroutine_fn)
    assert coroutine_fn.__name__ == REWARD_FUNC_NAME == fn.__name__
    expected = _call(fn, episodes)
    got = asyncio.run(
        coroutine_fn(
            ["p"] * 4,
            ["c"] * 4,
            episode_id=episodes,
            oracle_identity=identities_for(episodes),
            trainer_state=_State(3),
        )
    )
    assert got == expected
    assert expected[3] == pytest.approx((1.0 + 0.0 + 1.0) / 3)


def test_http_endpoint_posts_descriptor_and_parses_receipt() -> None:
    episode = "ep.0010.a"
    signed = receipt(episode, RewardOutcomeV1.VERIFIED)
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and request.url.path == "/v1/rewards"
        body = json.loads(request.content)
        seen.append(body)
        if body["episode_id"] == episode:
            return httpx.Response(200, json={"receipt": signed.model_dump(mode="json")})
        return httpx.Response(500, text="oracle down")

    endpoint = HttpRewardEndpoint("http://reward.test", transport=httpx.MockTransport(handler))
    descriptor = EpisodeDescriptor(
        episode, POLICY_CHECKPOINT, 3, CONTRACT.digest, task_id="task.test.0001"
    )
    assert endpoint.fetch(descriptor) == signed
    assert asyncio.run(endpoint.afetch(descriptor)) == signed
    assert seen[0]["policy_checkpoint_id"] == POLICY_CHECKPOINT
    assert seen[0]["reward_contract_digest"] == CONTRACT.digest
    # Without an identity the body is the one this client always sent.
    assert seen[0] == {
        "episode_id": episode,
        "policy_checkpoint_id": POLICY_CHECKPOINT,
        "policy_update": 3,
        "reward_contract_digest": CONTRACT.digest,
        "task_id": "task.test.0001",
        "metadata": {},
    }
    with pytest.raises(RewardEndpointError, match="HTTP 500"):
        endpoint.fetch(EpisodeDescriptor("ep.0010.b", POLICY_CHECKPOINT, 3, CONTRACT.digest))


# -- oracle identity ------------------------------------------------------------------------


def test_http_body_carries_metadata_oracle_identity_and_bearer() -> None:
    """The wire body is exactly what the flow reward worker reads."""

    episode = "ep.0012.a"
    signed = receipt(episode, RewardOutcomeV1.VERIFIED)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"receipt": signed.model_dump(mode="json")})

    endpoint = HttpRewardEndpoint(
        "http://reward.test", token="secret-token", transport=httpx.MockTransport(handler)
    )
    descriptor = EpisodeDescriptor(
        episode,
        POLICY_CHECKPOINT,
        3,
        CONTRACT.digest,
        oracle_identity={"record_id": "rec-0012"},
        runtime_signal="completed",
    )
    assert endpoint.fetch(descriptor) == signed
    request = seen[0]
    assert request.headers["Authorization"] == "Bearer secret-token"
    body = json.loads(request.content)
    assert body == {
        "episode_id": episode,
        "policy_checkpoint_id": POLICY_CHECKPOINT,
        "policy_update": 3,
        "reward_contract_digest": CONTRACT.digest,
        "metadata": {
            "oracle_identity": {"record_id": "rec-0012"},
            "runtime_signal": "completed",
        },
    }
    assert "oracle_identity" not in body  # only under metadata, never top-level
    assert body["metadata"]["oracle_identity"] == identity_for(episode) | {"record_id": "rec-0012"}


def test_identity_column_reaches_every_descriptor() -> None:
    episodes = ["ep.0013.a", "ep.0013.b", "ep.0013.c", "ep.0013.d"]
    receipts = receipts_by_episode({item: RewardOutcomeV1.VERIFIED for item in episodes})
    seen: list[EpisodeDescriptor] = []

    def fetch(descriptor: EpisodeDescriptor):
        seen.append(descriptor)
        return receipts[descriptor.episode_id]

    options = {
        "reward_contract_digest": CONTRACT.digest,
        "policy_checkpoint_id": POLICY_CHECKPOINT,
        "num_generations": 4,
        "certificate": CERTIFICATE,
    }
    fn = CertifiedRewardFunction(CallableRewardSource(fetch), **options)
    # Values are stringified and keys sorted, the shape the worker validates.
    identities = [{"record_id": 1000 + index} for index in range(4)]
    signals = ["completed", "completed", "halted_before_effect", "completed"]
    assert _call(fn, episodes, oracle_identity=identities, runtime_signal=signals) == [1.0] * 4
    assert [d.oracle_identity for d in seen] == [{"record_id": str(1000 + i)} for i in range(4)]
    assert [d.runtime_signal for d in seen] == signals
    assert seen[2].as_payload()["metadata"] == {
        "oracle_identity": {"record_id": "1002"},
        "runtime_signal": "halted_before_effect",
    }


def test_missing_identity_column_is_refused_before_any_fetch() -> None:
    episodes = ["ep.0014.a", "ep.0014.b", "ep.0014.c", "ep.0014.d"]
    receipts = receipts_by_episode({item: RewardOutcomeV1.VERIFIED for item in episodes})
    calls: list[str] = []

    def fetch(descriptor: EpisodeDescriptor):
        calls.append(descriptor.episode_id)
        return receipts[descriptor.episode_id]

    fn = CertifiedRewardFunction(
        CallableRewardSource(fetch),
        reward_contract_digest=CONTRACT.digest,
        policy_checkpoint_id=POLICY_CHECKPOINT,
        num_generations=4,
        certificate=CERTIFICATE,
    )
    with pytest.raises(OracleIdentityError, match="'oracle_identity'.*identity_missing"):
        _call(fn, episodes, oracle_identity=None)
    assert calls == []

    # A renamed column is named in the error too.
    renamed = CertifiedRewardFunction(
        CallableRewardSource(fetch),
        reward_contract_digest=CONTRACT.digest,
        policy_checkpoint_id=POLICY_CHECKPOINT,
        num_generations=4,
        certificate=CERTIFICATE,
        oracle_identity_column="patient",
    )
    with pytest.raises(OracleIdentityError, match="'patient'"):
        _call(renamed, episodes)
    assert calls == []
    assert (
        _call(renamed, episodes, oracle_identity=None, patient=identities_for(episodes))
        == [1.0] * 4
    )
    assert calls == episodes


def test_empty_identity_row_is_refused_naming_the_episode() -> None:
    episodes = ["ep.0015.a", "ep.0015.b", "ep.0015.c", "ep.0015.d"]
    receipts = receipts_by_episode({item: RewardOutcomeV1.VERIFIED for item in episodes})
    fn = _reward_fn(receipts)
    bad = identities_for(episodes)
    bad[2] = {}
    with pytest.raises(OracleIdentityError, match="ep.0015.c"):
        _call(fn, episodes, oracle_identity=bad)
    bad[2] = {"record_id": ""}
    with pytest.raises(OracleIdentityError, match="empty key or value"):
        _call(fn, episodes, oracle_identity=bad)
    bad[2] = None
    with pytest.raises(OracleIdentityError, match="carries no oracle identity"):
        _call(fn, episodes, oracle_identity=bad)
    with pytest.raises(OracleIdentityError, match="3 rows for 4 episodes"):
        _call(fn, episodes, oracle_identity=identities_for(episodes[:3]))


def test_identity_column_can_be_disabled_for_registered_episodes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``None`` means the environment called ``begin_episode``; nothing is sent."""

    episodes = ["ep.0016.a", "ep.0016.b", "ep.0016.c", "ep.0016.d"]
    receipts = receipts_by_episode({item: RewardOutcomeV1.VERIFIED for item in episodes})
    seen: list[EpisodeDescriptor] = []

    def fetch(descriptor: EpisodeDescriptor):
        seen.append(descriptor)
        return receipts[descriptor.episode_id]

    with caplog.at_level(logging.WARNING, logger="openadapt_evals.reward.trl"):
        fn = CertifiedRewardFunction(
            CallableRewardSource(fetch),
            reward_contract_digest=CONTRACT.digest,
            policy_checkpoint_id=POLICY_CHECKPOINT,
            num_generations=4,
            certificate=CERTIFICATE,
            oracle_identity_column=None,
        )
    assert any("begin_episode" in record.getMessage() for record in caplog.records)
    assert _call(fn, episodes, oracle_identity=None) == [1.0] * 4
    assert all(d.oracle_identity is None for d in seen)
    assert all(d.as_payload()["metadata"] == {} for d in seen)


def test_endpoint_error_can_drop_instead_of_raise(caplog: pytest.LogCaptureFixture) -> None:
    episodes = ["ep.0011.a", "ep.0011.b", "ep.0011.c", "ep.0011.d"]
    receipts = receipts_by_episode({item: RewardOutcomeV1.VERIFIED for item in episodes[:3]})

    def fetch(descriptor: EpisodeDescriptor):
        if descriptor.episode_id not in receipts:
            raise RewardEndpointError("unreachable")
        return receipts[descriptor.episode_id]

    options = {
        "reward_contract_digest": CONTRACT.digest,
        "policy_checkpoint_id": POLICY_CHECKPOINT,
        "num_generations": 4,
        "certificate": CERTIFICATE,
        "require_certified": False,
    }
    strict = CertifiedRewardFunction(CallableRewardSource(fetch), **options)
    with pytest.raises(RewardEndpointError):
        _call(strict, episodes)
    lenient = CertifiedRewardFunction(
        CallableRewardSource(fetch), on_endpoint_error="unscored", **options
    )
    with caplog.at_level(logging.ERROR):
        assert _call(lenient, episodes) == [1.0, 1.0, 1.0, 1.0]
    assert lenient.metadata_columns()["reward_unscored"][3] is True
    assert any("no receipt" in record.getMessage() for record in caplog.records)
