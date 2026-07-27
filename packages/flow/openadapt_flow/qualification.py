"""Versioned qualification-project contract for governed workflow bundles.

The compiled :class:`~openadapt_flow.ir.Workflow` remains the executable source
of truth.  A qualification project does not copy its actions, identities, or
effects; it attaches review policy and evidence to those existing contracts by
stable step id and effect-contract hash.

The models in this module intentionally contain no customer values, screenshots,
or verifier credentials.  Case inputs and evidence are references plus digests,
so the project can be sealed into the bundle without creating a second data
egress path.
"""

from __future__ import annotations

import hashlib
import json
import re
from base64 import b64decode, b64encode
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Callable, Final, Iterable, Literal, Optional
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from openadapt_flow.identity_signals import (
    canonical_normalizers,
    parameterize_identity_text,
    signal_hash_key,
)
from openadapt_flow.verification import VerificationTier

if TYPE_CHECKING:  # pragma: no cover
    from openadapt_flow.ir import IdentityTemplate, Step, Workflow
    from openadapt_flow.policy import Policy


QUALIFICATION_SCHEMA: Final[Literal["openadapt.qualification-project/v1"]] = (
    "openadapt.qualification-project/v1"
)
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PARAM_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_CONTEXT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


def _valid_application_identity(value: str) -> bool:
    """Accept a native app id or the exact bounded HTTP(S) origin observer emits."""

    if "://" not in value:
        return _CONTEXT_ID_RE.fullmatch(value) is not None
    try:
        parts = urlsplit(value)
        scheme = parts.scheme.lower()
        hostname = parts.hostname
        port = parts.port
    except (AttributeError, TypeError, ValueError):
        return False
    if (
        scheme not in {"http", "https"}
        or not hostname
        or parts.username is not None
        or parts.password is not None
        or parts.path
        or parts.query
        or parts.fragment
    ):
        return False
    rendered_host = (
        f"[{hostname.lower().rstrip('.')}]"
        if ":" in hostname
        else hostname.lower().rstrip(".")
    )
    origin = f"{scheme}://{rendered_host}"
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        origin += f":{port}"
    return value == origin and len(value) <= 320


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class IdentityEvidenceSource(str, Enum):
    STRUCTURED = "structured"
    IDENTIFIER_REGION = "identifier_region"
    CAPTURED_CONTEXT = "captured_context"
    APPLICATION = "application"
    SESSION = "session"
    WORKFLOW_STATE = "workflow_state"


class IdentityMatchMode(str, Enum):
    EXACT = "exact"
    NORMALIZED = "normalized"


class IdentityNormalizer(str, Enum):
    """Explicit, bounded transforms permitted for a normalized comparison."""

    UNICODE_NFKC = "unicode_nfkc"
    CASEFOLD = "casefold"
    COLLAPSE_WHITESPACE = "collapse_whitespace"
    STRIP_PUNCTUATION = "strip_punctuation"


class IdentitySignalKey(str, Enum):
    """Closed, PHI-free semantic keys for qualified identity evidence."""

    SUBJECT_NAME = "subject_name"
    RECORD_ID = "record_id"
    SECONDARY_IDENTIFIER = "secondary_identifier"
    APPLICATION = "application"
    SESSION = "session"
    WORKFLOW_STATE = "workflow_state"


class IdentityEnforcement(str, Enum):
    """Whether the policy names shipped runtime behavior or future intent."""

    CANONICAL_LADDER = "canonical_ladder"
    SIGNAL_QUORUM = "signal_quorum"


