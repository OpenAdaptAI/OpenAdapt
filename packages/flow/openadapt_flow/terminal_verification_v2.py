"""Production terminal-verification v2 contracts and producer.

Flow builds one success-only artifact after it revalidates a complete VERIFIED
report, run receipt, signed permit chain, and class-separated evidence. The
acceptor must reconstruct every value from immutable storage before it admits
the result as Production success.
"""

from __future__ import annotations

import hashlib
import re
from base64 import b64decode, b64encode, urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import (
    BaseModel,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

from openadapt_flow.ir import PostconditionContractEvidence, RunReport
from openadapt_flow.qualification_admission_v2 import (
    MAX_PERMIT_SNAPSHOT_AGE,
    ClosedSignedModel,
    canonical_json,
)
from openadapt_flow.receipt import ReceiptError, RunReceipt, build_receipt

SCHEMA: Final[Literal["openadapt.production-terminal-verification/v2"]] = (
    "openadapt.production-terminal-verification/v2"
)
SIGNATURE_DOMAIN: Final[bytes] = b"openadapt-production-terminal-verification-v2\0"
PERMIT_CHAIN_DOMAIN: Final[bytes] = b"openadapt-managed-delivery-permit-chain-v2\0"
PERMIT_CHAIN_SCHEMA: Final[Literal["openadapt.production-delivery-permit-chain/v2"]] = (
    "openadapt.production-delivery-permit-chain/v2"
)
PERMIT_PAYLOAD_DOMAIN: Final[bytes] = (
    b"openadapt.production-delivery-permit-payload.v2\0"
)
PERMIT_PAYLOAD_SCHEMA: Final[
    Literal["openadapt.production-delivery-permit-payload/v2"]
] = "openadapt.production-delivery-permit-payload/v2"
PERMIT_ARTIFACT_SCHEMA: Final[
    Literal["openadapt.production-delivery-permit-artifact/v2"]
] = "openadapt.production-delivery-permit-artifact/v2"
DELIVERY_RECEIPT_PAYLOAD_DOMAIN: Final[bytes] = (
    b"openadapt.production-delivery-receipt-payload.v2\0"
)
DELIVERY_RECEIPT_PAYLOAD_SCHEMA: Final[
    Literal["openadapt.production-delivery-receipt-payload/v2"]
] = "openadapt.production-delivery-receipt-payload/v2"
DELIVERY_RECEIPT_ARTIFACT_SCHEMA: Final[
    Literal["openadapt.production-delivery-receipt-artifact/v2"]
] = "openadapt.production-delivery-receipt-artifact/v2"
PERMIT_CHAIN_ENTRY_SCHEMA: Final[
    Literal["openadapt.production-delivery-permit-chain-entry/v2"]
] = "openadapt.production-delivery-permit-chain-entry/v2"
EXECUTION_AUTHORITY_DOMAIN: Final[bytes] = (
    b"openadapt.production-execution-authority.v2\0"
)
EXECUTION_AUTHORITY_SCHEMA: Final[
    Literal["openadapt.production-execution-authority/v2"]
] = "openadapt.production-execution-authority/v2"

_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+\-]{0,199}$")
_UTC_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_RUNNER_KEY_ID_RE = re.compile(r"^evidence-runner-ed25519-[a-f0-9]{16}$")
_AUTHORITY_KEY_ID_RE = re.compile(r"^delivery-authority-ed25519-[a-f0-9]{16}$")
JS_MAX_SAFE_INTEGER: Final[int] = 9_007_199_254_740_991
MAX_DELIVERY_ARTIFACT_BYTES: Final[int] = 1_048_576
EXECUTION_OUTCOME_DOMAIN: Final[bytes] = b"openadapt-production-execution-outcome-v2\0"


class ProductionTerminalVerificationError(ValueError):
    """The production success proof is invalid or does not match live state."""


@dataclass(frozen=True)
class PreparedProductionTerminalEvidence:
    """Private exact report bytes and their safe terminal projections."""

    report_bytes: bytes
    report_sha256: str
    flow_run_id_sha256: str
    bundle_content_digest: str
    execution_outcome: "ProductionExecutionOutcome"
    run_receipt: "ProductionRunReceipt"
    run_receipt_sha256: str
    report: RunReport = dataclass_field(repr=False)


def _parse_utc(value: str, *, field: str) -> datetime:
    if not isinstance(value, str) or _UTC_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be a canonical UTC timestamp")
    return datetime.fromisoformat(value[:-1] + "+00:00").astimezone(timezone.utc)


def _sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def evidence_runner_key_id(public_key: bytes) -> str:
    if len(public_key) != 32:
        raise ValueError("evidence runner public key is invalid")
    return f"evidence-runner-ed25519-{hashlib.sha256(public_key).hexdigest()[:16]}"


def evidence_runner_signer_sha256(public_key: bytes) -> str:
    if len(public_key) != 32:
        raise ValueError("evidence runner public key is invalid")
    return hashlib.sha256(public_key).hexdigest()


class TerminalContractCounts(ClosedSignedModel):
    authorization: StrictInt = Field(ge=1, le=JS_MAX_SAFE_INTEGER)
    identity: StrictInt = Field(ge=1, le=JS_MAX_SAFE_INTEGER)
    postcondition: StrictInt = Field(ge=1, le=JS_MAX_SAFE_INTEGER)
    effect: StrictInt = Field(ge=1, le=JS_MAX_SAFE_INTEGER)


class ProductionExecutionOutcome(ClosedSignedModel):
    """Complete, explicit projection of the current Flow outcome contract."""

    version: Literal["openadapt.execution-outcome/v1"] = (
        "openadapt.execution-outcome/v1"
    )
    outcome: Literal["VERIFIED"] = "VERIFIED"
    profile: Literal["standard", "regulated"]
    production_eligible: Literal[True] = True
    qualification_evidence_only: Literal[False] = False
    execution_completed: Literal[True] = True
    required_contracts: TerminalContractCounts
    passed_contracts: TerminalContractCounts
    workflow_contract_sha256: str = Field(pattern=_SHA256_RE)
    postcondition_evidence: tuple[PostconditionContractEvidence, ...] = Field(
        min_length=1, max_length=10_000
    )
    evidence_classes: tuple[
        Literal[
            "authorization",
            "identity",
            "postcondition",
            "effect_tier_1",
            "effect_tier_2",
            "effect_tier_3",
            "model",
        ],
        ...,
    ] = Field(min_length=4, max_length=7)
    model_calls: StrictInt = Field(ge=0, le=JS_MAX_SAFE_INTEGER)
    external_network_calls: Literal["none", "observed"]
    compensation_actions: Literal[0] = 0

    @model_validator(mode="after")
    def _complete_verified_outcome(self) -> "ProductionExecutionOutcome":
        if self.passed_contracts != self.required_contracts:
            raise ValueError(
                "terminal VERIFIED outcome lacks complete contract coverage"
            )
        if self.required_contracts.authorization != 1:
            raise ValueError("terminal VERIFIED outcome requires one authorization")
        if len(self.postcondition_evidence) != self.required_contracts.postcondition:
            raise ValueError("terminal postcondition evidence cardinality is invalid")
        if any(
            item.verdict != "passed"
            or item.workflow_contract_sha256 != self.workflow_contract_sha256
            for item in self.postcondition_evidence
        ):
            raise ValueError("terminal postcondition evidence is not fully verified")
        keys = tuple(
            (item.result_index, item.contract_kind, item.contract_index)
            for item in self.postcondition_evidence
        )
        if len(keys) != len(set(keys)):
            raise ValueError("terminal postcondition evidence is duplicated")
        if any(
            item.result_index > JS_MAX_SAFE_INTEGER
            or item.step_index > JS_MAX_SAFE_INTEGER
            or item.contract_index > JS_MAX_SAFE_INTEGER
            for item in self.postcondition_evidence
        ):
            raise ValueError("terminal postcondition index exceeds the safe range")
        if self.evidence_classes != tuple(sorted(self.evidence_classes)) or len(
            self.evidence_classes
        ) != len(set(self.evidence_classes)):
            raise ValueError("terminal evidence classes must be unique and ordered")
        required_classes = {"authorization", "identity", "postcondition"}
        if not required_classes.issubset(self.evidence_classes):
            raise ValueError(
                "terminal VERIFIED outcome lacks required evidence classes"
            )
        effect_classes = {
            item for item in self.evidence_classes if item.startswith("effect_tier_")
        }
        if len(effect_classes) != 1:
            raise ValueError("terminal VERIFIED outcome requires one effect tier")
        if (self.model_calls > 0) != ("model" in self.evidence_classes):
            raise ValueError("terminal model evidence does not match its call count")
        if self.model_calls > 0 and self.external_network_calls != "observed":
            raise ValueError("terminal model calls require observed network evidence")
        return self

    def artifact_sha256(self) -> str:
        return hashlib.sha256(
            EXECUTION_OUTCOME_DOMAIN + canonical_json(self)
        ).hexdigest()


