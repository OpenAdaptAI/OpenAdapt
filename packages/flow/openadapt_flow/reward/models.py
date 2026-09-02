"""Local reward-worker models: the contract bundle, the episode, the recipe.

The portable contract and receipt are ``RewardContractV1`` and
``RewardEvidenceReceiptV1`` from ``openadapt-types``. The contract names its
required effects, forbidden effects, and oracle by digest only. This module
holds the local, digest-checked bundle that carries the bytes behind those
digests, plus the episode descriptor a trainer submits.

Extra keys are forbidden everywhere. Screenshot, OCR, and parameter fields
have no place in a receipt; the same allow-list discipline as
:mod:`openadapt_flow.execute.models` applies.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal, Optional

from openadapt_types.oracle import OracleChannel, OracleTier, tier_of
from openadapt_types.process_capability import _digest_payload
from openadapt_types.reward import (
    RewardCertificateV1,
    RewardContractV1,
)
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from openadapt_flow.reward import REWARD_NOTICE
from openadapt_flow.runtime.effects.effect import Effect

CONTRACT_FILE = "contract.json"
REQUIRED_EFFECTS_FILE = "required_effects.json"
FORBIDDEN_EFFECTS_FILE = "forbidden_effects.json"
ORACLE_FILE = "oracle.json"
CERTIFICATE_FILE = "certificate.json"

_OPAQUE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$"

#: Fields a shareable reward receipt or evidence summary must never carry.
#: Mirrors the Execute allow-list; the reward path adds the trainer-side
#: names a rollout buffer tends to attach to an episode.
FORBIDDEN_RECEIPT_KEYS = frozenset(
    {
        "screenshot",
        "screenshots",
        "frames",
        "frame",
        "video",
        "trajectory",
        "observations",
        "observation",
        "actions",
        "action",
        "ocr",
        "ocr_text",
        "typed_value",
        "typed_values",
        "parameters",
        "parameter",
        "prompt",
        "prompts",
        "completion",
        "completions",
        "completion_ids",
        "url",
        "hostname",
        "coordinate",
        "coordinates",
        "application_name",
        "organization_name",
        "user_name",
        "workflow_name",
        "phi",
        "image",
        "after_png",
        "before_png",
        "note",
        "record_id",
        "records",
    }
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


RuntimeSignal = Literal[
    "completed",
    "halted_before_effect",
    "refused",
    "rejected_policy",
    "failed_platform",
]


class EpisodeDescriptorV1(_Strict):
    """What a trainer submits to have one episode's terminal effect read.

    The wire shape is the one ``openadapt_evals.reward.receipts.
    EpisodeDescriptor`` sends: ``episode_id``, ``policy_checkpoint_id``,
    ``policy_update``, ``reward_contract_digest``, and optional ``task_id``,
    ``environment_id``, ``metadata``. The digest binds the receipt to the
    contract this worker serves; a different digest is refused.

    The oracle needs to know which record to read. That identity comes from
    one of three places, checked in this order: the ``oracle_identity``
    field, ``metadata["oracle_identity"]``, or a registration made by
    ``RewardWorker.begin_episode`` before the rollout ran. Its keys must be
    exactly the contract's ``oracle.identity_keys``; an extra key is refused,
    a missing key is refused.

    ``runtime_signal`` (or ``metadata["runtime_signal"]``) is what the
    episode runtime reported about its own end. The oracle read decides; the
    signal only picks which zero-or-penalty outcome applies when the store
    agrees that nothing landed.
    """

    schema_version: Literal["openadapt.reward-episode/v1"] = (
        "openadapt.reward-episode/v1"
    )
    episode_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    policy_checkpoint_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    policy_update: StrictInt = Field(ge=0)
    reward_contract_digest: StrictStr = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    task_id: Optional[StrictStr] = Field(default=None, pattern=_OPAQUE_ID_PATTERN)
    environment_id: Optional[StrictStr] = Field(
        default=None, pattern=_OPAQUE_ID_PATTERN
    )
    metadata: dict[StrictStr, Any] = Field(default_factory=dict)
    oracle_identity: Optional[dict[StrictStr, StrictStr]] = Field(
        default=None, min_length=1, max_length=32
    )
    runtime_signal: Optional[RuntimeSignal] = None

    @field_validator("oracle_identity")
    @classmethod
    def _identity_values(
        cls, values: Optional[dict[str, str]]
    ) -> Optional[dict[str, str]]:
        if values is None:
            return None
        return _clean_identity(values)

    @field_validator("metadata")
    @classmethod
    def _metadata_keys(cls, values: dict[str, Any]) -> dict[str, Any]:
        bad = FORBIDDEN_RECEIPT_KEYS.intersection(values)
        if bad:
            raise ValueError(
                "metadata carries rollout or PHI keys: " + ", ".join(sorted(bad))
            )
        return values

    def resolved_identity(self) -> Optional[dict[str, str]]:
        """The oracle identity the descriptor itself carries, if any."""

        if self.oracle_identity is not None:
            return dict(self.oracle_identity)
        raw = self.metadata.get("oracle_identity")
        if raw is None:
            return None
        if not isinstance(raw, dict) or not raw:
            raise ValueError("metadata.oracle_identity must be a non-empty object")
        return _clean_identity({str(k): str(v) for k, v in raw.items()})

    def resolved_signal(self) -> str:
        if self.runtime_signal is not None:
            return self.runtime_signal
        raw = self.metadata.get("runtime_signal", "completed")
        if raw not in RUNTIME_SIGNALS:
            raise ValueError(f"metadata.runtime_signal {raw!r} is not a known signal")
        return str(raw)


RUNTIME_SIGNALS = frozenset(
    {
        "completed",
        "halted_before_effect",
        "refused",
        "rejected_policy",
        "failed_platform",
    }
)


def _clean_identity(values: dict[str, str]) -> dict[str, str]:
    for key, value in values.items():
        if not key or not value:
            raise ValueError("oracle_identity keys and values must be non-empty")
    return dict(sorted(values.items()))


class OracleRecipeV1(_Strict):
    """How the worker reads the system of record. Interface only.

    A recipe carries no credential. ``headers_env`` and ``token_env`` name
    environment variables the worker reads at start; the bundle on disk
    never holds the secret. A per-system-of-record recipe for a real
    deployment stays private; this public shape is the mechanism.
    """

    kind: Literal["json_file", "screen_dump", "rest", "sqlite", "fhir", "file_arrival"]
    #: ``json_file`` / ``screen_dump``: a JSON document on disk. Relative
    #: paths resolve against the bundle directory.
    path: Optional[StrictStr] = None
    #: ``json_file`` / ``rest``: key of the records list in the document
    #: (``null`` when the document is the list).
    records_key: Optional[StrictStr] = "records"
    #: ``rest`` / ``fhir``: base URL of the read endpoint.
    base_url: Optional[StrictStr] = None
    #: ``rest``: path of the records document.
    records_path: StrictStr = "/api/db"
    #: ``rest``: environment variable holding a JSON object of headers.
    headers_env: Optional[StrictStr] = None
    #: ``sqlite``: database file. ``query``: one read-only SELECT.
    query: Optional[StrictStr] = None
    #: ``fhir``: resource type and search parameters; ``token_env`` names the
    #: bearer token variable.
    resource_type: StrictStr = "Observation"
    search_params: dict[StrictStr, StrictStr] = Field(default_factory=dict)
    field_paths: Optional[dict[StrictStr, StrictStr]] = None
    token_env: Optional[StrictStr] = None
    #: ``file_arrival``: watched directory and glob.
    pattern: StrictStr = "*"
    timeout_s: float = 5.0

    @model_validator(mode="after")
    def _kind_fields(self) -> "OracleRecipeV1":
        needs_path = {"json_file", "screen_dump", "sqlite", "file_arrival"}
        if self.kind in needs_path and not self.path:
            raise ValueError(f"oracle recipe {self.kind} requires path")
        if self.kind in {"rest", "fhir"} and not self.base_url:
            raise ValueError(f"oracle recipe {self.kind} requires base_url")
        if self.kind == "sqlite" and not self.query:
            raise ValueError("oracle recipe sqlite requires query")
        return self

    @property
    def channel(self) -> OracleChannel:
        return _RECIPE_CHANNEL[self.kind]

    @property
    def tier(self) -> OracleTier:
        return tier_of(self.channel)

    @property
    def digest(self) -> str:
        return _digest_payload(self.model_dump(mode="json"))

    def resolve_path(self, base_dir: Path) -> Path:
        path = Path(self.path or "")
        return path if path.is_absolute() else base_dir / path

    def headers(self) -> Optional[dict[str, str]]:
        if not self.headers_env:
            return None
        raw = os.environ.get(self.headers_env, "")
        if not raw:
            return None
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError(f"{self.headers_env} must hold a JSON object")
        return {str(k): str(v) for k, v in parsed.items()}

    def token(self) -> Optional[str]:
        if not self.token_env:
            return None
        return os.environ.get(self.token_env) or None


#: The recipe kind sets the channel, and the channel sets the tier. A
#: payload cannot upgrade a screen dump into a system-of-record read.
_RECIPE_CHANNEL: dict[str, OracleChannel] = {
    "json_file": OracleChannel.FILE,
    "screen_dump": OracleChannel.OCR,
    "rest": OracleChannel.API,
    "sqlite": OracleChannel.DB,
    "fhir": OracleChannel.API,
    "file_arrival": OracleChannel.FILE,
}


class RewardBundle(_Strict):
    """One reward contract with the bytes behind its digests, checked.

    Loading refuses when any digest in the contract disagrees with the
    file it names, when the oracle recipe's channel disagrees with the
    contract's oracle channel, or when an effect references an identity
    key the contract does not declare.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    directory: Path
    contract: RewardContractV1
    required_effects: tuple[Effect, ...]
    forbidden_effects: tuple[Effect, ...]
    oracle: OracleRecipeV1
    certificate: Optional[RewardCertificateV1] = None

    @classmethod
    def load(cls, directory: Path | str) -> "RewardBundle":
        base = Path(directory).expanduser().resolve()
        if not base.is_dir():
            raise BundleError(f"reward contract directory is missing: {base}")
        contract = RewardContractV1.model_validate(_read_json(base / CONTRACT_FILE))
        required_raw = _read_json(base / REQUIRED_EFFECTS_FILE)
        forbidden_raw = _read_json(base / FORBIDDEN_EFFECTS_FILE)
        oracle_raw = _read_json(base / ORACLE_FILE)
        for name, raw, want in (
            (
                REQUIRED_EFFECTS_FILE,
                required_raw,
                contract.required_effect_contract_digest,
            ),
            (
                FORBIDDEN_EFFECTS_FILE,
                forbidden_raw,
                contract.forbidden_effect_contract_digest,
            ),
            (ORACLE_FILE, oracle_raw, contract.oracle.oracle_contract_digest),
        ):
            got = _digest_payload(raw)
            if got != want:
                raise BundleError(
                    f"{name} digest {got} does not match the contract's {want}"
                )
        oracle = OracleRecipeV1.model_validate(oracle_raw)
        if oracle.channel is not contract.oracle.channel:
            raise BundleError(
                f"oracle recipe channel {oracle.channel.value} does not match "
                f"the contract channel {contract.oracle.channel.value}"
            )
        required = tuple(_effects(required_raw, REQUIRED_EFFECTS_FILE))
        forbidden = tuple(_effects(forbidden_raw, FORBIDDEN_EFFECTS_FILE))
        if not required:
            raise BundleError("a reward contract requires at least one required effect")
        declared = set(contract.oracle.identity_keys)
        for effect in (*required, *forbidden):
            missing = effect.referenced_params() - declared
            if missing:
                names = ", ".join(sorted(missing))
                raise BundleError(
                    f"effect references identity keys the contract does not "
                    f"declare: {names}"
                )
        certificate: Optional[RewardCertificateV1] = None
        cert_path = base / CERTIFICATE_FILE
        if cert_path.is_file():
            certificate = RewardCertificateV1.model_validate(_read_json(cert_path))
            if certificate.reward_contract_digest != contract.digest:
                raise BundleError(
                    "certificate.json binds a different reward contract digest"
                )
        return cls(
            directory=base,
            contract=contract,
            required_effects=required,
            forbidden_effects=forbidden,
            oracle=oracle,
            certificate=certificate,
        )

    @property
    def identity_keys(self) -> tuple[str, ...]:
        return tuple(self.contract.oracle.identity_keys)

    def check_identity(self, identity: dict[str, str]) -> None:
        """Refuse an identity whose key set differs from the contract's."""

        want = set(self.identity_keys)
        got = set(identity)
        extra = sorted(got - want)
        missing = sorted(want - got)
        if extra:
            raise IdentityError(
                "oracle_identity carries keys the contract does not declare: "
                + ", ".join(extra)
            )
        if missing:
            raise IdentityError(
                "oracle_identity is missing declared keys: " + ", ".join(missing)
            )


