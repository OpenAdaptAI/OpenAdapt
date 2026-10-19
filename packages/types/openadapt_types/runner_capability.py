"""Closed, versioned capability advertisement for an OpenAdapt runner.

This module contains a public interface only. It does not contain application
bindings, deployment policy, qualification data, thresholds, or effect-oracle
recipes. A manifest reports what one runner can supply. It is not proof that a
specific run used those capabilities; qualification and run evidence provide
that proof separately.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime
from enum import Enum, IntEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator

RUNNER_CAPABILITY_MANIFEST_SCHEMA: Literal[
    "openadapt.runner-capability-manifest/v1"
] = "openadapt.runner-capability-manifest/v1"

_OPAQUE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$"
_SEMANTIC_VERSION_PATTERN = (
    r"^(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\."
    r"(0|[1-9][0-9]{0,8})$"
)
_TIMESTAMP_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(Z|[+-]\d{2}:\d{2})$"


class _StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExecutionSurface(str, Enum):
    WEB = "web"
    WINDOWS = "windows"
    MACOS = "macos"
    LINUX = "linux"
    RDP = "rdp"
    CITRIX = "citrix"


class ExecutionMode(str, Enum):
    IN_SESSION = "in_session"
    EXTERNAL = "external"


class ExecutionProfile(str, Enum):
    DEMO = "demo"
    STANDARD = "standard"
    REGULATED = "regulated"


class RunnerHostOS(str, Enum):
    WINDOWS = "windows"
    MACOS = "macos"
    LINUX = "linux"


class RunnerArchitecture(str, Enum):
    X86_64 = "x86_64"
    AARCH64 = "aarch64"


class EffectVerificationTier(IntEnum):
    """Evidence strength, where 1 is strongest and 4 is weakest."""

    INDEPENDENT_SYSTEM_INTERFACE = 1
    INDEPENDENT_SESSION = 2
    PERSISTED_STATE_REACQUISITION = 3
    IMMEDIATE_SCREEN_CONFIRMATION = 4


class RunnerCapability(str, Enum):
    """Closed capabilities that admission can require from a runner."""

    # Observation and resolution.
    PIXEL_OBSERVATION = "pixel_observation"
    STRUCTURAL_OBSERVATION = "structural_observation"
    PLAYWRIGHT_DOM = "playwright_dom"
    STRUCTURAL_RESOLUTION = "structural_resolution"
    VISUAL_RESOLUTION = "visual_resolution"
    OCR_RELATIONAL_RESOLUTION = "ocr_relational_resolution"

    # Actuation.
    ACTUATION = "actuation"
    PHYSICAL_INPUT_ACTUATION = "physical_input_actuation"
    STRUCTURAL_ACTUATION = "structural_actuation"
    PLAYWRIGHT_ACTUATION = "playwright_actuation"
    API_ACTUATION = "api_actuation"
    EXTERNAL_EXECUTOR_ACTUATION = "external_executor_actuation"

    # Identity and workflow state.
    APPLICATION_IDENTITY = "application_identity"
    SESSION_IDENTITY = "session_identity"
    WORKFLOW_STATE_IDENTITY = "workflow_state_identity"
    RECORD_IDENTITY = "record_identity"
    IDENTITY_VERIFICATION = "identity_verification"

    # Run continuity and evidence.
    GOVERNED_AUTHORIZATION = "governed_authorization"
    SETTLED_STATE_DETECTION = "settled_state_detection"
    SESSION_CONTINUITY = "session_continuity"
    DURABLE_RESUME = "durable_resume"
    POSTCONDITION_VERIFICATION = "postcondition_verification"
    EVIDENCE_EXPORT = "evidence_export"

    # Effect-verification strength.
    EFFECT_VERIFICATION = "effect_verification"
    INDEPENDENT_SYSTEM_OF_RECORD = "independent_system_of_record"
    INDEPENDENT_SESSION = "independent_session"
    PERSISTED_STATE_REACQUISITION = "persisted_state_reacquisition"
    IMMEDIATE_SCREEN_CONFIRMATION = "immediate_screen_confirmation"
    EFFECT_TIER_1 = "effect_tier_1"
    EFFECT_TIER_2 = "effect_tier_2"
    EFFECT_TIER_3 = "effect_tier_3"
    EFFECT_TIER_4 = "effect_tier_4"

    # Secret, parameter, and network boundaries.
    LOCAL_SECRET_RESOLUTION = "local_secret_resolution"
    PARAMETER_BY_REFERENCE = "parameter_by_reference"
    NO_EXTERNAL_EGRESS = "no_external_egress"
    POLICY_CONTROLLED_EGRESS = "policy_controlled_egress"


_EFFECT_CAPABILITIES_BY_TIER: Mapping[
    EffectVerificationTier, tuple[RunnerCapability, ...]
] = {
    EffectVerificationTier.INDEPENDENT_SYSTEM_INTERFACE: (
        RunnerCapability.EFFECT_TIER_1,
        RunnerCapability.INDEPENDENT_SYSTEM_OF_RECORD,
    ),
    EffectVerificationTier.INDEPENDENT_SESSION: (
        RunnerCapability.EFFECT_TIER_2,
        RunnerCapability.INDEPENDENT_SESSION,
    ),
    EffectVerificationTier.PERSISTED_STATE_REACQUISITION: (
        RunnerCapability.EFFECT_TIER_3,
        RunnerCapability.PERSISTED_STATE_REACQUISITION,
    ),
    EffectVerificationTier.IMMEDIATE_SCREEN_CONFIRMATION: (
        RunnerCapability.EFFECT_TIER_4,
        RunnerCapability.IMMEDIATE_SCREEN_CONFIRMATION,
    ),
}


def _parse_timestamp(value: str, field_name: str) -> datetime:
    if re.fullmatch(_TIMESTAMP_PATTERN, value) is None:
        raise ValueError(f"{field_name} must be an RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an RFC 3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    return parsed


def parse_semantic_version(value: str) -> tuple[int, int, int]:
    """Return a comparable released semantic version or raise ``ValueError``."""

    if re.fullmatch(_SEMANTIC_VERSION_PATTERN, value) is None:
        raise ValueError("version must have the form MAJOR.MINOR.PATCH")
    return tuple(int(part) for part in value.split("."))  # type: ignore[return-value]


def canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    """Return the language-neutral canonical JSON form used by this contract."""

    return json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _canonical_tuple(values: tuple[Enum, ...], field_name: str) -> tuple[Any, ...]:
    if len(set(values)) != len(values):
        raise ValueError(f"{field_name} must not contain duplicates")
    return tuple(sorted(values, key=lambda value: str(value.value)))


class RunnerCapabilityLaneV1(_StrictContract):
    """Capabilities bound to one exact surface and execution mode."""

    surface: ExecutionSurface
    execution_mode: ExecutionMode
    capabilities: tuple[RunnerCapability, ...] = Field(
        min_length=1,
        max_length=len(RunnerCapability),
        json_schema_extra={"uniqueItems": True},
    )
    supported_profiles: tuple[ExecutionProfile, ...] = Field(
        min_length=1,
        max_length=len(ExecutionProfile),
        json_schema_extra={"uniqueItems": True},
    )

    @model_validator(mode="after")
    def _validate_and_canonicalize(self) -> RunnerCapabilityLaneV1:
        object.__setattr__(
            self,
            "capabilities",
            _canonical_tuple(self.capabilities, "capabilities"),
        )
        object.__setattr__(
            self,
            "supported_profiles",
            _canonical_tuple(self.supported_profiles, "supported_profiles"),
        )
        return self

    def best_effect_tier(self) -> EffectVerificationTier | None:
        """Return the strongest tier supplied by this lane."""

        supported = [
            tier
            for tier, capabilities in _EFFECT_CAPABILITIES_BY_TIER.items()
            if any(capability in self.capabilities for capability in capabilities)
        ]
        return min(supported) if supported else None


class RunnerCapabilityManifestV1(_StrictContract):
    """A PHI-free, time-bounded advertisement from one runner installation."""

    schema_version: Literal["openadapt.runner-capability-manifest/v1"]
    installation_id: StrictStr = Field(pattern=_OPAQUE_ID_PATTERN)
    runner_id: StrictStr | None = Field(default=None, pattern=_OPAQUE_ID_PATTERN)
    agent_version: StrictStr = Field(
        min_length=5,
        max_length=64,
        pattern=_SEMANTIC_VERSION_PATTERN,
    )
    flow_version: StrictStr = Field(
        min_length=5,
        max_length=64,
        pattern=_SEMANTIC_VERSION_PATTERN,
    )
    host_os: RunnerHostOS
    architecture: RunnerArchitecture
    lanes: tuple[RunnerCapabilityLaneV1, ...] = Field(
        min_length=1,
        max_length=len(ExecutionSurface) * len(ExecutionMode),
        json_schema_extra={"uniqueItems": True},
    )
    generated_at: StrictStr = Field(
        min_length=20,
        max_length=40,
        pattern=_TIMESTAMP_PATTERN,
    )
    expires_at: StrictStr = Field(
        min_length=20,
        max_length=40,
        pattern=_TIMESTAMP_PATTERN,
    )

    @model_validator(mode="after")
    def _validate_and_canonicalize(self) -> RunnerCapabilityManifestV1:
        parse_semantic_version(self.agent_version)
        parse_semantic_version(self.flow_version)
        generated_at = _parse_timestamp(self.generated_at, "generated_at")
        expires_at = _parse_timestamp(self.expires_at, "expires_at")
        if expires_at <= generated_at:
            raise ValueError("expires_at must be later than generated_at")

        lane_keys = [(lane.surface, lane.execution_mode) for lane in self.lanes]
        if len(set(lane_keys)) != len(lane_keys):
            raise ValueError("lanes must have unique surface and execution-mode pairs")
        object.__setattr__(
            self,
            "lanes",
            tuple(
                sorted(
                    self.lanes,
                    key=lambda lane: (lane.surface.value, lane.execution_mode.value),
                )
            ),
        )
        return self

    def canonical_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.canonical_payload())

    @property
    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.canonical_bytes()).hexdigest()

    def is_expired(self, *, at: datetime | str) -> bool:
        """Compare expiry with an explicit instant without reading the clock."""

        instant = _parse_timestamp(at, "at") if isinstance(at, str) else at
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("at must include a timezone")
        return instant >= _parse_timestamp(self.expires_at, "expires_at")

    def is_not_yet_valid(self, *, at: datetime | str) -> bool:
        """Reject a manifest before its explicit generation instant."""

        instant = _parse_timestamp(at, "at") if isinstance(at, str) else at
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("at must include a timezone")
        return instant < _parse_timestamp(self.generated_at, "generated_at")

    @property
    def supported_surfaces(self) -> tuple[ExecutionSurface, ...]:
        return tuple(
            sorted({lane.surface for lane in self.lanes}, key=lambda item: item.value)
        )

    def lane_for(
        self, surface: ExecutionSurface, execution_mode: ExecutionMode
    ) -> RunnerCapabilityLaneV1 | None:
        return next(
            (
                lane
                for lane in self.lanes
                if lane.surface == surface and lane.execution_mode == execution_mode
            ),
            None,
        )