class ProductionRunReceipt(ClosedSignedModel):
    """Cross-language integer projection of every current RunReceipt field."""

    schema_version: Literal["openadapt.production-run-receipt/v1"] = (
        "openadapt.production-run-receipt/v1"
    )
    source_schema_version: Literal["openadapt.run-receipt/v2"] = (
        "openadapt.run-receipt/v2"
    )
    outcome: Literal["VERIFIED"]
    transaction_outcome: Literal["VERIFIED"]
    profile: Literal["standard", "regulated"]
    production_eligible: Literal[True]
    steps_total: StrictInt = Field(ge=1, le=JS_MAX_SAFE_INTEGER)
    steps_ok: StrictInt = Field(ge=1, le=JS_MAX_SAFE_INTEGER)
    heals: StrictInt = Field(ge=0, le=JS_MAX_SAFE_INTEGER)
    model_calls: StrictInt = Field(ge=0, le=JS_MAX_SAFE_INTEGER)
    est_cost_microusd: StrictInt = Field(ge=0, le=JS_MAX_SAFE_INTEGER)
    duration_ms: StrictInt = Field(ge=0, le=JS_MAX_SAFE_INTEGER)
    rung_histogram: dict[
        Literal[
            "structural",
            "template",
            "template_global",
            "geometry",
            "ocr",
            "grounder",
            "api",
        ],
        int,
    ]
    evidence_classes: tuple[
        Literal[
            "authorization",
            "identity",
            "postcondition",
            "effect_tier_1",
            "effect_tier_2",
            "effect_tier_3",
            "model",
        ],
        ...,
    ] = Field(min_length=4, max_length=7)
    effect_tier_reached: Literal[
        "independent_system",
        "independent_session",
        "persisted_state_reacquisition",
    ]
    authorization_required: StrictInt = Field(ge=1, le=JS_MAX_SAFE_INTEGER)
    authorization_confirmed: StrictInt = Field(ge=1, le=JS_MAX_SAFE_INTEGER)
    identity_required: StrictInt = Field(ge=1, le=JS_MAX_SAFE_INTEGER)
    identity_confirmed: StrictInt = Field(ge=1, le=JS_MAX_SAFE_INTEGER)
    postconditions_required: StrictInt = Field(ge=1, le=JS_MAX_SAFE_INTEGER)
    postconditions_confirmed: StrictInt = Field(ge=1, le=JS_MAX_SAFE_INTEGER)
    effects_required: StrictInt = Field(ge=1, le=JS_MAX_SAFE_INTEGER)
    effects_confirmed: StrictInt = Field(ge=1, le=JS_MAX_SAFE_INTEGER)
    identity_armed: StrictInt = Field(ge=1, le=JS_MAX_SAFE_INTEGER)
    identity_applicable: StrictInt = Field(ge=1, le=JS_MAX_SAFE_INTEGER)
    over_halt_count: Literal[0]
    substrate: Literal["web", "windows", "macos", "linux", "rdp", "citrix"]
    provenance: Literal["production"]
    receipt_builder_version: str = Field(
        pattern=r"^[0-9]+\.[0-9]+\.[0-9]+(?:-(?:a|b|rc)[0-9]+)?$"
    )
    external_network_calls: Literal["none", "observed"]
    bundle_digest: str = Field(pattern=_SHA256_RE)
    source_receipt_digest: str = Field(pattern=_SHA256_RE)
    source_receipt_sha256: str = Field(pattern=_SHA256_RE)
    generated_at: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:00:00Z$")

    @model_validator(mode="after")
    def _closed_receipt(self) -> "ProductionRunReceipt":
        for label, required, confirmed in (
            (
                "authorization",
                self.authorization_required,
                self.authorization_confirmed,
            ),
            ("identity", self.identity_required, self.identity_confirmed),
            (
                "postcondition",
                self.postconditions_required,
                self.postconditions_confirmed,
            ),
            ("effect", self.effects_required, self.effects_confirmed),
        ):
            if required != confirmed:
                raise ValueError(f"production receipt lacks complete {label} coverage")
        if self.authorization_required != 1:
            raise ValueError("production receipt requires one authorization")
        if self.steps_ok != self.steps_total:
            raise ValueError("production receipt requires all steps to succeed")
        if self.identity_armed != self.identity_applicable:
            raise ValueError("production receipt requires complete identity arming")
        if self.evidence_classes != tuple(sorted(self.evidence_classes)) or len(
            self.evidence_classes
        ) != len(set(self.evidence_classes)):
            raise ValueError("production receipt evidence classes are invalid")
        if len(self.rung_histogram) > 7 or any(
            type(value) is not int or value < 0 or value > JS_MAX_SAFE_INTEGER
            for value in self.rung_histogram.values()
        ):
            raise ValueError("production receipt rung counts are invalid")
        return self


def project_production_run_receipt(receipt: RunReceipt) -> ProductionRunReceipt:
    try:
        micros = Decimal(str(receipt.est_cost_usd)) * Decimal(1_000_000)
    except InvalidOperation as exc:
        raise ProductionTerminalVerificationError(
            "run receipt cost is not an exact integer microusd value"
        ) from exc
    if micros != micros.to_integral_value():
        raise ProductionTerminalVerificationError(
            "run receipt cost is not an exact integer microusd value"
        )
    source = receipt.model_dump(mode="json")
    # ``model_validate`` narrows the wider RunReceipt literals (for example a
    # non-production provenance) through the closed production validators
    # instead of a static cast; a value outside the production subset raises.
    return ProductionRunReceipt.model_validate(
        {
            "outcome": receipt.outcome,
            "transaction_outcome": receipt.transaction_outcome,
            "profile": receipt.profile,
            "production_eligible": receipt.production_eligible,
            "steps_total": receipt.steps_total,
            "steps_ok": receipt.steps_ok,
            "heals": receipt.heals,
            "model_calls": receipt.model_calls,
            "est_cost_microusd": int(micros),
            "duration_ms": receipt.duration_ms,
            "rung_histogram": receipt.rung_histogram,
            "evidence_classes": tuple(receipt.evidence_classes),
            "effect_tier_reached": receipt.effect_tier_reached,
            "authorization_required": receipt.authorization_required,
            "authorization_confirmed": receipt.authorization_confirmed,
            "identity_required": receipt.identity_required,
            "identity_confirmed": receipt.identity_confirmed,
            "postconditions_required": receipt.postconditions_required,
            "postconditions_confirmed": receipt.postconditions_confirmed,
            "effects_required": receipt.effects_required,
            "effects_confirmed": receipt.effects_confirmed,
            "identity_armed": receipt.identity_armed,
            "identity_applicable": receipt.identity_applicable,
            "over_halt_count": receipt.over_halt_count,
            "substrate": receipt.substrate,
            "provenance": receipt.provenance,
            "receipt_builder_version": receipt.receipt_builder_version,
            "external_network_calls": receipt.external_network_calls,
            "bundle_digest": receipt.bundle_digest,
            "source_receipt_digest": receipt.receipt_digest,
            "source_receipt_sha256": _sha256(source),
            "generated_at": receipt.generated_at,
        }
    )


def prepare_production_terminal_evidence(
    report: RunReport,
) -> PreparedProductionTerminalEvidence:
    """Revalidate the exact report and build the only terminal-safe projections.

    The returned report bytes are the bytes that the runner must store as the
    immutable report object. The production proof later binds the storage
    version and the SHA-256 of these exact bytes.
    """

    try:
        validated = RunReport.model_validate_json(report.model_dump_json())
    except (AttributeError, TypeError, ValueError) as exc:
        raise ProductionTerminalVerificationError(
            "terminal report failed typed JSON revalidation"
        ) from exc
    envelope = validated.outcome_envelope
    if (
        envelope is None
        or validated.run_id_sha256 is None
        or validated.bundle_content_digest is None
    ):
        raise ProductionTerminalVerificationError(
            "terminal report lacks its run or bundle binding"
        )
    try:
        receipt = project_production_run_receipt(build_receipt(validated))
        outcome = ProductionExecutionOutcome.model_validate(
            {
                "version": envelope.version,
                "outcome": envelope.outcome,
                "profile": envelope.profile,
                "production_eligible": envelope.production_eligible,
                "qualification_evidence_only": envelope.qualification_evidence_only,
                "execution_completed": envelope.execution_completed,
                "required_contracts": envelope.required_contracts.model_dump(
                    mode="json"
                ),
                "passed_contracts": envelope.passed_contracts.model_dump(mode="json"),
                "workflow_contract_sha256": envelope.workflow_contract_sha256,
                "postcondition_evidence": [
                    item.model_dump(mode="json")
                    for item in envelope.postcondition_evidence
                ],
                "evidence_classes": envelope.evidence_classes,
                "model_calls": envelope.model_calls,
                "external_network_calls": envelope.external_network_calls,
                "compensation_actions": envelope.compensation_actions,
            }
        )
    except (ReceiptError, ValueError) as exc:
        raise ProductionTerminalVerificationError(
            "terminal report is not a complete production VERIFIED outcome"
        ) from exc
    report_bytes = canonical_json(validated.model_dump(mode="json"))
    report_sha256 = hashlib.sha256(report_bytes).hexdigest()
    receipt_sha256 = _sha256(receipt.model_dump(mode="json"))
    return PreparedProductionTerminalEvidence(
        report_bytes=report_bytes,
        report_sha256=report_sha256,
        flow_run_id_sha256=validated.run_id_sha256,
        bundle_content_digest=validated.bundle_content_digest,
        execution_outcome=outcome,
        run_receipt=receipt,
        run_receipt_sha256=receipt_sha256,
        report=validated,
    )


def _evidence_manifest_sha256(domain: bytes, value: BaseModel) -> str:
    payload = value.model_dump(mode="json")
    payload.pop("manifest_sha256", None)
    return hashlib.sha256(domain + canonical_json(payload)).hexdigest()


class ProductionPolicyEvidenceManifest(ClosedSignedModel):
    schema_version: Literal["openadapt.production-policy-evidence/v1"] = (
        "openadapt.production-policy-evidence/v1"
    )
    admission_policy_sha256: str = Field(pattern=_SHA256_RE)
    governed_policy_contract_sha256: str = Field(pattern=_SHA256_RE)
    governed_runtime_inputs_digest: str = Field(pattern=_SHA256_RE)
    environment_digest: str = Field(pattern=_SHA256_RE)
    environment_contract_sha256: str = Field(pattern=_SHA256_RE)
    runtime_environment_sha256: str = Field(pattern=_SHA256_RE)
    identity_contract_sha256: str = Field(pattern=_SHA256_RE)
    effect_contract_sha256: str = Field(pattern=_SHA256_RE)
    minimum_effect_tier: Literal[1, 2, 3]
    manifest_sha256: str = Field(pattern=_SHA256_RE)

    @model_validator(mode="after")
    def _digest(self) -> "ProductionPolicyEvidenceManifest":
        expected = _evidence_manifest_sha256(
            b"openadapt-production-policy-evidence-v1\0", self
        )
        if self.manifest_sha256 != expected:
            raise ValueError("production policy evidence digest is invalid")
        return self


class ProductionAuthorizationEvidenceManifest(ClosedSignedModel):
    schema_version: Literal["openadapt.production-authorization-evidence/v1"] = (
        "openadapt.production-authorization-evidence/v1"
    )
    governed_authorization_id_sha256: str = Field(pattern=_SHA256_RE)
    admission_id: str = Field(pattern=_UUID_RE)
    admission_artifact_sha256: str = Field(pattern=_SHA256_RE)
    execution_authority_id: str = Field(pattern=_ID_RE)
    execution_authority_sha256: str = Field(pattern=_SHA256_RE)
    permit_chain_sha256: str = Field(pattern=_SHA256_RE)
    manifest_sha256: str = Field(pattern=_SHA256_RE)

    @model_validator(mode="after")
    def _digest(self) -> "ProductionAuthorizationEvidenceManifest":
        expected = _evidence_manifest_sha256(
            b"openadapt-production-authorization-evidence-v1\0", self
        )
        if self.manifest_sha256 != expected:
            raise ValueError("production authorization evidence digest is invalid")
        return self


class ProductionIdentitySignal(ClosedSignedModel):
    signal: Literal[
        "subject_name",
        "record_id",
        "secondary_identifier",
        "application",
        "session",
        "workflow_state",
    ]
    source: Literal[
        "structured",
        "identifier_region",
        "captured_context",
        "application",
        "session",
        "workflow_state",
        "api_parameter",
    ]
    evidence_class: Literal[
        "application_structured_text",
        "recorded_and_live_region",
        "captured_context_ocr",
        "application_identity",
        "session_identity",
        "workflow_state_identity",
        "api_request_effect_binding",
    ]
    verdict: Literal["verified", "conflict", "unverifiable"]
    match: Literal["exact", "normalized"]


