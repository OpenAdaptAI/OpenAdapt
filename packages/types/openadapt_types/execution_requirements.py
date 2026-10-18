"""Closed execution requirements and pure runner-capability matching."""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    StringConstraints,
    model_validator,
)

from openadapt_types.runner_capability import (
    ExecutionMode,
    ExecutionProfile,
    ExecutionSurface,
    RunnerCapability,
    RunnerCapabilityManifestV1,
    canonical_json_bytes,
    parse_semantic_version,
)

EXECUTION_REQUIREMENTS_SCHEMA: Literal["openadapt.execution-requirements/v1"] = (
    "openadapt.execution-requirements/v1"
)
CAPABILITY_MATCH_SCHEMA: Literal["openadapt.capability-match/v1"] = (
    "openadapt.capability-match/v1"
)

_OPAQUE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$"
_SHA256_PATTERN = r"^sha256:[0-9a-f]{64}$"
_SEMANTIC_VERSION_PATTERN = (
    r"^(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\."
    r"(0|[1-9][0-9]{0,8})$"
)

OpaqueId = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=8,
        max_length=128,
        pattern=_OPAQUE_ID_PATTERN,
    ),
]


class _StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _sorted_unique_enums(values: tuple[Enum, ...], field_name: str) -> tuple[Any, ...]:
    if len(set(values)) != len(values):
        raise ValueError(f"{field_name} must not contain duplicates")
    return tuple(sorted(values, key=lambda value: str(value.value)))


