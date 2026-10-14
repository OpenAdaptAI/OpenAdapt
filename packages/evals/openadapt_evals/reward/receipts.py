"""Receipt consumption shared by the TRL and verl adapters.

Everything here is trainer-agnostic: how an episode is described to the
reward endpoint, how a receipt is fetched, how a receipt becomes a scored
episode, and how unscored episodes are removed from a GRPO group.

Contract source: ``openadapt_types.reward``. This module never re-derives the
scoring rules; it calls ``score()`` and reads the receipt's own fields, then
refuses the combinations a trainer must never accept.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable, Hashable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol, runtime_checkable

from openadapt_types.reward import (
    REWARD_CERTIFIED_MINIMUM_TIER,
    RewardCertificatePolicyV1,
    RewardCertificateStateV1,
    RewardCertificateV1,
    RewardCertificationRefused,
    RewardEvidenceReceiptV1,
    RewardOutcomeV1,
    RewardScoringClassV1,
    score,
)

logger = logging.getLogger(__name__)

REWARDS_ROUTE = "/v1/rewards"


class RewardEndpointError(RuntimeError):
    """The reward endpoint returned no usable receipt."""


class UncertifiedRewardError(RuntimeError):
    """A scored receipt is not certified and the trainer requires certification.

    Raised in ``require_certified`` mode. The preregistration says an expired,
    un-renewed certificate halts the arm; this is that halt.
    """


class ReceiptMismatchError(ValueError):
    """The receipt does not bind the contract, certificate, or episode expected."""


class OracleIdentityError(ValueError):
    """An episode names no oracle identity, or names one the worker cannot use.

    The reward worker needs the identity keys the contract declares
    (``oracle.identity_keys``) to know which record to read. Without them it
    answers HTTP 422 ``identity_missing`` and the batch stops mid-flight. The
    adapters raise this before the first HTTP call instead.
    """


ORACLE_IDENTITY_KEY = "oracle_identity"
RUNTIME_SIGNAL_KEY = "runtime_signal"


def clean_oracle_identity(raw: Any, *, where: str) -> dict[str, str]:
    """Coerce one identity mapping into the ``{str: str}`` shape the worker takes.

    ``where`` names the column (and episode) for the error message. Keys and
    values are stringified, the way flow's ``episode_from_columns`` does;
    an empty key or value is refused because the worker refuses it too.
    """

    if raw is None:
        raise OracleIdentityError(f"{where} carries no oracle identity")
    if not isinstance(raw, Mapping):
        raise OracleIdentityError(
            f"{where} must be a mapping of identity keys to values, got {type(raw).__name__}"
        )
    if not raw:
        raise OracleIdentityError(f"{where} is an empty oracle identity")
    identity: dict[str, str] = {}
    for key, value in raw.items():
        text_key = str(key)
        text_value = "" if value is None else str(value)
        if not text_key or not text_value:
            raise OracleIdentityError(
                f"{where}: oracle identity key {text_key!r} has an empty key or value"
            )
        identity[text_key] = text_value
    return dict(sorted(identity.items()))


@dataclass(frozen=True)
class EpisodeDescriptor:
    """What the trainer tells the reward endpoint about one episode.

    ``episode_id`` names the rollout the oracle must read. The other fields
    bind the receipt to a contract and a policy checkpoint so the trainer can
    refuse a receipt that answers a different question.

    ``oracle_identity`` is the record the oracle reads, keyed exactly as the
    contract's ``oracle.identity_keys``. On the wire it travels as
    ``metadata.oracle_identity``, the place the flow worker reads it from.
    ``runtime_signal`` travels as ``metadata.runtime_signal`` and is what the
    episode runtime said about its own end; the worker defaults it to
    ``completed`` when absent.
    """

    episode_id: str
    policy_checkpoint_id: str
    policy_update: int
    reward_contract_digest: str
    task_id: str | None = None
    environment_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    oracle_identity: Mapping[str, str] | None = None
    runtime_signal: str | None = None

    def as_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        metadata = dict(self.metadata)
        identity = payload.pop("oracle_identity")
        if identity is not None:
            metadata[ORACLE_IDENTITY_KEY] = dict(identity)
        signal = payload.pop("runtime_signal")
        if signal is not None:
            metadata[RUNTIME_SIGNAL_KEY] = signal
        payload["metadata"] = metadata
        return {key: value for key, value in payload.items() if value is not None}


@dataclass(frozen=True)
class ScoredEpisode:
    """One receipt after the trainer-side checks.

    ``scalar`` is ``None`` exactly when the episode is unscored. ``certified``
    is recomputed here; the receipt's own flag is an input, not the answer.
    """

    episode_id: str
    receipt_id: str
    outcome: RewardOutcomeV1
    scalar: float | None
    certified: bool
    development_only: bool
    certificate_state: RewardCertificateStateV1
    oracle_tier: int
    calibration_scope: str | None = None
    certificate_id: str | None = None

    @property
    def unscored(self) -> bool:
        return self.scalar is None

    def metadata(self) -> dict[str, Any]:
        """Per-sample fields a trainer can log beside the scalar."""

        return {
            "reward_episode_id": self.episode_id,
            "reward_receipt_id": self.receipt_id,
            "reward_outcome": self.outcome.value,
            "reward_unscored": self.unscored,
            "reward_certified": self.certified,
            "reward_development_only": self.development_only,
            "reward_certificate_state": self.certificate_state.value,
            "reward_certificate_id": self.certificate_id,
            "reward_calibration_scope": self.calibration_scope,
            "reward_oracle_tier": self.oracle_tier,
        }


CALIBRATION_SCOPES: frozenset[str] = frozenset({"synthetic", "production"})


def calibration_scope_of(obj: Any) -> str | None:
    """Read ``calibration_scope`` from a receipt or certificate.

    The field says what corpus the bound was calibrated on. ``synthetic`` is
    the only scope any certificate carries today (MockMed / ExtraDup);
    ``production`` needs the Phase-1 calibration, which is not published.
    ``None`` means the object predates the field or omits it; nothing
    without a scope is labelled certified here.
    """

    value = getattr(obj, "calibration_scope", None)
    if value is None:
        return None
    value = getattr(value, "value", value)
    if not isinstance(value, str) or value not in CALIBRATION_SCOPES:
        raise ReceiptMismatchError(f"unknown calibration_scope {value!r}")
    return value


@runtime_checkable
class RewardSource(Protocol):
    """Anything that turns an episode descriptor into a receipt."""

    def fetch(self, descriptor: EpisodeDescriptor) -> RewardEvidenceReceiptV1: ...

    async def afetch(self, descriptor: EpisodeDescriptor) -> RewardEvidenceReceiptV1: ...


def parse_receipt(payload: Any) -> RewardEvidenceReceiptV1:
    """Validate an endpoint response into a receipt.

    Accepts a receipt object, a receipt dict, or ``{"receipt": {...}}``. A
    payload that fails the contract raises ``RewardEndpointError``; the
    adapters never fall back to a guessed scalar.
    """

    if isinstance(payload, RewardEvidenceReceiptV1):
        return payload
    if isinstance(payload, Mapping) and "receipt" in payload and "schema_version" not in payload:
        payload = payload["receipt"]
    try:
        return RewardEvidenceReceiptV1.model_validate(payload)
    except Exception as exc:  # pydantic.ValidationError or a wrong type
        raise RewardEndpointError(f"reward endpoint returned an invalid receipt: {exc}") from exc


class CallableRewardSource:
    """Wrap a local callable (sync or async) as a reward source.

    The callable receives an ``EpisodeDescriptor`` and returns anything
    ``parse_receipt`` accepts. Tests and the proof harness use this; a
    trainer on a box with the flow reward worker in-process can too.
    """

    def __init__(
        self,
        fn: Callable[[EpisodeDescriptor], Any] | Callable[[EpisodeDescriptor], Awaitable[Any]],
    ) -> None:
        self._fn = fn

    def fetch(self, descriptor: EpisodeDescriptor) -> RewardEvidenceReceiptV1:
        result = self._fn(descriptor)
        if asyncio.iscoroutine(result):
            result = asyncio.run(result)
        return parse_receipt(result)

    async def afetch(self, descriptor: EpisodeDescriptor) -> RewardEvidenceReceiptV1:
        result = self._fn(descriptor)
        if asyncio.iscoroutine(result):
            result = await result
        return parse_receipt(result)


class HttpRewardEndpoint:
    """``POST {base_url}/v1/rewards`` with an episode descriptor.

    The flow reward worker owns the route. This client sends the descriptor
    as JSON (``EpisodeDescriptor.as_payload``, identity and signal under
    ``metadata``) and expects a receipt (or ``{"receipt": ...}``) back with
    status 200. Any other status, a transport error, or an invalid body
    raises ``RewardEndpointError``. Nothing here invents a scalar.
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout_seconds: float = 60.0,
        headers: Mapping[str, str] | None = None,
        max_concurrency: int = 8,
        transport: Any = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.headers = dict(headers or {})
        # The flow worker requires ``Authorization: Bearer <token>`` on the
        # rewards route; ``token`` is the short way to set that header.
        if token is not None:
            self.headers["Authorization"] = f"Bearer {token}"
        self.max_concurrency = max(1, int(max_concurrency))
        # An httpx transport, for tests (``httpx.MockTransport``).
        self.transport = transport

    @property
    def url(self) -> str:
        return f"{self.base_url}{REWARDS_ROUTE}"

    def fetch(self, descriptor: EpisodeDescriptor) -> RewardEvidenceReceiptV1:
        import httpx

        try:
            with httpx.Client(timeout=self.timeout_seconds, transport=self.transport) as client:
                response = client.post(self.url, json=descriptor.as_payload(), headers=self.headers)
        except httpx.HTTPError as exc:
            raise RewardEndpointError(f"reward endpoint unreachable: {exc}") from exc
        return self._receipt_from_response(response.status_code, response)

    async def afetch(self, descriptor: EpisodeDescriptor) -> RewardEvidenceReceiptV1:
        import httpx

        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds, transport=self.transport
            ) as client:
                response = await client.post(
                    self.url, json=descriptor.as_payload(), headers=self.headers
                )
        except httpx.HTTPError as exc:
            raise RewardEndpointError(f"reward endpoint unreachable: {exc}") from exc
        return self._receipt_from_response(response.status_code, response)

    async def afetch_many(
        self, descriptors: Sequence[EpisodeDescriptor]
    ) -> list[RewardEvidenceReceiptV1]:
        semaphore = asyncio.Semaphore(self.max_concurrency)

        async def one(descriptor: EpisodeDescriptor) -> RewardEvidenceReceiptV1:
            async with semaphore:
                return await self.afetch(descriptor)

        return list(await asyncio.gather(*(one(item) for item in descriptors)))

    @staticmethod
    def _receipt_from_response(status_code: int, response: Any) -> RewardEvidenceReceiptV1:
        if status_code != 200:
            raise RewardEndpointError(
                f"reward endpoint returned HTTP {status_code}: {response.text[:200]!r}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise RewardEndpointError("reward endpoint returned non-JSON") from exc
        return parse_receipt(body)


def fetch_receipts(
    source: RewardSource, descriptors: Sequence[EpisodeDescriptor]
) -> list[RewardEvidenceReceiptV1]:
    """Fetch one receipt per descriptor, in order, synchronously."""

    return [source.fetch(descriptor) for descriptor in descriptors]


async def afetch_receipts(
    source: RewardSource, descriptors: Sequence[EpisodeDescriptor]
) -> list[RewardEvidenceReceiptV1]:
    """Fetch receipts concurrently when the source supports it."""

    many = getattr(source, "afetch_many", None)
    if callable(many):
        return list(await many(descriptors))
    return list(await asyncio.gather(*(source.afetch(item) for item in descriptors)))


def assess_receipt(
    receipt: RewardEvidenceReceiptV1,
    *,
    policy_update: int,
    expected_contract_digest: str | None = None,
    expected_episode_id: str | None = None,
    certificate: RewardCertificateV1 | None = None,
    certificate_policy: RewardCertificatePolicyV1 | None = None,
) -> ScoredEpisode:
    """Turn one receipt into a scored episode, applying the trainer-side rules.

    * The receipt must bind the expected contract digest and episode id.
    * When the trainer holds both the certificate and the contract's
      ``certificate_policy``, the certificate has to clear that policy. A
      certificate with a looser epsilon, delta, threshold, corpus, or expiry
      than the contract asked for turns ``certified`` off here, and says so
      at WARNING. Omitting the policy leaves the receipt's own flag standing,
      which is what a trainer that was handed only a contract digest can do.
    * ``development_only`` (tier 0 or 1) can never be certified. The receipt
      contract already forbids that combination; this re-checks it so a
      receipt built outside the contract cannot slip through.
    * If the trainer holds the certificate, expiry is re-evaluated at the
      trainer's current ``policy_update``, not the update stamped on the
      receipt. A certificate that has expired since the receipt was issued
      turns ``certified`` off here.
    * An expired or absent certificate is logged at WARNING with the ids a
      person needs to renew it.
    * The scalar is the receipt's scalar. ``None`` stays ``None``.
    """

    if (
        expected_contract_digest is not None
        and receipt.reward_contract_digest != expected_contract_digest
    ):
        raise ReceiptMismatchError(
            f"receipt {receipt.receipt_id} binds contract {receipt.reward_contract_digest}, "
            f"expected {expected_contract_digest}"
        )
    if expected_episode_id is not None and receipt.episode_id != expected_episode_id:
        raise ReceiptMismatchError(
            f"receipt {receipt.receipt_id} is for episode {receipt.episode_id}, "
            f"expected {expected_episode_id}"
        )

    tier = int(receipt.oracle_tier)
    development_only = tier < REWARD_CERTIFIED_MINIMUM_TIER
    if receipt.certified and development_only:
        raise RewardCertificationRefused(
            f"receipt {receipt.receipt_id} claims certification at oracle tier {tier}"
        )

    scope = calibration_scope_of(receipt)
    if certificate is not None:
        if (
            receipt.certificate_digest is not None
            and receipt.certificate_digest != certificate.digest
        ):
            raise ReceiptMismatchError(
                f"receipt {receipt.receipt_id} references certificate "
                f"{receipt.certificate_digest}, trainer holds {certificate.digest}"
            )
        certificate_scope = calibration_scope_of(certificate)
        if scope is None:
            scope = certificate_scope
        elif certificate_scope is not None and certificate_scope != scope:
            raise ReceiptMismatchError(
                f"receipt {receipt.receipt_id} scope {scope} differs from certificate "
                f"scope {certificate_scope}"
            )
        verdict = score(receipt.reward_outcome, tier, certificate, policy_update)
        state = certificate.state_at(policy_update)
        certified = bool(receipt.certified and verdict.certified)
        if certificate_policy is not None and not certificate.satisfies(
            certificate_policy
        ):
            logger.warning(
                "reward certificate %s is weaker than the contract policy "
                "(epsilon=%s delta=%s threshold=%s expiry=%d against epsilon=%s "
                "delta=%s threshold=%s expiry=%d); receipt %s is not certified",
                receipt.certificate_id,
                certificate.epsilon,
                certificate.delta,
                certificate.threshold,
                certificate.expiry_policy_updates,
                certificate_policy.epsilon,
                certificate_policy.delta,
                certificate_policy.threshold,
                certificate_policy.expiry_policy_updates,
                receipt.receipt_id,
            )
            certified = False
    else:
        state = receipt.certificate_state
        certified = bool(
            receipt.certified and not development_only and state is RewardCertificateStateV1.CURRENT
        )

    if certified and scope is None:
        logger.warning(
            "receipt %s carries no calibration_scope; it is not labelled certified",
            receipt.receipt_id,
        )
        certified = False
    if certified:
        logger.info(
            "certified reward: receipt %s certificate %s scope=%s tier=%d",
            receipt.receipt_id,
            receipt.certificate_id,
            scope,
            tier,
        )

    if state is RewardCertificateStateV1.EXPIRED:
        expires_at = certificate.expires_at_policy_update if certificate is not None else None
        logger.warning(
            "reward certificate %s expired: policy_update=%d expires_at=%s receipt=%s episode=%s",
            receipt.certificate_id,
            policy_update,
            expires_at,
            receipt.receipt_id,
            receipt.episode_id,
        )
    elif state is RewardCertificateStateV1.NOT_YET_VALID:
        logger.warning(
            "reward certificate %s is not yet valid at policy_update=%d (receipt %s)",
            receipt.certificate_id,
            policy_update,
            receipt.receipt_id,
        )
    elif development_only:
        logger.info(
            "development_only reward: receipt %s oracle tier %d cannot be certified",
            receipt.receipt_id,
            tier,
        )

    scalar = receipt.scalar_reward
    unscored = receipt.scoring_class is RewardScoringClassV1.UNSCORED
    if unscored != (scalar is None):
        # The contract validator guarantees this; a receipt built by hand
        # around the validator must still not reach the trainer.
        raise ReceiptMismatchError(
            f"receipt {receipt.receipt_id}: outcome {receipt.reward_outcome.value} "
            f"and scalar {scalar!r} disagree"
        )
    if scalar is not None and not math.isfinite(scalar):
        raise ReceiptMismatchError(f"receipt {receipt.receipt_id} scalar is not finite")

    return ScoredEpisode(
        episode_id=receipt.episode_id,
        receipt_id=receipt.receipt_id,
        outcome=receipt.reward_outcome,
        scalar=scalar,
        certified=certified,
        development_only=development_only,
        certificate_state=state,
        oracle_tier=tier,
        calibration_scope=scope,
        certificate_id=receipt.certificate_id,
    )


def require_certified_or_unscored(episode: ScoredEpisode) -> None:
    """Raise unless the episode is certified or unscored.

    An unscored episode carries no reward and is dropped, so it needs no
    certificate. A scored, uncertified episode would train the policy on an
    uncertified signal, which is the failure this adapter exists to stop.
    """

    if episode.unscored or episode.certified:
        return
    raise UncertifiedRewardError(
        f"episode {episode.episode_id} (receipt {episode.receipt_id}) is scored but not "
        f"certified: oracle_tier={episode.oracle_tier} certificate_state="
        f"{episode.certificate_state.value} development_only={episode.development_only}"
    )


def fill_unscored_with_group_mean(
    values: Sequence[float | None],
    groups: Sequence[Hashable],
) -> list[float | None]:
    """Give each unscored sample the mean of its scored group-mates.

    GRPO computes each sample's advantage as its reward minus the group mean
    (optionally divided by the group std). A sample whose reward equals that
    mean has advantage exactly zero, contributes no policy gradient, and
    leaves the scored samples' mean unchanged. That is the closest thing to
    "drop this sample" a reward function can express when the trainer's
    contract is one scalar per completion.

    A group with no scored sample stays ``None`` throughout; the caller
    decides what its trainer does with an all-``None`` group.

    The sample still counts in the trainer's loss normalisation, and the
    group std shrinks slightly because one term is zero. Neither changes the
    sign or ranking of any scored sample's advantage.
    """

    if len(values) != len(groups):
        raise ValueError("values and groups must have the same length")
    sums: dict[Hashable, float] = {}
    counts: dict[Hashable, int] = {}
    for value, group in zip(values, groups):
        if value is None:
            continue
        sums[group] = sums.get(group, 0.0) + float(value)
        counts[group] = counts.get(group, 0) + 1
    filled: list[float | None] = []
    for value, group in zip(values, groups):
        if value is not None:
            filled.append(float(value))
        elif counts.get(group):
            filled.append(sums[group] / counts[group])
        else:
            filled.append(None)
    return filled


def consecutive_groups(keys: Iterable[Hashable], group_size: int | None) -> list[int]:
    """Assign group ids to a flat batch.

    With ``group_size`` (TRL's ``num_generations``), every consecutive block of
    that size is one group. Without it, consecutive equal keys form a group,
    which matches how TRL repeats each prompt ``num_generations`` times in a
    row.
    """

    keys = list(keys)
    if group_size is not None:
        if group_size <= 0:
            raise ValueError("group_size must be positive")
        if len(keys) % group_size:
            raise ValueError(f"batch of {len(keys)} is not a multiple of group_size {group_size}")
        return [index // group_size for index in range(len(keys))]
    ids: list[int] = []
    current = 0
    previous: Hashable | None = None
    for index, key in enumerate(keys):
        if index and key != previous:
            current += 1
        ids.append(current)
        previous = key
    return ids