class ProductionIdentityResult(ClosedSignedModel):
    result_index: StrictInt = Field(ge=0, le=JS_MAX_SAFE_INTEGER)
    status: Literal["verified"]
    mode: Literal["context", "param", "structured", "pixel", "vlm", "signal_quorum"]
    signals: tuple[ProductionIdentitySignal, ...] = Field(max_length=32)

    @model_validator(mode="after")
    def _ordered_signals(self) -> "ProductionIdentityResult":
        keys = tuple(
            (item.signal, item.source, item.evidence_class, item.verdict, item.match)
            for item in self.signals
        )
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("production identity signals must be unique and ordered")
        return self


class ProductionIdentityEvidenceManifest(ClosedSignedModel):
    schema_version: Literal["openadapt.production-identity-evidence/v1"] = (
        "openadapt.production-identity-evidence/v1"
    )
    identity_contract_sha256: str = Field(pattern=_SHA256_RE)
    workflow_contract_sha256: str = Field(pattern=_SHA256_RE)
    required: StrictInt = Field(ge=1, le=JS_MAX_SAFE_INTEGER)
    confirmed: StrictInt = Field(ge=1, le=JS_MAX_SAFE_INTEGER)
    results: tuple[ProductionIdentityResult, ...] = Field(
        min_length=1, max_length=10_000
    )
    manifest_sha256: str = Field(pattern=_SHA256_RE)

    @model_validator(mode="after")
    def _digest(self) -> "ProductionIdentityEvidenceManifest":
        if self.required != self.confirmed or len(self.results) != self.required:
            raise ValueError("production identity evidence coverage is incomplete")
        indices = tuple(item.result_index for item in self.results)
        if indices != tuple(sorted(indices)) or len(indices) != len(set(indices)):
            raise ValueError("production identity evidence results are invalid")
        expected = _evidence_manifest_sha256(
            b"openadapt-production-identity-evidence-v1\0", self
        )
        if self.manifest_sha256 != expected:
            raise ValueError("production identity evidence digest is invalid")
        return self


class ProductionPostconditionEvidenceManifest(ClosedSignedModel):
    schema_version: Literal["openadapt.production-postcondition-evidence/v1"] = (
        "openadapt.production-postcondition-evidence/v1"
    )
    workflow_contract_sha256: str = Field(pattern=_SHA256_RE)
    required: StrictInt = Field(ge=1, le=JS_MAX_SAFE_INTEGER)
    confirmed: StrictInt = Field(ge=1, le=JS_MAX_SAFE_INTEGER)
    records: tuple[PostconditionContractEvidence, ...] = Field(
        min_length=1, max_length=10_000
    )
    manifest_sha256: str = Field(pattern=_SHA256_RE)

    @model_validator(mode="after")
    def _digest(self) -> "ProductionPostconditionEvidenceManifest":
        if self.required != self.confirmed or len(self.records) != self.required:
            raise ValueError("production postcondition evidence coverage is incomplete")
        if any(
            item.verdict != "passed"
            or item.workflow_contract_sha256 != self.workflow_contract_sha256
            for item in self.records
        ):
            raise ValueError("production postcondition evidence is invalid")
        expected = _evidence_manifest_sha256(
            b"openadapt-production-postcondition-evidence-v1\0", self
        )
        if self.manifest_sha256 != expected:
            raise ValueError("production postcondition evidence digest is invalid")
        return self


class ProductionEffectEvidence(ClosedSignedModel):
    result_index: StrictInt = Field(ge=0, le=JS_MAX_SAFE_INTEGER)
    effect_contract_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    verifier_identity: str | None = Field(
        default=None, pattern=r"^sha256:[a-f0-9]{64}$"
    )
    verification_tier: Literal[1, 2, 3]
    final_verdict: Literal["confirmed"]
    observed_effect: Literal["present"]
    reconciliation_completed: StrictBool
    reconciliation_actions: StrictInt = Field(ge=0, le=JS_MAX_SAFE_INTEGER)


class ProductionEffectEvidenceManifest(ClosedSignedModel):
    schema_version: Literal["openadapt.production-effect-evidence/v1"] = (
        "openadapt.production-effect-evidence/v1"
    )
    effect_contract_sha256: str = Field(pattern=_SHA256_RE)
    workflow_contract_sha256: str = Field(pattern=_SHA256_RE)
    required: StrictInt = Field(ge=1, le=JS_MAX_SAFE_INTEGER)
    confirmed: StrictInt = Field(ge=1, le=JS_MAX_SAFE_INTEGER)
    records: tuple[ProductionEffectEvidence, ...] = Field(
        min_length=1, max_length=10_000
    )
    manifest_sha256: str = Field(pattern=_SHA256_RE)

    @model_validator(mode="after")
    def _digest(self) -> "ProductionEffectEvidenceManifest":
        if self.required != self.confirmed or len(self.records) != self.required:
            raise ValueError("production effect evidence coverage is incomplete")
        keys = tuple(
            (item.result_index, item.effect_contract_hash) for item in self.records
        )
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("production effect evidence records are invalid")
        expected = _evidence_manifest_sha256(
            b"openadapt-production-effect-evidence-v1\0", self
        )
        if self.manifest_sha256 != expected:
            raise ValueError("production effect evidence digest is invalid")
        return self


class ProductionEvidenceManifests(ClosedSignedModel):
    policy: ProductionPolicyEvidenceManifest
    authorization: ProductionAuthorizationEvidenceManifest
    identity: ProductionIdentityEvidenceManifest
    postcondition: ProductionPostconditionEvidenceManifest
    effect: ProductionEffectEvidenceManifest


def build_evidence_manifest(model: type[BaseModel], **values: Any) -> BaseModel:
    """Build one domain-separated manifest through its closed typed validator."""

    draft = model.model_construct(**values, manifest_sha256="0" * 64)
    domain = {
        ProductionPolicyEvidenceManifest: b"openadapt-production-policy-evidence-v1\0",
        ProductionAuthorizationEvidenceManifest: b"openadapt-production-authorization-evidence-v1\0",
        ProductionIdentityEvidenceManifest: b"openadapt-production-identity-evidence-v1\0",
        ProductionPostconditionEvidenceManifest: b"openadapt-production-postcondition-evidence-v1\0",
        ProductionEffectEvidenceManifest: b"openadapt-production-effect-evidence-v1\0",
    }.get(model)
    if domain is None:
        raise TypeError("unsupported production evidence manifest")
    digest = _evidence_manifest_sha256(domain, draft)
    return model.model_validate({**values, "manifest_sha256": digest})


def delivery_authority_key_id(public_key: bytes) -> str:
    if len(public_key) != 32:
        raise ValueError("delivery authority public key is invalid")
    return f"delivery-authority-ed25519-{hashlib.sha256(public_key).hexdigest()[:16]}"


def delivery_authority_signer_sha256(public_key: bytes) -> str:
    if len(public_key) != 32:
        raise ValueError("delivery authority public key is invalid")
    return hashlib.sha256(public_key).hexdigest()


class DeliveryAuthoritySigner(ClosedSignedModel):
    algorithm: Literal["ed25519"] = "ed25519"
    key_id: str = Field(pattern=_AUTHORITY_KEY_ID_RE)
    public_key: str

    @field_validator("public_key")
    @classmethod
    def _canonical_public_key(cls, value: str) -> str:
        try:
            decoded = b64decode(value, validate=True)
        except (TypeError, ValueError) as exc:
            raise ValueError("delivery authority public key is invalid") from exc
        if len(decoded) != 32 or b64encode(decoded).decode("ascii") != value:
            raise ValueError("delivery authority public key is invalid")
        return value

    @model_validator(mode="after")
    def _key_id_matches(self) -> "DeliveryAuthoritySigner":
        if (
            delivery_authority_key_id(b64decode(self.public_key, validate=True))
            != self.key_id
        ):
            raise ValueError("delivery authority key id does not match its public key")
        return self

    def signer_sha256(self) -> str:
        return delivery_authority_signer_sha256(
            b64decode(self.public_key, validate=True)
        )


class ProductionExecutionAuthorityPayload(ClosedSignedModel):
    """Immutable server-retained authority for one Production run."""

    schema_version: Literal["openadapt.production-execution-authority/v2"] = (
        EXECUTION_AUTHORITY_SCHEMA
    )
    execution_authority_id: str = Field(pattern=_UUID_RE)
    tenant_id: str = Field(pattern=_UUID_RE)
    run_id: str = Field(pattern=_UUID_RE)
    flow_run_id_sha256: str = Field(pattern=_SHA256_RE)
    workflow_id: str = Field(pattern=_UUID_RE)
    workflow_version_id: str = Field(pattern=_UUID_RE)
    bundle_version_id: str = Field(pattern=_UUID_RE)
    bundle_artifact_sha256: str = Field(pattern=_SHA256_RE)
    bundle_content_digest: str = Field(pattern=_SHA256_RE)
    runtime_validation_id: str = Field(pattern=_UUID_RE)
    runtime_substrate: Literal["web", "windows", "macos", "linux", "rdp", "citrix"]
    runtime_boundary_id: str = Field(pattern=_ID_RE)
    admission_id: str = Field(pattern=_UUID_RE)
    admission_artifact_sha256: str = Field(pattern=_SHA256_RE)
    admission_policy_sha256: str = Field(pattern=_SHA256_RE)
    evidence_identity_sha256: str = Field(pattern=_SHA256_RE)
    environment_digest: str = Field(pattern=_SHA256_RE)
    environment_contract_sha256: str = Field(pattern=_SHA256_RE)
    runtime_environment_sha256: str = Field(pattern=_SHA256_RE)
    identity_contract_sha256: str = Field(pattern=_SHA256_RE)
    effect_contract_sha256: str = Field(pattern=_SHA256_RE)
    admitted_runtime_build_sha256: str = Field(pattern=_SHA256_RE)
    evidence_runner_signer_sha256: str = Field(pattern=_SHA256_RE)
    qualification_signer_registry_sha256: str = Field(pattern=_SHA256_RE)
    qualification_signer_registry_revision: StrictInt = Field(
        ge=1, le=JS_MAX_SAFE_INTEGER
    )
    qualification_signer_registry_checked_at: str
    qualification_signer_registry_expires_at: str
    execution_profile: Literal["standard", "regulated"]
    dispatch_binding_sha256: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    execution_authority_signer_sha256: str = Field(pattern=_SHA256_RE)
    created_at: str

    @model_validator(mode="after")
    def _closed_authority(self) -> "ProductionExecutionAuthorityPayload":
        if hashlib.sha256(self.run_id.encode("utf-8")).hexdigest() != (
            self.flow_run_id_sha256
        ):
            raise ValueError("execution authority run identity digest is invalid")
        if self.workflow_version_id != self.bundle_version_id:
            raise ValueError("execution authority workflow and bundle versions differ")
        if self.admission_id == self.runtime_validation_id:
            raise ValueError("execution authority admission identity is invalid")
        checked = _parse_utc(
            self.qualification_signer_registry_checked_at,
            field="execution authority registry checked_at",
        )
        expires = _parse_utc(
            self.qualification_signer_registry_expires_at,
            field="execution authority registry expires_at",
        )
        created = _parse_utc(self.created_at, field="execution authority created_at")
        if not checked <= created < expires:
            raise ValueError("execution authority signer registry is not fresh")
        if created - checked > MAX_PERMIT_SNAPSHOT_AGE:
            raise ValueError("execution authority signer registry check is stale")
        return self

    def canonical_bytes(self) -> bytes:
        return canonical_json(self)

    def artifact_sha256(self) -> str:
        validated = ProductionExecutionAuthorityPayload.model_validate(
            self.model_dump(mode="json")
        )
        return hashlib.sha256(
            EXECUTION_AUTHORITY_DOMAIN + validated.canonical_bytes()
        ).hexdigest()


