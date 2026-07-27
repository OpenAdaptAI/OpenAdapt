"""Typed, PHI-free capability evidence for Desktop qualification runs.

Qualification requirements are operator-authored policy.  They are never proof
that a runner actually supplied a capability.  This module derives a closed set
of capability names only from the exact retained Flow run report.  The
resulting receipt is stored beside the qualification evidence and its hash is
covered by the runner's case-result signature.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, cast

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

QualificationTargetKind = Literal["web", "windows", "macos", "linux", "rdp", "citrix"]
CapabilitySource = Literal[
    "execution_report",
    "resolution",
    "actuation",
    "identity",
    "postcondition",
    "effect_verifier",
    "execution_profile",
]
KnownRunnerCapability = Literal[
    "actuation",
    "application_identity",
    "effect_verification",
    "governed_authorization",
    "identity_verification",
    "immediate_screen_confirmation",
    "independent_session",
    "independent_system_of_record",
    "persisted_state_reacquisition",
    "pixel_observation",
    "playwright_dom",
    "postcondition_verification",
    "session_continuity",
    "settled_state_detection",
    "structural_observation",
    "workflow_state_identity",
]


class ObservedRunnerCapability(BaseModel):
    """One capability derived from one typed report field."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: KnownRunnerCapability
    source: CapabilitySource


class QualificationCapabilityObservation(BaseModel):
    """Bounded capability receipt for one exact local qualification run."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: Literal["openadapt.qualification-capability-observation/v1"] = Field(
        "openadapt.qualification-capability-observation/v1",
        alias="schema",
    )
    report_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_target_kind: QualificationTargetKind
    observed_target_kind: QualificationTargetKind | None = None
    target_kind_matches: bool = False
    execution_profile: Literal["demo", "standard", "regulated"] | None = None
    runtime_version: str = Field(min_length=1, max_length=64)
    observations: list[ObservedRunnerCapability] = Field(default_factory=list)

    @model_validator(mode="after")
    def _canonical_observations(self) -> "QualificationCapabilityObservation":
        pairs = [(item.name, item.source) for item in self.observations]
        if len(pairs) != len(set(pairs)):
            raise ValueError("capability observations must be unique")
        self.observations = sorted(
            self.observations,
            key=lambda item: (item.name, item.source),
        )
        if self.target_kind_matches != (self.observed_target_kind == self.expected_target_kind):
            raise ValueError("target_kind_matches contradicts the observed target")
        return self

    @property
    def observed_capabilities(self) -> list[str]:
        return sorted({item.name for item in self.observations})


class SignedQualificationCapabilityObservation(QualificationCapabilityObservation):
    """Capability receipt bound to one exact project, workflow, and run."""

    project_id: str = Field(min_length=1)
    project_revision: int = Field(ge=1)
    project_contract_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    workflow_contract_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    environment_contract_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    environment_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    case_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    observed_at: AwareDatetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    attestation_key_id: str = Field(min_length=1)
    attestation_signature: str = Field(min_length=1)


def _signature_payload(observation: SignedQualificationCapabilityObservation) -> bytes:
    payload = observation.model_dump(
        mode="json",
        by_alias=True,
        exclude={"attestation_signature"},
    )
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def sign_qualification_capability_observation(
    observation: QualificationCapabilityObservation,
    *,
    project_id: str,
    project_revision: int,
    project_contract_sha256: str,
    workflow_contract_sha256: str,
    environment_contract_sha256: str,
    environment_digest: str,
    case_id: str,
    run_id: str,
    attestation_key_id: str,
    private_key: bytes,
) -> SignedQualificationCapabilityObservation:
    """Bind and sign an observation without copying application data."""

    unsigned = {
        **observation.model_dump(mode="json", by_alias=False),
        "project_id": project_id,
        "project_revision": project_revision,
        "project_contract_sha256": project_contract_sha256,
        "workflow_contract_sha256": workflow_contract_sha256,
        "environment_contract_sha256": environment_contract_sha256,
        "environment_digest": environment_digest,
        "case_id": case_id,
        "run_id": run_id,
        "attestation_key_id": attestation_key_id,
        "attestation_signature": "pending",
    }
    bound = SignedQualificationCapabilityObservation.model_validate(unsigned)
    signature = Ed25519PrivateKey.from_private_bytes(private_key).sign(_signature_payload(bound))
    return bound.model_copy(
        update={"attestation_signature": base64.b64encode(signature).decode("ascii")}
    )


def verify_qualification_capability_observation(
    observation: SignedQualificationCapabilityObservation,
    *,
    public_key_base64: str,
) -> bool:
    """Verify the Desktop runner's Ed25519 capability attestation."""

    try:
        public_key = Ed25519PublicKey.from_public_bytes(
            base64.b64decode(public_key_base64, validate=True)
        )
        public_key.verify(
            base64.b64decode(observation.attestation_signature, validate=True),
            _signature_payload(observation),
        )
    except (InvalidSignature, ValueError):
        return False
    return True


