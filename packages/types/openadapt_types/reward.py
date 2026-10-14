"""Versioned reward contracts for training against verified terminal effects.

A reward receipt reuses the evidence and signature mechanisms of the Execute
contracts.  It states one thing: OpenAdapt verified the terminal effect of one
episode against one reward contract.  It does not state that Flow governed the
policy's actions.  It is not an Execute Seal.

An arbitrary model rollout never receives ``ExecuteEvidenceReceiptV1``.  A
production Flow result requires a qualified deterministic program with zero
model use.  The reward receipt therefore carries its own schema id, its own
outcome enum, and no ``execution_id``, ``workflow_digest``,
``qualification_id``, or ``contracts`` field, so the two receipts cannot be
exchanged for one another.

The certificate fields follow the certified-reward RL preregistration: a
distribution-free bound P(false-accept) <= epsilon at confidence 1 - delta,
calibrated on a corpus that is referenced by digest only, with an expiry
denominated in policy updates.  The corpus contents, tuned adversary
parameters, and deployment thresholds stay private.

What this version does NOT do, stated here so no reader infers it:

* There is no issuer key registry, so nothing here can decide whether an
  ``issuer_key_id`` names a key anyone trusts.  ``signature`` is checked for
  its encoding and length only.  A consumer that wants issuer identity must
  verify the signature itself, against a key it already holds.
* There is no revocation list and no revocation check.  The only way to
  withdraw a certificate in this version is to let its policy-update expiry
  run out, or to stop distributing it.

Because neither exists, the contracts refuse the claims that would depend on
them.  ``calibration_scope`` accepts ``synthetic`` and nothing else, and
``issuer`` accepts ``self_signed`` and nothing else.  A production-scope
certificate is unrepresentable in this version, not merely unissued.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from enum import Enum
from typing import Any, Literal, NamedTuple

from pydantic import (
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from openadapt_types.execute import OracleTierV1
from openadapt_types.oracle import (
    PRODUCTION_SEAL_MINIMUM_TIER,
    OracleChannel,
    OracleTier,
    tier_of,
)
from openadapt_types.process_capability import (
    _digest_payload,
    _parse_timestamp,
    _StrictContract,
    _validate_signature,
)

REWARD_CONTRACT_SCHEMA: Literal["openadapt.reward-contract/v1"] = (
    "openadapt.reward-contract/v1"
)
REWARD_CERTIFICATE_SCHEMA: Literal["openadapt.reward-certificate/v1"] = (
    "openadapt.reward-certificate/v1"
)
REWARD_EVIDENCE_RECEIPT_SCHEMA: Literal["openadapt.reward-evidence-receipt/v1"] = (
    "openadapt.reward-evidence-receipt/v1"
)

# A certified reward needs the same oracle floor as a production Seal.  The
# floor is shared.  The receipt is not.
REWARD_CERTIFIED_MINIMUM_TIER = PRODUCTION_SEAL_MINIMUM_TIER

_OPAQUE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$"
_NAME_PATTERN = r"^[A-Za-z][A-Za-z0-9_-]{0,127}$"
_SHA256_PATTERN = r"^sha256:[a-f0-9]{64}$"
_TIMESTAMP_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(Z|[+-]\d{2}:\d{2})$"
_MAX_REWARD_MAGNITUDE = 1_000_000.0
_MAX_COMPONENTS = 64
_MAX_POLICY_UPDATES = 9_007_199_254_740_991


class RewardOutcomeV1(str, Enum):
    """Terminal outcome of one episode as the reward oracle saw it.

    Each member has exactly one scoring class, listed in
    ``REWARD_SCORING_CLASS``.  The class decides whether a scalar exists.
    """

    VERIFIED = "verified"
    """The independent oracle read the required terminal effect.  Admitted
    positive reward."""

    HALTED_BEFORE_EFFECT = "halted_before_effect"
    """The runtime stopped the episode before any consequential effect.
    Zero or the declared penalty."""

    REFUSED = "refused"
    """The policy declined the task before any effect.  Zero or the declared
    penalty.  Kept apart from a runtime halt so a trainer can weigh them
    differently."""

    REJECTED_POLICY = "rejected_policy"
    """An admission or policy contract refused the episode.  Zero or the
    declared penalty."""

    WRONG_EFFECT = "wrong_effect"
    """The oracle read a terminal effect that differs from the required one,
    or a forbidden effect.  Zero or the declared penalty.  This is the silent
    wrong action the certificate bounds."""

    RECONCILIATION_REQUIRED = "reconciliation_required"
    """Delivery or effect is uncertain.  UNSCORED.  Never 0.0."""

    FAILED_PLATFORM = "failed_platform"
    """The runner or oracle failed for a reason unrelated to the policy.
    UNSCORED.  Never 0.0."""


class RewardScoringClassV1(str, Enum):
    ADMITTED_POSITIVE = "admitted_positive"
    ZERO_OR_PENALTY = "zero_or_penalty"
    UNSCORED = "unscored"


REWARD_SCORING_CLASS: Mapping[RewardOutcomeV1, RewardScoringClassV1] = {
    RewardOutcomeV1.VERIFIED: RewardScoringClassV1.ADMITTED_POSITIVE,
    RewardOutcomeV1.HALTED_BEFORE_EFFECT: RewardScoringClassV1.ZERO_OR_PENALTY,
    RewardOutcomeV1.REFUSED: RewardScoringClassV1.ZERO_OR_PENALTY,
    RewardOutcomeV1.REJECTED_POLICY: RewardScoringClassV1.ZERO_OR_PENALTY,
    RewardOutcomeV1.WRONG_EFFECT: RewardScoringClassV1.ZERO_OR_PENALTY,
    RewardOutcomeV1.RECONCILIATION_REQUIRED: RewardScoringClassV1.UNSCORED,
    RewardOutcomeV1.FAILED_PLATFORM: RewardScoringClassV1.UNSCORED,
}

UNSCORED_REWARD_OUTCOMES = frozenset(
    outcome
    for outcome, scoring_class in REWARD_SCORING_CLASS.items()
    if scoring_class is RewardScoringClassV1.UNSCORED
)


class RewardCertificateStateV1(str, Enum):
    ABSENT = "absent"
    NOT_YET_VALID = "not_yet_valid"
    CURRENT = "current"
    EXPIRED = "expired"


class RewardUncertaintyStateV1(str, Enum):
    NONE = "none"
    DELIVERY_UNCERTAIN = "delivery_uncertain"
    EFFECT_UNCERTAIN = "effect_uncertain"
    ORACLE_UNAVAILABLE = "oracle_unavailable"


class RewardCalibrationScopeV1(str, Enum):
    """What corpus the certificate was calibrated against.

    ``synthetic`` is the only member.  A production scope would assert that
    the bound came from the Phase-1 calibration on a real corpus, and nothing
    in this package can check that assertion, so the value does not exist
    here.  Widening the enum later is backward compatible; a consumer must
    show the scope beside the word certified either way.
    """

    SYNTHETIC = "synthetic"


class RewardCertificateIssuerV1(str, Enum):
    """Who signed the certificate.

    ``self_signed`` is the only member: a certificate the holder computed for
    itself.  An organization issuer would assert an identity, and there is no
    issuer key registry to resolve ``issuer_key_id`` against, so that value
    does not exist here.  The enum keeps its shape so a registry can add a
    member without changing the field.
    """

    SELF_SIGNED = "self_signed"


class RewardCertificationRefused(ValueError):
    """Raised when a tier-0 or tier-1 reward receipt is marked certified."""


def refuse_development_certification(tier: OracleTier | int) -> None:
    """Raise unless ``tier`` may carry a certified reward."""

    if int(tier) < REWARD_CERTIFIED_MINIMUM_TIER:
        raise RewardCertificationRefused(
            "a certified reward requires oracle tier 2 or 3"
        )


def _finite(value: float, field_name: str) -> float:
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError(f"{field_name} must be finite")
    return value


class RewardComponentV1(_StrictContract):
    """One named term of the reward vector and its weight in the scalar."""

    name: StrictStr = Field(pattern=_NAME_PATTERN)
    weight: StrictFloat = Field(gt=0.0, le=_MAX_REWARD_MAGNITUDE, allow_inf_nan=False)


class RewardOracleV1(_StrictContract):
    """The independent oracle that reads the terminal effect.

    The channel sets the tier.  The identity keys name the record the oracle
    reads.  The oracle contract digest binds the read recipe without carrying
    it.
    """

    channel: OracleChannel
    identity_keys: tuple[StrictStr, ...] = Field(
        min_length=1,
        max_length=32,
        json_schema_extra={"uniqueItems": True},
    )
    oracle_contract_digest: StrictStr = Field(pattern=_SHA256_PATTERN)

    @field_validator("identity_keys")
    @classmethod
    def _canonical_keys(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(re.fullmatch(_NAME_PATTERN, value) is None for value in values):
            raise ValueError("identity_keys contains an invalid key")
        if len(values) != len(set(values)):
            raise ValueError("identity_keys must not contain duplicates")
        return tuple(sorted(values))

    @property
    def tier(self) -> OracleTier:
        return tier_of(self.channel)


class RewardScoringPolicyV1(_StrictContract):
    """How each scoring class becomes a scalar.

    Uncertain episodes and platform failures are fixed to ``unscored`` in
    this version.  A contract cannot declare them to be zero.
    """

    verified_reward: StrictFloat = Field(
        default=1.0,
        gt=0.0,
        le=_MAX_REWARD_MAGNITUDE,
        allow_inf_nan=False,
    )
    halted_before_effect_reward: StrictFloat = Field(
        default=0.0,
        ge=-_MAX_REWARD_MAGNITUDE,
        le=0.0,
        allow_inf_nan=False,
    )
    refused_reward: StrictFloat = Field(
        default=0.0,
        ge=-_MAX_REWARD_MAGNITUDE,
        le=0.0,
        allow_inf_nan=False,
    )
    rejected_policy_reward: StrictFloat = Field(
        default=0.0,
        ge=-_MAX_REWARD_MAGNITUDE,
        le=0.0,
        allow_inf_nan=False,
    )
    wrong_effect_reward: StrictFloat = Field(
        default=-1.0,
        ge=-_MAX_REWARD_MAGNITUDE,
        le=0.0,
        allow_inf_nan=False,
    )
    uncertain_episodes: Literal["unscored"] = "unscored"
    platform_failures: Literal["unscored"] = "unscored"

    def scalar_for(self, outcome: RewardOutcomeV1) -> float | None:
        """Return the declared scalar, or ``None`` for an unscored outcome."""

        declared: Mapping[RewardOutcomeV1, float] = {
            RewardOutcomeV1.VERIFIED: self.verified_reward,
            RewardOutcomeV1.HALTED_BEFORE_EFFECT: self.halted_before_effect_reward,
            RewardOutcomeV1.REFUSED: self.refused_reward,
            RewardOutcomeV1.REJECTED_POLICY: self.rejected_policy_reward,
            RewardOutcomeV1.WRONG_EFFECT: self.wrong_effect_reward,
        }
        return declared.get(RewardOutcomeV1(outcome))


DEFAULT_REWARD_SCORING = RewardScoringPolicyV1()


class RewardCertificatePolicyV1(_StrictContract):
    """The certificate a reward must hold before a trainer may call it certified.

    ``epsilon`` bounds the false-accept probability.  ``delta`` is one minus
    the confidence.  ``threshold`` is the checker decision threshold the bound
    was calibrated at.  ``calibration_corpus_digest`` names the corpus without
    carrying it.  ``expiry_policy_updates`` is the number of policy updates a
    certificate stays current after issue.
    """

    epsilon: StrictFloat = Field(gt=0.0, lt=1.0, allow_inf_nan=False)
    delta: StrictFloat = Field(gt=0.0, lt=1.0, allow_inf_nan=False)
    threshold: StrictFloat = Field(allow_inf_nan=False)
    calibration_corpus_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    expiry_policy_updates: StrictInt = Field(ge=1, le=_MAX_POLICY_UPDATES)


class RewardContractV1(_StrictContract):
    """One immutable statement of what earns reward and who checks it."""

    schema_version: Literal["openadapt.reward-contract/v1"] = REWARD_CONTRACT_SCHEMA
    contract_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    contract_version: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    task_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    task_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    environment_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    environment_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    required_effect_contract_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    forbidden_effect_contract_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    oracle: RewardOracleV1
    components: tuple[RewardComponentV1, ...] = Field(
        min_length=1,
        max_length=_MAX_COMPONENTS,
        json_schema_extra={"uniqueItems": True},
    )
    scoring: RewardScoringPolicyV1 = Field(default_factory=RewardScoringPolicyV1)
    certificate_policy: RewardCertificatePolicyV1

    @field_validator("components")
    @classmethod
    def _canonical_components(
        cls, values: tuple[RewardComponentV1, ...]
    ) -> tuple[RewardComponentV1, ...]:
        names = tuple(item.name for item in values)
        if len(names) != len(set(names)):
            raise ValueError("reward component names must be unique")
        return tuple(sorted(values, key=lambda item: item.name))

    @property
    def component_names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.components)

    @property
    def digest(self) -> str:
        return _digest_payload(self.model_dump(mode="json"))


class RewardCertificateV1(_StrictContract):
    """An expiring bound on one reward contract's false-accept rate.

    Expiry counts policy updates, not wall-clock time.  A certificate issued
    at update ``i`` with expiry ``n`` is current for updates ``i`` through
    ``i + n - 1``.  Expiry is the only withdrawal mechanism in this version.
    There is no revocation list and nothing here checks one.

    ``signature`` is validated for base64 encoding and ed25519 length only.
    This package holds no issuer key registry, so it cannot say whether
    ``issuer_key_id`` names a key anyone trusts.  A consumer that needs issuer
    identity verifies the signature itself against a key it already holds.
    Until such a registry exists, ``issuer`` and ``calibration_scope`` each
    carry exactly one admissible value.
    """

    schema_version: Literal["openadapt.reward-certificate/v1"] = (
        REWARD_CERTIFICATE_SCHEMA
    )
    certificate_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    reward_contract_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    checker_configuration_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    epsilon: StrictFloat = Field(gt=0.0, lt=1.0, allow_inf_nan=False)
    delta: StrictFloat = Field(gt=0.0, lt=1.0, allow_inf_nan=False)
    threshold: StrictFloat = Field(allow_inf_nan=False)
    calibration_corpus_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    calibration_scope: RewardCalibrationScopeV1
    issued_at_policy_update: StrictInt = Field(ge=0, le=_MAX_POLICY_UPDATES)
    expiry_policy_updates: StrictInt = Field(ge=1, le=_MAX_POLICY_UPDATES)
    issued_at: StrictStr = Field(pattern=_TIMESTAMP_PATTERN)
    issuer: RewardCertificateIssuerV1
    issuer_key_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    signature_algorithm: Literal["ed25519"] = "ed25519"
    signature: StrictStr = Field(min_length=88, max_length=88)

    @field_validator("signature")
    @classmethod
    def _signature(cls, value: str) -> str:
        return _validate_signature(value, 64, "reward certificate signature")

    @model_validator(mode="after")
    def _issue_window(self) -> RewardCertificateV1:
        _parse_timestamp(self.issued_at, "issued_at")
        if self.issued_at_policy_update + self.expiry_policy_updates > _MAX_POLICY_UPDATES:
            raise ValueError("reward certificate expiry overflows the update counter")
        return self

    @property
    def expires_at_policy_update(self) -> int:
        """The first policy update at which this certificate is expired."""

        return self.issued_at_policy_update + self.expiry_policy_updates

    def state_at(self, policy_update: int) -> RewardCertificateStateV1:
        if policy_update < self.issued_at_policy_update:
            return RewardCertificateStateV1.NOT_YET_VALID
        if policy_update >= self.expires_at_policy_update:
            return RewardCertificateStateV1.EXPIRED
        return RewardCertificateStateV1.CURRENT

    def is_current(self, policy_update: int) -> bool:
        """True when ``policy_update`` falls inside the issue window."""

        return self.state_at(policy_update) is RewardCertificateStateV1.CURRENT

    def unmet(self, policy: RewardCertificatePolicyV1) -> tuple[str, ...]:
        """Every way this certificate falls short of ``policy``, in order.

        An empty tuple means the certificate is at least as strong as the
        contract asks.  ``score`` reports these strings so a caller learns
        why a reward was not certified instead of reading a bare ``False``.
        """

        reasons: list[str] = []
        if self.epsilon > policy.epsilon:
            reasons.append(
                f"certificate epsilon {self.epsilon} exceeds the contract's "
                f"{policy.epsilon}"
            )
        if self.delta > policy.delta:
            reasons.append(
                f"certificate delta {self.delta} exceeds the contract's {policy.delta}"
            )
        if self.threshold != policy.threshold:
            reasons.append(
                f"certificate threshold {self.threshold} is not the contract's "
                f"{policy.threshold}"
            )
        if self.calibration_corpus_digest != policy.calibration_corpus_digest:
            reasons.append(
                "certificate names a calibration corpus the contract does not"
            )
        if self.expiry_policy_updates > policy.expiry_policy_updates:
            reasons.append(
                f"certificate expiry {self.expiry_policy_updates} policy updates "
                f"exceeds the contract's {policy.expiry_policy_updates}"
            )
        return tuple(reasons)

    def satisfies(self, policy: RewardCertificatePolicyV1) -> bool:
        """True when this certificate is at least as strong as the policy asks."""

        return not self.unmet(policy)

    def unsigned_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode="json",
            exclude={"signature", "signature_algorithm"},
        )

    @property
    def digest(self) -> str:
        return _digest_payload(self.model_dump(mode="json"))


def certificate_state(
    certificate: RewardCertificateV1 | None, policy_update: int
) -> RewardCertificateStateV1:
    """Classify a certificate reference at one policy update."""

    if certificate is None:
        return RewardCertificateStateV1.ABSENT
    return certificate.state_at(policy_update)


class RewardScoreV1(NamedTuple):
    scalar: float | None
    certified: bool
    development_only: bool
    certification_refusals: tuple[str, ...] = ()


def score(
    outcome: RewardOutcomeV1,
    tier: OracleTier | int,
    certificate: RewardCertificateV1 | None,
    policy_update: int,
    *,
    contract: RewardContractV1,
) -> RewardScoreV1:
    """Score one episode.  Pure.  Never turns an unscored outcome into 0.0.

    The contract is required, and it supplies both halves of the answer: its
    ``scoring`` maps the outcome to a scalar, and its ``certificate_policy``
    is the bar the certificate has to clear.  There is no way to score an
    episode without naming the contract it was scored against, so a caller
    cannot certify a reward against a policy nobody read.

    * ``scalar`` is ``None`` for ``RECONCILIATION_REQUIRED`` and
      ``FAILED_PLATFORM``.  A trainer must drop or hold those episodes.
    * ``certified`` is true only at tier 2 or 3 with a certificate that names
      this contract by digest, is current at ``policy_update``, and satisfies
      ``contract.certificate_policy``.  Its scope is ``synthetic``, which is
      the only scope this version can represent.
    * ``development_only`` is true at tier 0 or 1.  A tier-0 reward can train
      a local experiment.  It can never be certified.
    * ``certification_refusals`` lists every reason ``certified`` is false.
      It is empty when ``certified`` is true.
    """

    if policy_update < 0:
        raise ValueError("policy_update must be non-negative")
    development_only = int(tier) < REWARD_CERTIFIED_MINIMUM_TIER
    state = certificate_state(certificate, policy_update)
    refusals: list[str] = []
    if development_only:
        refusals.append(
            f"oracle tier {int(tier)} is development only; "
            f"certification needs tier {REWARD_CERTIFIED_MINIMUM_TIER} or above"
        )
    if certificate is None:
        refusals.append("no reward certificate was presented")
    else:
        if state is not RewardCertificateStateV1.CURRENT:
            refusals.append(
                f"the certificate is {state.value} at policy update {policy_update}"
            )
        if certificate.reward_contract_digest != contract.digest:
            refusals.append("the certificate names a different reward contract")
        refusals.extend(certificate.unmet(contract.certificate_policy))
    scalar = contract.scoring.scalar_for(RewardOutcomeV1(outcome))
    return RewardScoreV1(scalar, not refusals, development_only, tuple(refusals))


class RewardEvidenceReceiptV1(_StrictContract):
    """A signed statement that one episode's terminal effect was verified.

    This receipt binds a reward contract, a policy checkpoint, an episode,
    and the oracle read.  It is not an Execute Seal.  It carries no
    ``execution_id``, ``workflow_digest``, ``qualification_id``, or Execute
    contract block, and it does not claim that Flow governed the policy.
    """

    schema_version: Literal["openadapt.reward-evidence-receipt/v1"] = (
        REWARD_EVIDENCE_RECEIPT_SCHEMA
    )
    receipt_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    reward_contract_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    policy_checkpoint_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    policy_update: StrictInt = Field(ge=0, le=_MAX_POLICY_UPDATES)
    episode_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    oracle_tier: OracleTierV1
    reward_outcome: RewardOutcomeV1
    evidence_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    reward_components: dict[StrictStr, StrictFloat] = Field(
        default_factory=dict,
        max_length=_MAX_COMPONENTS,
    )
    scalar_reward: StrictFloat | None = Field(
        default=None,
        ge=-_MAX_REWARD_MAGNITUDE,
        le=_MAX_REWARD_MAGNITUDE,
        allow_inf_nan=False,
    )
    certificate_id: StrictStr | None = Field(default=None, pattern=_OPAQUE_ID_PATTERN)
    certificate_digest: StrictStr | None = Field(default=None, pattern=_SHA256_PATTERN)
    certificate_state: RewardCertificateStateV1
    calibration_corpus_digest: StrictStr | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    calibration_scope: RewardCalibrationScopeV1 | None = None
    uncertainty: RewardUncertaintyStateV1
    certified: StrictBool
    development_only: StrictBool
    issuer_key_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    nonce: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    issued_at: StrictStr = Field(pattern=_TIMESTAMP_PATTERN)
    signature_algorithm: Literal["ed25519"] = "ed25519"
    signature: StrictStr = Field(min_length=88, max_length=88)

    @field_validator("reward_components")
    @classmethod
    def _canonical_components(cls, values: dict[str, float]) -> dict[str, float]:
        for name, value in values.items():
            if re.fullmatch(_NAME_PATTERN, name) is None:
                raise ValueError("reward_components contains an invalid name")
            _finite(value, f"reward_components[{name}]")
            if abs(value) > _MAX_REWARD_MAGNITUDE:
                raise ValueError(f"reward_components[{name}] is out of range")
        return dict(sorted(values.items()))

    @field_validator("signature")
    @classmethod
    def _signature(cls, value: str) -> str:
        return _validate_signature(value, 64, "reward receipt signature")

    @model_validator(mode="after")
    def _scoring_contract(self) -> RewardEvidenceReceiptV1:
        _parse_timestamp(self.issued_at, "issued_at")
        expected_development = self.oracle_tier < REWARD_CERTIFIED_MINIMUM_TIER
        if self.development_only != expected_development:
            raise ValueError("development_only must be true exactly at oracle tier 0 or 1")
        if self.certified:
            refuse_development_certification(self.oracle_tier)
            if self.certificate_state is not RewardCertificateStateV1.CURRENT:
                raise ValueError("a certified reward requires a current certificate")
            if self.calibration_corpus_digest is None:
                raise ValueError("a certified reward requires a calibration corpus digest")
            if self.calibration_scope is None:
                raise ValueError("a certified reward requires a stated calibration scope")
        has_reference = (
            self.certificate_id is not None
            or self.certificate_digest is not None
            or self.calibration_corpus_digest is not None
            or self.calibration_scope is not None
        )
        if self.certificate_state is RewardCertificateStateV1.ABSENT:
            if has_reference:
                raise ValueError("an absent certificate cannot carry a reference")
        elif (
            self.certificate_id is None
            or self.certificate_digest is None
            or self.calibration_corpus_digest is None
            or self.calibration_scope is None
        ):
            raise ValueError(
                "a referenced certificate requires id, digest, corpus digest, and scope"
            )

        scoring_class = REWARD_SCORING_CLASS[self.reward_outcome]
        if scoring_class is RewardScoringClassV1.UNSCORED:
            if self.scalar_reward is not None:
                raise ValueError(
                    f"{self.reward_outcome.value} is unscored and cannot carry a scalar"
                )
            if self.reward_components:
                raise ValueError(
                    f"{self.reward_outcome.value} is unscored and cannot carry components"
                )
        else:
            if self.scalar_reward is None:
                raise ValueError(f"{self.reward_outcome.value} requires a scalar reward")
            if not self.reward_components:
                raise ValueError(
                    f"{self.reward_outcome.value} requires at least one reward component"
                )
            if scoring_class is RewardScoringClassV1.ADMITTED_POSITIVE:
                if self.scalar_reward <= 0.0:
                    raise ValueError("a verified reward must be positive")
            elif self.scalar_reward > 0.0:
                raise ValueError(
                    f"{self.reward_outcome.value} yields zero or a declared penalty"
                )

        if self.reward_outcome is RewardOutcomeV1.VERIFIED:
            if self.uncertainty is not RewardUncertaintyStateV1.NONE:
                raise ValueError("a verified reward cannot carry uncertainty")
        if self.reward_outcome is RewardOutcomeV1.RECONCILIATION_REQUIRED:
            if self.uncertainty is RewardUncertaintyStateV1.NONE:
                raise ValueError("reconciliation_required requires an uncertainty state")
        return self

    @property
    def scoring_class(self) -> RewardScoringClassV1:
        return REWARD_SCORING_CLASS[self.reward_outcome]

    def certification_refusals(
        self,
        contract: RewardContractV1,
        certificate: RewardCertificateV1 | None,
    ) -> tuple[str, ...]:
        """Recheck this receipt's ``certified`` flag against its own sources.

        The receipt carries digests, not the contract and certificate
        themselves, so it cannot check its own ``certified`` flag while it is
        being validated.  A reader who holds both re-runs the decision here
        and gets the same refusal strings ``score`` returns, plus any
        disagreement between the receipt and the pair it was handed.  An
        empty tuple means the flag is supported by what the reader has.
        """

        refusals: list[str] = []
        if self.reward_contract_digest != contract.digest:
            refusals.append("the receipt names a different reward contract")
        if certificate is None:
            if self.certificate_id is not None:
                refusals.append("the receipt references a certificate that was not given")
        elif self.certificate_digest != certificate.digest:
            refusals.append("the receipt references a different certificate")
        scored = score(
            self.reward_outcome,
            self.oracle_tier,
            certificate,
            self.policy_update,
            contract=contract,
        )
        refusals.extend(scored.certification_refusals)
        return tuple(dict.fromkeys(refusals))

    def unsigned_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode="json",
            exclude={"signature", "signature_algorithm"},
        )

    @property
    def digest(self) -> str:
        return _digest_payload(self.model_dump(mode="json"))
