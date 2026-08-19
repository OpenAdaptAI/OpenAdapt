"""v2 production qualification contract (coordinated Flow/Cloud upgrade).

This module holds the closed cross-language models and verification helpers
for the ``managed_qualification_authority_v2`` capability.  The v1 module
(:mod:`openadapt_flow.qualification_admission`) remains the envelope that the
current dispatch path verifies; v2 is the frozen contract that replaces it in
the coordinated activation.  Both fail closed.

Design rules that this module enforces:

* one strict SemVer grammar for every Flow / Playwright / Capture / Desktop
  version that a signer binds;
* an exact, sorted runtime-component inventory that binds only the artifacts a
  substrate uses (web: browser engine + wrapper; native: Capture + OS observer +
  wrapper; RDP/Citrix: Capture + remote transport + wrapper, no UIA claim);
* ``evidence_runner_signer_sha256`` names the Ed25519 evidence signer;
* the admission ID and the runtime-validation ID are separate authorities;
* every cross-language integer is a strict JavaScript-safe integer and every
  fixed boolean rejects the numeric ``0`` / ``1`` spellings;
* the signer registry is a durable, monotonic checkpoint, never an in-memory
  cache, and a runtime halts when the checkpoint is absent.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import typing
from base64 import b64decode, b64encode
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final, Literal, Mapping
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

SCHEMA: Final[Literal["openadapt.qualification-admission/v2"]] = (
    "openadapt.qualification-admission/v2"
)
SIGNATURE_DOMAIN: Final[bytes] = b"openadapt-qualification-admission-v2\0"
EVIDENCE_IDENTITY_DOMAIN: Final[bytes] = (
    b"OpenAdapt production acceptance evidence identity v2\0"
)
SIGNER_REGISTRY_DOMAIN: Final[bytes] = b"OpenAdapt qualification signer registry v2\0"
RUNTIME_COMPONENT_MANIFEST_DOMAIN: Final[bytes] = (
    b"openadapt-runtime-component-manifest-v1\0"
)
ADMITTED_RUNTIME_BUILD_DOMAIN: Final[bytes] = b"openadapt-admitted-runtime-build-v1\0"
ADMISSION_POLICY_DOMAIN: Final[bytes] = (
    b"OpenAdapt production acceptance admission policy v1\0"
)

_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_HEX40_RE = re.compile(r"^[a-f0-9]{40}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+\-]{0,199}$")
_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+:\-]{0,127}$")
_SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_PINNED_IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._/:\-]{0,255}@sha256:[a-f0-9]{64}$")
_UTC_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_KEY_ID_RE = re.compile(r"^qa-ed25519-[a-f0-9]{16}$")
MAX_ADMISSION_LIFETIME: Final[timedelta] = timedelta(days=30)
MAX_REGISTRY_LIFETIME: Final[timedelta] = timedelta(days=7)
MAX_PERMIT_SNAPSHOT_AGE: Final[timedelta] = timedelta(seconds=60)
JS_MAX_SAFE_INTEGER: Final[int] = 9_007_199_254_740_991


PRODUCTION_ACCEPTANCE_ADMISSION_POLICY: Final[dict[str, Any]] = {
    "schema_version": "openadapt.production-acceptance-admission-policy/v1",
    "qualification_admission_schema": SCHEMA,
    "qualification_campaign_schema": "openadapt.qualification-campaign/v2",
    "qualification_trial_schema": "openadapt.qualification-trial-row/v2",
    "qualification_receipt_schema": "openadapt.qualification-evidence-receipt/v2",
    "admission_signature_domain": "openadapt-qualification-admission-v2\\0",
    "receipt_signature_domain": "openadapt-qualification-evidence-receipt-v2\\0",
    "issuer_workflow": (
        "OpenAdaptAI/openadapt-internal/.github/workflows/"
        "production-qualification-admission.yml"
    ),
    "issuer_ref_prefix": "refs/heads/main@",
    "maximum_lifetime_seconds": 30 * 24 * 60 * 60,
    "minimum_trials_per_condition": 3,
    "failure_taxonomy": [
        "collateral_effect",
        "duplicate_effect",
        "healthy_path_model_call",
        "operator_intervention",
        "over_halt",
        "platform_failure",
        "safe_halt",
        "silent_incorrect_success",
        "uncertain_delivery",
        "verified",
        "wrong_record",
    ],
    "external_signer_registry_required": True,
    "registry_freshness_required_at_actuation": True,
    "revocation_check_required": True,
    "distinct_admission_and_runtime_ids_required": True,
    "shared_evidence_identity_required": True,
}


class QualificationAdmissionError(ValueError):
    """The v2 production authority is stale, invalid, or inconsistent."""


def canonical_json(value: Any) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def contract_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _domain_sha256(domain: bytes, value: Any) -> str:
    return hashlib.sha256(domain + canonical_json(value)).hexdigest()


PRODUCTION_ACCEPTANCE_ADMISSION_POLICY_SHA256: Final[str] = _domain_sha256(
    ADMISSION_POLICY_DOMAIN,
    PRODUCTION_ACCEPTANCE_ADMISSION_POLICY,
)


def _literal_kinds(annotation: Any) -> tuple[bool, bool]:
    """Return ``(fixed_bool, fixed_int)`` for a ``Literal[...]`` annotation."""

    origin = typing.get_origin(annotation)
    if origin is Literal:
        args = typing.get_args(annotation)
        return (
            all(isinstance(arg, bool) for arg in args),
            all(isinstance(arg, int) and not isinstance(arg, bool) for arg in args),
        )
    if origin is typing.Union or (
        origin is not None and getattr(origin, "__name__", "") == "UnionType"
    ):
        kinds = [
            _literal_kinds(arg)
            for arg in typing.get_args(annotation)
            if arg is not type(None)
        ]
        if kinds and all(kind[0] for kind in kinds):
            return (True, False)
        if kinds and all(kind[1] for kind in kinds):
            return (False, True)
    return (False, False)


def reject_numeric_boolean_confusion(model: type[BaseModel], data: Any) -> Any:
    """Refuse ``1`` for a fixed ``True`` and ``False`` for a fixed ``0``.

    Pydantic accepts a numeric spelling of a fixed boolean and a boolean
    spelling of a fixed integer.  A cross-language signed record must reject
    both so that Python and TypeScript verifiers agree on the same bytes.
    """

    if not isinstance(data, Mapping):
        return data
    for name, info in model.model_fields.items():
        if name not in data:
            continue
        value = data[name]
        fixed_bool, fixed_int = _literal_kinds(info.annotation)
        if fixed_bool and not isinstance(value, bool):
            raise ValueError(f"{name} must be a boolean")
        if fixed_int and (isinstance(value, bool) or not isinstance(value, int)):
            raise ValueError(f"{name} must be an integer")
    return data


class ClosedSignedModel(BaseModel):
    """Frozen, closed model whose fixed booleans and integers are strict."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="before")
    @classmethod
    def _strict_fixed_scalars(cls, data: Any) -> Any:
        return reject_numeric_boolean_confusion(cls, data)


