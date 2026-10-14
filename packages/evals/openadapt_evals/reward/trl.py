"""TRL GRPO reward function backed by certified reward receipts.

TRL's reward-function contract (https://huggingface.co/docs/trl/main/en/grpo_trainer,
"Using a custom reward function"): the function receives ``prompts``,
``completions``, ``completion_ids``, ``trainer_state``, and every dataset
column as keyword arguments, and returns one float per completion. It may
be a plain callable or an ``async def`` coroutine; async reward functions
are awaited concurrently.

The same page says a reward function "can also return ``None`` when the
reward is not applicable to those samples" and that such a function "is
excluded from the reward calculation for that sample". Excluded is not
dropped. In ``GRPOTrainer._calculate_rewards`` a ``None`` becomes ``NaN``,
the per-function rewards are combined with ``nansum``, and the group mean
and std are taken over the combined values. With one reward function a
``None`` row therefore trains as reward ``0.0``, which is exactly what the
contract forbids for ``reconciliation_required`` and ``failed_platform``.

So this adapter drops an unscored episode a different way. It gives the
episode the mean reward of its scored group-mates. GRPO's advantage is the
reward minus the group mean, so that episode's advantage is zero, it
contributes no policy gradient, and the scored episodes' mean is unchanged.
See ``fill_unscored_with_group_mean`` for the residual effect on the group
std. A group with no scored episode at all is returned as ``None`` for every
member; TRL then logs its "returned None" warning and the group trains with
zero advantage.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

from openadapt_types.reward import RewardCertificatePolicyV1, RewardCertificateV1

from openadapt_evals.reward.receipts import (
    ORACLE_IDENTITY_KEY,
    RUNTIME_SIGNAL_KEY,
    EpisodeDescriptor,
    OracleIdentityError,
    RewardEndpointError,
    RewardSource,
    ScoredEpisode,
    afetch_receipts,
    assess_receipt,
    clean_oracle_identity,
    consecutive_groups,
    fetch_receipts,
    fill_unscored_with_group_mean,
    require_certified_or_unscored,
)

logger = logging.getLogger(__name__)

REWARD_FUNC_NAME = "openadapt_certified_reward"


class CertifiedRewardFunction:
    """A TRL ``reward_funcs`` entry that scores completions from receipts.

    Pass an instance directly for the synchronous path, or
    ``instance.as_async()`` for TRL's concurrent async path.

    Args:
        source: where receipts come from (``HttpRewardEndpoint`` or
            ``CallableRewardSource``).
        reward_contract_digest: the contract every receipt must bind.
        policy_checkpoint_id: sent to the endpoint in each descriptor.
        num_generations: TRL's ``num_generations``. Each consecutive block of
            this size is one GRPO group. When ``None``, consecutive equal
            prompts form a group.
        require_certified: when true (default), a scored receipt that is not
            certified raises ``UncertifiedRewardError`` and stops training.
            Set false for a tier-0 or tier-1 development run; every such
            receipt is then logged as ``development_only``.
        certificate: the certificate the trainer holds. When given, expiry
            is re-evaluated at the trainer's own global step.
        certificate_policy: the contract's ``certificate_policy``. Give it
            alongside ``certificate`` and the trainer checks that the bound
            it holds is at least as tight as the contract demanded; a looser
            certificate stops being counted as certified.
        policy_update: overrides ``trainer_state.global_step`` (an int or a
            zero-argument callable).
        episode_id_column: the dataset column that names each episode.
        oracle_identity_column: the dataset column that holds each episode's
            oracle identity, a mapping keyed exactly as the contract's
            ``oracle.identity_keys`` (for MockMed, ``{"patient_id": ...}``).
            It is sent as ``metadata.oracle_identity``, where the flow
            reward worker reads it. The column is required: a batch without
            it raises ``OracleIdentityError`` before any HTTP call, because
            the worker would answer 422 ``identity_missing`` for every
            episode. Pass ``None`` only when the environment registered
            every identity with ``RewardWorker.begin_episode`` in-process.
        runtime_signal_column: the dataset column with the runtime's own
            end-of-episode signal (``completed``, ``halted_before_effect``,
            ``refused``, ``rejected_policy``, ``failed_platform``), sent as
            ``metadata.runtime_signal``. Optional: when the column is
            absent the worker assumes ``completed``.
        on_endpoint_error: ``"raise"`` (default) or ``"unscored"``. The
            latter treats a transport failure as a dropped episode and logs
            it at ERROR; there is no receipt for it.
    """

    def __init__(
        self,
        source: RewardSource,
        *,
        reward_contract_digest: str,
        policy_checkpoint_id: str,
        num_generations: int | None = None,
        require_certified: bool = True,
        certificate: RewardCertificateV1 | None = None,
        certificate_policy: RewardCertificatePolicyV1 | None = None,
        policy_update: int | Callable[[], int] | None = None,
        episode_id_column: str = "episode_id",
        task_id_column: str | None = "task_id",
        oracle_identity_column: str | None = ORACLE_IDENTITY_KEY,
        runtime_signal_column: str | None = RUNTIME_SIGNAL_KEY,
        environment_id: str | None = None,
        on_endpoint_error: str = "raise",
    ) -> None:
        if on_endpoint_error not in {"raise", "unscored"}:
            raise ValueError("on_endpoint_error must be 'raise' or 'unscored'")
        self.source = source
        self.reward_contract_digest = reward_contract_digest
        self.policy_checkpoint_id = policy_checkpoint_id
        self.num_generations = num_generations
        self.require_certified = require_certified
        self.certificate = certificate
        self.certificate_policy = certificate_policy
        self._policy_update = policy_update
        self.episode_id_column = episode_id_column
        self.task_id_column = task_id_column
        self.oracle_identity_column = oracle_identity_column
        self.runtime_signal_column = runtime_signal_column
        self.environment_id = environment_id
        self.on_endpoint_error = on_endpoint_error
        self.last_batch: list[ScoredEpisode | None] = []
        # TRL names reward columns after ``reward_func.__name__``.
        self.__name__ = REWARD_FUNC_NAME
        if not require_certified:
            logger.warning(
                "require_certified=False: rewards from this function may be "
                "development_only and are never certified"
            )
        if oracle_identity_column is None:
            logger.warning(
                "oracle_identity_column=None: episodes carry no oracle identity; the "
                "reward worker refuses any episode not registered with begin_episode"
            )

    # -- descriptor construction ---------------------------------------------------

    def policy_update(self, kwargs: dict[str, Any]) -> int:
        if callable(self._policy_update):
            return int(self._policy_update())
        if self._policy_update is not None:
            return int(self._policy_update)
        state = kwargs.get("trainer_state")
        step = getattr(state, "global_step", None)
        if step is None:
            raise ValueError(
                "policy_update is unknown: pass policy_update= or let TRL supply trainer_state"
            )
        return int(step)

    def descriptors(
        self, completions: Sequence[Any], kwargs: dict[str, Any]
    ) -> list[EpisodeDescriptor]:
        episode_ids = kwargs.get(self.episode_id_column)
        if episode_ids is None or len(episode_ids) != len(completions):
            raise ValueError(
                f"dataset column {self.episode_id_column!r} must supply one episode id per completion"
            )
        task_ids = kwargs.get(self.task_id_column) if self.task_id_column else None
        identities = self.identities(episode_ids, kwargs)
        signals = self._column(self.runtime_signal_column, episode_ids, kwargs)
        update = self.policy_update(kwargs)
        found: list[EpisodeDescriptor] = []
        for index, episode_id in enumerate(episode_ids):
            found.append(
                EpisodeDescriptor(
                    episode_id=str(episode_id),
                    policy_checkpoint_id=self.policy_checkpoint_id,
                    policy_update=update,
                    reward_contract_digest=self.reward_contract_digest,
                    task_id=str(task_ids[index]) if task_ids is not None else None,
                    environment_id=self.environment_id,
                    oracle_identity=identities[index] if identities is not None else None,
                    runtime_signal=str(signals[index]) if signals is not None else None,
                )
            )
        return found

    def identities(
        self, episode_ids: Sequence[Any], kwargs: dict[str, Any]
    ) -> list[dict[str, str]] | None:
        """One cleaned oracle identity per episode, or ``None`` when disabled.

        Raises ``OracleIdentityError`` before any receipt is fetched when the
        column is required and absent, or when any row has no usable identity.
        """

        column = self.oracle_identity_column
        if column is None:
            return None
        raw = kwargs.get(column)
        if raw is None:
            raise OracleIdentityError(
                f"dataset column {column!r} is missing: the reward worker needs each "
                "episode's oracle identity (its contract's identity_keys) or it answers "
                "422 identity_missing. Add the column, or pass oracle_identity_column=None "
                "when every identity was registered with RewardWorker.begin_episode."
            )
        if len(raw) != len(episode_ids):
            raise OracleIdentityError(
                f"dataset column {column!r} has {len(raw)} rows for {len(episode_ids)} episodes"
            )
        return [
            clean_oracle_identity(item, where=f"column {column!r}, episode {episode_id!s}")
            for item, episode_id in zip(raw, episode_ids)
        ]

    @staticmethod
    def _column(
        name: str | None, episode_ids: Sequence[Any], kwargs: dict[str, Any]
    ) -> Sequence[Any] | None:
        if name is None:
            return None
        values = kwargs.get(name)
        if values is None:
            return None
        if len(values) != len(episode_ids):
            raise ValueError(
                f"dataset column {name!r} has {len(values)} rows for {len(episode_ids)} episodes"
            )
        return values

    # -- scoring --------------------------------------------------------------------

    def _assess(
        self, receipts: Sequence[Any], descriptors: Sequence[EpisodeDescriptor]
    ) -> list[ScoredEpisode | None]:
        scored: list[ScoredEpisode | None] = []
        for receipt, descriptor in zip(receipts, descriptors):
            if receipt is None:
                scored.append(None)
                continue
            episode = assess_receipt(
                receipt,
                policy_update=descriptor.policy_update,
                expected_contract_digest=self.reward_contract_digest,
                expected_episode_id=descriptor.episode_id,
                certificate=self.certificate,
                certificate_policy=self.certificate_policy,
            )
            if self.require_certified:
                require_certified_or_unscored(episode)
            scored.append(episode)
        return scored

    def _rewards(
        self, prompts: Sequence[Any], scored: Sequence[ScoredEpisode | None]
    ) -> list[float | None]:
        values = [episode.scalar if episode is not None else None for episode in scored]
        groups = consecutive_groups(
            [_prompt_key(prompt) for prompt in prompts], self.num_generations
        )
        filled = fill_unscored_with_group_mean(values, groups)
        dropped = sum(1 for value in values if value is None)
        if dropped:
            logger.info(
                "dropped %d of %d episodes as unscored (advantage pinned to zero)",
                dropped,
                len(values),
            )
        return filled

    def metadata_columns(self) -> dict[str, list[Any]]:
        """Per-sample metadata for the last batch, as columns."""

        columns: dict[str, list[Any]] = {}
        for episode in self.last_batch:
            record = episode.metadata() if episode is not None else {"reward_unscored": True}
            for key, value in record.items():
                columns.setdefault(key, []).append(value)
        return columns

    def _fetch(self, descriptors: Sequence[EpisodeDescriptor]) -> list[Any]:
        if self.on_endpoint_error == "raise":
            return fetch_receipts(self.source, descriptors)
        receipts: list[Any] = []
        for descriptor in descriptors:
            try:
                receipts.append(self.source.fetch(descriptor))
            except RewardEndpointError as exc:
                logger.error("episode %s dropped, no receipt: %s", descriptor.episode_id, exc)
                receipts.append(None)
        return receipts

    async def _afetch(self, descriptors: Sequence[EpisodeDescriptor]) -> list[Any]:
        if self.on_endpoint_error == "raise":
            return await afetch_receipts(self.source, descriptors)
        receipts: list[Any] = []
        for descriptor in descriptors:
            try:
                receipts.append(await self.source.afetch(descriptor))
            except RewardEndpointError as exc:
                logger.error("episode %s dropped, no receipt: %s", descriptor.episode_id, exc)
                receipts.append(None)
        return receipts

    def __call__(
        self, prompts: Sequence[Any], completions: Sequence[Any], **kwargs: Any
    ) -> list[float | None]:
        descriptors = self.descriptors(completions, kwargs)
        receipts = self._fetch(descriptors)
        self.last_batch = self._assess(receipts, descriptors)
        return self._rewards(prompts, self.last_batch)

    async def acall(
        self, prompts: Sequence[Any], completions: Sequence[Any], **kwargs: Any
    ) -> list[float | None]:
        descriptors = self.descriptors(completions, kwargs)
        receipts = await self._afetch(descriptors)
        self.last_batch = self._assess(receipts, descriptors)
        return self._rewards(prompts, self.last_batch)

    def as_async(self) -> Callable[..., Any]:
        """Return an ``async def`` TRL can await concurrently with other rewards."""

        async def openadapt_certified_reward(
            prompts: Sequence[Any], completions: Sequence[Any], **kwargs: Any
        ) -> list[float | None]:
            return await self.acall(prompts, completions, **kwargs)

        openadapt_certified_reward.__name__ = REWARD_FUNC_NAME
        return openadapt_certified_reward


def _prompt_key(prompt: Any) -> Any:
    """A hashable key for a TRL prompt (a string or a list of chat messages)."""

    if isinstance(prompt, str):
        return prompt
    try:
        return repr(prompt)
    except Exception:  # a prompt whose repr fails still needs a key
        return id(prompt)