class IdentitySignalPolicy(BaseModel):
    """How one semantic identity signal is compared using retained evidence."""

    model_config = ConfigDict(extra="forbid")

    key: IdentitySignalKey
    source: IdentityEvidenceSource
    match: IdentityMatchMode = IdentityMatchMode.EXACT
    normalizers: list[IdentityNormalizer] = Field(default_factory=list)
    region: Optional[tuple[int, int, int, int]] = None
    extract_pattern: Optional[str] = Field(
        default=None,
        max_length=256,
        description=(
            "Optional explicit regular expression with one named 'value' group. "
            "For structured/context text, only that field participates in the "
            "identity vote; the semantic key alone never extracts a value."
        ),
    )
    expected_value: Optional[str] = Field(
        default=None,
        max_length=320,
        description=(
            "Qualified PHI-free identifier expected from a dedicated "
            "application/session/workflow-state runtime observer."
        ),
    )
    params: list[str] = Field(
        default_factory=list,
        description=(
            "Explicit workflow parameters whose whole demonstrated values are "
            "replaced before comparison. No implicit substring inference occurs."
        ),
    )

    @field_validator("params")
    @classmethod
    def _clean_params(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("identity signal parameters must be unique")
        if any(not _PARAM_RE.fullmatch(value) for value in values):
            raise ValueError("identity signal parameters must be safe parameter names")
        return values

    @model_validator(mode="after")
    def _normalization_is_explicit(self) -> "IdentitySignalPolicy":
        dedicated_sources = {
            IdentitySignalKey.APPLICATION: IdentityEvidenceSource.APPLICATION,
            IdentitySignalKey.SESSION: IdentityEvidenceSource.SESSION,
            IdentitySignalKey.WORKFLOW_STATE: IdentityEvidenceSource.WORKFLOW_STATE,
        }
        required_source = dedicated_sources.get(self.key)
        if required_source is not None and self.source is not required_source:
            raise ValueError(
                f"semantic signal {self.key.value!r} requires dedicated "
                f"{required_source.value!r} runtime observation"
            )
        if required_source is not None:
            if self.expected_value is None:
                raise ValueError(
                    f"dedicated {required_source.value!r} observation requires "
                    "an explicit expected_value"
                )
            if self.key is IdentitySignalKey.SESSION:
                if not re.fullmatch(r"[a-f0-9]{64}", self.expected_value):
                    raise ValueError(
                        "session expected_value must be a 64-character "
                        "lowercase hexadecimal identity digest"
                    )
            elif (
                self.key is IdentitySignalKey.APPLICATION
                and not _valid_application_identity(self.expected_value)
            ):
                raise ValueError(
                    "application expected_value must be a bounded PHI-free "
                    "identifier or canonical HTTP(S) origin"
                )
            elif (
                self.key is IdentitySignalKey.WORKFLOW_STATE
                and not _CONTEXT_ID_RE.fullmatch(self.expected_value)
            ):
                raise ValueError(
                    "workflow_state expected_value must be a bounded PHI-free "
                    "identifier"
                )
        if required_source is None and self.source in set(dedicated_sources.values()):
            raise ValueError(
                f"record identity signal {self.key.value!r} cannot use dedicated "
                f"{self.source.value!r} runtime observation"
            )
        if required_source is None and self.expected_value is not None:
            raise ValueError(
                "expected_value applies only to dedicated "
                "application/session/workflow-state observations"
            )
        if (
            self.source
            in {
                IdentityEvidenceSource.STRUCTURED,
                IdentityEvidenceSource.CAPTURED_CONTEXT,
            }
            and self.extract_pattern is None
        ):
            raise ValueError(
                "structured/context identity signals require extract_pattern; "
                "a semantic key alone cannot turn unrelated text into evidence"
            )
        if self.extract_pattern is not None:
            if self.source not in {
                IdentityEvidenceSource.STRUCTURED,
                IdentityEvidenceSource.CAPTURED_CONTEXT,
            }:
                raise ValueError(
                    "extract_pattern applies only to structured/context text"
                )
            try:
                compiled = re.compile(self.extract_pattern)
            except re.error as exc:
                raise ValueError(f"invalid identity extract_pattern: {exc}") from exc
            if compiled.groupindex != {"value": 1} or compiled.groups != 1:
                raise ValueError(
                    "identity extract_pattern must contain exactly one named "
                    "'value' capture group"
                )
        if self.match is IdentityMatchMode.EXACT and self.normalizers:
            raise ValueError("exact identity matching cannot apply normalizers")
        if self.match is IdentityMatchMode.NORMALIZED and not self.normalizers:
            raise ValueError(
                "normalized identity matching requires at least one explicit normalizer"
            )
        canonical_normalizers(self.normalizers)
        if self.source in set(dedicated_sources.values()) and (
            self.params or self.region is not None
        ):
            raise ValueError(
                "dedicated application/session/workflow-state observations do "
                "not accept params or pixel regions"
            )
        if self.source in {
            IdentityEvidenceSource.IDENTIFIER_REGION,
            IdentityEvidenceSource.CAPTURED_CONTEXT,
        }:
            if self.region is None:
                raise ValueError(
                    "pixel identity evidence requires an explicit qualified region"
                )
            if self.region[2] <= 0 or self.region[3] <= 0:
                raise ValueError("identity region width and height must be positive")
        elif self.region is not None:
            raise ValueError("region applies only to pixel identity evidence")
        return self


class IdentityPolicy(BaseModel):
    """Quorum policy for one canonical Flow action."""

    model_config = ConfigDict(extra="forbid")

    step_id: str
    enforcement: IdentityEnforcement = IdentityEnforcement.SIGNAL_QUORUM
    signals: list[IdentitySignalPolicy] = Field(default_factory=list)
    quorum: int = Field(default=0, ge=0)

    @field_validator("step_id")
    @classmethod
    def _valid_step_id(cls, value: str) -> str:
        if not _ID_RE.fullmatch(value):
            raise ValueError("step_id must be a stable non-empty identifier")
        return value

    @model_validator(mode="after")
    def _valid_quorum(self) -> "IdentityPolicy":
        if self.enforcement is IdentityEnforcement.CANONICAL_LADDER:
            if self.signals or self.quorum:
                raise ValueError(
                    "canonical_ladder uses the executable Flow identity contract; "
                    "signals and quorum must be empty"
                )
            return self
        if not self.signals:
            raise ValueError("signal_quorum requires at least one signal")
        if self.quorum < 1:
            raise ValueError("signal_quorum requires quorum >= 1")
        if self.quorum > len(self.signals):
            raise ValueError("identity quorum cannot exceed the number of signals")
        keys = [signal.key for signal in self.signals]
        if len(keys) != len(set(keys)):
            raise ValueError("identity signal semantic keys must be unique")
        return self


class EffectVerificationPolicy(BaseModel):
    """Evidence strength assigned to one exact actuation-path effect."""

    model_config = ConfigDict(extra="forbid")

    step_id: str
    actuation_path: Literal["gui", "api"] = "gui"
    effect_index: int = Field(ge=0)
    effect_contract_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    tier: VerificationTier


class ActionRiskClass(str, Enum):
    UNKNOWN = "unknown"
    READ_ONLY = "read_only"
    STATE_CHANGING = "state_changing"
    CONSEQUENTIAL = "consequential"
    IRREVERSIBLE = "irreversible"


class ActionRiskClassification(BaseModel):
    """Operator-reviewed business risk for one executable action."""

    model_config = ConfigDict(extra="forbid")

    step_id: str
    classification: ActionRiskClass
    explanation: str = Field(min_length=1, max_length=512)
    operator_confirmed: bool = False


class QualificationCaseKind(str, Enum):
    REPRESENTATIVE = "representative"
    AMBIGUITY = "ambiguity"
    WRONG_IDENTITY = "wrong_identity"
    STALE_IDENTITY = "stale_identity"
    WEAK_EFFECT = "weak_effect"
    MISSING_EFFECT = "missing_effect"


class QualificationOutcome(str, Enum):
    VERIFIED = "verified"
    COMPLETED_UNVERIFIED = "completed_unverified"
    HALTED = "halted"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


class EvidenceRef(BaseModel):
    """Local or customer-controlled evidence reference; never evidence content."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal[
        "run_report",
        "identity",
        "effect",
        "fault_campaign",
        "other",
    ]
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    relative_path: str = Field(min_length=1, max_length=512)

    @field_validator("relative_path")
    @classmethod
    def _relative_only(cls, value: str) -> str:
        if "\\" in value:
            raise ValueError("evidence path must use POSIX separators")
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("evidence path must be bundle-relative without '..'")
        return path.as_posix()


class QualificationCaseResult(BaseModel):
    """One runner-produced result for a qualification case."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    project_id: str
    project_revision: int = Field(ge=1)
    project_contract_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    workflow_contract_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    environment_contract_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    environment_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    runtime_version: str = Field(min_length=1, max_length=64)
    runner_id: str = Field(min_length=1, max_length=128)
    runner_capabilities: list[str] = Field(default_factory=list)
    status: Literal["passed", "failed", "blocked"]
    observed_outcome: QualificationOutcome
    evidence: list[EvidenceRef] = Field(default_factory=list)
    detail_code: Optional[str] = Field(default=None, max_length=128)
    completed_at: str = Field(default_factory=_now)
    attestation_key_id: str = Field(min_length=1, max_length=128)
    attestation_signature: str = ""

    @model_validator(mode="after")
    def _passed_needs_evidence(self) -> "QualificationCaseResult":
        if self.status == "passed" and not self.evidence:
            raise ValueError("a passed qualification case requires evidence")
        capabilities = [item.strip() for item in self.runner_capabilities]
        if any(not item for item in capabilities) or len(capabilities) != len(
            set(capabilities)
        ):
            raise ValueError("runner capabilities must be unique and non-empty")
        self.runner_capabilities = sorted(capabilities)
        return self


class QualificationCase(BaseModel):
    """A representative or deterministic fault case.

    ``input_ref`` names a customer-controlled parameter fixture.  Parameter
    values and secrets do not enter the qualification project.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: QualificationCaseKind
    description: str = Field(default="", max_length=512)
    input_ref: Optional[str] = Field(default=None, max_length=256)
    expected_outcome: QualificationOutcome
    required: bool = True
    results: list[QualificationCaseResult] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        if not _ID_RE.fullmatch(value):
            raise ValueError("case id must be a stable non-empty identifier")
        return value

    @model_validator(mode="after")
    def _faults_halt(self) -> "QualificationCase":
        if self.kind is QualificationCaseKind.REPRESENTATIVE:
            if self.expected_outcome is not QualificationOutcome.VERIFIED:
                raise ValueError("representative cases must expect VERIFIED")
        elif self.expected_outcome is not QualificationOutcome.HALTED:
            raise ValueError("deterministic fault cases must expect HALTED")
        return self


class EnvironmentBoundary(BaseModel):
    """Application/environment scope in which the qualification is valid."""

    model_config = ConfigDict(extra="forbid")

    target_kind: Literal["web", "windows", "macos", "linux", "rdp", "citrix"]
    application: str = Field(min_length=1, max_length=256)
    application_version: str = Field(min_length=1, max_length=128)
    environment_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    runtime_version: str = Field(min_length=1, max_length=64)
    required_capabilities: list[str] = Field(default_factory=list)

    @field_validator("required_capabilities")
    @classmethod
    def _unique_capabilities(cls, value: list[str]) -> list[str]:
        cleaned = sorted({item.strip() for item in value if item.strip()})
        if len(cleaned) != len(value):
            raise ValueError("required capabilities must be unique and non-empty")
        return cleaned

    def contract_sha256(self) -> str:
        encoded = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(encoded).hexdigest()


class RequalificationCondition(BaseModel):
    """A bounded condition that invalidates this qualification."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal[
        "workflow_changed",
        "application_version_changed",
        "environment_changed",
        "identity_policy_changed",
        "effect_policy_changed",
        "runtime_version_changed",
        "expiry",
        "operator_requested",
    ]
    description: str = Field(default="", max_length=512)


class QualificationCertification(BaseModel):
    """Last persisted qualification decision."""

    model_config = ConfigDict(extra="forbid")

    project_revision: int
    project_contract_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    workflow_contract_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    environment_contract_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    policy_name: str
    policy_contract_sha256: Optional[str] = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    policy_contract: Optional[dict[str, Any]] = None
    passed: bool
    report_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    certified_at: str = Field(default_factory=_now)


def _default_fault_cases() -> list[QualificationCase]:
    return [
        QualificationCase(
            id=f"fault-{kind.value.replace('_', '-')}",
            kind=kind,
            description=f"Deterministic {kind.value.replace('_', ' ')} refusal case",
            expected_outcome=QualificationOutcome.HALTED,
        )
        for kind in (
            QualificationCaseKind.AMBIGUITY,
            QualificationCaseKind.WRONG_IDENTITY,
            QualificationCaseKind.STALE_IDENTITY,
            QualificationCaseKind.WEAK_EFFECT,
            QualificationCaseKind.MISSING_EFFECT,
        )
    ]