class BundleError(ValueError):
    """The bundle on disk does not match its contract."""


class IdentityError(ValueError):
    """The episode's oracle identity does not fit the contract."""


class SelfSignedRewardEnvelopeV1(_Strict):
    """Local envelope around a portable reward receipt.

    ``execute_seal`` and ``production_seal`` are always false. ``issuer`` is
    always ``self_signed``. The receipt inside is signed on its own; this
    envelope adds the local key fingerprint and the notice.
    """

    # No ``schema_version`` here on purpose: the trainer-side client treats a
    # body with ``receipt`` and no ``schema_version`` as an envelope.
    envelope: Literal["openadapt.reward-self-signed-envelope/v1"] = (
        "openadapt.reward-self-signed-envelope/v1"
    )
    issuer: Literal["self_signed"] = "self_signed"
    issuer_key_fingerprint: StrictStr = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    execute_seal: Literal[False] = False
    production_seal: Literal[False] = False
    verify_host: Literal["local"] = "local"
    flow_governed_policy: Literal[False] = False
    notice: Literal[
        "Reward receipt. Not an Execute Seal. Flow did not govern the policy."
    ] = REWARD_NOTICE
    unscored: StrictBool
    receipt: dict[str, Any]


def assert_no_forbidden_keys(payload: dict[str, Any]) -> None:
    """Refuse a receipt dict that carries a PHI, screenshot, or rollout field."""

    extra = FORBIDDEN_RECEIPT_KEYS.intersection(payload)
    if extra:
        names = ", ".join(sorted(extra))
        raise ValueError(f"reward receipt forbids extra/PHI keys: {names}")


def _read_json(path: Path) -> Any:
    if not path.is_file():
        raise BundleError(f"reward bundle file is missing: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BundleError(f"reward bundle file is not JSON: {path}") from exc


def _effects(raw: Any, name: str) -> list[Effect]:
    if not isinstance(raw, list):
        raise BundleError(f"{name} must be a JSON list of effects")
    effects: list[Effect] = []
    for item in raw:
        if not isinstance(item, dict):
            raise BundleError(f"{name} entries must be JSON objects")
        effects.append(Effect.model_validate(item))
    return effects