class ProductionDeliveryPermitPayload(ClosedSignedModel):
    schema_version: Literal["openadapt.production-delivery-permit-payload/v2"] = (
        PERMIT_PAYLOAD_SCHEMA
    )
    execution_authority_id: str = Field(pattern=_UUID_RE)
    execution_authority_sha256: str = Field(pattern=_SHA256_RE)
    permit_id: str = Field(pattern=_ID_RE)
    run_id: str = Field(pattern=_UUID_RE)
    flow_run_id_sha256: str = Field(pattern=_SHA256_RE)
    run_request_sha256: str = Field(pattern=_SHA256_RE)
    action_request_sha256: str = Field(pattern=_SHA256_RE)
    admission_artifact_sha256: str = Field(pattern=_SHA256_RE)
    evidence_identity_sha256: str = Field(pattern=_SHA256_RE)
    environment_digest: str = Field(pattern=_SHA256_RE)
    qualification_signer_registry_sha256: str = Field(pattern=_SHA256_RE)
    qualification_signer_registry_revision: StrictInt = Field(
        ge=1, le=JS_MAX_SAFE_INTEGER
    )
    qualification_signer_registry_checked_at: str
    qualification_signer_registry_expires_at: str
    input_edge_sequence: StrictInt = Field(ge=1, le=JS_MAX_SAFE_INTEGER)
    authority_sequence: StrictInt = Field(ge=0, le=JS_MAX_SAFE_INTEGER)
    issued_at: str

    @model_validator(mode="after")
    def _valid_permit_time(self) -> "ProductionDeliveryPermitPayload":
        checked = _parse_utc(
            self.qualification_signer_registry_checked_at,
            field="permit registry checked_at",
        )
        expires = _parse_utc(
            self.qualification_signer_registry_expires_at,
            field="permit registry expires_at",
        )
        issued = _parse_utc(self.issued_at, field="permit issued_at")
        if not checked <= issued < expires:
            raise ValueError("permit was not issued under a fresh signer registry")
        if issued - checked > MAX_PERMIT_SNAPSHOT_AGE:
            raise ValueError(
                "permit registry check is stale; the issuing authority must "
                "recheck the signer registry within 60 seconds of permit issue"
            )
        if hashlib.sha256(self.run_id.encode("utf-8")).hexdigest() != (
            self.flow_run_id_sha256
        ):
            raise ValueError("permit run identity digest is invalid")
        return self

    def canonical_bytes(self) -> bytes:
        return canonical_json(self)

    def payload_sha256(self) -> str:
        return hashlib.sha256(
            PERMIT_PAYLOAD_DOMAIN + self.canonical_bytes()
        ).hexdigest()


class ProductionDeliveryReceiptPayload(ClosedSignedModel):
    schema_version: Literal["openadapt.production-delivery-receipt-payload/v2"] = (
        DELIVERY_RECEIPT_PAYLOAD_SCHEMA
    )
    execution_authority_id: str = Field(pattern=_UUID_RE)
    permit_id: str = Field(pattern=_ID_RE)
    permit_artifact_sha256: str = Field(pattern=_SHA256_RE)
    authenticated_runner_id_sha256: str = Field(pattern=_SHA256_RE)
    authenticated_session_id_sha256: str = Field(pattern=_SHA256_RE)
    one_use_claim_id: str = Field(pattern=_UUID_RE)
    runtime_delivery_sequence: StrictInt = Field(ge=0, le=JS_MAX_SAFE_INTEGER)
    delivered_at: str

    @field_validator("delivered_at")
    @classmethod
    def _valid_delivery_time(cls, value: str) -> str:
        _parse_utc(value, field="delivery receipt delivered_at")
        return value

    def canonical_bytes(self) -> bytes:
        return canonical_json(self)

    def payload_sha256(self) -> str:
        return hashlib.sha256(
            DELIVERY_RECEIPT_PAYLOAD_DOMAIN + self.canonical_bytes()
        ).hexdigest()


class ProductionDeliveryPermitArtifact(ClosedSignedModel):
    schema_version: Literal["openadapt.production-delivery-permit-artifact/v2"] = (
        PERMIT_ARTIFACT_SCHEMA
    )
    payload: ProductionDeliveryPermitPayload
    payload_sha256: str = Field(pattern=_SHA256_RE)
    signer: DeliveryAuthoritySigner
    signature: str = Field(min_length=86, max_length=86)

    @field_validator("signature")
    @classmethod
    def _canonical_signature(cls, value: str) -> str:
        _decode_ed25519_signature(value, field="delivery permit")
        return value

    @model_validator(mode="after")
    def _verify_artifact(self) -> "ProductionDeliveryPermitArtifact":
        payload = ProductionDeliveryPermitPayload.model_validate(
            self.payload.model_dump(mode="json")
        )
        if self.payload_sha256 != payload.payload_sha256():
            raise ValueError("delivery permit payload digest is invalid")
        _verify_authority_signature(
            self.signer,
            self.signature,
            PERMIT_PAYLOAD_DOMAIN + payload.canonical_bytes(),
            field="delivery permit",
        )
        return self

    def canonical_bytes(self) -> bytes:
        return canonical_json(self)

    def artifact_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


class ProductionDeliveryReceiptArtifact(ClosedSignedModel):
    schema_version: Literal["openadapt.production-delivery-receipt-artifact/v2"] = (
        DELIVERY_RECEIPT_ARTIFACT_SCHEMA
    )
    payload: ProductionDeliveryReceiptPayload
    payload_sha256: str = Field(pattern=_SHA256_RE)
    signer: DeliveryAuthoritySigner
    signature: str = Field(min_length=86, max_length=86)

    @field_validator("signature")
    @classmethod
    def _canonical_signature(cls, value: str) -> str:
        _decode_ed25519_signature(value, field="delivery receipt")
        return value

    @model_validator(mode="after")
    def _verify_artifact(self) -> "ProductionDeliveryReceiptArtifact":
        payload = ProductionDeliveryReceiptPayload.model_validate(
            self.payload.model_dump(mode="json")
        )
        if self.payload_sha256 != payload.payload_sha256():
            raise ValueError("delivery receipt payload digest is invalid")
        _verify_authority_signature(
            self.signer,
            self.signature,
            DELIVERY_RECEIPT_PAYLOAD_DOMAIN + payload.canonical_bytes(),
            field="delivery receipt",
        )
        return self

    def canonical_bytes(self) -> bytes:
        return canonical_json(self)

    def artifact_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def _decode_ed25519_signature(value: str, *, field: str) -> bytes:
    try:
        decoded = urlsafe_b64decode(value + "==")
    except ValueError as exc:
        raise ValueError(f"{field} signature is invalid") from exc
    if (
        len(decoded) != 64
        or urlsafe_b64encode(decoded).decode("ascii").rstrip("=") != value
    ):
        raise ValueError(f"{field} signature is invalid")
    return decoded


def _verify_authority_signature(
    signer: DeliveryAuthoritySigner,
    signature: str,
    message: bytes,
    *,
    field: str,
) -> None:
    try:
        Ed25519PublicKey.from_public_bytes(
            b64decode(signer.public_key, validate=True)
        ).verify(_decode_ed25519_signature(signature, field=field), message)
    except (InvalidSignature, ValueError) as exc:
        raise ValueError(f"{field} authority signature is invalid") from exc


def _authority_signer(private_key: Ed25519PrivateKey) -> DeliveryAuthoritySigner:
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return DeliveryAuthoritySigner(
        key_id=delivery_authority_key_id(public_key),
        public_key=b64encode(public_key).decode("ascii"),
    )


def sign_production_delivery_permit(
    payload: ProductionDeliveryPermitPayload,
    private_key: Ed25519PrivateKey,
) -> ProductionDeliveryPermitArtifact:
    payload = ProductionDeliveryPermitPayload.model_validate(
        payload.model_dump(mode="json")
    )
    signature = private_key.sign(PERMIT_PAYLOAD_DOMAIN + payload.canonical_bytes())
    return ProductionDeliveryPermitArtifact(
        payload=payload,
        payload_sha256=payload.payload_sha256(),
        signer=_authority_signer(private_key),
        signature=urlsafe_b64encode(signature).decode("ascii").rstrip("="),
    )


def sign_production_delivery_receipt(
    payload: ProductionDeliveryReceiptPayload,
    private_key: Ed25519PrivateKey,
) -> ProductionDeliveryReceiptArtifact:
    payload = ProductionDeliveryReceiptPayload.model_validate(
        payload.model_dump(mode="json")
    )
    signature = private_key.sign(
        DELIVERY_RECEIPT_PAYLOAD_DOMAIN + payload.canonical_bytes()
    )
    return ProductionDeliveryReceiptArtifact(
        payload=payload,
        payload_sha256=payload.payload_sha256(),
        signer=_authority_signer(private_key),
        signature=urlsafe_b64encode(signature).decode("ascii").rstrip("="),
    )