def qualification_signer_key_id(public_key: bytes) -> str:
    if len(public_key) != 32:
        raise ValueError("qualification signer public key is invalid")
    return f"qa-ed25519-{hashlib.sha256(public_key).hexdigest()[:16]}"


def _parse_utc(value: str, *, field: str) -> datetime:
    if not isinstance(value, str) or _UTC_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be a canonical UTC timestamp")
    return datetime.fromisoformat(value[:-1] + "+00:00").astimezone(timezone.utc)


def _canonical_utc(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _uuid(value: str, *, field: str) -> str:
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be a canonical UUID") from exc
    if str(parsed) != value:
        raise ValueError(f"{field} must be a canonical UUID")
    return value


def _canonical_base64(value: str, *, size: int, field: str) -> bytes:
    try:
        decoded = b64decode(value, validate=True)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be canonical base64") from exc
    if len(decoded) != size or b64encode(decoded).decode("ascii") != value:
        raise ValueError(f"{field} is invalid")
    return decoded


def _strict_semver(value: str, *, field: str) -> str:
    match = _SEMVER_RE.fullmatch(value)
    if match is None:
        raise ValueError(f"{field} must be strict SemVer")
    prerelease = match.group(4)
    if prerelease is not None and any(
        item.isdigit() and len(item) > 1 and item.startswith("0")
        for item in prerelease.split(".")
    ):
        raise ValueError(f"{field} must be strict SemVer")
    return value


class ManagedBrowserBuild(ClosedSignedModel):
    playwright_version: str = Field(pattern=_SAFE_TOKEN_RE)
    browser_base_image: str = Field(pattern=_PINNED_IMAGE_RE)

    @field_validator("playwright_version")
    @classmethod
    def _playwright_semver(cls, value: str) -> str:
        return _strict_semver(value, field="Playwright version")


class RuntimeComponent(ClosedSignedModel):
    name: str = Field(pattern=_ID_RE)
    version: str = Field(pattern=_SAFE_TOKEN_RE)
    release_commit: str = Field(pattern=_HEX40_RE)
    artifact_sha256: str = Field(pattern=_SHA256_RE)
    roles: tuple[
        Literal[
            "actuator",
            "observer",
            "operator_surface",
            "orchestrator",
            "transport",
        ],
        ...,
    ] = Field(min_length=1, max_length=5)
    capabilities: tuple[
        Literal[
            "accessibility_observation",
            "atspi_observation",
            "browser_actuation",
            "browser_observation",
            "desktop_operator_surface",
            "flow_runtime",
            "native_actuation",
            "native_capture",
            "pixel_observation",
            "remote_transport",
            "runner_wrapper",
            "uia_observation",
        ],
        ...,
    ] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def _closed_component(self) -> "RuntimeComponent":
        if self.name in {
            "openadapt-capture",
            "openadapt-desktop",
            "openadapt-flow",
        }:
            _strict_semver(self.version, field=f"{self.name} version")
        for label, values in (
            ("roles", self.roles),
            ("capabilities", self.capabilities),
        ):
            if values != tuple(sorted(values)) or len(values) != len(set(values)):
                raise ValueError(
                    f"runtime component {label} must be unique and ordered"
                )
        required_role = {
            "accessibility_observation": "observer",
            "atspi_observation": "observer",
            "browser_actuation": "actuator",
            "browser_observation": "observer",
            "desktop_operator_surface": "operator_surface",
            "flow_runtime": "orchestrator",
            "native_actuation": "actuator",
            "native_capture": "observer",
            "pixel_observation": "observer",
            "remote_transport": "transport",
            "runner_wrapper": "orchestrator",
            "uia_observation": "observer",
        }
        for capability in self.capabilities:
            if required_role[capability] not in self.roles:
                raise ValueError(
                    f"runtime capability {capability} lacks its required role"
                )
        return self


class RuntimeComponentManifest(ClosedSignedModel):
    schema_version: Literal["openadapt.runtime-component-manifest/v1"] = (
        "openadapt.runtime-component-manifest/v1"
    )
    components: tuple[RuntimeComponent, ...] = Field(min_length=2, max_length=32)

    @model_validator(mode="after")
    def _closed_components(self) -> "RuntimeComponentManifest":
        names = tuple(item.name for item in self.components)
        if names != tuple(sorted(names)) or len(names) != len(set(names)):
            raise ValueError("runtime components must be unique and ordered")
        flow = next(
            (item for item in self.components if item.name == "openadapt-flow"),
            None,
        )
        if flow is None or "flow_runtime" not in flow.capabilities:
            raise ValueError("runtime components must include openadapt-flow")
        if not any("actuator" in item.roles for item in self.components):
            raise ValueError("runtime components must include an actuator")
        if not any("observer" in item.roles for item in self.components):
            raise ValueError("runtime components must include an observer")
        return self

    def artifact_sha256(self) -> str:
        return _domain_sha256(RUNTIME_COMPONENT_MANIFEST_DOMAIN, self)


class SubstrateRuntimeBinding(ClosedSignedModel):
    os_family: Literal["windows", "macos", "linux"]
    transport: Literal["local", "browser", "rdp", "citrix"]
    runtime_boundary_sha256: str = Field(pattern=_SHA256_RE)


class AdmittedRuntimeBuildIdentity(ClosedSignedModel):
    schema_version: Literal["openadapt.admitted-runtime-build/v1"] = (
        "openadapt.admitted-runtime-build/v1"
    )
    substrate: Literal["web", "windows", "macos", "linux", "rdp", "citrix"]
    flow_version: str = Field(pattern=_SAFE_TOKEN_RE)
    flow_release_commit: str = Field(pattern=_HEX40_RE)
    flow_wheel_sha256: str = Field(pattern=_SHA256_RE)
    runtime_manifest: RuntimeComponentManifest
    runtime_manifest_sha256: str = Field(pattern=_SHA256_RE)
    runner_build: str = Field(pattern=_SAFE_TOKEN_RE)
    runner_artifact_sha256: str = Field(pattern=_SHA256_RE)
    managed_browser: ManagedBrowserBuild | None
    substrate_runtime: SubstrateRuntimeBinding

    @model_validator(mode="after")
    def _closed_runtime_identity(self) -> "AdmittedRuntimeBuildIdentity":
        _strict_semver(self.flow_version, field="Flow version")
        if self.runner_build != "report-v5":
            raise ValueError("production runner must use report-v5")
        if self.runtime_manifest_sha256 != self.runtime_manifest.artifact_sha256():
            raise ValueError("runtime manifest digest is invalid")
        expected_transport = {
            "web": "browser",
            "windows": "local",
            "macos": "local",
            "linux": "local",
            "rdp": "rdp",
            "citrix": "citrix",
        }[self.substrate]
        if self.substrate == "web":
            if self.managed_browser is None:
                raise ValueError("web runtime must bind its managed browser")
        elif self.managed_browser is not None:
            raise ValueError("non-web runtime cannot bind a managed browser")
        if self.substrate in {"windows", "macos", "linux"}:
            if self.substrate_runtime.os_family != self.substrate:
                raise ValueError("native runtime OS does not match its substrate")
        if self.substrate_runtime.transport != expected_transport:
            raise ValueError("runtime transport does not match its substrate")
        flow = next(
            item
            for item in self.runtime_manifest.components
            if item.name == "openadapt-flow"
        )
        if (
            flow.version,
            flow.release_commit,
            flow.artifact_sha256,
        ) != (
            self.flow_version,
            self.flow_release_commit,
            self.flow_wheel_sha256,
        ):
            raise ValueError("runtime manifest Flow identity does not match")
        wrappers = [
            component
            for component in self.runtime_manifest.components
            if "runner_wrapper" in component.capabilities
        ]
        if len(wrappers) != 1:
            raise ValueError("runtime manifest must bind exactly one runner wrapper")
        wrapper = wrappers[0]
        if (wrapper.version, wrapper.artifact_sha256) != (
            self.runner_build,
            self.runner_artifact_sha256,
        ):
            raise ValueError("runtime manifest runner identity does not match")
        capabilities = {
            capability
            for component in self.runtime_manifest.components
            for capability in component.capabilities
        }
        required = {
            "flow_runtime",
            "runner_wrapper",
        }
        forbidden: set[str] = set()
        if self.substrate == "web":
            required.update({"browser_actuation", "browser_observation"})
            forbidden.update(
                {
                    "accessibility_observation",
                    "atspi_observation",
                    "native_actuation",
                    "native_capture",
                    "remote_transport",
                    "uia_observation",
                }
            )
        elif self.substrate in {"windows", "macos", "linux"}:
            observer = {
                "windows": "uia_observation",
                "macos": "accessibility_observation",
                "linux": "atspi_observation",
            }[self.substrate]
            required.update({"native_actuation", "native_capture", observer})
            forbidden.update(
                {"browser_actuation", "browser_observation", "remote_transport"}
            )
        else:
            required.update(
                {
                    "native_actuation",
                    "native_capture",
                    "pixel_observation",
                    "remote_transport",
                }
            )
            forbidden.update(
                {
                    "accessibility_observation",
                    "atspi_observation",
                    "browser_actuation",
                    "browser_observation",
                    "uia_observation",
                }
            )
        if not required.issubset(capabilities):
            missing = ", ".join(sorted(required - capabilities))
            raise ValueError(f"runtime manifest lacks required capabilities: {missing}")
        if forbidden & capabilities:
            invalid = ", ".join(sorted(forbidden & capabilities))
            raise ValueError(f"runtime manifest has invalid capabilities: {invalid}")
        if self.substrate != "web" and not any(
            component.name == "openadapt-capture"
            and "native_capture" in component.capabilities
            for component in self.runtime_manifest.components
        ):
            raise ValueError("native runtime must bind openadapt-capture")
        return self

    def artifact_sha256(self) -> str:
        return _domain_sha256(ADMITTED_RUNTIME_BUILD_DOMAIN, self)


class ProductionAcceptanceEvidenceIdentity(ClosedSignedModel):
    schema_version: Literal["openadapt.production-acceptance-evidence-identity/v2"] = (
        "openadapt.production-acceptance-evidence-identity/v2"
    )
    runtime_build_identity: AdmittedRuntimeBuildIdentity
    evidence_runner_signer_sha256: str = Field(pattern=_SHA256_RE)
    deployment_manifest_sha256: str = Field(pattern=_SHA256_RE)
    qualification_signer_registry_sha256: str = Field(pattern=_SHA256_RE)
    qualification_signer_registry_revision: StrictInt = Field(
        ge=1, le=JS_MAX_SAFE_INTEGER
    )
    tenant_id: str
    workflow_id: str
    workflow_version_id: str
    workflow_digest: str = Field(pattern=_SHA256_RE)
    bundle_version_id: str
    bundle_artifact_sha256: str = Field(pattern=_SHA256_RE)
    bundle_content_digest: str = Field(pattern=_SHA256_RE)
    environment_digest: str = Field(pattern=_SHA256_RE)
    governed_authorization_template_sha256: str = Field(pattern=_SHA256_RE)
    application_contract_sha256: str = Field(pattern=_SHA256_RE)
    substrate_contract_sha256: str = Field(pattern=_SHA256_RE)
    environment_contract_sha256: str = Field(pattern=_SHA256_RE)
    runtime_environment_sha256: str = Field(pattern=_SHA256_RE)
    runtime_contract_sha256: str = Field(pattern=_SHA256_RE)
    input_policy_sha256: str = Field(pattern=_SHA256_RE)
    action_policy_sha256: str = Field(pattern=_SHA256_RE)
    network_policy_sha256: str = Field(pattern=_SHA256_RE)
    identity_contract_sha256: str = Field(pattern=_SHA256_RE)
    effect_contract_sha256: str = Field(pattern=_SHA256_RE)
    operator_contract_sha256: str = Field(pattern=_SHA256_RE)
    runtime_validation_id: str
    admission_id: str
    campaign_id: str
    campaign_contract_sha256: str = Field(pattern=_SHA256_RE)
    oracle_contract_sha256: str = Field(pattern=_SHA256_RE)
    qualification_campaign_schema: Literal["openadapt.qualification-campaign/v2"]
    qualification_trial_schema: Literal["openadapt.qualification-trial-row/v2"]
    qualification_receipt_schema: Literal["openadapt.qualification-evidence-receipt/v2"]
    admission_policy_sha256: str = Field(pattern=_SHA256_RE)

    @field_validator(
        "tenant_id",
        "workflow_id",
        "workflow_version_id",
        "bundle_version_id",
        "runtime_validation_id",
        "admission_id",
        "campaign_id",
    )
    @classmethod
    def _canonical_uuid(cls, value: str, info: Any) -> str:
        return _uuid(value, field=info.field_name)

    @model_validator(mode="after")
    def _closed_identity(self) -> "ProductionAcceptanceEvidenceIdentity":
        if self.admission_id == self.runtime_validation_id:
            raise ValueError("admission and runtime validation identities must differ")
        if self.workflow_version_id != self.bundle_version_id:
            raise ValueError("workflow and bundle versions must match")
        if self.workflow_digest != self.bundle_content_digest:
            raise ValueError("workflow digest must match bundle content digest")
        if (
            self.admission_policy_sha256
            != PRODUCTION_ACCEPTANCE_ADMISSION_POLICY_SHA256
        ):
            raise ValueError("admission policy digest is invalid")
        return self

    def artifact_sha256(self) -> str:
        return _domain_sha256(EVIDENCE_IDENTITY_DOMAIN, self)


class QualificationCondition(ClosedSignedModel):
    task: str = Field(min_length=1, max_length=128, pattern=_ID_RE)
    condition: str = Field(min_length=1, max_length=128, pattern=_ID_RE)
    required_trials: StrictInt = Field(ge=3, le=10_000)
    observed_trials: StrictInt = Field(ge=3, le=10_000)

    @model_validator(mode="after")
    def _complete_count(self) -> "QualificationCondition":
        if self.observed_trials < self.required_trials:
            raise ValueError("observed trials do not satisfy the required count")
        return self


class QualificationCampaignBinding(ClosedSignedModel):
    campaign_id: str
    artifact_sha256: str = Field(pattern=_SHA256_RE)
    contract_sha256: str = Field(pattern=_SHA256_RE)
    outcomes_sha256: str = Field(pattern=_SHA256_RE)
    oracle_id: str = Field(min_length=1, max_length=200, pattern=_ID_RE)
    oracle_contract_sha256: str = Field(pattern=_SHA256_RE)
    tasks: tuple[QualificationCondition, ...] = Field(min_length=1, max_length=1_000)
    failure_taxonomy: tuple[str, ...] = Field(min_length=2, max_length=128)
    decision: Literal["admitted"] = "admitted"

    @field_validator("campaign_id")
    @classmethod
    def _campaign_uuid(cls, value: str) -> str:
        return _uuid(value, field="campaign_id")

    @model_validator(mode="after")
    def _closed_campaign(self) -> "QualificationCampaignBinding":
        task_keys = tuple((item.task, item.condition) for item in self.tasks)
        if task_keys != tuple(sorted(task_keys)) or len(task_keys) != len(
            set(task_keys)
        ):
            raise ValueError("qualification task conditions must be unique and ordered")
        if self.failure_taxonomy != tuple(
            PRODUCTION_ACCEPTANCE_ADMISSION_POLICY["failure_taxonomy"]
        ):
            raise ValueError("qualification failure taxonomy does not match policy")
        return self


class QualificationIssuer(ClosedSignedModel):
    key_id: str = Field(pattern=_KEY_ID_RE)
    workflow: str = Field(min_length=1, max_length=200, pattern=_ID_RE)
    ref: str = Field(min_length=1, max_length=200, pattern=_ID_RE)


class QualificationAdmissionPayload(ClosedSignedModel):
    schema_version: Literal["openadapt.qualification-admission/v2"] = SCHEMA
    admission_id: str
    tenant_id: str
    workflow_id: str
    workflow_version_id: str
    bundle_version_id: str
    runtime_validation_id: str
    bundle_artifact_sha256: str = Field(pattern=_SHA256_RE)
    bundle_content_digest: str = Field(pattern=_SHA256_RE)
    environment_digest: str = Field(pattern=_SHA256_RE)
    governed_authorization_template_sha256: str = Field(pattern=_SHA256_RE)
    application_contract_sha256: str = Field(pattern=_SHA256_RE)
    substrate_contract_sha256: str = Field(pattern=_SHA256_RE)
    environment_contract_sha256: str = Field(pattern=_SHA256_RE)
    runtime_environment_sha256: str = Field(pattern=_SHA256_RE)
    runtime_contract_sha256: str = Field(pattern=_SHA256_RE)
    input_policy_sha256: str = Field(pattern=_SHA256_RE)
    action_policy_sha256: str = Field(pattern=_SHA256_RE)
    network_policy_sha256: str = Field(pattern=_SHA256_RE)
    identity_contract_sha256: str = Field(pattern=_SHA256_RE)
    effect_contract_sha256: str = Field(pattern=_SHA256_RE)
    operator_contract_sha256: str = Field(pattern=_SHA256_RE)
    evidence_identity: ProductionAcceptanceEvidenceIdentity
    campaign: QualificationCampaignBinding
    issuer: QualificationIssuer
    issued_at: str
    not_before: str
    expires_at: str

    @field_validator(
        "admission_id",
        "tenant_id",
        "workflow_id",
        "workflow_version_id",
        "bundle_version_id",
        "runtime_validation_id",
    )
    @classmethod
    def _canonical_uuid(cls, value: str, info: Any) -> str:
        return _uuid(value, field=info.field_name)

    @model_validator(mode="after")
    def _closed_payload(self) -> "QualificationAdmissionPayload":
        if self.admission_id == self.runtime_validation_id:
            raise ValueError(
                "qualification admission identity must be independent from "
                "runtime validation identity"
            )
        issued = _parse_utc(self.issued_at, field="issued_at")
        not_before = _parse_utc(self.not_before, field="not_before")
        expires = _parse_utc(self.expires_at, field="expires_at")
        if (
            not issued - timedelta(minutes=5)
            <= not_before
            <= issued + timedelta(minutes=5)
        ):
            raise ValueError("qualification admission start is outside issue skew")
        if expires <= not_before:
            raise ValueError("qualification admission expiry must follow its start")
        if expires > issued + MAX_ADMISSION_LIFETIME:
            raise ValueError("qualification admission lifetime exceeds 30 days")
        if (
            self.issuer.workflow
            != PRODUCTION_ACCEPTANCE_ADMISSION_POLICY["issuer_workflow"]
        ):
            raise ValueError("qualification issuer workflow does not match policy")
        expected_ref_prefix = PRODUCTION_ACCEPTANCE_ADMISSION_POLICY[
            "issuer_ref_prefix"
        ]
        if (
            re.fullmatch(
                re.escape(expected_ref_prefix) + r"[a-f0-9]{40}", self.issuer.ref
            )
            is None
        ):
            raise ValueError("qualification issuer ref does not match policy")
        identity = self.evidence_identity
        for field in (
            "admission_id",
            "tenant_id",
            "workflow_id",
            "workflow_version_id",
            "bundle_version_id",
            "runtime_validation_id",
            "bundle_artifact_sha256",
            "bundle_content_digest",
            "environment_digest",
            "governed_authorization_template_sha256",
            "application_contract_sha256",
            "substrate_contract_sha256",
            "environment_contract_sha256",
            "runtime_environment_sha256",
            "runtime_contract_sha256",
            "input_policy_sha256",
            "action_policy_sha256",
            "network_policy_sha256",
            "identity_contract_sha256",
            "effect_contract_sha256",
            "operator_contract_sha256",
        ):
            if getattr(self, field) != getattr(identity, field):
                raise ValueError(f"evidence identity {field} differs from admission")
        if (
            identity.campaign_id != self.campaign.campaign_id
            or identity.campaign_contract_sha256 != self.campaign.contract_sha256
            or identity.oracle_contract_sha256 != self.campaign.oracle_contract_sha256
        ):
            raise ValueError("evidence identity differs from qualification campaign")
        return self

    def canonical_bytes(self) -> bytes:
        return canonical_json(self)


class QualificationAdmissionEnvelope(ClosedSignedModel):
    payload: QualificationAdmissionPayload
    algorithm: Literal["ed25519"] = "ed25519"
    signature: str = Field(min_length=88, max_length=88)

    @field_validator("signature")
    @classmethod
    def _signature(cls, value: str) -> str:
        _canonical_base64(value, size=64, field="qualification admission signature")
        return value

    def artifact_sha256(self) -> str:
        return contract_sha256(self)


class QualificationSignerRegistryEntry(ClosedSignedModel):
    algorithm: Literal["ed25519"]
    key_id: str = Field(pattern=_KEY_ID_RE)
    public_key: str
    allowed_workflows: tuple[str, ...] = Field(min_length=1, max_length=128)
    allowed_ref_prefixes: tuple[str, ...] = Field(min_length=1, max_length=128)
    status: Literal["active", "revoked"]
    revoked_at: str | None

    @field_validator("public_key")
    @classmethod
    def _public_key(cls, value: str) -> str:
        _canonical_base64(value, size=32, field="qualification signer public key")
        return value

    @model_validator(mode="after")
    def _closed_signer(self) -> "QualificationSignerRegistryEntry":
        if (
            qualification_signer_key_id(b64decode(self.public_key, validate=True))
            != self.key_id
        ):
            raise ValueError("qualification signer key id does not match its key")
        for label, values in (
            ("workflow", self.allowed_workflows),
            ("ref prefix", self.allowed_ref_prefixes),
        ):
            if values != tuple(sorted(values)) or len(values) != len(set(values)):
                raise ValueError(f"qualification signer {label}s must be ordered")
            if any(_ID_RE.fullmatch(value) is None for value in values):
                raise ValueError(f"qualification signer {label} is invalid")
        if self.status == "active" and self.revoked_at is not None:
            raise ValueError("active qualification signer has a revocation time")
        if self.status == "revoked" and self.revoked_at is None:
            raise ValueError("revoked qualification signer lacks a revocation time")
        if self.revoked_at is not None:
            _parse_utc(self.revoked_at, field="qualification signer revoked_at")
        return self


class QualificationSignerRegistry(ClosedSignedModel):
    schema_version: Literal["openadapt.qualification-signer-registry/v2"] = (
        "openadapt.qualification-signer-registry/v2"
    )
    revision: StrictInt = Field(ge=1, le=JS_MAX_SAFE_INTEGER)
    generated_at: str
    expires_at: str
    signers: tuple[QualificationSignerRegistryEntry, ...] = Field(
        min_length=1, max_length=128
    )

    @model_validator(mode="after")
    def _closed_registry(self) -> "QualificationSignerRegistry":
        generated = _parse_utc(self.generated_at, field="registry generated_at")
        expires = _parse_utc(self.expires_at, field="registry expires_at")
        if expires <= generated or expires > generated + MAX_REGISTRY_LIFETIME:
            raise ValueError("qualification signer registry interval is invalid")
        key_ids = tuple(item.key_id for item in self.signers)
        if key_ids != tuple(sorted(key_ids)) or len(key_ids) != len(set(key_ids)):
            raise ValueError("qualification signer registry must be unique and ordered")
        for signer in self.signers:
            if (
                signer.revoked_at is not None
                and _parse_utc(
                    signer.revoked_at, field="qualification signer revoked_at"
                )
                > generated
            ):
                raise ValueError("qualification signer revocation time is invalid")
        return self

    def artifact_sha256(self) -> str:
        return _domain_sha256(SIGNER_REGISTRY_DOMAIN, self)


class QualificationAdmissionExpected(ClosedSignedModel):
    """Exact values reproduced from live state, not from the signed payload."""

    tenant_id: str
    workflow_id: str
    workflow_version_id: str
    bundle_version_id: str
    runtime_validation_id: str
    bundle_artifact_sha256: str = Field(pattern=_SHA256_RE)
    bundle_content_digest: str = Field(pattern=_SHA256_RE)
    environment_digest: str = Field(pattern=_SHA256_RE)
    governed_authorization_template_sha256: str = Field(pattern=_SHA256_RE)
    application_contract_sha256: str = Field(pattern=_SHA256_RE)
    substrate_contract_sha256: str = Field(pattern=_SHA256_RE)
    environment_contract_sha256: str = Field(pattern=_SHA256_RE)
    runtime_environment_sha256: str = Field(pattern=_SHA256_RE)
    runtime_contract_sha256: str = Field(pattern=_SHA256_RE)
    input_policy_sha256: str = Field(pattern=_SHA256_RE)
    action_policy_sha256: str = Field(pattern=_SHA256_RE)
    network_policy_sha256: str = Field(pattern=_SHA256_RE)
    identity_contract_sha256: str = Field(pattern=_SHA256_RE)
    effect_contract_sha256: str = Field(pattern=_SHA256_RE)
    operator_contract_sha256: str = Field(pattern=_SHA256_RE)
    runtime_build_identity: AdmittedRuntimeBuildIdentity
    evidence_runner_signer_sha256: str = Field(pattern=_SHA256_RE)
    deployment_manifest_sha256: str = Field(pattern=_SHA256_RE)

    @field_validator(
        "tenant_id",
        "workflow_id",
        "workflow_version_id",
        "bundle_version_id",
        "runtime_validation_id",
    )
    @classmethod
    def _canonical_uuid(cls, value: str, info: Any) -> str:
        return _uuid(value, field=info.field_name)


class QualificationPermitTrustSnapshot(ClosedSignedModel):
    """Registry state observed by the authority that issues an action permit."""

    qualification_signer_registry_sha256: str = Field(pattern=_SHA256_RE)
    qualification_signer_registry_revision: StrictInt = Field(
        ge=1, le=JS_MAX_SAFE_INTEGER
    )
    qualification_signer_registry_checked_at: str
    qualification_signer_registry_expires_at: str

    @model_validator(mode="after")
    def _canonical_times(self) -> "QualificationPermitTrustSnapshot":
        checked = _parse_utc(
            self.qualification_signer_registry_checked_at,
            field="qualification signer registry checked_at",
        )
        expires = _parse_utc(
            self.qualification_signer_registry_expires_at,
            field="qualification signer registry expires_at",
        )
        if checked >= expires:
            raise ValueError("qualification signer registry snapshot is expired")
        return self


class VerifiedQualificationAdmission(ClosedSignedModel):
    """Validated production authority returned only after all exact checks pass."""

    admission_artifact_sha256: str = Field(pattern=_SHA256_RE)
    evidence_identity_sha256: str = Field(pattern=_SHA256_RE)
    registry_sha256: str = Field(pattern=_SHA256_RE)
    registry_revision: StrictInt = Field(ge=1, le=JS_MAX_SAFE_INTEGER)
    registry_expires_at: str
    issuer_key_id: str = Field(pattern=_KEY_ID_RE)
    admission_id: str
    runtime_validation_id: str

    @field_validator("admission_id", "runtime_validation_id")
    @classmethod
    def _canonical_uuid(cls, value: str, info: Any) -> str:
        return _uuid(value, field=info.field_name)


_EXPECTED_PAYLOAD_FIELDS: Final[tuple[str, ...]] = (
    "tenant_id",
    "workflow_id",
    "workflow_version_id",
    "bundle_version_id",
    "runtime_validation_id",
    "bundle_artifact_sha256",
    "bundle_content_digest",
    "environment_digest",
    "governed_authorization_template_sha256",
    "application_contract_sha256",
    "substrate_contract_sha256",
    "environment_contract_sha256",
    "runtime_environment_sha256",
    "runtime_contract_sha256",
    "input_policy_sha256",
    "action_policy_sha256",
    "network_policy_sha256",
    "identity_contract_sha256",
    "effect_contract_sha256",
    "operator_contract_sha256",
)


def load_qualification_signer_registry(
    value: str | bytes | Mapping[str, Any],
) -> QualificationSignerRegistry:
    """Load one closed v2 registry from a trusted local or control-plane source."""

    try:
        raw = json.loads(value) if isinstance(value, (str, bytes)) else dict(value)
        return QualificationSignerRegistry.model_validate(raw)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise QualificationAdmissionError(
            "qualification signer registry is invalid"
        ) from exc


def sign_qualification_admission(
    payload: QualificationAdmissionPayload,
    private_key: Ed25519PrivateKey,
) -> QualificationAdmissionEnvelope:
    """Sign one reviewed v2 production qualification decision."""

    signature = private_key.sign(SIGNATURE_DOMAIN + payload.canonical_bytes())
    return QualificationAdmissionEnvelope(
        payload=payload,
        signature=b64encode(signature).decode("ascii"),
    )


def _verify_registry(
    registry: QualificationSignerRegistry,
    *,
    now: datetime,
) -> None:
    generated = _parse_utc(registry.generated_at, field="registry generated_at")
    expires = _parse_utc(registry.expires_at, field="registry expires_at")
    if generated > now + timedelta(minutes=5):
        raise QualificationAdmissionError(
            "qualification signer registry is future-issued"
        )
    if now >= expires:
        raise QualificationAdmissionError("qualification signer registry has expired")


def verify_qualification_admission(
    envelope: QualificationAdmissionEnvelope,
    *,
    registry: QualificationSignerRegistry,
    expected: QualificationAdmissionExpected,
    revoked_admission_ids: set[str] | frozenset[str] = frozenset(),
    now: datetime | None = None,
) -> VerifiedQualificationAdmission:
    """Verify v2 production authority against independent live values."""

    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    _verify_registry(registry, now=current)
    payload = envelope.payload
    identity = payload.evidence_identity
    registry_sha256 = registry.artifact_sha256()
    if identity.qualification_signer_registry_sha256 != registry_sha256:
        raise QualificationAdmissionError(
            "qualification admission signer registry digest does not match"
        )
    if identity.qualification_signer_registry_revision != registry.revision:
        raise QualificationAdmissionError(
            "qualification admission signer registry revision does not match"
        )
    signer = next(
        (item for item in registry.signers if item.key_id == payload.issuer.key_id),
        None,
    )
    if signer is None:
        raise QualificationAdmissionError("qualification signer is not trusted")
    if signer.status != "active":
        raise QualificationAdmissionError("qualification signer is revoked")
    if payload.issuer.workflow not in signer.allowed_workflows:
        raise QualificationAdmissionError(
            "qualification issuer workflow is not allowed"
        )
    if not any(
        payload.issuer.ref.startswith(prefix) for prefix in signer.allowed_ref_prefixes
    ):
        raise QualificationAdmissionError("qualification issuer ref is not allowed")
    try:
        public_key = Ed25519PublicKey.from_public_bytes(
            b64decode(signer.public_key, validate=True)
        )
        public_key.verify(
            b64decode(envelope.signature, validate=True),
            SIGNATURE_DOMAIN + payload.canonical_bytes(),
        )
    except (InvalidSignature, ValueError) as exc:
        raise QualificationAdmissionError(
            "qualification admission signature is invalid"
        ) from exc

    issued = _parse_utc(payload.issued_at, field="issued_at")
    not_before = _parse_utc(payload.not_before, field="not_before")
    expires = _parse_utc(payload.expires_at, field="expires_at")
    if issued > current + timedelta(minutes=5):
        raise QualificationAdmissionError("qualification admission is future-issued")
    if current < not_before:
        raise QualificationAdmissionError("qualification admission is not active")
    if current >= expires:
        raise QualificationAdmissionError("qualification admission has expired")
    if payload.admission_id in revoked_admission_ids:
        raise QualificationAdmissionError("qualification admission is revoked")

    for field in _EXPECTED_PAYLOAD_FIELDS:
        if getattr(payload, field) != getattr(expected, field):
            raise QualificationAdmissionError(
                f"qualification admission {field} does not match the live run"
            )
    if identity.runtime_build_identity != expected.runtime_build_identity:
        raise QualificationAdmissionError(
            "qualification admission runtime build does not match the live run"
        )
    if identity.evidence_runner_signer_sha256 != expected.evidence_runner_signer_sha256:
        raise QualificationAdmissionError(
            "qualification admission evidence signer does not match the live run"
        )
    if identity.deployment_manifest_sha256 != expected.deployment_manifest_sha256:
        raise QualificationAdmissionError(
            "qualification admission deployment manifest does not match the live run"
        )
    if identity.environment_digest != expected.environment_digest:
        raise QualificationAdmissionError(
            "qualification admission environment does not match the live run"
        )
    return VerifiedQualificationAdmission(
        admission_artifact_sha256=envelope.artifact_sha256(),
        evidence_identity_sha256=identity.artifact_sha256(),
        registry_sha256=registry_sha256,
        registry_revision=registry.revision,
        registry_expires_at=registry.expires_at,
        issuer_key_id=payload.issuer.key_id,
        admission_id=payload.admission_id,
        runtime_validation_id=payload.runtime_validation_id,
    )


def verify_qualification_admission_for_actuation(
    envelope: QualificationAdmissionEnvelope,
    *,
    registry: QualificationSignerRegistry,
    expected: QualificationAdmissionExpected,
    permit_trust_snapshot: QualificationPermitTrustSnapshot,
    revoked_admission_ids: set[str] | frozenset[str] = frozenset(),
    now: datetime | None = None,
) -> VerifiedQualificationAdmission:
    """Reverify authority and its current registry immediately before action."""

    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    verified = verify_qualification_admission(
        envelope,
        registry=registry,
        expected=expected,
        revoked_admission_ids=revoked_admission_ids,
        now=current,
    )
    checked = _parse_utc(
        permit_trust_snapshot.qualification_signer_registry_checked_at,
        field="qualification signer registry checked_at",
    )
    snapshot_expires = _parse_utc(
        permit_trust_snapshot.qualification_signer_registry_expires_at,
        field="qualification signer registry expires_at",
    )
    if checked > current + timedelta(seconds=5):
        raise QualificationAdmissionError(
            "qualification signer registry snapshot is future-issued"
        )
    if current - checked > MAX_PERMIT_SNAPSHOT_AGE:
        raise QualificationAdmissionError(
            "qualification signer registry snapshot is stale"
        )
    if current >= snapshot_expires:
        raise QualificationAdmissionError(
            "qualification signer registry snapshot has expired"
        )
    expected_snapshot = (
        verified.registry_sha256,
        verified.registry_revision,
        verified.registry_expires_at,
    )
    actual_snapshot = (
        permit_trust_snapshot.qualification_signer_registry_sha256,
        permit_trust_snapshot.qualification_signer_registry_revision,
        permit_trust_snapshot.qualification_signer_registry_expires_at,
    )
    if actual_snapshot != expected_snapshot:
        raise QualificationAdmissionError(
            "qualification signer registry snapshot does not match"
        )
    return verified


class QualificationSignerRegistryCheckpoint(ClosedSignedModel):
    """Durable registry state retained in the authority transaction."""

    schema_version: Literal["openadapt.qualification-signer-checkpoint/v1"] = (
        "openadapt.qualification-signer-checkpoint/v1"
    )
    revision: StrictInt = Field(ge=1, le=JS_MAX_SAFE_INTEGER)
    artifact_sha256: str = Field(pattern=_SHA256_RE)
    generated_at: str

    @field_validator("generated_at")
    @classmethod
    def _generated_at(cls, value: str) -> str:
        _parse_utc(value, field="registry checkpoint generated_at")
        return value


def _registry_checkpoint(
    registry: QualificationSignerRegistry,
) -> QualificationSignerRegistryCheckpoint:
    return QualificationSignerRegistryCheckpoint(
        revision=registry.revision,
        artifact_sha256=registry.artifact_sha256(),
        generated_at=registry.generated_at,
    )


def initialize_qualification_signer_registry_checkpoint(
    registry: QualificationSignerRegistry,
    *,
    now: datetime | None = None,
) -> QualificationSignerRegistryCheckpoint:
    """Provision the first durable checkpoint for one signer trust domain.

    This is a provisioning-only operation.  A runtime never calls it: a runtime
    that finds no durable checkpoint must halt (see
    :func:`validate_qualification_signer_registry_transition`).  The caller
    must persist the returned checkpoint before any admission or permit can be
    granted under this registry.
    """

    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    _verify_registry(registry, now=current)
    return _registry_checkpoint(registry)


def validate_qualification_signer_registry_transition(
    registry: QualificationSignerRegistry,
    *,
    prior_checkpoint: QualificationSignerRegistryCheckpoint,
    now: datetime | None = None,
) -> QualificationSignerRegistryCheckpoint:
    """Validate one registry against the durable checkpoint and return the next.

    The caller must lock and persist the returned checkpoint in the same
    transaction that retains the admission or action authority.  One checkpoint
    exists per production signer trust domain, not per run or tenant.  An
    in-memory cache cannot provide rollback protection across a restart, so
    ``prior_checkpoint`` is required; a missing durable checkpoint halts.

    Rules:

    * a lower revision is a rollback and is refused;
    * the same revision must present the complete same checkpoint (revision,
      artifact digest, and generation time) or it is equivocation;
    * a higher revision must not carry an earlier generation time;
    * an expired or future-dated registry never advances the checkpoint.
    """

    if not isinstance(prior_checkpoint, QualificationSignerRegistryCheckpoint):
        raise QualificationAdmissionError(
            "qualification signer registry checkpoint is absent; the runtime "
            "cannot admit without its durable trust checkpoint"
        )
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    _verify_registry(registry, now=current)
    checkpoint = _registry_checkpoint(registry)
    if checkpoint.revision < prior_checkpoint.revision:
        raise QualificationAdmissionError(
            "qualification signer registry revision rolled back"
        )
    if checkpoint.revision == prior_checkpoint.revision:
        if checkpoint != prior_checkpoint:
            raise QualificationAdmissionError(
                "qualification signer registry revision changed content"
            )
        return checkpoint
    if _parse_utc(
        checkpoint.generated_at, field="registry checkpoint generated_at"
    ) < _parse_utc(
        prior_checkpoint.generated_at,
        field="prior registry checkpoint generated_at",
    ):
        raise QualificationAdmissionError(
            "qualification signer registry generation time rolled back"
        )
    return checkpoint


def load_protected_qualification_signer_registry(
    path: str | Path,
    *,
    prior_checkpoint: QualificationSignerRegistryCheckpoint,
    now: datetime | None = None,
) -> tuple[QualificationSignerRegistry, QualificationSignerRegistryCheckpoint]:
    """Load a protected file and return the registry plus its next checkpoint.

    The caller persists the returned checkpoint before it uses the registry.
    """

    registry = _load_protected_registry_file(path)
    checkpoint = validate_qualification_signer_registry_transition(
        registry,
        prior_checkpoint=prior_checkpoint,
        now=now,
    )
    return registry, checkpoint


def _protected_owner_and_mode(status: os.stat_result, *, label: str) -> None:
    if os.name == "nt":
        # POSIX mode bits and the owner fallback do not establish a Windows
        # ACL. Fail closed instead of claiming a protection this loader cannot
        # verify; a Windows runner receives its registry from the control plane.
        raise QualificationAdmissionError(
            f"{label} protection cannot be verified on this platform"
        )
    if status.st_mode & 0o077:
        raise QualificationAdmissionError(f"{label} permissions are unsafe")
    if status.st_uid not in {0, os.geteuid()}:
        raise QualificationAdmissionError(f"{label} owner is unsafe")


def _read_protected_file(path: str | Path, *, label: str, limit: int) -> bytes:
    resolved = Path(path)
    try:
        before = resolved.lstat()
    except OSError as exc:
        raise QualificationAdmissionError(f"{label} is unavailable") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise QualificationAdmissionError(f"{label} path is not a regular file")
    _protected_owner_and_mode(before, label=label)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(resolved, flags)
        try:
            after = os.fstat(descriptor)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise QualificationAdmissionError(f"{label} changed while loading")
            if not stat.S_ISREG(after.st_mode):
                raise QualificationAdmissionError(f"{label} path is not a regular file")
            # Recheck from the open descriptor: the pre-open lstat can race.
            _protected_owner_and_mode(after, label=label)
            if after.st_size > limit:
                raise QualificationAdmissionError(f"{label} is too large")
            value = os.read(descriptor, after.st_size + 1)
            if len(value) != after.st_size:
                raise QualificationAdmissionError(f"{label} changed while loading")
        finally:
            os.close(descriptor)
    except QualificationAdmissionError:
        raise
    except OSError as exc:
        raise QualificationAdmissionError(f"{label} could not be read safely") from exc
    return value


def _load_protected_registry_file(path: str | Path) -> QualificationSignerRegistry:
    value = _read_protected_file(
        path, label="qualification signer registry file", limit=1_000_000
    )
    return load_qualification_signer_registry(value)


def read_qualification_signer_checkpoint_file(
    path: str | Path,
) -> QualificationSignerRegistryCheckpoint:
    """Read the durable local checkpoint; a missing or unsafe file halts."""

    value = _read_protected_file(
        path, label="qualification signer checkpoint file", limit=65_536
    )
    try:
        return QualificationSignerRegistryCheckpoint.model_validate_json(value)
    except ValueError as exc:
        raise QualificationAdmissionError(
            "qualification signer checkpoint file is invalid"
        ) from exc


def _write_private_file(path: Path, payload: bytes, *, exclusive: bool) -> None:
    if os.name == "nt":
        raise QualificationAdmissionError(
            "qualification signer checkpoint file protection cannot be "
            "verified on this platform"
        )
    if exclusive:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError as exc:
            raise QualificationAdmissionError(
                "qualification signer checkpoint already exists"
            ) from exc
        except OSError as exc:
            raise QualificationAdmissionError(
                "qualification signer checkpoint could not be created"
            ) from exc
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(temporary, flags, 0o600)
    except OSError as exc:
        raise QualificationAdmissionError(
            "qualification signer checkpoint could not be written"
        ) from exc
    try:
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise QualificationAdmissionError(
            "qualification signer checkpoint could not be written"
        ) from exc


def initialize_local_qualification_signer_checkpoint(
    registry_path: str | Path,
    checkpoint_path: str | Path,
    *,
    now: datetime | None = None,
) -> QualificationSignerRegistryCheckpoint:
    """Provision the durable local checkpoint once; refuse if it exists."""

    registry = _load_protected_registry_file(registry_path)
    checkpoint = initialize_qualification_signer_registry_checkpoint(registry, now=now)
    _write_private_file(
        Path(checkpoint_path), canonical_json(checkpoint), exclusive=True
    )
    return checkpoint


def load_local_qualification_signer_registry(
    registry_path: str | Path,
    checkpoint_path: str | Path,
    *,
    now: datetime | None = None,
) -> tuple[QualificationSignerRegistry, QualificationSignerRegistryCheckpoint]:
    """Load the local registry against its durable checkpoint and advance it.

    A missing checkpoint halts.  The next checkpoint is persisted before the
    registry is returned, so a later rollback of the registry file cannot
    authorize an action.
    """

    prior = read_qualification_signer_checkpoint_file(checkpoint_path)
    registry, checkpoint = load_protected_qualification_signer_registry(
        registry_path, prior_checkpoint=prior, now=now
    )
    if checkpoint != prior:
        _write_private_file(
            Path(checkpoint_path), canonical_json(checkpoint), exclusive=False
        )
    return registry, checkpoint
