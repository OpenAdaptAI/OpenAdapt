"""Reward-function adapters for TRL GRPO and verl.

Both adapters turn a trainer's per-sample data into an episode descriptor,
ask a scorer for a signed receipt, and hand back the scalar. An UNSCORED
episode never becomes 0.0:

* TRL: the adapter returns ``None`` for that sample. TRL documents ``None``
  as "this reward function does not apply to this sample" and excludes it
  from the reward calculation.
* verl: the reward manager has no such sentinel, so the adapter returns
  :data:`UNSCORED_REWARD` (``nan``) together with ``"openadapt_unscored":
  True`` in the dict. A NaN poisons a group's advantage instead of
  silently training on 0.0. :func:`drop_unscored` removes those samples
  from a group before the loss is computed; wire it in, or filter on the
  ``openadapt_unscored`` key.

TRL contract (read 2026-09-01):
https://huggingface.co/docs/trl/main/en/grpo_trainer#using-a-custom-reward-function
    reward_func(prompts, completions, completion_ids, trainer_state, **kwargs)
    -> list[float | None]
Every dataset column except ``prompt`` arrives in ``**kwargs`` as a list
aligned with ``completions``. ``trainer_state.global_step`` is the policy
update counter the certificate expiry is denominated in.

verl contract (read 2026-09-01):
https://verl.readthedocs.io/en/latest/preparation/reward_function.html
https://github.com/volcengine/verl/blob/main/verl/workers/reward_manager/naive.py
    compute_score(data_source, solution_str, ground_truth, extra_info=None)
    -> float | dict  (a dict must carry "score"; other keys are logged)
Configured through ``custom_reward_function.path`` and
``custom_reward_function.name``.
"""

from __future__ import annotations

import math
from typing import (
    Any,
    Callable,
    Iterable,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    TypeVar,
)

from openadapt_types.reward import RewardEvidenceReceiptV1

from openadapt_flow.reward.models import EpisodeDescriptorV1

#: The verl-side sentinel for an episode the oracle could not score.
#: ``nan`` on purpose: a trainer that forgets to drop it sees a NaN loss,
#: not a quiet 0.0 that teaches the policy that uncertainty is failure.
UNSCORED_REWARD: float = float("nan")

T = TypeVar("T")


def is_unscored(value: Any) -> bool:
    """True for ``None`` (TRL) and for the NaN sentinel (verl)."""

    if value is None:
        return True
    return isinstance(value, float) and math.isnan(value)


def drop_unscored(
    rewards: Sequence[Any], *aligned: Sequence[T]
) -> tuple[list[float], list[list[T]]]:
    """Filter a group down to the scored samples.

    Returns the kept rewards and, for each aligned sequence passed, the
    kept items in the same positions. A group that loses every sample
    comes back empty; the trainer must skip that group.
    """

    keep = [index for index, value in enumerate(rewards) if not is_unscored(value)]
    kept_rewards = [float(rewards[index]) for index in keep]
    kept_aligned = [[seq[index] for index in keep] for seq in aligned]
    return kept_rewards, kept_aligned


class RewardScorer(Protocol):
    """Anything that turns an episode descriptor into a reward envelope.

    :class:`openadapt_flow.reward.worker.RewardWorker` scores in-process.
    :class:`HttpRewardClient` calls a worker over HTTP from a trainer node.
    """

    def score_episode(self, payload: Mapping[str, Any]) -> dict[str, Any]: ...


