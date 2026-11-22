"""Data model of the qualification-project contract.

Constants, enums and Pydantic models of a qualification project, and the
helpers they use. They moved here unchanged from ``openadapt_flow/qualification.py``;
``openadapt_flow.qualification`` re-exports every name, so existing imports keep
working.
"""

from __future__ import annotations

import hashlib
import json
import re
from base64 import b64decode
from datetime import datetime, timezone
from enum import Enum
from pathlib import PurePosixPath
from typing import (
    TYPE_CHECKING,
    Any,
    Final,
    Literal,
    Mapping,
    Optional,
)
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)

from openadapt_flow.identity_signals import (
    canonical_normalizers,
)
from openadapt_flow.judgment_cases import (
    JudgmentCaseV1,
    JudgmentFactSchemaBindingV1,
)
from openadapt_flow.lookup_capabilities import QualifiedLookup
from openadapt_flow.qualified_business_decisions import (
    QualifiedBusinessDecisionDelivery,
)
from openadapt_flow.verification import VerificationTier

if TYPE_CHECKING:  # pragma: no cover
    from openadapt_flow.ir import Workflow


QUALIFICATION_SCHEMA: Final[Literal["openadapt.qualification-project/v1"]] = (
    "openadapt.qualification-project/v1"
)
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PARAM_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_CONTEXT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_QUALIFIED_ENTITY_LABEL_RE = re.compile(r"^[a-z][a-z0-9]*(?:[ _-][a-z0-9]+){0,3}$")


def entity_label_options() -> list[dict[str, str]]:
    """Return the reviewed remote-safe class labels and their fallbacks.

    This is the public, JSON-serializable source for Desktop and Flow. The
    values are presentation classes only; no identity value or identifier may
    enter this API.
    """

    return [
        {"label": "patient record", "fallback": "record"},
        {"label": "member record", "fallback": "record"},
        {"label": "insurance claim", "fallback": "item"},
        {"label": "loan application", "fallback": "item"},
        {"label": "customer account", "fallback": "record"},
        {"label": "service request", "fallback": "item"},
        {"label": "case", "fallback": "item"},
        {"label": "order", "fallback": "item"},
        {"label": "invoice", "fallback": "item"},
        {"label": "document", "fallback": "item"},
        {"label": "record", "fallback": "record"},
        {"label": "item", "fallback": "item"},
    ]


#: Derived compatibility view of :func:`entity_label_options` for callers that
#: need only labels. Do not add an independent list of remote-safe classes.
REMOTE_SAFE_ENTITY_LABELS: Final[tuple[str, ...]] = tuple(
    option["label"] for option in entity_label_options()
)


def _qualification_identifier_sha256(value: str, *, kind: str) -> str:
    if not _ID_RE.fullmatch(value):
        raise ValueError(f"qualification {kind} id is invalid")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def qualification_campaign_id_sha256(campaign_id: str) -> str:
    """Return the canonical digest used to bind one qualification campaign.

    The campaign identifier stays local.  Reports and authorizations retain
    only this digest, so Desktop and Flow must derive it through this one API.
    """

    return _qualification_identifier_sha256(campaign_id, kind="campaign")


def qualification_run_id_sha256(run_id: str) -> str:
    """Return the canonical digest used to bind one qualification run."""

    return _qualification_identifier_sha256(run_id, kind="run")


def api_folded_step_ids(
    workflow: "Workflow", action_paths: Mapping[str, str]
) -> set[str]:
    """The steps an API step folds on the path a case names for its save.

    An API step (a ``web_form`` binding) stands for the GUI steps it
    replaces. On its API path they are folded, not executed, and the save's
    own result carries the action and record-check evidence. On any other
    path nothing is folded.
    """

    return {
        step_id
        for save in workflow.steps
        if save.api_binding is not None
        and save.api_binding.kind == "web_form"
        and save.api_binding.web_form is not None
        and action_paths.get(save.id) == "api"
        for step_id in save.api_binding.web_form.replaces_steps[:-1]
    }