class ProductionDeliveryPermit(ClosedSignedModel):
    """One acknowledged input edge in the terminal permit chain."""

    schema_version: Literal["openadapt.production-delivery-permit-chain-entry/v2"] = (
        PERMIT_CHAIN_ENTRY_SCHEMA
    )
    permit_artifact: ProductionDeliveryPermitArtifact
    permit_artifact_sha256: str = Field(pattern=_SHA256_RE)
    delivery_receipt_artifact: ProductionDeliveryReceiptArtifact
    delivery_receipt_artifact_sha256: str = Field(pattern=_SHA256_RE)

    @model_validator(mode="after")
    def _closed_acknowledged_edge(self) -> "ProductionDeliveryPermit":
        permit = ProductionDeliveryPermitArtifact.model_validate(
            self.permit_artifact.model_dump(mode="json")
        )
        receipt = ProductionDeliveryReceiptArtifact.model_validate(
            self.delivery_receipt_artifact.model_dump(mode="json")
        )
        if self.permit_artifact_sha256 != permit.artifact_sha256():
            raise ValueError("delivery permit artifact digest is invalid")
        if self.delivery_receipt_artifact_sha256 != receipt.artifact_sha256():
            raise ValueError("delivery receipt artifact digest is invalid")
        if permit.signer != receipt.signer:
            raise ValueError("delivery receipt signer differs from permit signer")
        permit_payload = permit.payload
        receipt_payload = receipt.payload
        if (
            receipt_payload.execution_authority_id
            != permit_payload.execution_authority_id
            or receipt_payload.permit_id != permit_payload.permit_id
            or receipt_payload.permit_artifact_sha256 != self.permit_artifact_sha256
        ):
            raise ValueError("delivery receipt does not bind its exact permit")
        issued = _parse_utc(permit_payload.issued_at, field="permit issued_at")
        delivered = _parse_utc(
            receipt_payload.delivered_at,
            field="delivery receipt delivered_at",
        )
        registry_expires = _parse_utc(
            permit_payload.qualification_signer_registry_expires_at,
            field="permit registry expires_at",
        )
        if not issued <= delivered < registry_expires:
            raise ValueError("delivery receipt chronology is invalid")
        return self

    @classmethod
    def build(
        cls,
        permit_artifact: ProductionDeliveryPermitArtifact,
        delivery_receipt_artifact: ProductionDeliveryReceiptArtifact,
    ) -> "ProductionDeliveryPermit":
        """Build one edge from the two exact retained signed artifacts."""

        return cls(
            permit_artifact=permit_artifact,
            permit_artifact_sha256=permit_artifact.artifact_sha256(),
            delivery_receipt_artifact=delivery_receipt_artifact,
            delivery_receipt_artifact_sha256=(
                delivery_receipt_artifact.artifact_sha256()
            ),
        )

    @property
    def _permit(self) -> ProductionDeliveryPermitPayload:
        return self.permit_artifact.payload

    @property
    def _receipt(self) -> ProductionDeliveryReceiptPayload:
        return self.delivery_receipt_artifact.payload

    @property
    def execution_authority_id(self) -> str:
        return self._permit.execution_authority_id

    @property
    def execution_authority_sha256(self) -> str:
        return self._permit.execution_authority_sha256

    @property
    def permit_id(self) -> str:
        return self._permit.permit_id

    @property
    def run_id(self) -> str:
        return self._permit.run_id

    @property
    def flow_run_id_sha256(self) -> str:
        return self._permit.flow_run_id_sha256

    @property
    def run_request_sha256(self) -> str:
        return self._permit.run_request_sha256

    @property
    def action_request_sha256(self) -> str:
        return self._permit.action_request_sha256

    @property
    def admission_artifact_sha256(self) -> str:
        return self._permit.admission_artifact_sha256

    @property
    def evidence_identity_sha256(self) -> str:
        return self._permit.evidence_identity_sha256

    @property
    def environment_digest(self) -> str:
        return self._permit.environment_digest

    @property
    def qualification_signer_registry_sha256(self) -> str:
        return self._permit.qualification_signer_registry_sha256

    @property
    def qualification_signer_registry_revision(self) -> int:
        return self._permit.qualification_signer_registry_revision

    @property
    def qualification_signer_registry_expires_at(self) -> str:
        return self._permit.qualification_signer_registry_expires_at

    @property
    def input_edge_sequence(self) -> int:
        return self._permit.input_edge_sequence

    @property
    def authority_sequence(self) -> int:
        return self._permit.authority_sequence

    @property
    def runtime_delivery_sequence(self) -> int:
        return self._receipt.runtime_delivery_sequence

    @property
    def issued_at(self) -> str:
        return self._permit.issued_at

    @property
    def delivered_at(self) -> str:
        return self._receipt.delivered_at

    @property
    def authority_signer_sha256(self) -> str:
        return self.permit_artifact.signer.signer_sha256()

    @property
    def authenticated_runner_id_sha256(self) -> str:
        return self._receipt.authenticated_runner_id_sha256

    @property
    def authenticated_session_id_sha256(self) -> str:
        return self._receipt.authenticated_session_id_sha256

    @property
    def one_use_claim_id(self) -> str:
        return self._receipt.one_use_claim_id


class ProductionDeliveryPermitChain(ClosedSignedModel):
    schema_version: Literal["openadapt.production-delivery-permit-chain/v2"] = (
        PERMIT_CHAIN_SCHEMA
    )
    entries: tuple[ProductionDeliveryPermit, ...] = Field(
        min_length=1, max_length=10_000
    )
    permit_chain_sha256: str = Field(pattern=_SHA256_RE)

    @model_validator(mode="after")
    def _closed_chain(self) -> "ProductionDeliveryPermitChain":
        # Pydantic does not revalidate an already-created nested model by
        # default.  Reparse each retained permit so ``model_copy`` or another
        # in-memory mutation cannot enter a trusted chain with a stale digest.
        for item in self.entries:
            ProductionDeliveryPermit.model_validate(item.model_dump(mode="json"))
        authority = self.entries[0].execution_authority_id
        authority_digest = self.entries[0].execution_authority_sha256
        admission_digest = self.entries[0].admission_artifact_sha256
        evidence_identity_digest = self.entries[0].evidence_identity_sha256
        environment_digest = self.entries[0].environment_digest
        registry_digest = self.entries[0].qualification_signer_registry_sha256
        registry_revision = self.entries[0].qualification_signer_registry_revision
        registry_expiry = self.entries[0].qualification_signer_registry_expires_at
        run_request_digest = self.entries[0].run_request_sha256
        run_id = self.entries[0].run_id
        flow_run_id_sha256 = self.entries[0].flow_run_id_sha256
        authority_signer_sha256 = self.entries[0].authority_signer_sha256
        authenticated_runner_id_sha256 = self.entries[0].authenticated_runner_id_sha256
        authenticated_session_id_sha256 = self.entries[
            0
        ].authenticated_session_id_sha256
        if any(
            item.execution_authority_id != authority
            or item.execution_authority_sha256 != authority_digest
            or item.run_id != run_id
            or item.flow_run_id_sha256 != flow_run_id_sha256
            or item.admission_artifact_sha256 != admission_digest
            or item.evidence_identity_sha256 != evidence_identity_digest
            or item.environment_digest != environment_digest
            or item.qualification_signer_registry_sha256 != registry_digest
            or item.qualification_signer_registry_revision != registry_revision
            or item.qualification_signer_registry_expires_at != registry_expiry
            or item.run_request_sha256 != run_request_digest
            or item.authority_signer_sha256 != authority_signer_sha256
            or item.authenticated_runner_id_sha256 != authenticated_runner_id_sha256
            or item.authenticated_session_id_sha256 != authenticated_session_id_sha256
            for item in self.entries
        ):
            raise ValueError("delivery permit chain changes its production authority")
        input_sequences = tuple(item.input_edge_sequence for item in self.entries)
        authority_sequences = tuple(item.authority_sequence for item in self.entries)
        delivery_sequences = tuple(
            item.runtime_delivery_sequence for item in self.entries
        )
        exact = tuple(range(1, len(self.entries) + 1))
        if input_sequences != exact:
            raise ValueError(
                "delivery input-edge sequence must be exact and contiguous"
            )
        if authority_sequences != tuple(
            range(authority_sequences[0], authority_sequences[0] + len(self.entries))
        ):
            raise ValueError("delivery authority sequence must be contiguous")
        if delivery_sequences != tuple(
            range(delivery_sequences[0], delivery_sequences[0] + len(self.entries))
        ):
            raise ValueError("runtime delivery sequence must be contiguous")
        permit_ids = tuple(item.permit_id for item in self.entries)
        if len(permit_ids) != len(set(permit_ids)):
            raise ValueError("delivery permit chain repeats a permit")
        action_requests = tuple(item.action_request_sha256 for item in self.entries)
        if len(action_requests) != len(set(action_requests)):
            raise ValueError("delivery permit chain repeats an action request")
        claim_ids = tuple(item.one_use_claim_id for item in self.entries)
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("delivery permit chain repeats a one-use claim")
        event_times = tuple(
            (
                _parse_utc(item.issued_at, field="permit issued_at"),
                _parse_utc(item.delivered_at, field="permit delivered_at"),
            )
            for item in self.entries
        )
        if any(
            previous[1] > current[0]
            for previous, current in zip(event_times, event_times[1:])
        ):
            raise ValueError("delivery permit chronology is invalid")
        expected = hashlib.sha256(
            PERMIT_CHAIN_DOMAIN
            + canonical_json(
                {
                    "schema_version": self.schema_version,
                    "entries": [item.model_dump(mode="json") for item in self.entries],
                }
            )
        ).hexdigest()
        if self.permit_chain_sha256 != expected:
            raise ValueError("delivery permit chain digest is invalid")
        return self

    @classmethod
    def build(
        cls, entries: tuple[ProductionDeliveryPermit, ...]
    ) -> "ProductionDeliveryPermitChain":
        payload = {
            "schema_version": PERMIT_CHAIN_SCHEMA,
            "entries": [item.model_dump(mode="json") for item in entries],
        }
        digest = hashlib.sha256(
            PERMIT_CHAIN_DOMAIN + canonical_json(payload)
        ).hexdigest()
        return cls(entries=entries, permit_chain_sha256=digest)