class HttpRewardClient:
    """Thin client for ``POST /v1/rewards`` on a reward worker."""

    def __init__(self, base_url: str, token: str, *, timeout_s: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_s = timeout_s

    def score_episode(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        import httpx

        response = httpx.post(
            f"{self.base_url}/v1/rewards",
            json=dict(payload),
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=self.timeout_s,
        )
        if response.status_code == 409:
            raise RuntimeError(
                f"episode already scored: {response.json().get('detail')}"
            )
        response.raise_for_status()
        return dict(response.json())


def scalar_of(envelope: Mapping[str, Any]) -> Optional[float]:
    """The receipt's scalar, or ``None`` when the episode is unscored."""

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

    ``oracle_identity`` may be ``None`` when the environment registered the
    identity with ``RewardWorker.begin_episode`` before the rollout.
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


# -- TRL ----------------------------------------------------------------------


def trl_reward_function(
    scorer: RewardScorer,
    *,
    policy_checkpoint_id: str,
    reward_contract_digest: str,
    episode_column: str = "episode_id",
    identity_column: str = "oracle_identity",
    signal_column: str = "runtime_signal",
) -> Callable[..., list[Optional[float]]]:
    """Build a ``reward_funcs`` entry for ``trl.GRPOTrainer``.

    The dataset carries one row per episode with ``episode_column`` (the
    episode id the environment ran under), optionally ``identity_column``
    (a dict of the oracle identity keys; omit it when the environment
    registers the identity with ``begin_episode``), and optionally
    ``signal_column``. The policy update comes from
    ``trainer_state.global_step``. Each call returns one float per
    completion, or ``None`` for an unscored episode.
    """

    def openadapt_verified_effect_reward(
        prompts: Sequence[Any],
        completions: Sequence[Any],
        completion_ids: Optional[Sequence[Any]] = None,
        trainer_state: Any = None,
        **kwargs: Any,
    ) -> list[Optional[float]]:
        del prompts, completion_ids
        episodes = kwargs.get(episode_column)
        if episodes is None:
            raise KeyError(f"dataset must carry {episode_column!r}")
        identities = kwargs.get(identity_column) or [None] * len(completions)
        signals = kwargs.get(signal_column) or ["completed"] * len(completions)
        policy_update = int(getattr(trainer_state, "global_step", 0) or 0)
        rewards: list[Optional[float]] = []
        for episode_id, identity, signal in zip(episodes, identities, signals):
            envelope = scorer.score_episode(
                episode_from_columns(
                    episode_id=episode_id,
                    policy_checkpoint_id=policy_checkpoint_id,
                    policy_update=policy_update,
                    reward_contract_digest=reward_contract_digest,
                    oracle_identity=identity,
                    runtime_signal=signal,
                )
            )
            rewards.append(scalar_of(envelope))
        return rewards

    return openadapt_verified_effect_reward


# -- verl ---------------------------------------------------------------------


def verl_compute_score(
    scorer: RewardScorer,
    *,
    policy_checkpoint_id: str,
    reward_contract_digest: str,
    episode_key: str = "openadapt_episode",
) -> Callable[..., dict[str, Any]]:
    """Build a verl ``compute_score`` over a reward worker.

    ``extra_info[episode_key]`` must hold ``episode_id`` and
    ``policy_update``, plus ``oracle_identity`` unless the environment
    registered it with ``begin_episode``, and optionally ``runtime_signal``. The returned dict
    carries ``score`` (the scalar, or :data:`UNSCORED_REWARD`), the receipt
    id, and the flags a filter needs. verl stores every extra key in the
    batch's non-tensor data, so ``openadapt_unscored`` survives to the point
    where :func:`drop_unscored` can act on it.
    """

    def compute_score(
        data_source: Any,
        solution_str: Any,
        ground_truth: Any,
        extra_info: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        del data_source, solution_str, ground_truth
        info = dict(extra_info or {})
        episode = info.get(episode_key)
        if not isinstance(episode, Mapping):
            raise KeyError(f"extra_info must carry {episode_key!r}")
        envelope = scorer.score_episode(
            episode_from_columns(
                episode_id=str(episode["episode_id"]),
                policy_checkpoint_id=policy_checkpoint_id,
                policy_update=int(episode.get("policy_update", 0)),
                reward_contract_digest=reward_contract_digest,
                oracle_identity=episode.get("oracle_identity"),
                runtime_signal=str(episode.get("runtime_signal", "completed")),
            )
        )
        receipt = envelope["receipt"]
        scalar = scalar_of(envelope)
        return {
            "score": UNSCORED_REWARD if scalar is None else float(scalar),
            "openadapt_unscored": scalar is None,
            "openadapt_reward_outcome": receipt["reward_outcome"],
            "openadapt_receipt_id": receipt["receipt_id"],
            "openadapt_certified": bool(receipt["certified"]),
            "openadapt_development_only": bool(receipt["development_only"]),
        }

    return compute_score


def scored_groups(
    groups: Iterable[Sequence[Mapping[str, Any]]],
) -> list[list[Mapping[str, Any]]]:
    """Drop unscored samples from each verl group of ``compute_score`` dicts."""

    kept: list[list[Mapping[str, Any]]] = []
    for group in groups:
        rewards = [item["score"] for item in group]
        _kept_rewards, (kept_items,) = drop_unscored(rewards, list(group))
        kept.append(kept_items)
    return kept