def _qualification_actuation_path(
    value: Optional[str],
) -> Optional[Literal["gui", "api"]]:
    """Project one closed runtime actuation receipt onto a qualified path."""
    from openadapt_flow.execution_profiles import AUTOMATED_GUI_ACTUATIONS

    if value == "api":
        return "api"
    if value in AUTOMATED_GUI_ACTUATIONS:
        return "gui"
    return None


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


class QualificationActionTarget(BaseModel):
    """One exact workflow action and actuation path exercised by a case."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: str = Field(pattern=_ID_RE)
    actuation_path: Literal["gui", "api"]


class EvidenceRef(BaseModel):
    """Local or customer-controlled evidence reference; never evidence content."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal[
        "run_report",
        "identity",
        "effect",
        "case_input",
        "fault_receipt",
        "fault_mutation",
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
    campaign_id_sha256: Optional[str] = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    case_input_sha256: Optional[str] = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    run_id_sha256: Optional[str] = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    evidence: list[EvidenceRef] = Field(default_factory=list)
    detail_code: Optional[str] = Field(default=None, max_length=128)
    completed_at: str = Field(default_factory=_now)
    attestation_key_id: str = Field(min_length=1, max_length=128)
    attestation_signature: str = ""

    @model_serializer(mode="wrap")
    def _serialize_compatible(self, handler: Any) -> dict[str, Any]:
        """Keep existing signed result payloads byte-stable."""

        data: dict[str, Any] = handler(self)
        for field in (
            "campaign_id_sha256",
            "case_input_sha256",
            "run_id_sha256",
        ):
            if data.get(field) is None:
                data.pop(field, None)
        return data

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
    runtime_input_sha256: Optional[str] = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    action_targets: list[QualificationActionTarget] = Field(default_factory=list)
    fault_target: Optional[QualificationActionTarget] = None
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
        targets = sorted(
            self.action_targets,
            key=lambda item: (item.step_id, item.actuation_path),
        )
        if self.kind is QualificationCaseKind.REPRESENTATIVE:
            if self.fault_target is not None:
                raise ValueError("a representative case cannot bind a fault target")
            step_ids = [item.step_id for item in targets]
            if len(step_ids) != len(set(step_ids)):
                raise ValueError(
                    "a representative case can exercise only one path per step"
                )
        else:
            step_ids = [item.step_id for item in targets]
            if len(step_ids) != len(set(step_ids)):
                raise ValueError("a fault case can permit only one path per step")
            if self.fault_target is None and len(targets) == 1:
                # Projects created before fault_target used the sole permitted
                # action as both the execution scope and mutation target.
                self.fault_target = targets[0]
            elif self.fault_target is None and len(targets) > 1:
                raise ValueError(
                    "a fault case with multiple permitted actions requires one "
                    "exact fault target"
                )
            if self.fault_target is not None and self.fault_target not in targets:
                raise ValueError(
                    "a fault target must be inside the permitted action scope"
                )
        self.action_targets = targets
        return self

    @model_serializer(mode="wrap")
    def _serialize_compatible(self, handler: Any) -> dict[str, Any]:
        """Keep old projects loadable while certification fails closed."""

        data: dict[str, Any] = handler(self)
        if self.runtime_input_sha256 is None:
            data.pop("runtime_input_sha256", None)
        if not self.action_targets:
            data.pop("action_targets", None)
        if self.fault_target is None or (
            len(self.action_targets) == 1
            and self.fault_target == self.action_targets[0]
        ):
            # Preserve the legacy single-target project contract. The validator
            # reconstructs the implied target when it loads the project.
            data.pop("fault_target", None)
        return data

    def resolved_fault_target(self) -> Optional[QualificationActionTarget]:
        """Return the explicit target or the legacy sole permitted action."""

        if self.kind is QualificationCaseKind.REPRESENTATIVE:
            return None
        if self.fault_target is not None:
            return self.fault_target
        if len(self.action_targets) == 1:
            return self.action_targets[0]
        return None


_QUALIFICATION_CASE_CONTRACT_DOMAIN = b"openadapt-qualification-case-contract-v1\0"