def build_production_evidence_manifests(
    prepared: PreparedProductionTerminalEvidence,
    *,
    admission_policy_sha256: str,
    environment_digest: str,
    environment_contract_sha256: str,
    runtime_environment_sha256: str,
    identity_contract_sha256: str,
    effect_contract_sha256: str,
    admission_id: str,
    admission_artifact_sha256: str,
    execution_authority_id: str,
    execution_authority_sha256: str,
    permit_chain: ProductionDeliveryPermitChain,
) -> ProductionEvidenceManifests:
    """Recompute all five class-separated manifests from the retained report."""

    report = prepared.report
    outcome = prepared.execution_outcome
    if (
        report.governed_policy_contract_sha256 is None
        or report.governed_runtime_inputs_digest is None
        or report.governed_minimum_effect_tier not in {1, 2, 3}
        or report.governed_authorization_id is None
    ):
        raise ProductionTerminalVerificationError(
            "terminal report lacks retained policy or authorization evidence"
        )
    policy = build_evidence_manifest(
        ProductionPolicyEvidenceManifest,
        admission_policy_sha256=admission_policy_sha256,
        governed_policy_contract_sha256=report.governed_policy_contract_sha256,
        governed_runtime_inputs_digest=report.governed_runtime_inputs_digest,
        environment_digest=environment_digest,
        environment_contract_sha256=environment_contract_sha256,
        runtime_environment_sha256=runtime_environment_sha256,
        identity_contract_sha256=identity_contract_sha256,
        effect_contract_sha256=effect_contract_sha256,
        minimum_effect_tier=report.governed_minimum_effect_tier,
    )
    authorization = build_evidence_manifest(
        ProductionAuthorizationEvidenceManifest,
        governed_authorization_id_sha256=hashlib.sha256(
            report.governed_authorization_id.encode("utf-8")
        ).hexdigest(),
        admission_id=admission_id,
        admission_artifact_sha256=admission_artifact_sha256,
        execution_authority_id=execution_authority_id,
        execution_authority_sha256=execution_authority_sha256,
        permit_chain_sha256=permit_chain.permit_chain_sha256,
    )
    identity_results: list[ProductionIdentityResult] = []
    for result_index, result in enumerate(report.results):
        if result.skipped or result.exception_handled or result.identity is None:
            continue
        identity_results.append(
            ProductionIdentityResult.model_validate(
                {
                    "result_index": result_index,
                    "status": result.identity.status,
                    "mode": result.identity.mode,
                    "signals": tuple(
                        sorted(
                            (
                                ProductionIdentitySignal.model_validate(
                                    {
                                        "signal": item.signal,
                                        "source": item.source,
                                        "evidence_class": item.evidence_class,
                                        "verdict": item.verdict,
                                        "match": item.match,
                                    }
                                )
                                for item in result.identity.signal_evidence
                            ),
                            key=lambda item: (
                                item.signal,
                                item.source,
                                item.evidence_class,
                                item.verdict,
                                item.match,
                            ),
                        )
                    ),
                }
            )
        )
    identity = build_evidence_manifest(
        ProductionIdentityEvidenceManifest,
        identity_contract_sha256=identity_contract_sha256,
        workflow_contract_sha256=outcome.workflow_contract_sha256,
        required=outcome.required_contracts.identity,
        confirmed=outcome.passed_contracts.identity,
        results=tuple(identity_results),
    )
    postcondition = build_evidence_manifest(
        ProductionPostconditionEvidenceManifest,
        workflow_contract_sha256=outcome.workflow_contract_sha256,
        required=outcome.required_contracts.postcondition,
        confirmed=outcome.passed_contracts.postcondition,
        records=outcome.postcondition_evidence,
    )
    effect_records: list[ProductionEffectEvidence] = []
    for result_index, result in enumerate(report.results):
        if result.skipped or result.exception_handled:
            continue
        for item in result.effect_evidence:
            effect_records.append(
                ProductionEffectEvidence.model_validate(
                    {
                        "result_index": result_index,
                        "effect_contract_hash": item.effect_contract_hash,
                        "verifier_identity": item.verifier_identity,
                        "verification_tier": item.verification_tier,
                        "final_verdict": item.final_verdict,
                        "observed_effect": item.observed_effect,
                        "reconciliation_completed": item.reconciliation_completed,
                        "reconciliation_actions": item.reconciliation_actions,
                    }
                )
            )
    effect_records.sort(key=lambda item: (item.result_index, item.effect_contract_hash))
    effect = build_evidence_manifest(
        ProductionEffectEvidenceManifest,
        effect_contract_sha256=effect_contract_sha256,
        workflow_contract_sha256=outcome.workflow_contract_sha256,
        required=outcome.required_contracts.effect,
        confirmed=outcome.passed_contracts.effect,
        records=tuple(effect_records),
    )
    return ProductionEvidenceManifests.model_validate(
        {
            "policy": policy,
            "authorization": authorization,
            "identity": identity,
            "postcondition": postcondition,
            "effect": effect,
        }
    )


class ProductionTerminalVerificationPayload(ClosedSignedModel):
    schema_version: Literal["openadapt.production-terminal-verification/v2"] = SCHEMA
    terminal_sequence: Literal[1] = 1
    execution_purpose: Literal["production"] = "production"
    run_id: str = Field(pattern=_UUID_RE)
    flow_run_id_sha256: str = Field(pattern=_SHA256_RE)
    tenant_id: str = Field(pattern=_UUID_RE)
    workflow_id: str = Field(pattern=_UUID_RE)
    workflow_version_id: str = Field(pattern=_UUID_RE)
    bundle_version_id: str = Field(pattern=_UUID_RE)
    bundle_artifact_sha256: str = Field(pattern=_SHA256_RE)
    bundle_content_digest: str = Field(pattern=_SHA256_RE)
    environment_digest: str = Field(pattern=_SHA256_RE)
    environment_contract_sha256: str = Field(pattern=_SHA256_RE)
    runtime_environment_sha256: str = Field(pattern=_SHA256_RE)
    identity_contract_sha256: str = Field(pattern=_SHA256_RE)
    effect_contract_sha256: str = Field(pattern=_SHA256_RE)
    runtime_validation_id: str = Field(pattern=_UUID_RE)
    runtime_substrate: Literal["web", "windows", "macos", "linux", "rdp", "citrix"]
    admission_id: str = Field(pattern=_UUID_RE)
    admission_artifact_sha256: str = Field(pattern=_SHA256_RE)
    admission_policy_sha256: str = Field(pattern=_SHA256_RE)
    evidence_identity_sha256: str = Field(pattern=_SHA256_RE)
    admitted_runtime_build_sha256: str = Field(pattern=_SHA256_RE)
    evidence_runner_signer_sha256: str = Field(pattern=_SHA256_RE)
    qualification_signer_registry_sha256: str = Field(pattern=_SHA256_RE)
    qualification_signer_registry_revision: StrictInt = Field(
        ge=1, le=JS_MAX_SAFE_INTEGER
    )
    execution_authority_id: str = Field(pattern=_ID_RE)
    execution_authority_sha256: str = Field(pattern=_SHA256_RE)
    execution_authority_signer_sha256: str = Field(pattern=_SHA256_RE)
    permit_chain: ProductionDeliveryPermitChain
    permit_count: StrictInt = Field(ge=1, le=JS_MAX_SAFE_INTEGER)
    final_authority_sequence: StrictInt = Field(ge=0, le=JS_MAX_SAFE_INTEGER)
    final_runtime_delivery_sequence: StrictInt = Field(ge=0, le=JS_MAX_SAFE_INTEGER)
    workflow_contract_sha256: str = Field(pattern=_SHA256_RE)
    execution_outcome: ProductionExecutionOutcome
    execution_outcome_sha256: str = Field(pattern=_SHA256_RE)
    run_receipt: ProductionRunReceipt
    run_receipt_sha256: str = Field(pattern=_SHA256_RE)
    run_report_sha256: str = Field(pattern=_SHA256_RE)
    run_report_object_version: str = Field(pattern=_ID_RE)
    run_report_object_sha256: str = Field(pattern=_SHA256_RE)
    evidence_manifests: ProductionEvidenceManifests
    verified_at: str
    issued_at: str

    @model_validator(mode="after")
    def _closed_terminal_success(self) -> "ProductionTerminalVerificationPayload":
        if hashlib.sha256(self.run_id.encode("utf-8")).hexdigest() != (
            self.flow_run_id_sha256
        ):
            raise ValueError("terminal run identity digest is invalid")
        if self.admission_id == self.runtime_validation_id:
            raise ValueError("terminal admission and runtime identities must differ")
        if self.workflow_version_id != self.bundle_version_id:
            raise ValueError("terminal workflow and bundle versions must match")
        chain = self.permit_chain
        final = chain.entries[-1]
        if (
            self.execution_authority_id,
            self.execution_authority_sha256,
            self.execution_authority_signer_sha256,
            self.admission_artifact_sha256,
        ) != (
            final.execution_authority_id,
            final.execution_authority_sha256,
            final.authority_signer_sha256,
            final.admission_artifact_sha256,
        ):
            raise ValueError("terminal authority does not match its permit chain")
        if (
            self.run_id != final.run_id
            or self.flow_run_id_sha256 != final.flow_run_id_sha256
        ):
            raise ValueError("terminal run identity does not match its permit chain")
        if (
            self.evidence_identity_sha256,
            self.environment_digest,
            self.qualification_signer_registry_sha256,
            self.qualification_signer_registry_revision,
        ) != (
            final.evidence_identity_sha256,
            final.environment_digest,
            final.qualification_signer_registry_sha256,
            final.qualification_signer_registry_revision,
        ):
            raise ValueError("terminal qualification state does not match its permits")
        if (
            self.permit_count,
            self.final_authority_sequence,
            self.final_runtime_delivery_sequence,
        ) != (
            len(chain.entries),
            final.authority_sequence,
            final.runtime_delivery_sequence,
        ):
            raise ValueError("terminal permit counts do not match the permit chain")
        receipt = self.run_receipt
        outcome = self.execution_outcome
        manifests = self.evidence_manifests
        if (
            self.workflow_contract_sha256 != outcome.workflow_contract_sha256
            or self.execution_outcome_sha256 != outcome.artifact_sha256()
        ):
            raise ValueError("terminal execution outcome digest is invalid")
        if (
            manifests.policy.admission_policy_sha256 != self.admission_policy_sha256
            or manifests.policy.environment_digest != self.environment_digest
            or manifests.policy.environment_contract_sha256
            != self.environment_contract_sha256
            or manifests.policy.runtime_environment_sha256
            != self.runtime_environment_sha256
            or manifests.policy.identity_contract_sha256
            != self.identity_contract_sha256
            or manifests.policy.effect_contract_sha256 != self.effect_contract_sha256
            or manifests.authorization.admission_id != self.admission_id
            or manifests.authorization.admission_artifact_sha256
            != self.admission_artifact_sha256
            or manifests.authorization.execution_authority_id
            != self.execution_authority_id
            or manifests.authorization.execution_authority_sha256
            != self.execution_authority_sha256
            or manifests.authorization.permit_chain_sha256 != chain.permit_chain_sha256
            or manifests.identity.identity_contract_sha256
            != self.identity_contract_sha256
            or manifests.identity.workflow_contract_sha256
            != self.workflow_contract_sha256
            or manifests.postcondition.workflow_contract_sha256
            != self.workflow_contract_sha256
            or manifests.effect.effect_contract_sha256 != self.effect_contract_sha256
            or manifests.effect.workflow_contract_sha256
            != self.workflow_contract_sha256
            or (
                manifests.identity.required,
                manifests.postcondition.required,
                manifests.effect.required,
            )
            != (
                outcome.required_contracts.identity,
                outcome.required_contracts.postcondition,
                outcome.required_contracts.effect,
            )
        ):
            raise ValueError("terminal evidence manifests do not match the run")
        if receipt.source_receipt_digest == "" or self.run_receipt_sha256 != _sha256(
            receipt.model_dump(mode="json")
        ):
            raise ValueError("terminal run receipt digest is invalid")
        if (
            receipt.profile != outcome.profile
            or receipt.provenance != "production"
            or receipt.substrate != self.runtime_substrate
            or receipt.bundle_digest != self.bundle_content_digest
            or receipt.authorization_required
            != outcome.required_contracts.authorization
            or receipt.authorization_confirmed != outcome.passed_contracts.authorization
            or receipt.identity_required != outcome.required_contracts.identity
            or receipt.identity_confirmed != outcome.passed_contracts.identity
            or receipt.postconditions_required
            != outcome.required_contracts.postcondition
            or receipt.postconditions_confirmed
            != outcome.passed_contracts.postcondition
            or receipt.effects_required != outcome.required_contracts.effect
            or receipt.effects_confirmed != outcome.passed_contracts.effect
            or tuple(receipt.evidence_classes) != outcome.evidence_classes
            or receipt.model_calls != outcome.model_calls
            or receipt.external_network_calls != outcome.external_network_calls
        ):
            raise ValueError(
                "terminal run receipt does not match its execution outcome"
            )
        verified = _parse_utc(self.verified_at, field="terminal verified_at")
        issued = _parse_utc(self.issued_at, field="terminal issued_at")
        if not verified <= issued <= verified + timedelta(minutes=5):
            raise ValueError("terminal proof issue time is invalid")
        final_delivered = _parse_utc(final.delivered_at, field="permit delivered_at")
        registry_expires = _parse_utc(
            final.qualification_signer_registry_expires_at,
            field="permit registry expires_at",
        )
        if not final_delivered <= verified < registry_expires:
            raise ValueError("terminal verification is outside permit chronology")
        if self.run_report_object_sha256 != self.run_report_sha256:
            raise ValueError(
                "terminal report object must contain the exact revalidated report bytes"
            )
        return self

    def canonical_bytes(self) -> bytes:
        return canonical_json(self)