def _sorted_unique_strings(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    if len(set(values)) != len(values):
        raise ValueError(f"{field_name} must not contain duplicates")
    return tuple(sorted(values))


class ExecutionRequirementsV1(_StrictContract):
    """The exact runner requirements selected for one qualified plan."""

    schema_version: Literal["openadapt.execution-requirements/v1"]
    workflow_family_id: OpaqueId
    portable_intent_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    selected_plan_id: OpaqueId
    plan_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    qualification_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    binding_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    surface: ExecutionSurface
    execution_mode: ExecutionMode
    profile: ExecutionProfile
    minimum_effect_tier: StrictInt = Field(ge=1, le=4)
    required_capabilities: tuple[RunnerCapability, ...] = Field(
        default=(),
        max_length=len(RunnerCapability),
        json_schema_extra={"uniqueItems": True},
    )
    permitted_runner_ids: tuple[OpaqueId, ...] = Field(
        default=(),
        max_length=128,
        json_schema_extra={"uniqueItems": True},
    )
    permitted_executor_ids: tuple[OpaqueId, ...] = Field(
        default=(),
        max_length=128,
        json_schema_extra={"uniqueItems": True},
    )
    minimum_runtime_version: StrictStr = Field(
        min_length=5,
        max_length=64,
        pattern=_SEMANTIC_VERSION_PATTERN,
    )
    maximum_runtime_version: StrictStr = Field(
        min_length=5,
        max_length=64,
        pattern=_SEMANTIC_VERSION_PATTERN,
    )
    authorization_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    runtime_input_digest: StrictStr = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _validate_and_canonicalize(self) -> ExecutionRequirementsV1:
        minimum = parse_semantic_version(self.minimum_runtime_version)
        maximum = parse_semantic_version(self.maximum_runtime_version)
        if maximum < minimum:
            raise ValueError(
                "maximum_runtime_version must not be less than minimum_runtime_version"
            )
        object.__setattr__(
            self,
            "required_capabilities",
            _sorted_unique_enums(
                self.required_capabilities,
                "required_capabilities",
            ),
        )
        object.__setattr__(
            self,
            "permitted_runner_ids",
            _sorted_unique_strings(
                self.permitted_runner_ids,
                "permitted_runner_ids",
            ),
        )
        object.__setattr__(
            self,
            "permitted_executor_ids",
            _sorted_unique_strings(
                self.permitted_executor_ids,
                "permitted_executor_ids",
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


class CapabilityMismatchCode(str, Enum):
    MANIFEST_NOT_YET_VALID = "manifest_not_yet_valid"
    MANIFEST_EXPIRED = "manifest_expired"
    SURFACE_UNSUPPORTED = "surface_unsupported"
    EXECUTION_MODE_UNSUPPORTED = "execution_mode_unsupported"
    PROFILE_UNSUPPORTED = "profile_unsupported"
    RUNTIME_VERSION_BELOW_MINIMUM = "runtime_version_below_minimum"
    RUNTIME_VERSION_ABOVE_MAXIMUM = "runtime_version_above_maximum"
    RUNNER_ID_UNASSIGNED = "runner_id_unassigned"
    RUNNER_ID_NOT_PERMITTED = "runner_id_not_permitted"
    EXECUTOR_ID_REQUIREMENT_UNSUPPORTED = "executor_id_requirement_unsupported"
    MINIMUM_EFFECT_TIER_UNSUPPORTED = "minimum_effect_tier_unsupported"
    REQUIRED_CAPABILITY_MISSING = "required_capability_missing"


class CapabilityMatchV1(_StrictContract):
    """A deterministic, PHI-free result from one manifest/requirement match."""

    schema_version: Literal["openadapt.capability-match/v1"]
    manifest_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    requirements_digest: StrictStr = Field(pattern=_SHA256_PATTERN)
    matched: StrictBool
    mismatch_codes: tuple[CapabilityMismatchCode, ...] = Field(
        default=(),
        max_length=len(CapabilityMismatchCode),
        json_schema_extra={"uniqueItems": True},
    )
    missing_capabilities: tuple[RunnerCapability, ...] = Field(
        default=(),
        max_length=len(RunnerCapability),
        json_schema_extra={"uniqueItems": True},
    )

    @model_validator(mode="after")
    def _validate_result(self) -> CapabilityMatchV1:
        codes = _sorted_unique_enums(self.mismatch_codes, "mismatch_codes")
        missing = _sorted_unique_enums(
            self.missing_capabilities,
            "missing_capabilities",
        )
        object.__setattr__(self, "mismatch_codes", codes)
        object.__setattr__(self, "missing_capabilities", missing)
        has_failure = bool(codes or missing)
        if self.matched == has_failure:
            raise ValueError("matched must be true exactly when no mismatch exists")
        has_missing_code = CapabilityMismatchCode.REQUIRED_CAPABILITY_MISSING in codes
        if bool(missing) != has_missing_code:
            raise ValueError(
                "required_capability_missing must match missing_capabilities"
            )
        return self


def match_runner_capabilities(
    manifest: RunnerCapabilityManifestV1,
    requirements: ExecutionRequirementsV1,
    *,
    at: datetime | str,
) -> CapabilityMatchV1:
    """Match one manifest without I/O, hidden policy, or a system-clock read."""

    codes: set[CapabilityMismatchCode] = set()
    if manifest.is_not_yet_valid(at=at):
        codes.add(CapabilityMismatchCode.MANIFEST_NOT_YET_VALID)
    if manifest.is_expired(at=at):
        codes.add(CapabilityMismatchCode.MANIFEST_EXPIRED)
    lane = manifest.lane_for(requirements.surface, requirements.execution_mode)
    if lane is None:
        if requirements.surface not in manifest.supported_surfaces:
            codes.add(CapabilityMismatchCode.SURFACE_UNSUPPORTED)
        else:
            codes.add(CapabilityMismatchCode.EXECUTION_MODE_UNSUPPORTED)
    elif requirements.profile not in lane.supported_profiles:
        codes.add(CapabilityMismatchCode.PROFILE_UNSUPPORTED)

    runtime_version = parse_semantic_version(manifest.flow_version)
    if runtime_version < parse_semantic_version(requirements.minimum_runtime_version):
        codes.add(CapabilityMismatchCode.RUNTIME_VERSION_BELOW_MINIMUM)
    if runtime_version > parse_semantic_version(requirements.maximum_runtime_version):
        codes.add(CapabilityMismatchCode.RUNTIME_VERSION_ABOVE_MAXIMUM)

    if requirements.permitted_runner_ids:
        if manifest.runner_id is None:
            codes.add(CapabilityMismatchCode.RUNNER_ID_UNASSIGNED)
        elif manifest.runner_id not in requirements.permitted_runner_ids:
            codes.add(CapabilityMismatchCode.RUNNER_ID_NOT_PERMITTED)
    if requirements.permitted_executor_ids:
        # A runner manifest does not attest an external executor identity. A
        # later executor contract can satisfy this restriction. This matcher
        # must not ignore it and admit an arbitrary executor.
        codes.add(CapabilityMismatchCode.EXECUTOR_ID_REQUIREMENT_UNSUPPORTED)

    if lane is None:
        missing: tuple[RunnerCapability, ...] = ()
    else:
        best_effect_tier = lane.best_effect_tier()
        if (
            best_effect_tier is None
            or best_effect_tier > requirements.minimum_effect_tier
        ):
            codes.add(CapabilityMismatchCode.MINIMUM_EFFECT_TIER_UNSUPPORTED)
        missing = tuple(
            sorted(
                set(requirements.required_capabilities) - set(lane.capabilities),
                key=lambda capability: capability.value,
            )
        )
    if missing:
        codes.add(CapabilityMismatchCode.REQUIRED_CAPABILITY_MISSING)

    ordered_codes = tuple(sorted(codes, key=lambda code: code.value))
    return CapabilityMatchV1(
        schema_version=CAPABILITY_MATCH_SCHEMA,
        manifest_digest=manifest.digest,
        requirements_digest=requirements.digest,
        matched=not ordered_codes,
        mismatch_codes=ordered_codes,
        missing_capabilities=missing,
    )