def current_signed_capability_observations(
    bundle_dir: Path,
    *,
    workflow_contract_sha256: str,
    project: Any,
) -> dict[str, SignedQualificationCapabilityObservation]:
    """Load the latest valid receipt for each current-revision required case."""

    current: dict[str, SignedQualificationCapabilityObservation] = {}
    evidence_root = bundle_dir / "qualification-evidence"
    if not evidence_root.is_dir():
        return current
    root = evidence_root.resolve()
    project_contract_sha256 = project.contract_sha256()
    environment_contract_sha256 = project.environment.contract_sha256()
    for path in evidence_root.glob("*/*/capability-observation.json"):
        try:
            if path.is_symlink() or any(
                parent.is_symlink() for parent in path.parents if parent != evidence_root.parent
            ):
                continue
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(root) or not resolved.is_file():
                continue
            observation = SignedQualificationCapabilityObservation.model_validate_json(
                resolved.read_text(encoding="utf-8")
            )
            public_key = project.trusted_runner_keys.get(observation.attestation_key_id)
            run_receipt_path = resolved.with_name("run-report-receipt.json")
            if run_receipt_path.is_symlink() or not run_receipt_path.is_file():
                continue
            run_receipt = json.loads(run_receipt_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if (
            observation.project_id != project.project_id
            or observation.project_revision != project.revision
            or observation.project_contract_sha256 != project_contract_sha256
            or observation.workflow_contract_sha256 != workflow_contract_sha256
            or observation.environment_contract_sha256 != environment_contract_sha256
            or observation.environment_digest != project.environment.environment_digest
            or observation.runtime_version != project.environment.runtime_version
            or observation.expected_target_kind != project.environment.target_kind
            or (
                project.environment.required_capabilities
                and observation.execution_profile not in {"standard", "regulated"}
            )
            or observation.case_id != path.parent.parent.name
            or observation.run_id != path.parent.name
            or run_receipt.get("schema") != "openadapt.qualification-run-receipt/v1"
            or run_receipt.get("run_id") != observation.run_id
            or run_receipt.get("report_sha256") != observation.report_sha256
            or not public_key
            or not verify_qualification_capability_observation(
                observation,
                public_key_base64=public_key,
            )
        ):
            continue
        previous = current.get(observation.case_id)
        if previous is None or observation.observed_at > previous.observed_at:
            current[observation.case_id] = observation
    return current


def _as_mapping(value: object) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _as_list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def collect_qualification_capabilities(
    report: Mapping[str, Any],
    *,
    expected_target_kind: QualificationTargetKind,
    runtime_version: str,
    report_sha256: str,
    action_kinds: Mapping[str, str],
) -> QualificationCapabilityObservation:
    """Derive capabilities from actual typed run evidence, never requirements.

    ``action_kinds`` is the compiled workflow's step-id to action-kind map.  It
    lets a successful physical-input step prove actuation without treating a
    successful WAIT as an action-delivery receipt.
    """

    from openadapt_flow.ir import RunReport

    # Capability evidence is a certification input, so derive it only from
    # Flow's closed report schema.  A plausible-looking arbitrary mapping must
    # not be able to mint identity, effect, or actuation capabilities.
    report = RunReport.model_validate(report).model_dump(mode="json")

    raw_target = report.get("execution_target_kind")
    observed_target: QualificationTargetKind | None = (
        cast(QualificationTargetKind, raw_target)
        if raw_target in {"web", "windows", "macos", "linux", "rdp", "citrix"}
        else None
    )
    target_matches = observed_target == expected_target_kind
    raw_profile = report.get("execution_profile")
    profile = raw_profile if raw_profile in {"demo", "standard", "regulated"} else None
    observations: set[tuple[KnownRunnerCapability, CapabilitySource]] = set()

    def add(name: KnownRunnerCapability, source: CapabilitySource) -> None:
        # A report for a different substrate is not evidence for this qualified
        # environment.  Runtime/profile evidence is likewise bound to the exact
        # target before entering a signed qualification receipt.
        if target_matches:
            observations.add((name, source))

    envelope = _as_mapping(report.get("outcome_envelope"))
    if envelope is not None:
        evidence_classes = set(_as_list(envelope.get("evidence_classes")))
        if "authorization" in evidence_classes:
            add("governed_authorization", "execution_report")
        if profile in {"standard", "regulated"}:
            # Flow's named execution-profile admission contract requires
            # settled-state detection.  The retained profile is therefore the
            # typed runtime result, not a Desktop assumption about configuration.
            add("settled_state_detection", "execution_profile")

    results = _as_list(report.get("results"))
    for raw_result in results:
        result = _as_mapping(raw_result)
        if result is None or result.get("skipped") is True:
            continue
        step_id = result.get("step_id")

        for resolution_name in ("resolution", "drag_end_resolution"):
            resolution = _as_mapping(result.get(resolution_name))
            if resolution is None:
                continue
            rung = resolution.get("rung")
            if rung == "structural":
                add("structural_observation", "resolution")
                if observed_target == "web":
                    add("playwright_dom", "resolution")
            elif rung in {"template", "ocr", "geometry", "model"}:
                add("pixel_observation", "resolution")

        if result.get("before_png") or result.get("after_png"):
            add("pixel_observation", "execution_report")

        action_kind = action_kinds.get(str(step_id), "")
        delivered = (
            _as_mapping(result.get("delivery_receipt")) is not None
            or _as_mapping(result.get("delivery_uncertainty")) is not None
            or result.get("actuation") == "api"
            or (result.get("ok") is True and action_kind not in {"", "wait"})
        )
        if delivered:
            add("actuation", "actuation")

        identity = _as_mapping(result.get("identity"))
        if identity is not None:
            add("identity_verification", "identity")
            for raw_signal in _as_list(identity.get("signal_evidence")):
                signal = _as_mapping(raw_signal)
                if signal is None:
                    continue
                source = signal.get("source")
                if source == "session":
                    add("session_continuity", "identity")
                elif source == "application":
                    add("application_identity", "identity")
                elif source == "workflow_state":
                    add("workflow_state_identity", "identity")

        if isinstance(result.get("postconditions_ok"), bool):
            add("postcondition_verification", "postcondition")

        for raw_effect in _as_list(result.get("effect_evidence")):
            effect = _as_mapping(raw_effect)
            if effect is None or not isinstance(effect.get("verification_tier"), int):
                continue
            add("effect_verification", "effect_verifier")
            tier = effect["verification_tier"]
            if tier == 1:
                add("independent_system_of_record", "effect_verifier")
            elif tier == 2:
                add("independent_session", "effect_verifier")
            elif tier == 3:
                add("persisted_state_reacquisition", "effect_verifier")
            elif tier == 4:
                add("immediate_screen_confirmation", "effect_verifier")

    return QualificationCapabilityObservation(
        report_sha256=report_sha256,
        expected_target_kind=expected_target_kind,
        observed_target_kind=observed_target,
        target_kind_matches=target_matches,
        execution_profile=profile,
        runtime_version=runtime_version,
        observations=[
            ObservedRunnerCapability(name=name, source=source) for name, source in observations
        ],
    )