class EvidenceRunnerSigner(ClosedSignedModel):
    algorithm: Literal["ed25519"] = "ed25519"
    key_id: str = Field(pattern=_RUNNER_KEY_ID_RE)
    public_key: str

    @field_validator("public_key")
    @classmethod
    def _canonical_public_key(cls, value: str) -> str:
        try:
            decoded = b64decode(value, validate=True)
        except (TypeError, ValueError) as exc:
            raise ValueError("evidence runner public key is invalid") from exc
        if len(decoded) != 32 or b64encode(decoded).decode("ascii") != value:
            raise ValueError("evidence runner public key is invalid")
        return value

    @model_validator(mode="after")
    def _key_id_matches(self) -> "EvidenceRunnerSigner":
        if (
            evidence_runner_key_id(b64decode(self.public_key, validate=True))
            != self.key_id
        ):
            raise ValueError("evidence runner key id does not match its public key")
        return self


class ProductionTerminalVerificationEnvelope(ClosedSignedModel):
    payload: ProductionTerminalVerificationPayload
    signer: EvidenceRunnerSigner
    signature: str = Field(min_length=86, max_length=86)

    @field_validator("signature")
    @classmethod
    def _canonical_signature(cls, value: str) -> str:
        try:
            decoded = urlsafe_b64decode(value + "==")
        except ValueError as exc:
            raise ValueError("terminal signature is invalid") from exc
        if (
            len(decoded) != 64
            or urlsafe_b64encode(decoded).decode("ascii").rstrip("=") != value
        ):
            raise ValueError("terminal signature is invalid")
        return value

    @model_validator(mode="after")
    def _signer_matches_payload(self) -> "ProductionTerminalVerificationEnvelope":
        public_key = b64decode(self.signer.public_key, validate=True)
        if (
            evidence_runner_signer_sha256(public_key)
            != self.payload.evidence_runner_signer_sha256
        ):
            raise ValueError("terminal signer does not match the admitted runner")
        return self

    def artifact_sha256(self) -> str:
        return _sha256(self)


class ProductionTerminalVerificationExpected(ClosedSignedModel):
    """Exact storage and runtime values read independently by the acceptor."""

    run_id: str = Field(pattern=_UUID_RE)
    flow_run_id_sha256: str = Field(pattern=_SHA256_RE)
    tenant_id: str = Field(pattern=_UUID_RE)
    workflow_id: str = Field(pattern=_UUID_RE)
    workflow_version_id: str = Field(pattern=_UUID_RE)
    bundle_version_id: str = Field(pattern=_UUID_RE)
    bundle_artifact_sha256: str = Field(pattern=_SHA256_RE)
    bundle_content_digest: str = Field(pattern=_SHA256_RE)
    environment_digest: str = Field(pattern=_SHA256_RE)
    environment_contract_sha256: str = Field(pattern=_SHA256_RE)
    runtime_environment_sha256: str = Field(pattern=_SHA256_RE)
    identity_contract_sha256: str = Field(pattern=_SHA256_RE)
    effect_contract_sha256: str = Field(pattern=_SHA256_RE)
    runtime_validation_id: str = Field(pattern=_UUID_RE)
    runtime_substrate: Literal["web", "windows", "macos", "linux", "rdp", "citrix"]
    admission_id: str = Field(pattern=_UUID_RE)
    admission_artifact_sha256: str = Field(pattern=_SHA256_RE)
    admission_policy_sha256: str = Field(pattern=_SHA256_RE)
    evidence_identity_sha256: str = Field(pattern=_SHA256_RE)
    admitted_runtime_build_sha256: str = Field(pattern=_SHA256_RE)
    evidence_runner_signer_sha256: str = Field(pattern=_SHA256_RE)
    qualification_signer_registry_sha256: str = Field(pattern=_SHA256_RE)
    qualification_signer_registry_revision: StrictInt = Field(
        ge=1, le=JS_MAX_SAFE_INTEGER
    )
    execution_authority_id: str = Field(pattern=_ID_RE)
    execution_authority_sha256: str = Field(pattern=_SHA256_RE)
    execution_authority_signer_sha256: str = Field(pattern=_SHA256_RE)
    permit_chain_sha256: str = Field(pattern=_SHA256_RE)
    permit_count: StrictInt = Field(ge=1, le=JS_MAX_SAFE_INTEGER)
    final_authority_sequence: StrictInt = Field(ge=0, le=JS_MAX_SAFE_INTEGER)
    final_runtime_delivery_sequence: StrictInt = Field(ge=0, le=JS_MAX_SAFE_INTEGER)
    authenticated_runner_id_sha256: str = Field(pattern=_SHA256_RE)
    authenticated_session_id_sha256: str = Field(pattern=_SHA256_RE)
    acknowledged_one_use_claim_ids: tuple[str, ...] = Field(
        min_length=1, max_length=10_000
    )
    workflow_contract_sha256: str = Field(pattern=_SHA256_RE)
    execution_outcome_sha256: str = Field(pattern=_SHA256_RE)
    run_receipt_sha256: str = Field(pattern=_SHA256_RE)
    run_report_sha256: str = Field(pattern=_SHA256_RE)
    run_report_object_version: str = Field(pattern=_ID_RE)
    run_report_object_sha256: str = Field(pattern=_SHA256_RE)
    evidence_manifests: ProductionEvidenceManifests

    @model_validator(mode="after")
    def _closed_delivery_state(self) -> "ProductionTerminalVerificationExpected":
        if len(self.acknowledged_one_use_claim_ids) != self.permit_count:
            raise ValueError("live delivery claim count does not match permit count")
        if len(set(self.acknowledged_one_use_claim_ids)) != self.permit_count:
            raise ValueError("live delivery claims are not globally one-use")
        for claim_id in self.acknowledged_one_use_claim_ids:
            if _UUID_RE.fullmatch(claim_id) is None:
                raise ValueError("live delivery claim id is invalid")
        return self


class ProductionTerminalVerificationContext(ClosedSignedModel):
    """Independent exact bindings needed to produce one terminal v2 proof."""

    run_id: str = Field(pattern=_UUID_RE)
    tenant_id: str = Field(pattern=_UUID_RE)
    workflow_id: str = Field(pattern=_UUID_RE)
    workflow_version_id: str = Field(pattern=_UUID_RE)
    bundle_version_id: str = Field(pattern=_UUID_RE)
    bundle_artifact_sha256: str = Field(pattern=_SHA256_RE)
    environment_digest: str = Field(pattern=_SHA256_RE)
    environment_contract_sha256: str = Field(pattern=_SHA256_RE)
    runtime_environment_sha256: str = Field(pattern=_SHA256_RE)
    identity_contract_sha256: str = Field(pattern=_SHA256_RE)
    effect_contract_sha256: str = Field(pattern=_SHA256_RE)
    runtime_validation_id: str = Field(pattern=_UUID_RE)
    runtime_substrate: Literal["web", "windows", "macos", "linux", "rdp", "citrix"]
    admission_id: str = Field(pattern=_UUID_RE)
    admission_artifact_sha256: str = Field(pattern=_SHA256_RE)
    admission_policy_sha256: str = Field(pattern=_SHA256_RE)
    evidence_identity_sha256: str = Field(pattern=_SHA256_RE)
    admitted_runtime_build_sha256: str = Field(pattern=_SHA256_RE)
    evidence_runner_signer_sha256: str = Field(pattern=_SHA256_RE)
    qualification_signer_registry_sha256: str = Field(pattern=_SHA256_RE)
    qualification_signer_registry_revision: StrictInt = Field(
        ge=1, le=JS_MAX_SAFE_INTEGER
    )
    execution_authority_id: str = Field(pattern=_UUID_RE)
    execution_authority_sha256: str = Field(pattern=_SHA256_RE)
    execution_authority_signer_sha256: str = Field(pattern=_SHA256_RE)
    permit_chain: ProductionDeliveryPermitChain
    run_report_object_version: str = Field(pattern=_ID_RE)
    verified_at: str
    issued_at: str

    @model_validator(mode="after")
    def _closed_context(self) -> "ProductionTerminalVerificationContext":
        if self.workflow_version_id != self.bundle_version_id:
            raise ValueError("terminal workflow and bundle versions must match")
        if self.admission_id == self.runtime_validation_id:
            raise ValueError("terminal admission and runtime identities must differ")
        _parse_utc(self.verified_at, field="terminal verified_at")
        _parse_utc(self.issued_at, field="terminal issued_at")
        return self


@dataclass(frozen=True)
class BuiltProductionTerminalVerification:
    """Signed proof plus the exact report object bytes that it binds."""

    envelope: ProductionTerminalVerificationEnvelope
    report_bytes: bytes = dataclass_field(repr=False)
    report_sha256: str


_EXPECTED_FIELDS: Final[tuple[str, ...]] = tuple(
    field
    for field in ProductionTerminalVerificationExpected.model_fields
    if field
    not in {
        "permit_chain_sha256",
        "authenticated_runner_id_sha256",
        "authenticated_session_id_sha256",
        "acknowledged_one_use_claim_ids",
    }
)


def sign_production_terminal_verification(
    payload: ProductionTerminalVerificationPayload,
    private_key: Ed25519PrivateKey,
) -> ProductionTerminalVerificationEnvelope:
    payload = ProductionTerminalVerificationPayload.model_validate(
        payload.model_dump(mode="json")
    )
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    signer = EvidenceRunnerSigner(
        key_id=evidence_runner_key_id(public_key),
        public_key=b64encode(public_key).decode("ascii"),
    )
    if (
        evidence_runner_signer_sha256(public_key)
        != payload.evidence_runner_signer_sha256
    ):
        raise ProductionTerminalVerificationError(
            "terminal signer does not match the admitted runner"
        )
    signature = private_key.sign(SIGNATURE_DOMAIN + payload.canonical_bytes())
    return ProductionTerminalVerificationEnvelope(
        payload=payload,
        signer=signer,
        signature=urlsafe_b64encode(signature).decode("ascii").rstrip("="),
    )