class QualificationProject(BaseModel):
    """Versioned qualification configuration sealed inside a Flow bundle."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["openadapt.qualification-project/v1"] = QUALIFICATION_SCHEMA
    project_id: str = Field(default_factory=lambda: str(uuid4()))
    revision: int = Field(default=1, ge=1)
    previous_revision_sha256: Optional[str] = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)
    environment: EnvironmentBoundary
    minimum_effect_tier: VerificationTier = (
        VerificationTier.PERSISTED_STATE_REACQUISITION
    )
    action_classifications: dict[str, ActionRiskClassification] = Field(
        default_factory=dict
    )
    identity_policies: dict[str, IdentityPolicy] = Field(default_factory=dict)
    effect_policies: list[EffectVerificationPolicy] = Field(default_factory=list)
    cases: list[QualificationCase] = Field(default_factory=_default_fault_cases)
    exclusions: list[str] = Field(default_factory=list)
    requalification_conditions: list[RequalificationCondition] = Field(
        default_factory=list
    )
    trusted_runner_keys: dict[str, str] = Field(default_factory=dict)
    last_certification: Optional[QualificationCertification] = None

    @model_validator(mode="after")
    def _consistent_keys(self) -> "QualificationProject":
        for key, policy in self.identity_policies.items():
            if key != policy.step_id:
                raise ValueError(
                    f"identity policy key {key!r} does not match step_id "
                    f"{policy.step_id!r}"
                )
        for key, classification in self.action_classifications.items():
            if key != classification.step_id:
                raise ValueError(
                    f"action classification key {key!r} does not match step_id "
                    f"{classification.step_id!r}"
                )
        for key_id, public_key in self.trusted_runner_keys.items():
            if not _ID_RE.fullmatch(key_id):
                raise ValueError("trusted runner key id is invalid")
            try:
                raw_key = b64decode(public_key, validate=True)
            except ValueError as exc:
                raise ValueError("trusted runner public key must be base64") from exc
            if len(raw_key) != 32:
                raise ValueError("trusted runner public key must be 32-byte Ed25519")
        case_ids = [case.id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("qualification case ids must be unique")
        effect_refs = [
            (binding.step_id, binding.actuation_path, binding.effect_index)
            for binding in self.effect_policies
        ]
        if len(effect_refs) != len(set(effect_refs)):
            raise ValueError("effect verification references must be unique")
        return self

    def revision_digest(self) -> str:
        return self.contract_sha256()

    def contract_sha256(self) -> str:
        """Digest the exact qualification contract, without result self-reference."""

        payload = self.model_dump(
            mode="json",
            exclude={
                "previous_revision_sha256",
                "created_at",
                "updated_at",
                "last_certification",
            },
        )
        for case in payload["cases"]:
            case["results"] = []
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


class QualificationRefusalCode(str, Enum):
    PROJECT_MISSING = "project_missing"
    ACTION_CLASSIFICATION_MISSING = "action_classification_missing"
    ACTION_CLASSIFICATION_UNCONFIRMED = "action_classification_unconfirmed"
    ACTION_CLASSIFICATION_CONFLICT = "action_classification_conflict"
    STEP_IDENTITY_UNARMED = "step_identity_unarmed"
    IDENTITY_POLICY_MISSING = "identity_policy_missing"
    IDENTITY_POLICY_UNENFORCED = "identity_policy_unenforced"
    IDENTITY_SIGNALS_NOT_INDEPENDENT = "identity_signals_not_independent"
    IDENTITY_SIGNAL_UNAVAILABLE = "identity_signal_unavailable"
    EFFECT_CONTRACT_MISSING = "effect_contract_missing"
    EFFECT_POLICY_MISSING = "effect_policy_missing"
    EFFECT_CONTRACT_CHANGED = "effect_contract_changed"
    EFFECT_TIER_INSUFFICIENT = "effect_tier_insufficient"
    HIGH_RISK_SCREEN_ONLY = "high_risk_screen_only"
    REPRESENTATIVE_CASE_MISSING = "representative_case_missing"
    FAULT_CASE_MISSING = "fault_case_missing"
    CASE_NOT_PASSED = "case_not_passed"
    CASE_EVIDENCE_MISSING = "case_evidence_missing"
    CASE_EVIDENCE_UNVERIFIED = "case_evidence_unverified"
    CASE_ATTESTATION_INVALID = "case_attestation_invalid"
    CASE_WORKFLOW_CHANGED = "case_workflow_changed"
    CASE_ENVIRONMENT_CHANGED = "case_environment_changed"
    CASE_RUNTIME_CHANGED = "case_runtime_changed"
    CASE_CAPABILITY_MISSING = "case_capability_missing"
    POLICY_VIOLATION = "policy_violation"


class QualificationRefusal(BaseModel):
    """Stable, machine-readable reason certification was refused."""

    model_config = ConfigDict(extra="forbid")

    code: QualificationRefusalCode
    path: str
    message: str
    step_id: Optional[str] = None
    case_id: Optional[str] = None
    details: dict[str, str | int | bool] = Field(default_factory=dict)


class QualificationReport(BaseModel):
    """Qualification coverage and certification decision."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["openadapt.qualification-report/v1"] = (
        "openadapt.qualification-report/v1"
    )
    workflow_name: str
    workflow_contract_sha256: str
    environment_contract_sha256: Optional[str]
    project_id: Optional[str]
    project_revision: Optional[int]
    policy_name: Optional[str]
    passed: bool
    action_count: int
    state_changing_action_count: int
    consequential_action_count: int
    identity_covered_action_count: int
    effect_required_action_count: int
    effect_covered_action_count: int
    minimum_effect_tier: Optional[VerificationTier]
    case_count: int
    passed_case_count: int
    refusals: list[QualificationRefusal] = Field(default_factory=list)
    generated_at: str = Field(default_factory=_now)

    def report_sha256(self) -> str:
        payload = self.model_dump(mode="json", exclude={"generated_at"})
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def render(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        lines = [
            f"{status}: qualification for {self.workflow_name!r}",
            (
                f"  identity coverage {self.identity_covered_action_count}/"
                f"{self.consequential_action_count}; effect coverage "
                f"{self.effect_covered_action_count}/"
                f"{self.effect_required_action_count}; cases "
                f"{self.passed_case_count}/{self.case_count}"
            ),
        ]
        for refusal in self.refusals:
            lines.append(f"  - {refusal.code.value}: {refusal.message}")
        return "\n".join(lines)


class QualificationError(ValueError):
    """A requested qualification mutation could not be applied."""


def workflow_contract_sha256(workflow: "Workflow") -> str:
    """Digest executable intent and its already-sealed visual assets.

    Qualification state and manifest certification metadata are excluded to
    avoid self-reference.  Existing manifest file hashes bind the plaintext
    template assets while keeping their contents inside the declared boundary.
    """

    content = workflow.model_dump(
        mode="json",
        exclude={"manifest", "qualification"},
    )
    payload = {
        "workflow": content,
        "file_hashes": (
            dict(sorted(workflow.manifest.file_hashes.items()))
            if workflow.manifest is not None
            else {}
        ),
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _steps_by_id(workflow: "Workflow") -> dict[str, "Step"]:
    from openadapt_flow.traversal import iter_workflow_steps

    steps: dict[str, Step] = {}
    for step in iter_workflow_steps(workflow):
        if step.id in steps:
            raise QualificationError(f"step id {step.id!r} is ambiguous")
        steps[step.id] = step
    return steps


def _inferred_action_classification(step: "Step") -> ActionRiskClassification:
    from openadapt_flow.ir import ActionKind

    effects = _declared_effects(step)
    if step.risk_review_required:
        classification = ActionRiskClass.UNKNOWN
        explanation = step.risk_explanation or "compiled Flow risk requires review"
    elif step.risk == "irreversible":
        classification = ActionRiskClass.IRREVERSIBLE
        explanation = step.risk_explanation or "compiled Flow risk is irreversible"
    elif effects:
        classification = ActionRiskClass.STATE_CHANGING
        explanation = "step declares a business-effect contract"
    elif step.action in {ActionKind.WAIT, ActionKind.SCROLL}:
        classification = ActionRiskClass.READ_ONLY
        explanation = f"{step.action.value} does not actuate business state"
    else:
        classification = ActionRiskClass.UNKNOWN
        explanation = "actuating step requires operator risk classification"
    return ActionRiskClassification(
        step_id=step.id,
        classification=classification,
        explanation=explanation,
        operator_confirmed=classification is ActionRiskClass.READ_ONLY,
    )


def _declared_effects(step: "Step") -> list[Any]:
    """Return effects across every declared alternative actuation path.

    Runtime path selection may give a native API binding precedence over GUI
    actuation; qualification examines the union only to establish a
    conservative risk floor.  It does not imply that both paths execute or
    that one path's evidence can satisfy the other's production contract.
    """

    effects = list(step.effects)
    if step.api_binding is not None:
        effects.extend(step.api_binding.effects)
    return effects


def _effect_risk_floor(step: "Step") -> Optional[ActionRiskClass]:
    """Return the hard floor independently proved by declared effects."""

    effects = _declared_effects(step)
    if any(effect.risk.strip().lower() == "irreversible" for effect in effects):
        return ActionRiskClass.IRREVERSIBLE
    if effects:
        return ActionRiskClass.STATE_CHANGING
    return None


def _executable_risk_floor(step: "Step") -> Optional[ActionRiskClass]:
    """Return the least-permissive class required by executable contracts."""

    effect_floor = _effect_risk_floor(step)
    if step.risk == "irreversible" or effect_floor is ActionRiskClass.IRREVERSIBLE:
        return ActionRiskClass.IRREVERSIBLE
    return effect_floor


def available_identity_sources(step: "Step") -> set[IdentityEvidenceSource]:
    anchor = step.anchor
    if anchor is None:
        return set()
    out: set[IdentityEvidenceSource] = set()
    template = anchor.identity_template
    if anchor.structured_identity or (template is not None and template.structured):
        out.add(IdentityEvidenceSource.STRUCTURED)
    if anchor.identifier_crop and anchor.identifier_region:
        out.add(IdentityEvidenceSource.IDENTIFIER_REGION)
    if anchor.context_text or (template is not None and template.tokens):
        out.add(IdentityEvidenceSource.CAPTURED_CONTEXT)
    # These sources are supplied live by the selected runner rather than by
    # target-row evidence. Capability negotiation/refusal happens at runtime.
    out.update(
        {
            IdentityEvidenceSource.APPLICATION,
            IdentityEvidenceSource.SESSION,
            IdentityEvidenceSource.WORKFLOW_STATE,
        }
    )
    return out


def identity_policy_independence_errors(policy: IdentityPolicy) -> list[str]:
    """Return fail-closed reasons when quorum votes reuse one observation.

    Each currently retained source is one observation channel per action:
    application structured text, one identifier crop/region, or one captured
    context band. Giving the same channel two field labels must not create two
    quorum votes.
    """

    if policy.enforcement is not IdentityEnforcement.SIGNAL_QUORUM:
        return []
    seen: set[IdentityEvidenceSource] = set()
    errors: list[str] = []
    for signal in policy.signals:
        if signal.source in seen:
            errors.append(
                f"source {signal.source.value!r} is reused by multiple signals"
            )
        seen.add(signal.source)
    pixel_signals = [
        signal
        for signal in policy.signals
        if signal.source
        in {
            IdentityEvidenceSource.IDENTIFIER_REGION,
            IdentityEvidenceSource.CAPTURED_CONTEXT,
        }
        and signal.region is not None
    ]
    for index, left in enumerate(pixel_signals):
        assert left.region is not None
        lx, ly, lw, lh = left.region
        for right in pixel_signals[index + 1 :]:
            assert right.region is not None
            rx, ry, rw, rh = right.region
            if lx < rx + rw and rx < lx + lw and ly < ry + rh and ry < ly + lh:
                errors.append(
                    "pixel regions overlap for semantic keys "
                    f"{left.key.value!r} and {right.key.value!r}"
                )
    return errors


def identity_signal_runtime_available(
    step: "Step",
    signal: IdentitySignalPolicy,
) -> bool:
    """Whether the shipped runtime can compare this exact retained signal."""

    anchor = step.anchor
    if anchor is None:
        return False
    if signal.source is IdentityEvidenceSource.IDENTIFIER_REGION:
        return bool(
            anchor.identifier_crop
            and anchor.identifier_region
            and signal.region == anchor.identifier_region
        )
    if signal.source is IdentityEvidenceSource.STRUCTURED:
        if anchor.structured_identity:
            return True
        template = anchor.identity_template
        return bool(
            template
            and signal_hash_key(
                signal.source,
                signal.match,
                signal.normalizers,
                extract_pattern=signal.extract_pattern,
                parameter_names=signal.params,
            )
            in template.signal_hashes
        )
    if signal.source is IdentityEvidenceSource.CAPTURED_CONTEXT:
        if anchor.context_text:
            return True
        template = anchor.identity_template
        return bool(
            template
            and signal_hash_key(
                signal.source,
                signal.match,
                signal.normalizers,
                extract_pattern=signal.extract_pattern,
                parameter_names=signal.params,
            )
            in template.signal_hashes
        )
    if signal.source in {
        IdentityEvidenceSource.APPLICATION,
        IdentityEvidenceSource.SESSION,
        IdentityEvidenceSource.WORKFLOW_STATE,
    }:
        return True
    return False


def _invalidate_certification(workflow: "Workflow") -> None:
    if workflow.qualification is not None:
        workflow.qualification.last_certification = None
    if workflow.manifest is None:
        return
    provenance = workflow.manifest.provenance
    provenance.policy_name = None
    provenance.certified = False
    provenance.certification_status = None
    provenance.certified_at = None
    provenance.expires_at = None


def _touch(project: QualificationProject, previous_digest: str) -> None:
    project.previous_revision_sha256 = previous_digest
    project.revision += 1
    project.updated_at = _now()
    project.last_certification = None


def init_project(
    workflow: "Workflow",
    *,
    environment: EnvironmentBoundary,
    minimum_effect_tier: VerificationTier = (
        VerificationTier.PERSISTED_STATE_REACQUISITION
    ),
    replace: bool = False,
) -> QualificationProject:
    """Attach a new qualification project to ``workflow``."""

    if workflow.qualification is not None and not replace:
        raise QualificationError("workflow already has a qualification project")
    classifications = {
        step_id: _inferred_action_classification(step)
        for step_id, step in _steps_by_id(workflow).items()
    }
    project = QualificationProject(
        environment=environment,
        minimum_effect_tier=minimum_effect_tier,
        action_classifications=classifications,
    )
    workflow.qualification = project
    _invalidate_certification(workflow)
    return project


def set_minimum_effect_tier(
    workflow: "Workflow",
    tier: VerificationTier,
) -> QualificationProject:
    """Set the project's minimum accepted effect-verification strength."""

    project = workflow.qualification
    if project is None:
        raise QualificationError(
            "initialize qualification before setting minimum effect strength"
        )
    candidate = VerificationTier(tier)
    if project.minimum_effect_tier == candidate:
        return project
    previous = project.revision_digest()
    project.minimum_effect_tier = candidate
    _touch(project, previous)
    _invalidate_certification(workflow)
    return project


def set_identity_policy(
    workflow: "Workflow",
    policy: IdentityPolicy,
    *,
    recorded_signal_values: Optional[dict[str, str]] = None,
) -> QualificationProject:
    """Set review policy for a step's existing executable identity ladder.

    ``recorded_signal_values`` supplies operator-selected values only when the
    corresponding plaintext source has already been removed from the bundle.
    Qualification converts each value directly into an extractor-scoped HMAC;
    neither this mapping nor its values are retained in the workflow.
    """

    project = workflow.qualification
    if project is None:
        raise QualificationError("initialize qualification before setting identity")
    step = _steps_by_id(workflow).get(policy.step_id)
    if step is None:
        raise QualificationError(f"unknown step id {policy.step_id!r}")
    available = available_identity_sources(step)
    pending_hashes: list[tuple["IdentityTemplate", str, str]] = []
    if policy.enforcement is IdentityEnforcement.CANONICAL_LADDER:
        if recorded_signal_values:
            raise QualificationError(
                "recorded identity values apply only to signal-quorum policies"
            )
        from openadapt_flow.policy import is_identity_armed

        if not is_identity_armed(step) or not available:
            raise QualificationError(
                "canonical identity ladder is not armed with retained evidence"
            )
    else:
        independence_errors = identity_policy_independence_errors(policy)
        if independence_errors:
            raise QualificationError(
                "identity quorum signals are not independent: "
                + "; ".join(independence_errors)
            )
        unavailable = sorted(
            {signal.source for signal in policy.signals} - available,
            key=lambda source: source.value,
        )
        if unavailable:
            raise QualificationError(
                "identity policy references unavailable evidence: "
                + ", ".join(source.value for source in unavailable)
            )
        supplied_values = recorded_signal_values or {}
        pending_keys: set[tuple[int, str]] = set()
        accepted_value_keys: set[str] = set()
        for signal in policy.signals:
            unknown_params = sorted(set(signal.params).difference(workflow.params))
            if unknown_params:
                raise QualificationError(
                    f"identity signal {signal.key.value!r} references unknown "
                    "workflow parameter(s): " + ", ".join(unknown_params)
                )
            anchor = step.anchor
            assert anchor is not None
            recorded = (
                anchor.structured_identity
                if signal.source is IdentityEvidenceSource.STRUCTURED
                else anchor.context_text
                if signal.source is IdentityEvidenceSource.CAPTURED_CONTEXT
                else None
            )
            if recorded is not None and signal.extract_pattern is not None:
                extracted = re.search(
                    signal.extract_pattern,
                    recorded,
                    flags=re.IGNORECASE,
                )
                if extracted is None:
                    raise QualificationError(
                        f"identity signal {signal.key.value!r} extract_pattern "
                        "does not match the retained source"
                    )
                recorded = extracted.group("value")
            if (
                recorded is None
                and signal.source
                in {
                    IdentityEvidenceSource.STRUCTURED,
                    IdentityEvidenceSource.CAPTURED_CONTEXT,
                }
                and signal.key.value in supplied_values
            ):
                template = anchor.identity_template
                if template is None or signal.extract_pattern is None:
                    raise QualificationError(
                        f"identity signal {signal.key.value!r} has no PHI-free "
                        "identity template to bind"
                    )
                from openadapt_flow.runtime.identity_template import (
                    qualified_signal_hash,
                )

                hash_key, digest, used = qualified_signal_hash(
                    template,
                    source=signal.source.value,
                    match=signal.match.value,
                    normalizers=signal.normalizers,
                    extract_pattern=signal.extract_pattern,
                    recorded_value=supplied_values[signal.key.value],
                    param_examples=workflow.params,
                    parameter_names=signal.params,
                )
                missing = sorted(set(signal.params).difference(used))
                if missing:
                    raise QualificationError(
                        f"identity signal {signal.key.value!r} parameter binding "
                        "does not occupy a complete value boundary in the "
                        "operator-selected recorded value: " + ", ".join(missing)
                    )
                pending_hashes.append((template, hash_key, digest))
                pending_keys.add((id(template), hash_key))
                accepted_value_keys.add(signal.key.value)
            if recorded is not None and signal.params:
                _parameterized, used = parameterize_identity_text(
                    recorded,
                    workflow.params,
                    names=signal.params,
                    minimum_chars=4,
                    case_sensitive=True,
                )
                missing = sorted(set(signal.params).difference(used))
                if missing:
                    raise QualificationError(
                        f"identity signal {signal.key.value!r} parameter binding "
                        "does not occupy a complete value boundary in the retained "
                        "source: " + ", ".join(missing)
                    )
        unused_value_keys = sorted(set(supplied_values).difference(accepted_value_keys))
        if unused_value_keys:
            raise QualificationError(
                "recorded identity values were supplied for signals that do not "
                "need a PHI-free binding: " + ", ".join(unused_value_keys)
            )
        runtime_unavailable = []
        for signal in policy.signals:
            if identity_signal_runtime_available(step, signal):
                continue
            anchor = step.anchor
            assert anchor is not None
            template = anchor.identity_template
            pending_key = signal_hash_key(
                signal.source,
                signal.match,
                signal.normalizers,
                extract_pattern=signal.extract_pattern,
                parameter_names=signal.params,
            )
            if template is not None and (id(template), pending_key) in pending_keys:
                continue
            runtime_unavailable.append(signal)
        if runtime_unavailable:
            raise QualificationError(
                "identity policy references retained evidence without the "
                "requested executable comparison: "
                + ", ".join(
                    f"{signal.key.value} ({signal.source.value})"
                    for signal in runtime_unavailable
                )
            )
    hashes_changed = any(
        template.signal_hashes.get(key) != digest
        for template, key, digest in pending_hashes
    )
    if project.identity_policies.get(policy.step_id) == policy and not hashes_changed:
        return project
    previous = project.revision_digest()
    for template, key, digest in pending_hashes:
        template.signal_hashes[key] = digest
    project.identity_policies[policy.step_id] = policy
    _touch(project, previous)
    _invalidate_certification(workflow)
    return project


def set_action_classification(
    workflow: "Workflow",
    classification: ActionRiskClassification,
) -> QualificationProject:
    """Persist an operator-reviewed classification for one executable action."""

    project = workflow.qualification
    if project is None:
        raise QualificationError("initialize qualification before classifying actions")
    step = _steps_by_id(workflow).get(classification.step_id)
    if step is None:
        raise QualificationError(f"unknown step id {classification.step_id!r}")
    if not classification.operator_confirmed:
        raise QualificationError("an operator classification must be confirmed")
    effect_floor = _effect_risk_floor(step)
    if (
        effect_floor is ActionRiskClass.IRREVERSIBLE
        and classification.classification is not ActionRiskClass.IRREVERSIBLE
    ):
        raise QualificationError(
            "an action with an irreversible effect contract cannot be down-classified"
        )
    if (
        effect_floor is ActionRiskClass.STATE_CHANGING
        and classification.classification is ActionRiskClass.READ_ONLY
    ):
        raise QualificationError(
            "an action with a declared business effect cannot be read-only"
        )
    runtime_risk: Literal["reversible", "irreversible"] = (
        "irreversible"
        if classification.classification
        in {ActionRiskClass.CONSEQUENTIAL, ActionRiskClass.IRREVERSIBLE}
        else "reversible"
    )
    runtime_explanation = f"operator-qualified override: {runtime_risk}"
    unchanged = (
        project.action_classifications.get(classification.step_id) == classification
        and step.risk == runtime_risk
        and step.risk_explanation == runtime_explanation
        and not step.risk_review_required
    )
    if unchanged:
        return project
    previous = project.revision_digest()
    step.risk = runtime_risk
    step.risk_explanation = runtime_explanation
    step.risk_review_required = False
    project.action_classifications[classification.step_id] = classification
    _touch(project, previous)
    _invalidate_certification(workflow)
    return project


def set_trusted_runner_key(
    workflow: "Workflow",
    *,
    key_id: str,
    public_key_base64: str,
) -> QualificationProject:
    """Trust an Ed25519 qualification runner public key."""

    project = workflow.qualification
    if project is None:
        raise QualificationError(
            "initialize qualification before trusting a qualification runner"
        )
    candidate = dict(project.trusted_runner_keys)
    candidate[key_id] = public_key_base64
    # Re-validate the closed schema before mutating the live project.
    QualificationProject.model_validate(
        {**project.model_dump(mode="json"), "trusted_runner_keys": candidate}
    )
    if project.trusted_runner_keys.get(key_id) == public_key_base64:
        return project
    previous = project.revision_digest()
    project.trusted_runner_keys = candidate
    _touch(project, previous)
    _invalidate_certification(workflow)
    return project


def set_effect_policy(
    workflow: "Workflow",
    *,
    step_id: str,
    effect_index: int,
    tier: VerificationTier,
    actuation_path: Literal["gui", "api"] = "gui",
) -> QualificationProject:
    """Assign verification strength to an existing effect contract."""

    project = workflow.qualification
    if project is None:
        raise QualificationError("initialize qualification before setting effects")
    step = _steps_by_id(workflow).get(step_id)
    if step is None:
        raise QualificationError(f"unknown step id {step_id!r}")
    effects = (
        step.effects
        if actuation_path == "gui"
        else (step.api_binding.effects if step.api_binding is not None else [])
    )
    if actuation_path == "api" and step.api_binding is None:
        raise QualificationError(f"step {step_id!r} has no API actuation path")
    if effect_index < 0 or effect_index >= len(effects):
        raise QualificationError(
            f"effect index {effect_index} is outside {actuation_path} path "
            f"for step {step_id!r}"
        )
    binding = EffectVerificationPolicy(
        step_id=step_id,
        actuation_path=actuation_path,
        effect_index=effect_index,
        effect_contract_hash=effects[effect_index].contract_hash(),
        tier=tier,
    )
    current = next(
        (
            existing
            for existing in project.effect_policies
            if (
                existing.step_id,
                existing.actuation_path,
                existing.effect_index,
            )
            == (step_id, actuation_path, effect_index)
        ),
        None,
    )
    if current == binding:
        return project
    previous = project.revision_digest()
    project.effect_policies = [
        existing
        for existing in project.effect_policies
        if (
            existing.step_id,
            existing.actuation_path,
            existing.effect_index,
        )
        != (step_id, actuation_path, effect_index)
    ]
    project.effect_policies.append(binding)
    project.effect_policies.sort(
        key=lambda item: (item.step_id, item.actuation_path, item.effect_index)
    )
    _touch(project, previous)
    _invalidate_certification(workflow)
    return project


def add_case(workflow: "Workflow", case: QualificationCase) -> QualificationProject:
    project = workflow.qualification
    if project is None:
        raise QualificationError("initialize qualification before adding cases")
    if any(existing.id == case.id for existing in project.cases):
        raise QualificationError(f"qualification case {case.id!r} already exists")
    previous = project.revision_digest()
    project.cases.append(case)
    _touch(project, previous)
    _invalidate_certification(workflow)
    return project


def _attestation_payload(result: QualificationCaseResult) -> bytes:
    payload = result.model_dump(
        mode="json",
        exclude={"attestation_signature"},
    )
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()


def sign_case_result(
    result: QualificationCaseResult,
    *,
    private_key: bytes,
) -> QualificationCaseResult:
    """Sign one result with a raw 32-byte Ed25519 private key."""

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    signer = Ed25519PrivateKey.from_private_bytes(private_key)
    signature = signer.sign(_attestation_payload(result))
    return result.model_copy(
        update={"attestation_signature": b64encode(signature).decode("ascii")}
    )


def _case_result_integrity_error(
    workflow: "Workflow",
    project: QualificationProject,
    result: QualificationCaseResult,
    *,
    evidence_root: Optional[Path],
) -> Optional[tuple[QualificationRefusalCode, str]]:
    """Return the first fail-closed attestation/evidence error."""

    if result.project_id != project.project_id:
        return (
            QualificationRefusalCode.CASE_ATTESTATION_INVALID,
            "case result project id does not match the qualification project",
        )
    if result.project_revision != project.revision:
        return (
            QualificationRefusalCode.CASE_ATTESTATION_INVALID,
            "case result project revision is stale",
        )
    if result.workflow_contract_sha256 != workflow_contract_sha256(workflow):
        return (
            QualificationRefusalCode.CASE_WORKFLOW_CHANGED,
            "case result is bound to a different executable workflow",
        )
    if (
        result.environment_contract_sha256 != project.environment.contract_sha256()
        or result.environment_digest != project.environment.environment_digest
    ):
        return (
            QualificationRefusalCode.CASE_ENVIRONMENT_CHANGED,
            "case result is bound to a different qualification environment",
        )
    if result.runtime_version != project.environment.runtime_version:
        return (
            QualificationRefusalCode.CASE_RUNTIME_CHANGED,
            "case result runtime version is outside the qualified boundary",
        )
    missing_capabilities = sorted(
        set(project.environment.required_capabilities) - set(result.runner_capabilities)
    )
    if missing_capabilities:
        return (
            QualificationRefusalCode.CASE_CAPABILITY_MISSING,
            "runner lacks required capabilities: " + ", ".join(missing_capabilities),
        )
    if result.project_contract_sha256 != project.contract_sha256():
        return (
            QualificationRefusalCode.CASE_ATTESTATION_INVALID,
            "case result is bound to a different qualification contract",
        )
    public_key_base64 = project.trusted_runner_keys.get(result.attestation_key_id)
    if public_key_base64 is None or not result.attestation_signature:
        return (
            QualificationRefusalCode.CASE_ATTESTATION_INVALID,
            "case result is not signed by a trusted qualification runner",
        )
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        verifier = Ed25519PublicKey.from_public_bytes(
            b64decode(public_key_base64, validate=True)
        )
        verifier.verify(
            b64decode(result.attestation_signature, validate=True),
            _attestation_payload(result),
        )
    except (InvalidSignature, ValueError):
        return (
            QualificationRefusalCode.CASE_ATTESTATION_INVALID,
            "case result signature is invalid",
        )
    # The runner signature binds every evidence digest and relative path.  A
    # certification operation supplies ``evidence_root`` and verifies the
    # actual bytes.  Later admission can independently recompute the exact
    # qualification decision from the signed, hash-bound references without
    # copying sensitive evidence into the portable bundle.
    if evidence_root is None:
        return None
    root = evidence_root.resolve()
    for evidence in result.evidence:
        candidate = root.joinpath(*PurePosixPath(evidence.relative_path).parts)
        cursor = root
        for part in PurePosixPath(evidence.relative_path).parts:
            cursor /= part
            if cursor.is_symlink():
                return (
                    QualificationRefusalCode.CASE_EVIDENCE_UNVERIFIED,
                    f"evidence path contains a symlink: {evidence.relative_path}",
                )
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            return (
                QualificationRefusalCode.CASE_EVIDENCE_UNVERIFIED,
                f"evidence file is missing: {evidence.relative_path}",
            )
        if not resolved.is_relative_to(root) or candidate.is_symlink():
            return (
                QualificationRefusalCode.CASE_EVIDENCE_UNVERIFIED,
                f"evidence path leaves its root: {evidence.relative_path}",
            )
        if not resolved.is_file():
            return (
                QualificationRefusalCode.CASE_EVIDENCE_UNVERIFIED,
                f"evidence is not a regular file: {evidence.relative_path}",
            )
        if hashlib.sha256(resolved.read_bytes()).hexdigest() != evidence.sha256:
            return (
                QualificationRefusalCode.CASE_EVIDENCE_UNVERIFIED,
                f"evidence hash mismatch: {evidence.relative_path}",
            )
    return None


def record_case_results(
    workflow: "Workflow",
    results: Iterable[QualificationCaseResult],
    *,
    evidence_root: Path | str,
) -> QualificationProject:
    """Record results produced by a local/Desktop/customer-controlled runner."""

    project = workflow.qualification
    if project is None:
        raise QualificationError("initialize qualification before recording results")
    cases = {case.id: case for case in project.cases}
    pending = list(results)
    for result in pending:
        case = cases.get(result.case_id)
        if case is None:
            raise QualificationError(f"unknown qualification case {result.case_id!r}")
        error = _case_result_integrity_error(
            workflow,
            project,
            result,
            evidence_root=Path(evidence_root),
        )
        if error is not None:
            raise QualificationError(f"{error[0].value}: {error[1]}")
    for result in pending:
        case = cases[result.case_id]
        case.results.append(result)
    project.updated_at = _now()
    _invalidate_certification(workflow)
    return project


def add_requalification_condition(
    workflow: "Workflow",
    condition: RequalificationCondition,
) -> QualificationProject:
    """Add a requalification trigger and advance the semantic revision."""

    project = workflow.qualification
    if project is None:
        raise QualificationError(
            "initialize qualification before adding requalification conditions"
        )
    if condition in project.requalification_conditions:
        return project
    previous = project.revision_digest()
    project.requalification_conditions.append(condition)
    _touch(project, previous)
    _invalidate_certification(workflow)
    return project


def run_cases(
    workflow: "Workflow",
    executor: Callable[
        [QualificationCase, QualificationProject, str],
        QualificationCaseResult,
    ],
    *,
    case_ids: Optional[set[str]] = None,
    evidence_root: Path | str,
) -> list[QualificationCaseResult]:
    """Execute selected cases through a caller-supplied local runner.

    Flow owns the schema and certification rules; Desktop or a deployment owns
    the environment-specific executor.  This keeps raw observations in the
    declared execution boundary while still providing one typed API.
    """

    project = workflow.qualification
    if project is None:
        raise QualificationError("initialize qualification before running cases")
    selected = [
        case for case in project.cases if case_ids is None or case.id in case_ids
    ]
    if case_ids is not None:
        missing = sorted(case_ids - {case.id for case in selected})
        if missing:
            raise QualificationError(
                "unknown qualification cases: " + ", ".join(missing)
            )
    contract = workflow_contract_sha256(workflow)
    results = [executor(case, project, contract) for case in selected]
    record_case_results(workflow, results, evidence_root=evidence_root)
    return results


def _latest_result(
    case: QualificationCase,
    project_revision: int,
) -> Optional[QualificationCaseResult]:
    matching = [
        result for result in case.results if result.project_revision == project_revision
    ]
    return matching[-1] if matching else None


def evaluate_qualification(
    workflow: "Workflow",
    *,
    policy: Optional["Policy"] = None,
    evidence_root: Optional[Path | str] = None,
) -> QualificationReport:
    """Evaluate qualification coverage without mutating or executing a workflow."""

    from openadapt_flow.policy import evaluate_policy, is_identity_armed
    from openadapt_flow.traversal import iter_workflow_steps

    steps = list(iter_workflow_steps(workflow))
    workflow_contract = workflow_contract_sha256(workflow)
    project = workflow.qualification
    if project is None:
        refusal = QualificationRefusal(
            code=QualificationRefusalCode.PROJECT_MISSING,
            path="qualification",
            message="workflow has no qualification project",
        )
        return QualificationReport(
            workflow_name=workflow.name,
            workflow_contract_sha256=workflow_contract,
            environment_contract_sha256=None,
            project_id=None,
            project_revision=None,
            policy_name=policy.name if policy else None,
            passed=False,
            action_count=len(steps),
            state_changing_action_count=0,
            consequential_action_count=len(steps),
            identity_covered_action_count=0,
            effect_required_action_count=len(steps),
            effect_covered_action_count=0,
            minimum_effect_tier=None,
            case_count=0,
            passed_case_count=0,
            refusals=[refusal],
        )

    refusals: list[QualificationRefusal] = []
    state_changing_steps: list[Step] = []
    consequential_steps: list[Step] = []
    for step in steps:
        classification = project.action_classifications.get(step.id)
        executable_floor = _executable_risk_floor(step)
        if classification is None:
            refusals.append(
                QualificationRefusal(
                    code=QualificationRefusalCode.ACTION_CLASSIFICATION_MISSING,
                    path=f"qualification.action_classifications.{step.id}",
                    step_id=step.id,
                    message="action has no reviewed business-risk classification",
                )
            )
            effective_classification = executable_floor
        elif (
            not classification.operator_confirmed
            or classification.classification is ActionRiskClass.UNKNOWN
        ):
            refusals.append(
                QualificationRefusal(
                    code=QualificationRefusalCode.ACTION_CLASSIFICATION_UNCONFIRMED,
                    path=f"qualification.action_classifications.{step.id}",
                    step_id=step.id,
                    message="action risk is unknown or has not been operator-confirmed",
                )
            )
            effective_classification = executable_floor
        else:
            effective_classification = classification.classification
            if (
                executable_floor is ActionRiskClass.IRREVERSIBLE
                and effective_classification is not ActionRiskClass.IRREVERSIBLE
            ):
                refusals.append(
                    QualificationRefusal(
                        code=QualificationRefusalCode.ACTION_CLASSIFICATION_CONFLICT,
                        path=f"qualification.action_classifications.{step.id}",
                        step_id=step.id,
                        message=(
                            "operator classification is weaker than the executable "
                            "irreversible action/effect contract"
                        ),
                    )
                )
                effective_classification = ActionRiskClass.IRREVERSIBLE
            elif (
                executable_floor is ActionRiskClass.STATE_CHANGING
                and effective_classification is ActionRiskClass.READ_ONLY
            ):
                refusals.append(
                    QualificationRefusal(
                        code=QualificationRefusalCode.ACTION_CLASSIFICATION_CONFLICT,
                        path=f"qualification.action_classifications.{step.id}",
                        step_id=step.id,
                        message=(
                            "an action with a declared business effect cannot be "
                            "classified read-only"
                        ),
                    )
                )
                effective_classification = ActionRiskClass.STATE_CHANGING

        if effective_classification is ActionRiskClass.STATE_CHANGING:
            state_changing_steps.append(step)
        elif effective_classification in {
            ActionRiskClass.CONSEQUENTIAL,
            ActionRiskClass.IRREVERSIBLE,
        }:
            consequential_steps.append(step)

    identity_covered = 0
    effect_covered = 0
    effect_bindings = {
        (binding.step_id, binding.actuation_path, binding.effect_index): binding
        for binding in project.effect_policies
    }

    for step in consequential_steps:
        identity_policy = project.identity_policies.get(step.id)
        if not is_identity_armed(step):
            refusals.append(
                QualificationRefusal(
                    code=QualificationRefusalCode.STEP_IDENTITY_UNARMED,
                    path=f"steps.{step.id}.identity_armed",
                    step_id=step.id,
                    message="consequential action is not identity-armed",
                )
            )
        if identity_policy is None:
            refusals.append(
                QualificationRefusal(
                    code=QualificationRefusalCode.IDENTITY_POLICY_MISSING,
                    path=f"qualification.identity_policies.{step.id}",
                    step_id=step.id,
                    message="consequential action has no identity match policy",
                )
            )
        elif identity_policy.enforcement is IdentityEnforcement.CANONICAL_LADDER:
            available = available_identity_sources(step)
            if not available:
                refusals.append(
                    QualificationRefusal(
                        code=QualificationRefusalCode.IDENTITY_SIGNAL_UNAVAILABLE,
                        path=f"qualification.identity_policies.{step.id}",
                        step_id=step.id,
                        message="canonical identity ladder has no retained evidence",
                    )
                )
            elif is_identity_armed(step):
                identity_covered += 1
        else:
            independence_errors = identity_policy_independence_errors(identity_policy)
            if independence_errors:
                refusals.append(
                    QualificationRefusal(
                        code=(
                            QualificationRefusalCode.IDENTITY_SIGNALS_NOT_INDEPENDENT
                        ),
                        path=f"qualification.identity_policies.{step.id}.signals",
                        step_id=step.id,
                        message=(
                            "identity quorum signals reuse correlated retained evidence"
                        ),
                        details={"error_count": len(independence_errors)},
                    )
                )
            unavailable_signals = [
                signal
                for signal in identity_policy.signals
                if not identity_signal_runtime_available(step, signal)
            ]
            for signal in unavailable_signals:
                refusals.append(
                    QualificationRefusal(
                        code=QualificationRefusalCode.IDENTITY_SIGNAL_UNAVAILABLE,
                        path=(
                            f"qualification.identity_policies.{step.id}."
                            f"signals.{signal.key.value}"
                        ),
                        step_id=step.id,
                        message=(
                            "qualified identity signal has no executable retained "
                            "comparison"
                        ),
                        details={
                            "signal": signal.key.value,
                            "source": signal.source.value,
                        },
                    )
                )
            if (
                is_identity_armed(step)
                and not independence_errors
                and not unavailable_signals
            ):
                identity_covered += 1

    effect_required_steps = state_changing_steps + consequential_steps
    consequential_ids = {step.id for step in consequential_steps}
    for step in effect_required_steps:
        step_effects_covered = True
        from openadapt_flow.policy import iter_effect_paths

        for actuation_path, path_effects in iter_effect_paths(step):
            if not path_effects:
                step_effects_covered = False
                refusals.append(
                    QualificationRefusal(
                        code=QualificationRefusalCode.EFFECT_CONTRACT_MISSING,
                        path=f"steps.{step.id}.{actuation_path}.effects",
                        step_id=step.id,
                        message=(
                            f"state-changing {actuation_path} path declares no "
                            "effect contract; one path cannot cover another"
                        ),
                    )
                )
                continue
            for index, effect in enumerate(path_effects):
                binding = effect_bindings.get((step.id, actuation_path, index))
                path = (
                    f"qualification.effect_policies.{step.id}.{actuation_path}.{index}"
                )
                if binding is None:
                    step_effects_covered = False
                    refusals.append(
                        QualificationRefusal(
                            code=QualificationRefusalCode.EFFECT_POLICY_MISSING,
                            path=path,
                            step_id=step.id,
                            message=(
                                f"{actuation_path} effect {index} has no "
                                "verification-strength policy"
                            ),
                        )
                    )
                    continue
                if binding.effect_contract_hash != effect.contract_hash():
                    step_effects_covered = False
                    refusals.append(
                        QualificationRefusal(
                            code=QualificationRefusalCode.EFFECT_CONTRACT_CHANGED,
                            path=f"{path}.effect_contract_hash",
                            step_id=step.id,
                            message=(
                                f"{actuation_path} effect {index} changed after "
                                "its tier was assigned"
                            ),
                        )
                    )
                if not binding.tier.satisfies(project.minimum_effect_tier):
                    step_effects_covered = False
                    refusals.append(
                        QualificationRefusal(
                            code=QualificationRefusalCode.EFFECT_TIER_INSUFFICIENT,
                            path=f"{path}.tier",
                            step_id=step.id,
                            message=(
                                f"{actuation_path} effect {index} tier "
                                f"{int(binding.tier)} is weaker than required tier "
                                f"{int(project.minimum_effect_tier)}"
                            ),
                            details={
                                "actual_tier": int(binding.tier),
                                "minimum_tier": int(project.minimum_effect_tier),
                            },
                        )
                    )
                if (
                    step.id in consequential_ids
                    and binding.tier is VerificationTier.IMMEDIATE_SCREEN
                ):
                    step_effects_covered = False
                    refusals.append(
                        QualificationRefusal(
                            code=QualificationRefusalCode.HIGH_RISK_SCREEN_ONLY,
                            path=f"{path}.tier",
                            step_id=step.id,
                            message=(
                                f"consequential {actuation_path} effect {index} "
                                "cannot qualify with immediate screen confirmation "
                                "alone"
                            ),
                        )
                    )
        if step_effects_covered:
            effect_covered += 1

    required_kinds = {
        QualificationCaseKind.AMBIGUITY,
        QualificationCaseKind.WRONG_IDENTITY,
        QualificationCaseKind.STALE_IDENTITY,
        QualificationCaseKind.WEAK_EFFECT,
        QualificationCaseKind.MISSING_EFFECT,
    }
    required_kinds_present = {case.kind for case in project.cases if case.required}
    if QualificationCaseKind.REPRESENTATIVE not in required_kinds_present:
        refusals.append(
            QualificationRefusal(
                code=QualificationRefusalCode.REPRESENTATIVE_CASE_MISSING,
                path="qualification.cases",
                message=(
                    "at least one required representative VERIFIED case is required"
                ),
            )
        )
    for kind in sorted(
        required_kinds - required_kinds_present,
        key=lambda item: item.value,
    ):
        refusals.append(
            QualificationRefusal(
                code=QualificationRefusalCode.FAULT_CASE_MISSING,
                path="qualification.cases",
                message=f"required {kind.value} fault case is missing",
                details={"kind": kind.value},
            )
        )

    passed_cases = 0
    required_cases = [case for case in project.cases if case.required]
    for case in required_cases:
        result = _latest_result(case, project.revision)
        if result is None or result.status != "passed":
            refusals.append(
                QualificationRefusal(
                    code=QualificationRefusalCode.CASE_NOT_PASSED,
                    path=f"qualification.cases.{case.id}.results",
                    case_id=case.id,
                    message=f"required case {case.id!r} has no passing current result",
                )
            )
            continue
        integrity_error = _case_result_integrity_error(
            workflow,
            project,
            result,
            evidence_root=Path(evidence_root) if evidence_root is not None else None,
        )
        if integrity_error is not None:
            refusals.append(
                QualificationRefusal(
                    code=integrity_error[0],
                    path=f"qualification.cases.{case.id}.results",
                    case_id=case.id,
                    message=integrity_error[1],
                )
            )
            continue
        if result.observed_outcome is not case.expected_outcome:
            refusals.append(
                QualificationRefusal(
                    code=QualificationRefusalCode.CASE_NOT_PASSED,
                    path=f"qualification.cases.{case.id}.results",
                    case_id=case.id,
                    message=(
                        f"case {case.id!r} observed {result.observed_outcome.value}; "
                        f"expected {case.expected_outcome.value}"
                    ),
                )
            )
            continue
        if not result.evidence:
            refusals.append(
                QualificationRefusal(
                    code=QualificationRefusalCode.CASE_EVIDENCE_MISSING,
                    path=f"qualification.cases.{case.id}.results",
                    case_id=case.id,
                    message=f"case {case.id!r} has no evidence reference",
                )
            )
            continue
        passed_cases += 1

    if policy is not None:
        policy_report = evaluate_policy(workflow, policy)
        for violation in policy_report.violations:
            refusals.append(
                QualificationRefusal(
                    code=QualificationRefusalCode.POLICY_VIOLATION,
                    path=(
                        f"steps.{violation.step_id}"
                        if violation.step_id
                        else "workflow"
                    ),
                    step_id=violation.step_id,
                    message=violation.reason,
                    details={"rule": violation.rule},
                )
            )

    return QualificationReport(
        workflow_name=workflow.name,
        workflow_contract_sha256=workflow_contract,
        environment_contract_sha256=project.environment.contract_sha256(),
        project_id=project.project_id,
        project_revision=project.revision,
        policy_name=policy.name if policy else None,
        passed=not refusals,
        action_count=len(steps),
        state_changing_action_count=len(state_changing_steps),
        consequential_action_count=len(consequential_steps),
        identity_covered_action_count=identity_covered,
        effect_required_action_count=len(effect_required_steps),
        effect_covered_action_count=effect_covered,
        minimum_effect_tier=project.minimum_effect_tier,
        case_count=len(required_cases),
        passed_case_count=passed_cases,
        refusals=refusals,
    )


def certify_project(
    workflow: "Workflow",
    *,
    policy: "Policy",
    evidence_root: Path | str,
) -> QualificationReport:
    """Evaluate and persist the exact qualification decision in memory."""

    project = workflow.qualification
    report = evaluate_qualification(
        workflow,
        policy=policy,
        evidence_root=evidence_root,
    )
    if project is not None:
        from openadapt_flow.policy import policy_contract_sha256

        project.last_certification = QualificationCertification(
            project_revision=project.revision,
            project_contract_sha256=project.contract_sha256(),
            workflow_contract_sha256=report.workflow_contract_sha256,
            environment_contract_sha256=project.environment.contract_sha256(),
            policy_name=policy.name,
            policy_contract_sha256=policy_contract_sha256(policy),
            policy_contract=policy.model_dump(mode="json"),
            passed=report.passed,
            report_sha256=report.report_sha256(),
        )
    workflow.stamp_certification(
        policy_name=policy.name,
        passed=report.passed,
        status="certified" if report.passed else "failed",
    )
    return report


def current_certification_matches(
    workflow: "Workflow",
    *,
    policy: Optional["Policy"] = None,
    policy_contract_digest: Optional[str] = None,
) -> bool:
    """Independently recompute the persisted production qualification.

    The persisted ``passed`` bit is never authority.  The exact policy,
    workflow, environment, project, signed case attestations, and resulting
    qualification-report digest must all reproduce from current bundle state.
    """

    project = workflow.qualification
    if project is None or project.last_certification is None:
        return False
    certification = project.last_certification
    if not certification.passed:
        return False

    from openadapt_flow.policy import Policy, policy_contract_sha256

    try:
        embedded_policy = (
            Policy.model_validate(certification.policy_contract)
            if certification.policy_contract is not None
            else None
        )
    except ValueError:
        return False
    if embedded_policy is None:
        return False
    embedded_digest = policy_contract_sha256(embedded_policy)
    if policy is not None:
        effective_policy = policy
        digest = policy_contract_sha256(policy)
        if policy_contract_digest is not None and policy_contract_digest != digest:
            return False
    elif policy_contract_digest is not None:
        effective_policy = embedded_policy
        digest = policy_contract_digest
    else:
        # The bundle-local copy is evidence of what was evaluated, not an
        # authority that can bootstrap its own production policy decision.
        return False
    if (
        certification.policy_name != effective_policy.name
        or certification.policy_name != embedded_policy.name
        or certification.policy_contract_sha256 != digest
        or embedded_digest != digest
    ):
        return False

    report = evaluate_qualification(workflow, policy=effective_policy)
    return bool(
        report.passed
        and certification.project_revision == project.revision
        and certification.project_contract_sha256 == project.contract_sha256()
        and certification.workflow_contract_sha256 == report.workflow_contract_sha256
        and certification.environment_contract_sha256
        == project.environment.contract_sha256()
        and certification.report_sha256 == report.report_sha256()
    )


def save_qualified_workflow(
    workflow: "Workflow",
    bundle_dir: Path | str,
    *,
    key: Optional[str] = None,
) -> Path:
    """Reseal a mutated workflow while preserving its at-rest mode."""

    project = workflow.qualification
    certification = project.last_certification if project is not None else None
    if project is not None and certification is not None:
        if (
            certification.project_revision != project.revision
            or certification.project_contract_sha256 != project.contract_sha256()
            or certification.workflow_contract_sha256
            != workflow_contract_sha256(workflow)
            or certification.environment_contract_sha256
            != project.environment.contract_sha256()
        ):
            _invalidate_certification(workflow)
            raise QualificationError(
                "workflow, environment, or project changed after certification"
            )
        if certification.passed:
            from openadapt_flow.policy import Policy

            try:
                embedded_policy = Policy.model_validate(certification.policy_contract)
            except ValueError:
                _invalidate_certification(workflow)
                raise QualificationError(
                    "persisted certification policy contract is invalid"
                ) from None
            if not current_certification_matches(workflow, policy=embedded_policy):
                _invalidate_certification(workflow)
                raise QualificationError(
                    "persisted certification cannot be independently reproduced"
                )
    if workflow.encrypted:
        path = workflow.save(bundle_dir, encrypt=True, key=key)
    else:
        path = workflow.save(bundle_dir, encrypt=False)
    if certification is not None and (
        certification.workflow_contract_sha256 != workflow_contract_sha256(workflow)
    ):
        _invalidate_certification(workflow)
        if workflow.encrypted:
            workflow.save(bundle_dir, encrypt=True, key=key)
        else:
            workflow.save(bundle_dir, encrypt=False)
        raise QualificationError(
            "sealed asset inventory changed after the qualification campaign"
        )
    return path


def project_schema() -> dict[str, Any]:
    """JSON Schema for Desktop/CLI/API consumers."""

    return QualificationProject.model_json_schema()