def qualification_case_contract_sha256(case: "QualificationCase") -> str:
    """Digest one stable case contract without descriptions or result rows."""

    if case.runtime_input_sha256 is None:
        raise ValueError("qualification case has no approved runtime-input digest")
    action_targets = [
        item.model_dump(mode="json")
        for item in sorted(
            case.action_targets,
            key=lambda item: (item.step_id, item.actuation_path),
        )
    ]
    payload = {
        "schema_version": "openadapt.qualification-case-contract/v1",
        "qualification_case_id": case.id,
        "qualification_case_kind": case.kind.value,
        "expected_outcome": case.expected_outcome.value,
        "runtime_input_sha256": case.runtime_input_sha256,
        "action_targets": action_targets,
        "fault_target": (
            case.fault_target.model_dump(mode="json")
            if case.fault_target is not None
            else None
        ),
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(_QUALIFICATION_CASE_CONTRACT_DOMAIN + encoded).hexdigest()


class EnvironmentBoundary(BaseModel):
    """Application/environment scope in which the qualification is valid."""

    model_config = ConfigDict(extra="forbid")

    target_kind: Literal["web", "windows", "macos", "linux", "rdp", "citrix"]
    application: str = Field(min_length=1, max_length=256)
    application_identity: Optional[str] = Field(default=None, max_length=320)
    application_version: str = Field(min_length=1, max_length=128)
    environment_observer_id: Optional[str] = Field(default=None, pattern=_ID_RE)
    environment_observer_contract_sha256: Optional[str] = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    environment_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    runtime_version: str = Field(min_length=1, max_length=64)
    required_capabilities: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _valid_observed_identity(self) -> "EnvironmentBoundary":
        if self.application_identity is not None and not _valid_application_identity(
            self.application_identity
        ):
            raise ValueError(
                "application_identity must be an exact native application id "
                "or bounded HTTP(S) origin"
            )
        observer = (
            self.environment_observer_id,
            self.environment_observer_contract_sha256,
        )
        if any(value is not None for value in observer) and not all(
            value is not None for value in observer
        ):
            raise ValueError("environment observer binding is incomplete")
        return self

    @model_serializer(mode="wrap")
    def _serialize_compatible(self, handler: Any) -> dict[str, Any]:
        """Keep old qualification contracts byte-stable until bindings exist."""

        data: dict[str, Any] = handler(self)
        if self.application_identity is None:
            data.pop("application_identity", None)
        if self.environment_observer_id is None:
            data.pop("environment_observer_id", None)
        if self.environment_observer_contract_sha256 is None:
            data.pop("environment_observer_contract_sha256", None)
        return data

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

    @property
    def expected_application_identity(self) -> Optional[str]:
        """Return the executable identity, separate from its display name."""

        return self.application_identity


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
    case_evidence_contract_sha256: Optional[str] = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    execution_profile: Literal["standard", "regulated"] = "standard"
    certified_at: str = Field(default_factory=_now)
    #: Digest of the project's qualified-lookup record (the lookup
    #: capabilities and surface the qualification ran with). None for a
    #: certification made before that record existed.
    lookup_sha256: Optional[str] = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_serializer(mode="wrap")
    def _serialize_compatible(self, handler: Any) -> dict[str, Any]:
        """Keep certifications made before the lookup record byte-stable."""

        data: dict[str, Any] = handler(self)
        if self.lookup_sha256 is None:
            data.pop("lookup_sha256", None)
        return data


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


class QualifiedEntityLabel(BaseModel):
    """An optional local presentation label for one qualified step.

    Qualification and execution do not require this label. Reviewed labels can
    cross a remote boundary. A custom label remains local and remote consumers
    receive its signed neutral ``record`` or ``item`` fallback.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: str = Field(pattern=_ID_RE.pattern)
    label: str = Field(
        min_length=1,
        max_length=63,
        json_schema_extra={
            "examples": ["patient record", "insurance claim", "record"],
            "x-openadapt-reviewed-remote-options": list(REMOTE_SAFE_ENTITY_LABELS),
        },
    )
    fallback: Literal["record", "item"]

    @field_validator("label")
    @classmethod
    def _safe_label(cls, value: str) -> str:
        if _QUALIFIED_ENTITY_LABEL_RE.fullmatch(value) is None:
            raise ValueError(
                "entity label must be a lowercase class name of at most four words"
            )
        return value

    @model_validator(mode="after")
    def _canonical_fallback(self) -> "QualifiedEntityLabel":
        expected = next(
            (
                option["fallback"]
                for option in entity_label_options()
                if option["label"] == self.label
            ),
            None,
        )
        if expected is not None and self.fallback != expected:
            raise ValueError("entity fallback must match the reviewed class mapping")
        return self


def remote_entity_label(
    entity: Optional[QualifiedEntityLabel],
) -> dict[str, str]:
    """Return the reviewed entity label that a remote V2 task can carry.

    Custom qualification labels remain inside the local bundle. Their signed
    neutral fallback crosses the remote boundary instead. A missing optional
    label uses ``record`` without weakening any other V2 binding.
    """

    if entity is None:
        return {"label": "record", "fallback": "record"}
    payload = entity.model_dump(mode="json", exclude={"step_id"})
    if payload in entity_label_options():
        return payload
    return {"label": entity.fallback, "fallback": entity.fallback}


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
    #: Multi-surface workflows only (docs/MULTI_SURFACE.md): the qualified
    #: environment of each NON-primary surface, keyed by surface id. The
    #: primary surface is ``environment``. A qualification case run of a
    #: multi-surface workflow observes every surface it switches to against
    #: its boundary here and refuses a surface that has none. Empty (and
    #: omitted from the contract bytes) for every single-surface project.
    surface_environments: dict[str, EnvironmentBoundary] = Field(default_factory=dict)
    minimum_effect_tier: VerificationTier = (
        VerificationTier.PERSISTED_STATE_REACQUISITION
    )
    action_classifications: dict[str, ActionRiskClassification] = Field(
        default_factory=dict
    )
    identity_policies: dict[str, IdentityPolicy] = Field(default_factory=dict)
    effect_policies: list[EffectVerificationPolicy] = Field(default_factory=list)
    #: Optional presentation-only names selected during qualification. They are
    #: not safety evidence, observations, or runtime-derived values. An empty
    #: mapping is valid and does not affect qualification admission.
    entity_labels: dict[str, "QualifiedEntityLabel"] = Field(default_factory=dict)
    #: Local reviewed cases for graph decision nodes. They are not executable
    #: rules. Each case binds to an exact workflow and decision contract.
    judgment_fact_schemas: list[JudgmentFactSchemaBindingV1] = Field(
        default_factory=list
    )
    judgment_cases: list[JudgmentCaseV1] = Field(default_factory=list)
    #: Reviewed static mobile presentation and delivery policy for exact graph
    #: decisions. Keys and live values remain outside the bundle.
    business_decision_deliveries: list[QualifiedBusinessDecisionDelivery] = Field(
        default_factory=list
    )
    cases: list[QualificationCase] = Field(default_factory=_default_fault_cases)
    exclusions: list[str] = Field(default_factory=list)
    requalification_conditions: list[RequalificationCondition] = Field(
        default_factory=list
    )
    trusted_runner_keys: dict[str, str] = Field(default_factory=dict)
    trusted_fault_driver_keys: dict[str, str] = Field(default_factory=dict)
    #: The lookup capabilities (structure, pixels) the signed cases actually
    #: used, the deployment surface they ran for, and the pixels-only pass.
    #: Written by ``openadapt-flow qualify`` after the cases run, so it is an
    #: outcome rather than part of the contract digest; the certification
    #: binds its digest. None on bundles qualified before it existed.
    lookup: Optional[QualifiedLookup] = None
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
        for key, entity in self.entity_labels.items():
            if key != entity.step_id:
                raise ValueError(
                    f"entity label key {key!r} does not match step_id "
                    f"{entity.step_id!r}"
                )
        for key_kind, keys in (
            ("runner", self.trusted_runner_keys),
            ("fault driver", self.trusted_fault_driver_keys),
        ):
            for key_id, public_key in keys.items():
                if not _ID_RE.fullmatch(key_id):
                    raise ValueError(f"trusted {key_kind} key id is invalid")
                try:
                    raw_key = b64decode(public_key, validate=True)
                except ValueError as exc:
                    raise ValueError(
                        f"trusted {key_kind} public key must be base64"
                    ) from exc
                if len(raw_key) != 32:
                    raise ValueError(
                        f"trusted {key_kind} public key must be 32-byte Ed25519"
                    )
        from openadapt_flow.ir import SURFACE_ID_PATTERN

        for surface_id in self.surface_environments:
            if not re.fullmatch(SURFACE_ID_PATTERN, surface_id):
                raise ValueError("surface environment key is not a surface id")
        case_ids = [case.id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("qualification case ids must be unique")
        judgment_case_ids = [case.id for case in self.judgment_cases]
        if len(judgment_case_ids) != len(set(judgment_case_ids)):
            raise ValueError("judgment case ids must be unique")
        schema_nodes = [
            (item.graph_id, item.state_id) for item in self.judgment_fact_schemas
        ]
        if len(schema_nodes) != len(set(schema_nodes)):
            raise ValueError("judgment fact schemas must bind unique graph states")
        delivery_nodes = [
            (item.graph_id, item.state_id) for item in self.business_decision_deliveries
        ]
        if len(delivery_nodes) != len(set(delivery_nodes)):
            raise ValueError(
                "business decision deliveries must bind unique graph states"
            )
        effect_refs = [
            (binding.step_id, binding.actuation_path, binding.effect_index)
            for binding in self.effect_policies
        ]
        if len(effect_refs) != len(set(effect_refs)):
            raise ValueError("effect verification references must be unique")
        return self

    @model_serializer(mode="wrap")
    def _serialize_compatible(self, handler: Any) -> dict[str, Any]:
        """Omit the additive trust store until a fault driver is bound."""

        data: dict[str, Any] = handler(self)
        if not self.surface_environments:
            data.pop("surface_environments", None)
        if not self.trusted_fault_driver_keys:
            data.pop("trusted_fault_driver_keys", None)
        if not self.judgment_fact_schemas:
            data.pop("judgment_fact_schemas", None)
        if not self.judgment_cases:
            data.pop("judgment_cases", None)
        if not self.business_decision_deliveries:
            data.pop("business_decision_deliveries", None)
        if self.lookup is None:
            data.pop("lookup", None)
        return data

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
                "lookup",
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
    REPRESENTATIVE_ACTION_UNCOVERED = "representative_action_uncovered"
    FAULT_CASE_MISSING = "fault_case_missing"
    FAULT_ACTION_UNCOVERED = "fault_action_uncovered"
    CASE_INPUT_UNBOUND = "case_input_unbound"
    CASE_TARGET_INVALID = "case_target_invalid"
    CASE_NOT_PASSED = "case_not_passed"
    CASE_EVIDENCE_MISSING = "case_evidence_missing"
    CASE_EVIDENCE_UNVERIFIED = "case_evidence_unverified"
    CASE_ATTESTATION_INVALID = "case_attestation_invalid"
    CASE_WORKFLOW_CHANGED = "case_workflow_changed"
    CASE_ENVIRONMENT_CHANGED = "case_environment_changed"
    CASE_RUNTIME_CHANGED = "case_runtime_changed"
    CASE_CAPABILITY_MISSING = "case_capability_missing"
    JUDGMENT_CASE_BINDING_INVALID = "judgment_case_binding_invalid"
    JUDGMENT_CASE_CONFLICT = "judgment_case_conflict"
    JUDGMENT_CASE_CONTRAST_MISSING = "judgment_case_contrast_missing"
    JUDGMENT_CASE_MORE_EVIDENCE_REQUIRED = "judgment_case_more_evidence_required"
    JUDGMENT_CASE_EVIDENCE_UNVERIFIED = "judgment_case_evidence_unverified"
    BUSINESS_DECISION_DELIVERY_INVALID = "business_decision_delivery_invalid"
    POLICY_VIOLATION = "policy_violation"
    SUBROUTINE_NOT_ADMITTED = "subroutine_not_admitted"


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