def build_production_terminal_verification(
    report: RunReport,
    *,
    context: ProductionTerminalVerificationContext,
    private_key: Ed25519PrivateKey,
) -> BuiltProductionTerminalVerification:
    """Build the complete production proof from one retained Flow report."""

    context = ProductionTerminalVerificationContext.model_validate(
        context.model_dump(mode="json")
    )
    prepared = prepare_production_terminal_evidence(report)
    manifests = build_production_evidence_manifests(
        prepared,
        admission_policy_sha256=context.admission_policy_sha256,
        environment_digest=context.environment_digest,
        environment_contract_sha256=context.environment_contract_sha256,
        runtime_environment_sha256=context.runtime_environment_sha256,
        identity_contract_sha256=context.identity_contract_sha256,
        effect_contract_sha256=context.effect_contract_sha256,
        admission_id=context.admission_id,
        admission_artifact_sha256=context.admission_artifact_sha256,
        execution_authority_id=context.execution_authority_id,
        execution_authority_sha256=context.execution_authority_sha256,
        permit_chain=context.permit_chain,
    )
    final = context.permit_chain.entries[-1]
    payload = ProductionTerminalVerificationPayload(
        run_id=context.run_id,
        flow_run_id_sha256=prepared.flow_run_id_sha256,
        tenant_id=context.tenant_id,
        workflow_id=context.workflow_id,
        workflow_version_id=context.workflow_version_id,
        bundle_version_id=context.bundle_version_id,
        bundle_artifact_sha256=context.bundle_artifact_sha256,
        bundle_content_digest=prepared.bundle_content_digest,
        environment_digest=context.environment_digest,
        environment_contract_sha256=context.environment_contract_sha256,
        runtime_environment_sha256=context.runtime_environment_sha256,
        identity_contract_sha256=context.identity_contract_sha256,
        effect_contract_sha256=context.effect_contract_sha256,
        runtime_validation_id=context.runtime_validation_id,
        runtime_substrate=context.runtime_substrate,
        admission_id=context.admission_id,
        admission_artifact_sha256=context.admission_artifact_sha256,
        admission_policy_sha256=context.admission_policy_sha256,
        evidence_identity_sha256=context.evidence_identity_sha256,
        admitted_runtime_build_sha256=context.admitted_runtime_build_sha256,
        evidence_runner_signer_sha256=context.evidence_runner_signer_sha256,
        qualification_signer_registry_sha256=(
            context.qualification_signer_registry_sha256
        ),
        qualification_signer_registry_revision=(
            context.qualification_signer_registry_revision
        ),
        execution_authority_id=context.execution_authority_id,
        execution_authority_sha256=context.execution_authority_sha256,
        execution_authority_signer_sha256=(context.execution_authority_signer_sha256),
        permit_chain=context.permit_chain,
        permit_count=len(context.permit_chain.entries),
        final_authority_sequence=final.authority_sequence,
        final_runtime_delivery_sequence=final.runtime_delivery_sequence,
        workflow_contract_sha256=(prepared.execution_outcome.workflow_contract_sha256),
        execution_outcome=prepared.execution_outcome,
        execution_outcome_sha256=prepared.execution_outcome.artifact_sha256(),
        run_receipt=prepared.run_receipt,
        run_receipt_sha256=prepared.run_receipt_sha256,
        run_report_sha256=prepared.report_sha256,
        run_report_object_version=context.run_report_object_version,
        run_report_object_sha256=prepared.report_sha256,
        evidence_manifests=manifests,
        verified_at=context.verified_at,
        issued_at=context.issued_at,
    )
    envelope = sign_production_terminal_verification(payload, private_key)
    return BuiltProductionTerminalVerification(
        envelope=envelope,
        report_bytes=prepared.report_bytes,
        report_sha256=prepared.report_sha256,
    )


def verify_production_terminal_verification(
    envelope: ProductionTerminalVerificationEnvelope,
    *,
    expected: ProductionTerminalVerificationExpected,
    now: datetime | None = None,
) -> str:
    """Verify the runner proof against independent DB and object-store state."""

    try:
        envelope = ProductionTerminalVerificationEnvelope.model_validate(
            envelope.model_dump(mode="json")
        )
        expected = ProductionTerminalVerificationExpected.model_validate(
            expected.model_dump(mode="json")
        )
    except ValueError as exc:
        raise ProductionTerminalVerificationError(
            "production terminal envelope is not canonical"
        ) from exc
    payload = envelope.payload
    public_key_bytes = b64decode(envelope.signer.public_key, validate=True)
    try:
        Ed25519PublicKey.from_public_bytes(public_key_bytes).verify(
            urlsafe_b64decode(envelope.signature + "=="),
            SIGNATURE_DOMAIN + payload.canonical_bytes(),
        )
    except (InvalidSignature, ValueError) as exc:
        raise ProductionTerminalVerificationError(
            "production terminal signature is invalid"
        ) from exc
    for field in _EXPECTED_FIELDS:
        if getattr(payload, field) != getattr(expected, field):
            raise ProductionTerminalVerificationError(
                f"production terminal {field} does not match live state"
            )
    if payload.permit_chain.permit_chain_sha256 != expected.permit_chain_sha256:
        raise ProductionTerminalVerificationError(
            "production terminal permit chain does not match live state"
        )
    chain = payload.permit_chain
    if any(
        entry.authenticated_runner_id_sha256 != expected.authenticated_runner_id_sha256
        for entry in chain.entries
    ):
        raise ProductionTerminalVerificationError(
            "production terminal runner identity does not match live state"
        )
    if any(
        entry.authenticated_session_id_sha256
        != expected.authenticated_session_id_sha256
        for entry in chain.entries
    ):
        raise ProductionTerminalVerificationError(
            "production terminal delivery session does not match live state"
        )
    if tuple(entry.one_use_claim_id for entry in chain.entries) != (
        expected.acknowledged_one_use_claim_ids
    ):
        raise ProductionTerminalVerificationError(
            "production terminal one-use claims do not match live state"
        )
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if _parse_utc(payload.issued_at, field="terminal issued_at") > current + timedelta(
        minutes=5
    ):
        raise ProductionTerminalVerificationError(
            "production terminal proof is future-issued"
        )
    return envelope.artifact_sha256()


def rebuild_production_delivery_permit_chain(
    permits: tuple[ProductionDeliveryPermit, ...],
) -> ProductionDeliveryPermitChain:
    """Rebuild the permit chain from independently retained permit records.

    The acceptor must not trust either artifact digest or the chain digest
    carried inside the proof. It loads each exact retained permit and receipt
    envelope, verifies both authority signatures, recomputes their digests,
    rebuilds the chain, and passes the rebuilt digest as the expected value.
    """

    return ProductionDeliveryPermitChain.build(permits)


def rebuild_production_delivery_permit_chain_from_artifacts(
    artifacts: tuple[tuple[bytes, bytes], ...],
) -> ProductionDeliveryPermitChain:
    """Rebuild a chain from exact retained permit and receipt envelope bytes."""

    if not artifacts or len(artifacts) > 10_000:
        raise ProductionTerminalVerificationError(
            "retained production delivery artifact count is invalid"
        )
    entries: list[ProductionDeliveryPermit] = []
    for permit_bytes, receipt_bytes in artifacts:
        if (
            not isinstance(permit_bytes, bytes)
            or not isinstance(receipt_bytes, bytes)
            or len(permit_bytes) == 0
            or len(receipt_bytes) == 0
            or len(permit_bytes) > MAX_DELIVERY_ARTIFACT_BYTES
            or len(receipt_bytes) > MAX_DELIVERY_ARTIFACT_BYTES
        ):
            raise ProductionTerminalVerificationError(
                "retained production delivery artifact size is invalid"
            )
        try:
            permit = ProductionDeliveryPermitArtifact.model_validate_json(permit_bytes)
            receipt = ProductionDeliveryReceiptArtifact.model_validate_json(
                receipt_bytes
            )
        except ValueError as exc:
            raise ProductionTerminalVerificationError(
                "retained production delivery artifact is invalid"
            ) from exc
        if (
            permit.canonical_bytes() != permit_bytes
            or receipt.canonical_bytes() != receipt_bytes
        ):
            raise ProductionTerminalVerificationError(
                "retained production delivery artifact is not canonical"
            )
        entries.append(ProductionDeliveryPermit.build(permit, receipt))
    return ProductionDeliveryPermitChain.build(tuple(entries))


def verify_production_terminal_verification_from_report(
    envelope: ProductionTerminalVerificationEnvelope,
    *,
    report_bytes: bytes,
    expected: ProductionTerminalVerificationExpected,
    now: datetime | None = None,
) -> str:
    """Verify the proof by re-deriving every projection from the retained report.

    ``report_bytes`` are the exact bytes of the immutable report object version
    that the proof names (``run_report_object_version``), loaded independently
    by the acceptor.  The acceptor hashes these bytes, parses the typed
    :class:`RunReport`, and reconstructs the outcome, receipt, and evidence
    manifests.  A proof whose projections do not equal the reconstruction is
    refused even when its internal digests are self-consistent, so a signed
    proof can never bind an arbitrary report hash to separately created
    outcome, receipt, or manifest data.
    """

    payload = envelope.payload
    digest = hashlib.sha256(report_bytes).hexdigest()
    if digest != expected.run_report_object_sha256:
        raise ProductionTerminalVerificationError(
            "terminal report bytes do not match the retained report object"
        )
    if digest != payload.run_report_sha256:
        raise ProductionTerminalVerificationError(
            "terminal proof does not bind the retained report bytes"
        )
    try:
        report = RunReport.model_validate_json(report_bytes)
    except ValueError as exc:
        raise ProductionTerminalVerificationError(
            "terminal report bytes failed typed revalidation"
        ) from exc
    prepared = prepare_production_terminal_evidence(report)
    if prepared.report_sha256 != digest:
        raise ProductionTerminalVerificationError(
            "terminal report bytes are not the canonical report encoding"
        )
    if (
        prepared.flow_run_id_sha256 != payload.flow_run_id_sha256
        or prepared.bundle_content_digest != payload.bundle_content_digest
    ):
        raise ProductionTerminalVerificationError(
            "terminal proof does not bind the retained report identity"
        )
    if prepared.execution_outcome != payload.execution_outcome:
        raise ProductionTerminalVerificationError(
            "terminal execution outcome does not derive from the retained report"
        )
    if (
        prepared.run_receipt != payload.run_receipt
        or prepared.run_receipt_sha256 != payload.run_receipt_sha256
    ):
        raise ProductionTerminalVerificationError(
            "terminal run receipt does not derive from the retained report"
        )
    manifests = build_production_evidence_manifests(
        prepared,
        admission_policy_sha256=payload.admission_policy_sha256,
        environment_digest=payload.environment_digest,
        environment_contract_sha256=payload.environment_contract_sha256,
        runtime_environment_sha256=payload.runtime_environment_sha256,
        identity_contract_sha256=payload.identity_contract_sha256,
        effect_contract_sha256=payload.effect_contract_sha256,
        admission_id=payload.admission_id,
        admission_artifact_sha256=payload.admission_artifact_sha256,
        execution_authority_id=payload.execution_authority_id,
        execution_authority_sha256=payload.execution_authority_sha256,
        permit_chain=payload.permit_chain,
    )
    if manifests != payload.evidence_manifests:
        raise ProductionTerminalVerificationError(
            "terminal evidence manifests do not derive from the retained report"
        )
    return verify_production_terminal_verification(envelope, expected=expected, now=now)
