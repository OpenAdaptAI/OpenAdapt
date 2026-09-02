"""The trainer-side client for a reward worker. Not a trainer adapter.

This module carries the pieces a trainer node needs to talk to a reward
worker over HTTP: the wire payload for one episode, the ``POST /v1/rewards``
client, and the receipt's scalar. It offers no ``reward_funcs`` entry for
TRL and no ``compute_score`` for verl, on purpose.

The trainer adapters live in ``openadapt_evals.reward``:
``CertifiedRewardFunction`` for TRL's ``GRPOTrainer`` and
``CertifiedRewardManager`` for verl's reward manager. They drop an unscored
episode by group-mean fill: the episode receives the mean reward of its
scored group-mates, so its GRPO advantage is exactly zero and the scored
mean is unchanged.

A ``None`` or NaN reward is not a drop in TRL. ``GRPOTrainer`` turns a
``None`` into NaN, combines the per-function rewards with ``nansum``, and
takes the group mean over the result, so with one reward function an
unscored episode trains as 0.0. That is the outcome the reward contract
forbids for ``reconciliation_required`` and ``failed_platform``. verl's
per-sample ``compute_score`` hook has no sentinel at all; whatever it
returns lands in the reward tensor.

openadapt-evals depends on openadapt-flow, so this package cannot import
the adapters. Install them on the trainer node with
``pip install 'openadapt-evals>=0.96.0'``.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Protocol

from openadapt_types.reward import RewardEvidenceReceiptV1

from openadapt_flow.reward.models import EpisodeDescriptorV1

REWARDS_ROUTE = "/v1/rewards"


class RewardScorer(Protocol):
    """Anything that turns an episode descriptor into a reward envelope.

    :class:`openadapt_flow.reward.worker.RewardWorker` scores in-process.
    :class:`HttpRewardClient` calls a worker over HTTP from a trainer node.
    """

    def score_episode(self, payload: Mapping[str, Any]) -> dict[str, Any]: ...


class HttpRewardClient:
    """Thin client for ``POST /v1/rewards`` on a reward worker.

    ``transport`` is an optional ``httpx`` transport, for tests
    (``httpx.MockTransport``).
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_s: float = 30.0,
        transport: Any = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_s = timeout_s
        self.transport = transport

    @property
    def url(self) -> str:
        return f"{self.base_url}{REWARDS_ROUTE}"

    def score_episode(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        import httpx

        with httpx.Client(timeout=self.timeout_s, transport=self.transport) as client:
            response = client.post(
                self.url,
                json=dict(payload),
                headers={"Authorization": f"Bearer {self.token}"},
            )
        if response.status_code == 409:
            raise RuntimeError(
                f"episode already scored: {response.json().get('detail')}"
            )
        response.raise_for_status()
        return dict(response.json())


def scalar_of(envelope: Mapping[str, Any]) -> Optional[float]:
    """The receipt's scalar, or ``None`` when the episode is unscored.

    ``None`` here is a client-side value, not a trainer reward. A trainer
    must never hand it to TRL or verl as the sample's reward; see the module
    docstring.
    """

    receipt = RewardEvidenceReceiptV1.model_validate(envelope["receipt"])
    return receipt.scalar_reward


def episode_from_columns(
    *,
    episode_id: str,
    policy_checkpoint_id: str,
    policy_update: int,
    reward_contract_digest: str,
    oracle_identity: Optional[Mapping[str, Any]],
    runtime_signal: str = "completed",
) -> dict[str, Any]:
    """Build the wire payload for one episode, in the trainer client's shape.

    The keys are the ones ``openadapt_evals.reward.receipts.EpisodeDescriptor``
    sends, plus the descriptor model's ``schema_version``; the worker accepts
    both. ``oracle_identity`` may be ``None`` when the environment registered
    the identity with ``RewardWorker.begin_episode`` before the rollout.
    """

    metadata: dict[str, Any] = {"runtime_signal": runtime_signal}
    if oracle_identity is not None:
        metadata["oracle_identity"] = {
            str(k): str(v) for k, v in oracle_identity.items()
        }
    return EpisodeDescriptorV1(
        episode_id=str(episode_id),
        policy_checkpoint_id=str(policy_checkpoint_id),
        policy_update=int(policy_update),
        reward_contract_digest=reward_contract_digest,
        metadata=metadata,
    ).model_dump(mode="json", exclude_none=True)
