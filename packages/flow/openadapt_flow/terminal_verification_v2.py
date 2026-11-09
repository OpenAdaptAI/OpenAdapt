"""Production terminal-verification v2 contracts and producer.

Flow builds one signed terminal artifact after it revalidates a complete
VERIFIED, HALTED_BEFORE_EFFECT, or RECONCILIATION_REQUIRED report. The
acceptor reconstructs every value from immutable storage. Only a VERIFIED
artifact can authorize a Production success or a billable result.
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

from openadapt_flow.ir import (
    EffectVerificationEvidence,
    ManagedResultLossEvidence,
    PostconditionContractEvidence,
    RunReport,
)
from openadapt_flow.qualification_admission_v2 import (
    MAX_PERMIT_SNAPSHOT_AGE,
    ClosedSignedModel,
    canonical_json,
)
from openadapt_flow.receipt import (
    ReceiptError,
    RunReceipt,
    _hour_utc,
    _over_halt_count,
    _receipt_builder_version,
    build_receipt,
)
from openadapt_flow.transaction import (
    TransactionOutcome,
    _attempt_state,
    _effect_absence_proven,
    _is_consequential_result,
    classify_transaction_outcome,
)

SCHEMA: Final[Literal["openadapt.production-terminal-verification/v2"]] = (
    "openadapt.production-terminal-verification/v2"
)
SIGNATURE_DOMAIN: Final[bytes] = b"openadapt-production-terminal-verification-v2\0"
SCHEMA_V3: Final[Literal["openadapt.production-terminal-verification/v3"]] = (
    "openadapt.production-terminal-verification/v3"
)
SIGNATURE_DOMAIN_V3: Final[bytes] = b"openadapt-production-terminal-verification-v3\0"
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
PENDING_PERMIT_SCHEMA: Final[
    Literal["openadapt.production-delivery-pending-permit/v2"]
] = "openadapt.production-delivery-pending-permit/v2"
EXECUTION_AUTHORITY_DOMAIN: Final[bytes] = (
    b"openadapt.production-execution-authority.v2\0"
)
EXECUTION_AUTHORITY_SCHEMA: Final[
    Literal["openadapt.production-execution-authority/v2"]
] = "openadapt.production-execution-authority/v2"
RESULT_LOSS_CLOSURE_REQUEST_DOMAIN: Final[bytes] = (
    b"openadapt.production-delivery-result-loss-closure-request.v2\0"
)
RESULT_LOSS_CLOSURE_PAYLOAD_DOMAIN: Final[bytes] = (
    b"openadapt.production-delivery-result-loss-closure-payload.v2\0"
)
RESULT_LOSS_CLOSURE_PAYLOAD_SCHEMA: Final[
    Literal["openadapt.production-delivery-result-loss-closure-payload/v2"]
] = "openadapt.production-delivery-result-loss-closure-payload/v2"
RESULT_LOSS_CLOSURE_ARTIFACT_SCHEMA: Final[
    Literal["openadapt.production-delivery-result-loss-closure-artifact/v2"]
] = "openadapt.production-delivery-result-loss-closure-artifact/v2"

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
    transaction_outcome: TransactionOutcome
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
    authorization: StrictInt = Field(ge=0, le=JS_MAX_SAFE_INTEGER)
    identity: StrictInt = Field(ge=0, le=JS_MAX_SAFE_INTEGER)
    postcondition: StrictInt = Field(ge=0, le=JS_MAX_SAFE_INTEGER)
    effect: StrictInt = Field(ge=0, le=JS_MAX_SAFE_INTEGER)


class ProductionExecutionOutcome(ClosedSignedModel):
    """Complete, explicit projection of the current Flow outcome contract."""

    version: Literal["openadapt.execution-outcome/v1"] = (
        "openadapt.execution-outcome/v1"
    )
    outcome: Literal[
        "VERIFIED",
        "HALTED",
        "FAILED",
        "ROLLED_BACK",
    ] = "VERIFIED"
    profile: Literal["standard", "regulated"]
    production_eligible: StrictBool = True
    qualification_evidence_only: Literal[False] = False
    execution_completed: StrictBool = True
    required_contracts: TerminalContractCounts
    passed_contracts: TerminalContractCounts
    workflow_contract_sha256: str = Field(pattern=_SHA256_RE)
    postcondition_evidence: tuple[PostconditionContractEvidence, ...] = Field(
        max_length=10_000
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
    ] = Field(min_length=1, max_length=7)
    model_calls: StrictInt = Field(ge=0, le=JS_MAX_SAFE_INTEGER)
    external_network_calls: Literal["none", "observed"]
    compensation_actions: StrictInt = Field(default=0, ge=0, le=JS_MAX_SAFE_INTEGER)
    managed_result_loss_evidence_sha256: str | None = Field(
        default=None,
        pattern=_SHA256_RE,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def _complete_terminal_outcome(self) -> "ProductionExecutionOutcome":
        result_loss = self.managed_result_loss_evidence_sha256 is not None
        required = self.required_contracts.model_dump(mode="python")
        passed = self.passed_contracts.model_dump(mode="python")
        if any(passed[key] > required[key] for key in required):
            raise ValueError("terminal passed contract counts exceed required counts")
        if self.required_contracts.authorization != 1:
            raise ValueError("terminal outcome requires one authorization")
        if self.passed_contracts.authorization != 1:
            raise ValueError("terminal outcome requires one passed authorization")
        if not result_loss and not self.postcondition_evidence:
            raise ValueError("ordinary terminal outcome requires a postcondition")
        if len(self.postcondition_evidence) != self.required_contracts.postcondition:
            raise ValueError("terminal postcondition evidence cardinality is invalid")
        if (
            sum(item.verdict == "passed" for item in self.postcondition_evidence)
            != self.passed_contracts.postcondition
        ):
            raise ValueError("terminal postcondition evidence count is invalid")
        if any(
            item.workflow_contract_sha256 != self.workflow_contract_sha256
            for item in self.postcondition_evidence
        ):
            raise ValueError("terminal postcondition evidence binding is invalid")
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
        if "authorization" not in self.evidence_classes:
            raise ValueError("terminal outcome lacks authorization evidence")
        if (self.passed_contracts.identity > 0) != (
            "identity" in self.evidence_classes
        ):
            raise ValueError("terminal identity evidence class is invalid")
        if (self.passed_contracts.postcondition > 0) != (
            "postcondition" in self.evidence_classes
        ):
            raise ValueError("terminal postcondition evidence class is invalid")
        effect_classes = {
            item for item in self.evidence_classes if item.startswith("effect_tier_")
        }
        if (self.passed_contracts.effect > 0) != bool(effect_classes) or len(
            effect_classes
        ) > 1:
            raise ValueError("terminal effect evidence classes are invalid")
        if (self.model_calls > 0) != ("model" in self.evidence_classes):
            raise ValueError("terminal model evidence does not match its call count")
        if self.model_calls > 0 and self.external_network_calls != "observed":
            raise ValueError("terminal model calls require observed network evidence")
        if (self.compensation_actions > 0) != (self.outcome == "ROLLED_BACK"):
            raise ValueError("terminal compensation evidence is invalid")
        if self.outcome == "VERIFIED":
            if self.passed_contracts != self.required_contracts:
                raise ValueError(
                    "terminal VERIFIED outcome lacks complete contract coverage"
                )
            if not self.production_eligible or not self.execution_completed:
                raise ValueError("terminal VERIFIED outcome is not production complete")
            required_classes = {"authorization", "identity", "postcondition"}
            if not required_classes.issubset(self.evidence_classes):
                raise ValueError(
                    "terminal VERIFIED outcome lacks required evidence classes"
                )
            if len(effect_classes) != 1:
                raise ValueError("terminal VERIFIED outcome requires one effect tier")
        elif self.production_eligible:
            raise ValueError("only a VERIFIED terminal outcome is production eligible")
        elif self.outcome == "HALTED" and self.execution_completed:
            raise ValueError("terminal HALTED outcome cannot claim completed execution")
        if result_loss and (
            self.outcome != "HALTED"
            or self.production_eligible
            or self.execution_completed
            or self.required_contracts
            != TerminalContractCounts(
                authorization=1,
                identity=0,
                postcondition=0,
                effect=0,
            )
            or self.passed_contracts != self.required_contracts
            or self.postcondition_evidence
            or self.evidence_classes != ("authorization",)
        ):
            raise ValueError("managed result loss execution outcome is invalid")
        return self

    def artifact_sha256(self) -> str:
        return hashlib.sha256(
            EXECUTION_OUTCOME_DOMAIN + canonical_json(self)
        ).hexdigest()


class ProductionRunReceipt(ClosedSignedModel):
    """Cross-language integer projection of one terminal report."""

    schema_version: Literal["openadapt.production-run-receipt/v1"] = (
        "openadapt.production-run-receipt/v1"
    )
    source_schema_version: Literal[
        "openadapt.run-receipt/v2",
        "openadapt.run-report/v1",
    ]
    outcome: Literal["VERIFIED", "HALTED", "FAILED", "ROLLED_BACK"]
    transaction_outcome: Literal[
        "VERIFIED",
        "HALTED_BEFORE_EFFECT",
        "RECONCILIATION_REQUIRED",
    ]
    profile: Literal["standard", "regulated"]
    production_eligible: StrictBool
    steps_total: StrictInt = Field(ge=1, le=JS_MAX_SAFE_INTEGER)
    steps_ok: StrictInt = Field(ge=0, le=JS_MAX_SAFE_INTEGER)
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
    ] = Field(min_length=1, max_length=7)
    effect_tier_reached: Literal[
        "none",
        "independent_system",
        "independent_session",
        "persisted_state_reacquisition",
    ]
    authorization_required: StrictInt = Field(ge=1, le=JS_MAX_SAFE_INTEGER)
    authorization_confirmed: StrictInt = Field(ge=1, le=JS_MAX_SAFE_INTEGER)
    identity_required: StrictInt = Field(ge=0, le=JS_MAX_SAFE_INTEGER)
    identity_confirmed: StrictInt = Field(ge=0, le=JS_MAX_SAFE_INTEGER)
    postconditions_required: StrictInt = Field(ge=0, le=JS_MAX_SAFE_INTEGER)
    postconditions_confirmed: StrictInt = Field(ge=0, le=JS_MAX_SAFE_INTEGER)
    effects_required: StrictInt = Field(ge=0, le=JS_MAX_SAFE_INTEGER)
    effects_confirmed: StrictInt = Field(ge=0, le=JS_MAX_SAFE_INTEGER)
    identity_armed: StrictInt = Field(ge=0, le=JS_MAX_SAFE_INTEGER)
    identity_applicable: StrictInt = Field(ge=0, le=JS_MAX_SAFE_INTEGER)
    over_halt_count: StrictInt = Field(ge=0, le=JS_MAX_SAFE_INTEGER)
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
    managed_result_loss_evidence_sha256: str | None = Field(
        default=None,
        pattern=_SHA256_RE,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def _closed_receipt(self) -> "ProductionRunReceipt":
        result_loss = self.managed_result_loss_evidence_sha256 is not None
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
            if confirmed > required:
                raise ValueError(f"terminal receipt exceeds required {label} coverage")
        if self.authorization_required != 1:
            raise ValueError("terminal receipt requires one authorization")
        if self.authorization_confirmed != 1:
            raise ValueError("terminal receipt requires one passed authorization")
        if not result_loss and (
            self.identity_required < 1 or self.postconditions_required < 1
        ):
            raise ValueError(
                "ordinary terminal receipt requires identity and postcondition contracts"
            )
        if self.steps_ok > self.steps_total:
            raise ValueError("terminal receipt successful step count is invalid")
        if self.evidence_classes != tuple(sorted(self.evidence_classes)) or len(
            self.evidence_classes
        ) != len(set(self.evidence_classes)):
            raise ValueError("production receipt evidence classes are invalid")
        if len(self.rung_histogram) > 7 or any(
            type(value) is not int or value < 0 or value > JS_MAX_SAFE_INTEGER
            for value in self.rung_histogram.values()
        ):
            raise ValueError("production receipt rung counts are invalid")
        effect_classes = {
            item for item in self.evidence_classes if item.startswith("effect_tier_")
        }
        expected_effect_tier = {
            "effect_tier_1": "independent_system",
            "effect_tier_2": "independent_session",
            "effect_tier_3": "persisted_state_reacquisition",
        }
        if self.effects_confirmed == 0:
            if effect_classes or self.effect_tier_reached != "none":
                raise ValueError("terminal receipt effect evidence is inconsistent")
        elif (
            len(effect_classes) != 1
            or self.effect_tier_reached
            != (expected_effect_tier[next(iter(effect_classes))])
        ):
            raise ValueError("terminal receipt effect tier is inconsistent")
        if self.transaction_outcome == "VERIFIED":
            if (
                self.outcome != "VERIFIED"
                or not self.production_eligible
                or self.source_schema_version != "openadapt.run-receipt/v2"
                or self.steps_ok != self.steps_total
                or self.identity_armed != self.identity_applicable
                or self.over_halt_count != 0
                or self.effect_tier_reached == "none"
            ):
                raise ValueError("VERIFIED terminal receipt is incomplete")
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
                    raise ValueError(
                        f"VERIFIED terminal receipt lacks complete {label} coverage"
                    )
        elif self.production_eligible:
            raise ValueError(
                "non-VERIFIED terminal receipt cannot be production eligible"
            )
        elif self.source_schema_version != "openadapt.run-report/v1":
            raise ValueError(
                "non-VERIFIED terminal receipt must derive from its report"
            )
        elif self.transaction_outcome == "HALTED_BEFORE_EFFECT":
            if (
                self.outcome != "HALTED"
                or self.effects_confirmed != 0
                or self.effect_tier_reached != "none"
            ):
                raise ValueError("HALTED terminal receipt does not prove no effect")
        if result_loss and (
            self.transaction_outcome != "RECONCILIATION_REQUIRED"
            or self.outcome != "HALTED"
            or self.production_eligible
            or self.source_schema_version != "openadapt.run-report/v1"
            or self.steps_total != 1
            or self.steps_ok != 0
            or self.identity_required != 0
            or self.identity_confirmed != 0
            or self.postconditions_required != 0
            or self.postconditions_confirmed != 0
            or self.effects_required != 0
            or self.effects_confirmed != 0
            or self.evidence_classes != ("authorization",)
            or self.effect_tier_reached != "none"
        ):
            raise ValueError("managed result loss receipt is invalid")
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
            "source_schema_version": "openadapt.run-receipt/v2",
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


def project_production_terminal_run_receipt(
    report: RunReport,
    *,
    transaction_outcome: TransactionOutcome,
) -> ProductionRunReceipt:
    """Project a non-success terminal report without creating a success receipt."""

    if transaction_outcome not in {
        TransactionOutcome.HALTED_BEFORE_EFFECT,
        TransactionOutcome.RECONCILIATION_REQUIRED,
    }:
        raise ProductionTerminalVerificationError(
            "non-success terminal receipt has an invalid transaction outcome"
        )
    envelope = report.outcome_envelope
    if (
        envelope is None
        or report.execution_outcome not in {"HALTED", "FAILED", "ROLLED_BACK"}
        or report.execution_profile not in {"standard", "regulated"}
        or report.production_eligible
        or report.execution_completed is True
        and report.execution_outcome == "HALTED"
        or report.transaction_outcome != transaction_outcome.value
        or report.transaction_billable is not False
        or report.bundle_content_digest is None
    ):
        raise ProductionTerminalVerificationError(
            "terminal report lacks its closed non-success transaction contract"
        )
    if not report.results:
        raise ProductionTerminalVerificationError(
            "terminal report contains no retained step result"
        )
    report_bytes = canonical_json(report.model_dump(mode="json"))
    report_sha256 = hashlib.sha256(report_bytes).hexdigest()
    try:
        micros = Decimal(str(round(float(report.est_model_cost_usd), 6))) * Decimal(
            1_000_000
        )
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ProductionTerminalVerificationError(
            "terminal report cost is not an exact integer microusd value"
        ) from exc
    if micros != micros.to_integral_value():
        raise ProductionTerminalVerificationError(
            "terminal report cost is not an exact integer microusd value"
        )
    required = envelope.required_contracts
    passed = envelope.passed_contracts
    confirmed_tiers = [
        int(item.verification_tier)
        for result in report.results
        for item in result.effect_evidence
        if item.final_verdict == "confirmed"
        and item.observed_effect == "present"
        and item.verification_tier is not None
    ]
    if int(passed.effect) > 0 and len(confirmed_tiers) != int(passed.effect):
        raise ProductionTerminalVerificationError(
            "terminal report lacks exact confirmed effect tiers"
        )
    effect_tier_reached = (
        {
            1: "independent_system",
            2: "independent_session",
            3: "persisted_state_reacquisition",
        }.get(max(confirmed_tiers))
        if confirmed_tiers
        else "none"
    )
    if effect_tier_reached is None:
        raise ProductionTerminalVerificationError(
            "terminal report effect tier is outside the production range"
        )
    try:
        return ProductionRunReceipt.model_validate(
            {
                "source_schema_version": "openadapt.run-report/v1",
                "outcome": report.execution_outcome,
                "transaction_outcome": transaction_outcome.value,
                "profile": report.execution_profile,
                "production_eligible": False,
                "steps_total": len(report.results),
                "steps_ok": sum(1 for result in report.results if result.ok),
                "heals": int(report.heal_count),
                "model_calls": int(report.model_calls),
                "est_cost_microusd": int(micros),
                "duration_ms": int(round(float(report.total_ms))),
                "rung_histogram": dict(report.rung_counts),
                "evidence_classes": tuple(sorted(envelope.evidence_classes)),
                "effect_tier_reached": effect_tier_reached,
                "authorization_required": int(required.authorization),
                "authorization_confirmed": int(passed.authorization),
                "identity_required": int(required.identity),
                "identity_confirmed": int(passed.identity),
                "postconditions_required": int(required.postcondition),
                "postconditions_confirmed": int(passed.postcondition),
                "effects_required": int(required.effect),
                "effects_confirmed": int(passed.effect),
                "identity_armed": int(report.identity_armed_steps),
                "identity_applicable": int(report.identity_applicable_steps),
                "over_halt_count": _over_halt_count(report),
                "substrate": report.execution_target_kind,
                "provenance": "production",
                "receipt_builder_version": _receipt_builder_version(),
                "external_network_calls": envelope.external_network_calls,
                "bundle_digest": report.bundle_content_digest,
                "source_receipt_digest": report_sha256,
                "source_receipt_sha256": report_sha256,
                "generated_at": _hour_utc(report.started_at),
                "managed_result_loss_evidence_sha256": (
                    report.managed_result_loss.evidence_sha256
                    if report.managed_result_loss is not None
                    else None
                ),
            }
        )
    except (TypeError, ValueError) as exc:
        raise ProductionTerminalVerificationError(
            "terminal report cannot produce a closed non-success receipt"
        ) from exc


def prepare_production_terminal_evidence(
    report: RunReport,
) -> PreparedProductionTerminalEvidence:
    """Revalidate the exact report and build its terminal-safe projections.

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
        or validated.workflow_contract_sha256 is None
    ):
        raise ProductionTerminalVerificationError(
            "terminal report lacks its run or bundle binding"
        )
    transaction_outcome = classify_transaction_outcome(validated)
    if transaction_outcome not in {
        TransactionOutcome.VERIFIED,
        TransactionOutcome.HALTED_BEFORE_EFFECT,
        TransactionOutcome.RECONCILIATION_REQUIRED,
    }:
        raise ProductionTerminalVerificationError(
            "terminal report does not have an admitted terminal transaction outcome"
        )
    if (
        validated.transaction_outcome != transaction_outcome.value
        or validated.transaction_billable is not transaction_outcome.is_billable
    ):
        raise ProductionTerminalVerificationError(
            "terminal report transaction fields differ from its retained evidence"
        )
    if transaction_outcome is TransactionOutcome.HALTED_BEFORE_EFFECT and any(
        _is_consequential_result(result) and not _effect_absence_proven(result)
        for result in validated.results
    ):
        raise ProductionTerminalVerificationError(
            "terminal HALTED report lacks exact effect-absence evidence"
        )
    try:
        if transaction_outcome is TransactionOutcome.VERIFIED:
            receipt = project_production_run_receipt(build_receipt(validated))
        else:
            receipt = project_production_terminal_run_receipt(
                validated,
                transaction_outcome=transaction_outcome,
            )
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
                "managed_result_loss_evidence_sha256": (
                    validated.managed_result_loss.evidence_sha256
                    if validated.managed_result_loss is not None
                    else None
                ),
            }
        )
    except (ReceiptError, ValueError) as exc:
        raise ProductionTerminalVerificationError(
            "terminal report is not a complete production terminal outcome"
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
        transaction_outcome=transaction_outcome,
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
    status: Literal["verified", "mismatch", "abstain", "unreadable"]
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
    required: StrictInt = Field(ge=0, le=JS_MAX_SAFE_INTEGER)
    confirmed: StrictInt = Field(ge=0, le=JS_MAX_SAFE_INTEGER)
    results: tuple[ProductionIdentityResult, ...] = Field(max_length=10_000)
    manifest_sha256: str = Field(pattern=_SHA256_RE)

    @model_validator(mode="after")
    def _digest(self) -> "ProductionIdentityEvidenceManifest":
        if self.confirmed > self.required or len(self.results) > self.required:
            raise ValueError("production identity evidence coverage is invalid")
        if sum(item.status == "verified" for item in self.results) != self.confirmed:
            raise ValueError("production identity confirmed count is invalid")
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
    required: StrictInt = Field(ge=0, le=JS_MAX_SAFE_INTEGER)
    confirmed: StrictInt = Field(ge=0, le=JS_MAX_SAFE_INTEGER)
    records: tuple[PostconditionContractEvidence, ...] = Field(max_length=10_000)
    manifest_sha256: str = Field(pattern=_SHA256_RE)

    @model_validator(mode="after")
    def _digest(self) -> "ProductionPostconditionEvidenceManifest":
        if self.confirmed > self.required or len(self.records) != self.required:
            raise ValueError("production postcondition evidence coverage is invalid")
        if any(
            item.workflow_contract_sha256 != self.workflow_contract_sha256
            for item in self.records
        ):
            raise ValueError("production postcondition evidence is invalid")
        if sum(item.verdict == "passed" for item in self.records) != self.confirmed:
            raise ValueError("production postcondition confirmed count is invalid")
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


class ProductionTerminalEffectState(ClosedSignedModel):
    """Remote-safe state for one effect contract on a non-success terminal."""

    record_kind: Literal["terminal_effect_state"] = "terminal_effect_state"
    result_index: StrictInt = Field(ge=0, le=JS_MAX_SAFE_INTEGER)
    effect_contract_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    attempt_state: Literal[
        "not_actuated",
        "delivered",
        "actuated_api",
        "delivery_uncertain",
    ]
    observed_effect: Literal["present", "absent", "conflicting", "unknown"]
    effect_verified: StrictBool
    verification_performed: StrictBool
    verifier_identity: str | None = Field(
        default=None, pattern=r"^sha256:[a-f0-9]{64}$"
    )
    verification_tier: StrictInt | None = Field(default=None, ge=1, le=3)
    final_verdict: Literal["confirmed", "refuted", "indeterminate"] | None
    resolved_delivery_uncertainty: StrictBool
    absence_basis: Literal["not_actuated", "verifier_refuted", "none"]
    reconciliation_completed: StrictBool
    reconciliation_actions: StrictInt = Field(ge=0, le=JS_MAX_SAFE_INTEGER)

    @model_validator(mode="after")
    def _closed_terminal_effect_state(self) -> "ProductionTerminalEffectState":
        if self.verification_performed != (self.final_verdict is not None):
            raise ValueError("terminal effect verification fields are inconsistent")
        if self.verification_performed != (self.verification_tier is not None):
            raise ValueError("terminal effect verification tier is inconsistent")
        if self.verification_performed != (self.verifier_identity is not None):
            raise ValueError("terminal effect verifier identity is inconsistent")
        if self.effect_verified != (
            self.final_verdict == "confirmed" and self.observed_effect == "present"
        ):
            raise ValueError("terminal effect verified state is inconsistent")
        if self.absence_basis == "not_actuated":
            if (
                self.attempt_state != "not_actuated"
                or self.observed_effect != "absent"
                or self.verification_performed
                or self.resolved_delivery_uncertainty
            ):
                raise ValueError("terminal non-actuation evidence is invalid")
        elif self.absence_basis == "verifier_refuted":
            if (
                self.attempt_state == "not_actuated"
                or self.observed_effect != "absent"
                or self.final_verdict != "refuted"
                or not self.verification_performed
            ):
                raise ValueError("terminal verifier absence evidence is invalid")
        elif self.attempt_state == "not_actuated" or (
            self.final_verdict == "refuted" and self.observed_effect == "absent"
        ):
            raise ValueError("terminal absence evidence must name its basis")
        if self.reconciliation_completed != (self.reconciliation_actions > 0):
            raise ValueError("terminal effect reconciliation fields are inconsistent")
        return self


class ProductionEffectEvidenceManifest(ClosedSignedModel):
    schema_version: Literal["openadapt.production-effect-evidence/v1"] = (
        "openadapt.production-effect-evidence/v1"
    )
    effect_contract_sha256: str = Field(pattern=_SHA256_RE)
    workflow_contract_sha256: str = Field(pattern=_SHA256_RE)
    required: StrictInt = Field(ge=0, le=JS_MAX_SAFE_INTEGER)
    confirmed: StrictInt = Field(ge=0, le=JS_MAX_SAFE_INTEGER)
    records: tuple[ProductionEffectEvidence | ProductionTerminalEffectState, ...] = (
        Field(max_length=10_000)
    )
    manifest_sha256: str = Field(pattern=_SHA256_RE)

    @model_validator(mode="after")
    def _digest(self) -> "ProductionEffectEvidenceManifest":
        if self.confirmed > self.required or len(self.records) != self.required:
            raise ValueError("production effect evidence coverage is invalid")
        if all(isinstance(item, ProductionEffectEvidence) for item in self.records):
            if self.required != self.confirmed:
                raise ValueError("production effect success coverage is incomplete")
        elif (
            sum(
                isinstance(item, ProductionTerminalEffectState)
                and item.final_verdict == "confirmed"
                and item.observed_effect == "present"
                for item in self.records
            )
            != self.confirmed
        ):
            raise ValueError("production effect confirmed count is invalid")
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


class ProductionDeliveryResultLossClosurePayload(ClosedSignedModel):
    """Cloud authority fence for one managed child result loss."""

    schema_version: Literal[
        "openadapt.production-delivery-result-loss-closure-payload/v2"
    ] = RESULT_LOSS_CLOSURE_PAYLOAD_SCHEMA
    closure_id: str = Field(pattern=_UUID_RE)
    closure_sequence: Literal[1] = 1
    closure_request_sha256: str = Field(pattern=_SHA256_RE)
    closed_at: str
    result_loss_observed_at: str
    receipt_absence_observed_at: str | None = None
    tenant_id: str = Field(pattern=_UUID_RE)
    run_id: str = Field(pattern=_UUID_RE)
    flow_run_id_sha256: str = Field(pattern=_SHA256_RE)
    dispatch_id: str = Field(pattern=_UUID_RE)
    dispatch_session_id: str = Field(pattern=_UUID_RE)
    managed_dispatch_binding_sha256: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    idempotency_key_sha256: str = Field(pattern=_SHA256_RE)
    authenticated_runner_id_sha256: str = Field(pattern=_SHA256_RE)
    authenticated_session_id_sha256: str = Field(pattern=_SHA256_RE)
    execution_authority_id: str = Field(pattern=_UUID_RE)
    execution_authority_sha256: str = Field(pattern=_SHA256_RE)
    execution_authority_signer_sha256: str = Field(pattern=_SHA256_RE)
    child_started_at: str
    child_start_evidence_sha256: str = Field(pattern=_SHA256_RE)
    run_store_identity_sha256: str = Field(pattern=_SHA256_RE)
    permit_chain_sha256: str = Field(pattern=_SHA256_RE)
    permit_count: StrictInt = Field(ge=1, le=JS_MAX_SAFE_INTEGER)
    acknowledged_permit_count: StrictInt = Field(ge=0, le=JS_MAX_SAFE_INTEGER)
    pending_permit_count: StrictInt = Field(ge=0, le=1)
    pending_permit_artifact_sha256: str | None = Field(
        default=None,
        pattern=_SHA256_RE,
    )
    run_request_sha256: str = Field(pattern=_SHA256_RE)
    pending_action_request_sha256: str | None = Field(
        default=None,
        pattern=_SHA256_RE,
    )
    final_input_edge_sequence: StrictInt = Field(ge=1, le=JS_MAX_SAFE_INTEGER)
    final_authority_sequence: StrictInt = Field(ge=0, le=JS_MAX_SAFE_INTEGER)
    final_runtime_delivery_sequence: StrictInt = Field(ge=0, le=JS_MAX_SAFE_INTEGER)
    delivery_state: Literal["CLOSED_UNRESOLVED_RESULT_LOSS"] = (
        "CLOSED_UNRESOLVED_RESULT_LOSS"
    )
    effect_absence_claimed: Literal[False] = False
    not_received_claimed: Literal[False] = False
    blind_retry_authorized: Literal[False] = False
    actuation_replay_authorized: Literal[False] = False
    new_permit_authorized: Literal[False] = False
    delivery_acknowledgment_authorized: Literal[False] = False
    terminal_callback_required: Literal[True] = True

    @model_validator(mode="after")
    def _closed_result_loss_fence(self) -> "ProductionDeliveryResultLossClosurePayload":
        if hashlib.sha256(self.run_id.encode("utf-8")).hexdigest() != (
            self.flow_run_id_sha256
        ):
            raise ValueError("result-loss closure run identity digest is invalid")
        if self.permit_count != (
            self.acknowledged_permit_count + self.pending_permit_count
        ):
            raise ValueError("result-loss closure permit counts are inconsistent")
        has_pending = self.pending_permit_count == 1
        if has_pending != (self.pending_permit_artifact_sha256 is not None):
            raise ValueError("result-loss closure pending permit binding is incomplete")
        if has_pending != (self.pending_action_request_sha256 is not None):
            raise ValueError("result-loss closure pending action binding is incomplete")
        if has_pending != (self.receipt_absence_observed_at is not None):
            raise ValueError(
                "result-loss closure receipt-absence binding is incomplete"
            )
        child_started = _parse_utc(
            self.child_started_at,
            field="result-loss closure child_started_at",
        )
        observed = _parse_utc(
            self.result_loss_observed_at,
            field="result-loss closure result_loss_observed_at",
        )
        closed = _parse_utc(self.closed_at, field="result-loss closure closed_at")
        if not child_started <= observed <= closed:
            raise ValueError("result-loss closure chronology is invalid")
        if self.receipt_absence_observed_at is not None:
            absence = _parse_utc(
                self.receipt_absence_observed_at,
                field="result-loss closure receipt_absence_observed_at",
            )
            if absence != observed:
                raise ValueError(
                    "result-loss closure receipt absence differs from observation"
                )
        return self

    def canonical_bytes(self) -> bytes:
        return canonical_json(self)

    def payload_sha256(self) -> str:
        return hashlib.sha256(
            RESULT_LOSS_CLOSURE_PAYLOAD_DOMAIN + self.canonical_bytes()
        ).hexdigest()


class ProductionDeliveryResultLossClosureArtifact(ClosedSignedModel):
    schema_version: Literal[
        "openadapt.production-delivery-result-loss-closure-artifact/v2"
    ] = RESULT_LOSS_CLOSURE_ARTIFACT_SCHEMA
    payload: ProductionDeliveryResultLossClosurePayload
    payload_sha256: str = Field(pattern=_SHA256_RE)
    signer: DeliveryAuthoritySigner
    signature: str = Field(min_length=86, max_length=86)

    @field_validator("signature")
    @classmethod
    def _canonical_signature(cls, value: str) -> str:
        _decode_ed25519_signature(value, field="delivery result-loss closure")
        return value

    @model_validator(mode="after")
    def _verify_artifact(self) -> "ProductionDeliveryResultLossClosureArtifact":
        payload = ProductionDeliveryResultLossClosurePayload.model_validate(
            self.payload.model_dump(mode="json")
        )
        if self.payload_sha256 != payload.payload_sha256():
            raise ValueError("delivery result-loss closure payload digest is invalid")
        if self.signer.signer_sha256() != payload.execution_authority_signer_sha256:
            raise ValueError("delivery result-loss closure signer is invalid")
        _verify_authority_signature(
            self.signer,
            self.signature,
            RESULT_LOSS_CLOSURE_PAYLOAD_DOMAIN + payload.canonical_bytes(),
            field="delivery result-loss closure",
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


def sign_production_delivery_result_loss_closure(
    payload: ProductionDeliveryResultLossClosurePayload,
    private_key: Ed25519PrivateKey,
) -> ProductionDeliveryResultLossClosureArtifact:
    """Sign one monotonic result-loss closure with the delivery authority."""

    payload = ProductionDeliveryResultLossClosurePayload.model_validate(
        payload.model_dump(mode="json")
    )
    signature = private_key.sign(
        RESULT_LOSS_CLOSURE_PAYLOAD_DOMAIN + payload.canonical_bytes()
    )
    return ProductionDeliveryResultLossClosureArtifact(
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


class ProductionPendingDeliveryPermit(ClosedSignedModel):
    """One final signed permit whose delivery receipt is unresolved."""

    schema_version: Literal["openadapt.production-delivery-pending-permit/v2"] = (
        PENDING_PERMIT_SCHEMA
    )
    permit_artifact: ProductionDeliveryPermitArtifact
    permit_artifact_sha256: str = Field(pattern=_SHA256_RE)
    delivery_state: Literal["UNRESOLVED"] = "UNRESOLVED"
    delivery_receipt_artifact: Literal[None] = None
    delivery_receipt_artifact_sha256: Literal[None] = None
    receipt_absence_observed_at: str
    actuation_replay_authorized: Literal[False] = False

    @model_validator(mode="after")
    def _closed_pending_edge(self) -> "ProductionPendingDeliveryPermit":
        permit = ProductionDeliveryPermitArtifact.model_validate(
            self.permit_artifact.model_dump(mode="json")
        )
        if self.permit_artifact_sha256 != permit.artifact_sha256():
            raise ValueError("pending delivery permit artifact digest is invalid")
        issued = _parse_utc(permit.payload.issued_at, field="permit issued_at")
        absence = _parse_utc(
            self.receipt_absence_observed_at,
            field="pending receipt absence observed_at",
        )
        registry_expires = _parse_utc(
            permit.payload.qualification_signer_registry_expires_at,
            field="permit registry expires_at",
        )
        if not issued <= absence < registry_expires:
            raise ValueError("pending delivery permit chronology is invalid")
        return self

    @classmethod
    def build(
        cls,
        permit_artifact: ProductionDeliveryPermitArtifact,
        *,
        receipt_absence_observed_at: str,
    ) -> "ProductionPendingDeliveryPermit":
        return cls(
            permit_artifact=permit_artifact,
            permit_artifact_sha256=permit_artifact.artifact_sha256(),
            receipt_absence_observed_at=receipt_absence_observed_at,
        )

    @property
    def _permit(self) -> ProductionDeliveryPermitPayload:
        return self.permit_artifact.payload

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
    def issued_at(self) -> str:
        return self._permit.issued_at

    @property
    def authority_signer_sha256(self) -> str:
        return self.permit_artifact.signer.signer_sha256()


class ProductionDeliveryPermitChain(ClosedSignedModel):
    schema_version: Literal["openadapt.production-delivery-permit-chain/v2"] = (
        PERMIT_CHAIN_SCHEMA
    )
    entries: tuple[ProductionDeliveryPermit, ...] = Field(max_length=10_000)
    pending: ProductionPendingDeliveryPermit | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    permit_chain_sha256: str = Field(pattern=_SHA256_RE)

    @model_validator(mode="after")
    def _closed_chain(self) -> "ProductionDeliveryPermitChain":
        # Pydantic does not revalidate an already-created nested model by
        # default.  Reparse each retained permit so ``model_copy`` or another
        # in-memory mutation cannot enter a trusted chain with a stale digest.
        for item in self.entries:
            ProductionDeliveryPermit.model_validate(item.model_dump(mode="json"))
        if self.pending is not None:
            ProductionPendingDeliveryPermit.model_validate(
                self.pending.model_dump(mode="json")
            )
        digest_payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "entries": [item.model_dump(mode="json") for item in self.entries],
        }
        if self.pending is not None:
            digest_payload["pending"] = self.pending.model_dump(mode="json")
        expected = hashlib.sha256(
            PERMIT_CHAIN_DOMAIN + canonical_json(digest_payload)
        ).hexdigest()
        if self.permit_chain_sha256 != expected:
            raise ValueError("delivery permit chain digest is invalid")
        all_permits: tuple[
            ProductionDeliveryPermit | ProductionPendingDeliveryPermit, ...
        ] = (*self.entries, *((self.pending,) if self.pending else ()))
        if not all_permits:
            return self
        first = all_permits[0]
        authority = first.execution_authority_id
        authority_digest = first.execution_authority_sha256
        admission_digest = first.admission_artifact_sha256
        evidence_identity_digest = first.evidence_identity_sha256
        environment_digest = first.environment_digest
        registry_digest = first.qualification_signer_registry_sha256
        registry_revision = first.qualification_signer_registry_revision
        registry_expiry = first.qualification_signer_registry_expires_at
        run_request_digest = first.run_request_sha256
        run_id = first.run_id
        flow_run_id_sha256 = first.flow_run_id_sha256
        authority_signer_sha256 = first.authority_signer_sha256
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
            for item in all_permits
        ):
            raise ValueError("delivery permit chain changes its production authority")
        if self.entries:
            authenticated_runner_id_sha256 = self.entries[
                0
            ].authenticated_runner_id_sha256
            authenticated_session_id_sha256 = self.entries[
                0
            ].authenticated_session_id_sha256
            if any(
                item.authenticated_runner_id_sha256 != authenticated_runner_id_sha256
                or item.authenticated_session_id_sha256
                != authenticated_session_id_sha256
                for item in self.entries
            ):
                raise ValueError(
                    "delivery permit chain changes its authenticated runner"
                )
        input_sequences = tuple(item.input_edge_sequence for item in all_permits)
        authority_sequences = tuple(item.authority_sequence for item in all_permits)
        delivery_sequences = tuple(
            item.runtime_delivery_sequence for item in self.entries
        )
        exact = tuple(range(1, len(all_permits) + 1))
        if input_sequences != exact:
            raise ValueError(
                "delivery input-edge sequence must be exact and contiguous"
            )
        if authority_sequences != tuple(
            range(authority_sequences[0], authority_sequences[0] + len(all_permits))
        ):
            raise ValueError("delivery authority sequence must be contiguous")
        if delivery_sequences and delivery_sequences != tuple(
            range(
                delivery_sequences[0], delivery_sequences[0] + len(delivery_sequences)
            )
        ):
            raise ValueError("runtime delivery sequence must be contiguous")
        permit_ids = tuple(item.permit_id for item in all_permits)
        if len(permit_ids) != len(set(permit_ids)):
            raise ValueError("delivery permit chain repeats a permit")
        action_requests = tuple(item.action_request_sha256 for item in all_permits)
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
        if self.pending is not None and self.entries:
            final_delivered = _parse_utc(
                self.entries[-1].delivered_at,
                field="permit delivered_at",
            )
            pending_issued = _parse_utc(
                self.pending.issued_at,
                field="pending permit issued_at",
            )
            if final_delivered > pending_issued:
                raise ValueError("pending delivery permit chronology is invalid")
        return self

    @classmethod
    def build(
        cls,
        entries: tuple[ProductionDeliveryPermit, ...],
        *,
        pending: ProductionPendingDeliveryPermit | None = None,
    ) -> "ProductionDeliveryPermitChain":
        payload: dict[str, Any] = {
            "schema_version": PERMIT_CHAIN_SCHEMA,
            "entries": [item.model_dump(mode="json") for item in entries],
        }
        if pending is not None:
            payload["pending"] = pending.model_dump(mode="json")
        digest = hashlib.sha256(
            PERMIT_CHAIN_DOMAIN + canonical_json(payload)
        ).hexdigest()
        return cls(entries=entries, pending=pending, permit_chain_sha256=digest)


def verify_production_delivery_result_loss_closure_binding(
    artifact: ProductionDeliveryResultLossClosureArtifact,
    *,
    permit_chain: ProductionDeliveryPermitChain,
    result_loss: ManagedResultLossEvidence,
    tenant_id: str,
    terminal_verified_at: str,
) -> str:
    """Verify one signed authority fence against the exact terminal snapshot."""

    artifact = ProductionDeliveryResultLossClosureArtifact.model_validate(
        artifact.model_dump(mode="json")
    )
    permit_chain = ProductionDeliveryPermitChain.model_validate(
        permit_chain.model_dump(mode="json")
    )
    if not permit_chain.entries and permit_chain.pending is None:
        raise ValueError("result-loss closure requires at least one permit")
    payload = artifact.payload
    all_permits: tuple[
        ProductionDeliveryPermit | ProductionPendingDeliveryPermit, ...
    ] = (
        *permit_chain.entries,
        *((permit_chain.pending,) if permit_chain.pending is not None else ()),
    )
    first = all_permits[0]
    final = all_permits[-1]
    final_acknowledged = permit_chain.entries[-1] if permit_chain.entries else None
    pending = permit_chain.pending
    expected_pending_digest = (
        pending.permit_artifact_sha256 if pending is not None else None
    )
    expected_pending_action = pending.action_request_sha256 if pending else None
    expected_receipt_absence = (
        pending.receipt_absence_observed_at if pending is not None else None
    )
    if (
        payload.tenant_id,
        payload.run_id,
        payload.flow_run_id_sha256,
        payload.dispatch_id,
        payload.dispatch_session_id,
        payload.managed_dispatch_binding_sha256,
        payload.idempotency_key_sha256,
        payload.authenticated_runner_id_sha256,
        payload.authenticated_session_id_sha256,
        payload.execution_authority_id,
        payload.execution_authority_sha256,
        payload.execution_authority_signer_sha256,
        payload.child_started_at,
        payload.child_start_evidence_sha256,
        payload.run_store_identity_sha256,
    ) != (
        tenant_id,
        result_loss.run_id,
        result_loss.flow_run_id_sha256,
        result_loss.dispatch_id,
        result_loss.dispatch_session_id,
        result_loss.managed_dispatch_binding_sha256,
        result_loss.idempotency_key_sha256,
        result_loss.authenticated_runner_id_sha256,
        result_loss.authenticated_session_id_sha256,
        result_loss.execution_authority_id,
        result_loss.execution_authority_sha256,
        result_loss.execution_authority_signer_sha256,
        result_loss.child_started_at,
        result_loss.child_start_evidence_sha256,
        result_loss.run_store_identity_sha256,
    ):
        raise ValueError("result-loss closure identity binding is invalid")
    if permit_chain.entries and any(
        (
            item.authenticated_runner_id_sha256,
            item.authenticated_session_id_sha256,
        )
        != (
            payload.authenticated_runner_id_sha256,
            payload.authenticated_session_id_sha256,
        )
        for item in permit_chain.entries
    ):
        raise ValueError(
            "result-loss closure authenticated delivery identity is invalid"
        )
    if (
        payload.permit_chain_sha256,
        payload.permit_count,
        payload.acknowledged_permit_count,
        payload.pending_permit_count,
        payload.pending_permit_artifact_sha256,
        payload.run_request_sha256,
        payload.pending_action_request_sha256,
        payload.final_input_edge_sequence,
        payload.final_authority_sequence,
        payload.final_runtime_delivery_sequence,
        payload.receipt_absence_observed_at,
    ) != (
        permit_chain.permit_chain_sha256,
        len(all_permits),
        len(permit_chain.entries),
        1 if pending is not None else 0,
        expected_pending_digest,
        first.run_request_sha256,
        expected_pending_action,
        final.input_edge_sequence,
        final.authority_sequence,
        final_acknowledged.runtime_delivery_sequence
        if final_acknowledged is not None
        else 0,
        expected_receipt_absence,
    ):
        raise ValueError("result-loss closure delivery snapshot is invalid")
    if (
        result_loss.delivery_result_loss_closure_artifact_sha256
        != artifact.artifact_sha256()
        or result_loss.observed_at != payload.result_loss_observed_at
        or result_loss.pending_permit_artifact_sha256 != expected_pending_digest
        or result_loss.run_request_sha256 != first.run_request_sha256
        or result_loss.pending_action_request_sha256 != expected_pending_action
    ):
        raise ValueError("result-loss evidence differs from its authority closure")
    child_started = _parse_utc(
        result_loss.child_started_at,
        field="managed child started_at",
    )
    first_issued = _parse_utc(first.issued_at, field="first permit issued_at")
    observed = _parse_utc(
        result_loss.observed_at,
        field="managed result loss observed_at",
    )
    closed = _parse_utc(payload.closed_at, field="result-loss closure closed_at")
    terminal_verified = _parse_utc(
        terminal_verified_at,
        field="terminal verified_at",
    )
    registry_expires = _parse_utc(
        first.qualification_signer_registry_expires_at,
        field="result-loss closure registry expires_at",
    )
    if not child_started <= first_issued <= observed <= closed <= terminal_verified:
        raise ValueError("result-loss closure chronology is invalid")
    if not terminal_verified < registry_expires:
        raise ValueError("result-loss closure signer registry is expired")
    if any(
        _parse_utc(item.issued_at, field="permit issued_at") > closed
        or _parse_utc(item.delivered_at, field="permit delivered_at") > closed
        for item in permit_chain.entries
    ):
        raise ValueError("result-loss closure excludes an acknowledged permit")
    if (
        pending is not None
        and _parse_utc(
            pending.issued_at,
            field="pending permit issued_at",
        )
        > observed
    ):
        raise ValueError("result-loss closure pending permit chronology is invalid")
    return artifact.artifact_sha256()


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
    effect_records: list[ProductionEffectEvidence | ProductionTerminalEffectState] = []
    for result_index, result in enumerate(report.results):
        if result.skipped or result.exception_handled:
            continue
        if prepared.transaction_outcome is TransactionOutcome.VERIFIED:
            for effect_item in result.effect_evidence:
                effect_records.append(
                    ProductionEffectEvidence.model_validate(
                        {
                            "result_index": result_index,
                            "effect_contract_hash": effect_item.effect_contract_hash,
                            "verifier_identity": effect_item.verifier_identity,
                            "verification_tier": effect_item.verification_tier,
                            "final_verdict": effect_item.final_verdict,
                            "observed_effect": effect_item.observed_effect,
                            "reconciliation_completed": (
                                effect_item.reconciliation_completed
                            ),
                            "reconciliation_actions": (
                                effect_item.reconciliation_actions
                            ),
                        }
                    )
                )
            continue
        if not _is_consequential_result(result):
            continue
        evidence_by_hash: dict[str, list[EffectVerificationEvidence]] = {}
        for effect_item in result.effect_evidence:
            evidence_by_hash.setdefault(effect_item.effect_contract_hash, []).append(
                effect_item
            )
        effect_hashes = tuple(sorted(set(result.effect_contract_hashes)))
        if len(effect_hashes) != len(result.effect_contract_hashes):
            raise ProductionTerminalVerificationError(
                "terminal effect contract hashes are duplicated"
            )
        attempt_state = _attempt_state(result)
        uncertainty_resolved = bool(
            result.delivery_uncertainty is not None
            and result.delivery_uncertainty.resolved_by_contract
        )
        for effect_hash in effect_hashes:
            matches = evidence_by_hash.pop(effect_hash, [])
            if len(matches) > 1:
                raise ProductionTerminalVerificationError(
                    "terminal effect evidence is duplicated"
                )
            terminal_item: EffectVerificationEvidence | None = (
                matches[0] if matches else None
            )
            if terminal_item is None:
                observed_effect = (
                    "absent" if attempt_state == "not_actuated" else "unknown"
                )
                final_verdict = None
                verification_tier = None
                verifier_identity = None
                reconciliation_completed = False
                reconciliation_actions = 0
                absence_basis = (
                    "not_actuated" if attempt_state == "not_actuated" else "none"
                )
            else:
                observed_effect = terminal_item.observed_effect
                final_verdict = terminal_item.final_verdict
                verification_tier = terminal_item.verification_tier
                verifier_identity = terminal_item.verifier_identity
                reconciliation_completed = terminal_item.reconciliation_completed
                reconciliation_actions = terminal_item.reconciliation_actions
                absence_basis = (
                    "verifier_refuted"
                    if terminal_item.final_verdict == "refuted"
                    and terminal_item.observed_effect == "absent"
                    else "none"
                )
            effect_records.append(
                ProductionTerminalEffectState.model_validate(
                    {
                        "result_index": result_index,
                        "effect_contract_hash": effect_hash,
                        "attempt_state": attempt_state,
                        "observed_effect": observed_effect,
                        "effect_verified": bool(
                            terminal_item is not None
                            and terminal_item.final_verdict == "confirmed"
                            and terminal_item.observed_effect == "present"
                        ),
                        "verification_performed": terminal_item is not None,
                        "verifier_identity": verifier_identity,
                        "verification_tier": verification_tier,
                        "final_verdict": final_verdict,
                        "resolved_delivery_uncertainty": uncertainty_resolved,
                        "absence_basis": absence_basis,
                        "reconciliation_completed": reconciliation_completed,
                        "reconciliation_actions": reconciliation_actions,
                    }
                )
            )
        if evidence_by_hash:
            raise ProductionTerminalVerificationError(
                "terminal effect evidence lacks a declared contract"
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


class ProductionTerminalVerificationPayloadV2(ClosedSignedModel):
    """Exact Flow 1.34.0 success-only terminal payload.

    Keep this wire shape frozen. New terminal outcomes use v3. The validator
    projects the legacy success into the current verifier so both readers keep
    the same safety checks without adding fields to the signed v2 bytes.
    """

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

    @model_validator(mode="before")
    @classmethod
    def _frozen_v2_shape(cls, value: Any) -> Any:
        if isinstance(value, dict):
            permit_chain = value.get("permit_chain")
            if isinstance(permit_chain, dict) and "pending" in permit_chain:
                raise ValueError("terminal v2 permit chain has unexpected fields")
            execution_outcome = value.get("execution_outcome")
            if isinstance(execution_outcome, dict) and (
                "managed_result_loss_evidence_sha256" in execution_outcome
            ):
                raise ValueError("terminal v2 execution outcome has unexpected fields")
            run_receipt = value.get("run_receipt")
            if isinstance(run_receipt, dict) and (
                "managed_result_loss_evidence_sha256" in run_receipt
            ):
                raise ValueError("terminal v2 run receipt has unexpected fields")
            manifests = value.get("evidence_manifests")
            effect = manifests.get("effect") if isinstance(manifests, dict) else None
            records = effect.get("records") if isinstance(effect, dict) else None
            if isinstance(records, (list, tuple)):
                legacy_effect_fields = set(ProductionEffectEvidence.model_fields)
                if any(
                    isinstance(record, dict)
                    and set(record).difference(legacy_effect_fields)
                    for record in records
                ):
                    raise ValueError(
                        "terminal v2 effect evidence has unexpected fields"
                    )
        return value

    @model_validator(mode="after")
    def _frozen_v2_success(self) -> "ProductionTerminalVerificationPayloadV2":
        if self.permit_chain.pending is not None:
            raise ValueError("terminal v2 cannot carry a pending permit")
        projected = self.model_dump(mode="json")
        projected.update(
            {
                "schema_version": SCHEMA_V3,
                "acknowledged_permit_count": self.permit_count,
                "pending_permit_count": 0,
            }
        )
        try:
            ProductionTerminalVerificationPayloadV3.model_validate(projected)
        except ValueError as exc:
            raise ValueError("terminal v2 success proof is invalid") from exc
        if self.run_receipt.transaction_outcome != "VERIFIED":
            raise ValueError("terminal v2 is success-only")
        return self

    def canonical_bytes(self) -> bytes:
        return canonical_json(self)


class ProductionTerminalVerificationPayloadV3(ClosedSignedModel):
    schema_version: Literal["openadapt.production-terminal-verification/v3"] = SCHEMA_V3
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
    permit_count: StrictInt = Field(ge=0, le=JS_MAX_SAFE_INTEGER)
    acknowledged_permit_count: StrictInt = Field(ge=0, le=JS_MAX_SAFE_INTEGER)
    pending_permit_count: StrictInt = Field(ge=0, le=1)
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
    managed_result_loss: ManagedResultLossEvidence | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    delivery_result_loss_closure: ProductionDeliveryResultLossClosureArtifact | None = (
        Field(
            default=None,
            exclude_if=lambda value: value is None,
        )
    )
    verified_at: str
    issued_at: str

    @model_validator(mode="after")
    def _closed_terminal_outcome(self) -> "ProductionTerminalVerificationPayloadV3":
        if hashlib.sha256(self.run_id.encode("utf-8")).hexdigest() != (
            self.flow_run_id_sha256
        ):
            raise ValueError("terminal run identity digest is invalid")
        if self.admission_id == self.runtime_validation_id:
            raise ValueError("terminal admission and runtime identities must differ")
        if self.workflow_version_id != self.bundle_version_id:
            raise ValueError("terminal workflow and bundle versions must match")
        chain = self.permit_chain
        receipt = self.run_receipt
        outcome = self.execution_outcome
        manifests = self.evidence_manifests
        final = chain.pending or (chain.entries[-1] if chain.entries else None)
        if final is not None:
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
                raise ValueError(
                    "terminal run identity does not match its permit chain"
                )
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
                raise ValueError(
                    "terminal qualification state does not match its permits"
                )
            expected_final_delivery_sequence = (
                chain.entries[-1].runtime_delivery_sequence if chain.entries else 0
            )
            if (
                self.permit_count,
                self.acknowledged_permit_count,
                self.pending_permit_count,
                self.final_authority_sequence,
                self.final_runtime_delivery_sequence,
            ) != (
                len(chain.entries) + (1 if chain.pending is not None else 0),
                len(chain.entries),
                1 if chain.pending is not None else 0,
                final.authority_sequence,
                expected_final_delivery_sequence,
            ):
                raise ValueError("terminal permit counts do not match the permit chain")
        elif (
            receipt.transaction_outcome != "HALTED_BEFORE_EFFECT"
            or self.permit_count != 0
            or self.acknowledged_permit_count != 0
            or self.pending_permit_count != 0
            or self.final_authority_sequence != 0
            or self.final_runtime_delivery_sequence != 0
        ):
            raise ValueError(
                "only a HALTED_BEFORE_EFFECT terminal may use an empty permit chain"
            )
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
        transaction_outcome = receipt.transaction_outcome
        result_loss = self.managed_result_loss
        result_loss_closure = self.delivery_result_loss_closure
        if (result_loss is not None) != (
            receipt.managed_result_loss_evidence_sha256 is not None
        ):
            raise ValueError("managed result loss receipt binding is incomplete")
        if (result_loss is not None) != (result_loss_closure is not None):
            raise ValueError("managed result loss authority closure is incomplete")
        if result_loss is not None:
            pending = chain.pending
            if (
                transaction_outcome != "RECONCILIATION_REQUIRED"
                or self.permit_count < 1
                or result_loss.evidence_sha256
                != receipt.managed_result_loss_evidence_sha256
                or result_loss.evidence_sha256
                != outcome.managed_result_loss_evidence_sha256
                or result_loss.run_id != self.run_id
                or result_loss.flow_run_id_sha256 != self.flow_run_id_sha256
                or result_loss.execution_authority_id != self.execution_authority_id
                or result_loss.execution_authority_sha256
                != self.execution_authority_sha256
                or result_loss.execution_authority_signer_sha256
                != self.execution_authority_signer_sha256
                or result_loss.pending_permit_artifact_sha256
                != (pending.permit_artifact_sha256 if pending is not None else None)
                or result_loss.run_request_sha256
                != (chain.pending or chain.entries[0]).run_request_sha256
                or result_loss.pending_action_request_sha256
                != (pending.action_request_sha256 if pending is not None else None)
                or outcome.external_network_calls != "observed"
                or receipt.external_network_calls != "observed"
                or manifests.identity.required != 0
                or manifests.identity.confirmed != 0
                or manifests.identity.results
                or manifests.postcondition.required != 0
                or manifests.postcondition.confirmed != 0
                or manifests.postcondition.records
                or manifests.effect.required != 0
                or manifests.effect.confirmed != 0
                or manifests.effect.records
            ):
                raise ValueError("managed result loss terminal binding is invalid")
            assert result_loss_closure is not None
            verify_production_delivery_result_loss_closure_binding(
                result_loss_closure,
                permit_chain=chain,
                result_loss=result_loss,
                tenant_id=self.tenant_id,
                terminal_verified_at=self.verified_at,
            )
        if transaction_outcome == "VERIFIED":
            if (
                outcome.outcome != "VERIFIED"
                or not outcome.production_eligible
                or not outcome.execution_completed
                or self.acknowledged_permit_count < 1
                or self.pending_permit_count != 0
            ):
                raise ValueError("terminal VERIFIED proof is incomplete")
        elif transaction_outcome == "HALTED_BEFORE_EFFECT":
            if (
                outcome.outcome != "HALTED"
                or outcome.production_eligible
                or outcome.execution_completed
                or receipt.production_eligible
                or self.permit_count != 0
                or any(
                    not isinstance(item, ProductionTerminalEffectState)
                    or item.absence_basis not in {"not_actuated", "verifier_refuted"}
                    for item in manifests.effect.records
                )
            ):
                raise ValueError("terminal HALTED proof lacks exact effect absence")
        elif transaction_outcome == "RECONCILIATION_REQUIRED":
            if (
                outcome.outcome == "VERIFIED"
                or outcome.production_eligible
                or receipt.production_eligible
                or self.permit_count < 1
            ):
                raise ValueError("terminal reconciliation proof is invalid")
            if result_loss is not None:
                pass
            elif chain.pending is not None:
                uncertain_records = tuple(
                    item
                    for item in manifests.effect.records
                    if isinstance(item, ProductionTerminalEffectState)
                    and item.attempt_state == "delivery_uncertain"
                )
                if not uncertain_records or any(
                    item.observed_effect not in {"conflicting", "unknown"}
                    or item.resolved_delivery_uncertainty
                    or item.absence_basis != "none"
                    for item in uncertain_records
                ):
                    raise ValueError(
                        "terminal pending delivery lacks unresolved effect evidence"
                    )
            elif not any(
                isinstance(item, ProductionTerminalEffectState)
                and (
                    item.final_verdict == "indeterminate"
                    or item.observed_effect in {"conflicting", "unknown"}
                    or (
                        item.attempt_state == "delivery_uncertain"
                        and not item.resolved_delivery_uncertainty
                    )
                )
                for item in manifests.effect.records
            ):
                raise ValueError(
                    "terminal reconciliation proof lacks inconclusive effect evidence"
                )
        else:
            raise ValueError("terminal reconciliation proof is invalid")
        if transaction_outcome != "VERIFIED" and (
            receipt.source_receipt_digest != self.run_report_sha256
            or receipt.source_receipt_sha256 != self.run_report_sha256
        ):
            raise ValueError("terminal non-success receipt does not bind its report")
        verified = _parse_utc(self.verified_at, field="terminal verified_at")
        issued = _parse_utc(self.issued_at, field="terminal issued_at")
        if not verified <= issued <= verified + timedelta(minutes=5):
            raise ValueError("terminal proof issue time is invalid")
        if chain.pending is not None:
            pending_issued = _parse_utc(
                chain.pending.issued_at,
                field="pending permit issued_at",
            )
            absence_observed = _parse_utc(
                chain.pending.receipt_absence_observed_at,
                field="pending receipt absence observed_at",
            )
            registry_expires = _parse_utc(
                chain.pending.qualification_signer_registry_expires_at,
                field="pending permit registry expires_at",
            )
            if not pending_issued <= absence_observed <= verified < registry_expires:
                raise ValueError(
                    "terminal verification is outside pending permit chronology"
                )
        elif chain.entries:
            final = chain.entries[-1]
            final_delivered = _parse_utc(
                final.delivered_at, field="permit delivered_at"
            )
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


# Keep the established Python API name on the current wire type.
ProductionTerminalVerificationPayload = ProductionTerminalVerificationPayloadV3


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


class ProductionTerminalVerificationEnvelopeV2(ClosedSignedModel):
    """Exact Flow 1.34.0 envelope for a success-only v2 payload."""

    payload: ProductionTerminalVerificationPayloadV2
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
    def _signer_matches_payload(self) -> "ProductionTerminalVerificationEnvelopeV2":
        public_key = b64decode(self.signer.public_key, validate=True)
        if (
            evidence_runner_signer_sha256(public_key)
            != self.payload.evidence_runner_signer_sha256
        ):
            raise ValueError("terminal signer does not match the admitted runner")
        return self

    def artifact_sha256(self) -> str:
        return _sha256(self)


class ProductionTerminalVerificationEnvelopeV3(ClosedSignedModel):
    """Current v3 envelope for all signed terminal outcomes."""

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


# Keep the established Python API name on the current wire type.
ProductionTerminalVerificationEnvelope = ProductionTerminalVerificationEnvelopeV3


def sign_production_terminal_verification_v2(
    payload: ProductionTerminalVerificationPayloadV2,
    private_key: Ed25519PrivateKey,
) -> ProductionTerminalVerificationEnvelopeV2:
    """Sign one frozen Flow 1.34.0 success payload without changing its bytes."""

    payload = ProductionTerminalVerificationPayloadV2.model_validate(
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
    return ProductionTerminalVerificationEnvelopeV2(
        payload=payload,
        signer=signer,
        signature=urlsafe_b64encode(signature).decode("ascii").rstrip("="),
    )


def verify_production_terminal_verification_v2_signature(
    envelope: ProductionTerminalVerificationEnvelopeV2,
) -> str:
    """Verify the frozen v2 signature for dual-read compatibility."""

    envelope = ProductionTerminalVerificationEnvelopeV2.model_validate(
        envelope.model_dump(mode="json")
    )
    try:
        Ed25519PublicKey.from_public_bytes(
            b64decode(envelope.signer.public_key, validate=True)
        ).verify(
            urlsafe_b64decode(envelope.signature + "=="),
            SIGNATURE_DOMAIN + envelope.payload.canonical_bytes(),
        )
    except (InvalidSignature, ValueError) as exc:
        raise ProductionTerminalVerificationError(
            "production terminal v2 signature is invalid"
        ) from exc
    return envelope.artifact_sha256()


def verify_production_terminal_verification_v3_signature(
    envelope: ProductionTerminalVerificationEnvelopeV3,
) -> str:
    """Verify the current v3 signature without claiming independent state."""

    envelope = ProductionTerminalVerificationEnvelopeV3.model_validate(
        envelope.model_dump(mode="json")
    )
    try:
        Ed25519PublicKey.from_public_bytes(
            b64decode(envelope.signer.public_key, validate=True)
        ).verify(
            urlsafe_b64decode(envelope.signature + "=="),
            SIGNATURE_DOMAIN_V3 + envelope.payload.canonical_bytes(),
        )
    except (InvalidSignature, ValueError) as exc:
        raise ProductionTerminalVerificationError(
            "production terminal v3 signature is invalid"
        ) from exc
    return envelope.artifact_sha256()


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
    permit_count: StrictInt = Field(ge=0, le=JS_MAX_SAFE_INTEGER)
    acknowledged_permit_count: StrictInt = Field(ge=0, le=JS_MAX_SAFE_INTEGER)
    pending_permit_count: StrictInt = Field(ge=0, le=1)
    pending_permit_artifact_sha256: str | None = Field(
        default=None,
        pattern=_SHA256_RE,
    )
    final_authority_sequence: StrictInt = Field(ge=0, le=JS_MAX_SAFE_INTEGER)
    final_runtime_delivery_sequence: StrictInt = Field(ge=0, le=JS_MAX_SAFE_INTEGER)
    authenticated_runner_id_sha256: str = Field(pattern=_SHA256_RE)
    authenticated_session_id_sha256: str = Field(pattern=_SHA256_RE)
    acknowledged_one_use_claim_ids: tuple[str, ...] = Field(max_length=10_000)
    workflow_contract_sha256: str = Field(pattern=_SHA256_RE)
    execution_outcome_sha256: str = Field(pattern=_SHA256_RE)
    run_receipt_sha256: str = Field(pattern=_SHA256_RE)
    run_report_sha256: str = Field(pattern=_SHA256_RE)
    run_report_object_version: str = Field(pattern=_ID_RE)
    run_report_object_sha256: str = Field(pattern=_SHA256_RE)
    evidence_manifests: ProductionEvidenceManifests
    managed_result_loss: ManagedResultLossEvidence | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    delivery_result_loss_closure: ProductionDeliveryResultLossClosureArtifact | None = (
        Field(
            default=None,
            exclude_if=lambda value: value is None,
        )
    )

    @model_validator(mode="after")
    def _closed_delivery_state(self) -> "ProductionTerminalVerificationExpected":
        if self.permit_count != (
            self.acknowledged_permit_count + self.pending_permit_count
        ):
            raise ValueError("live delivery permit counts are inconsistent")
        if len(self.acknowledged_one_use_claim_ids) != self.acknowledged_permit_count:
            raise ValueError("live delivery claim count does not match permit count")
        if (
            len(set(self.acknowledged_one_use_claim_ids))
            != self.acknowledged_permit_count
        ):
            raise ValueError("live delivery claims are not globally one-use")
        if (self.pending_permit_artifact_sha256 is not None) != (
            self.pending_permit_count == 1
        ):
            raise ValueError("live pending delivery binding is inconsistent")
        if (self.managed_result_loss is not None) != (
            self.delivery_result_loss_closure is not None
        ):
            raise ValueError("live managed result loss closure is incomplete")
        if self.managed_result_loss is not None and self.permit_count < 1:
            raise ValueError("live managed result loss lacks a retained permit")
        for claim_id in self.acknowledged_one_use_claim_ids:
            if _UUID_RE.fullmatch(claim_id) is None:
                raise ValueError("live delivery claim id is invalid")
        return self


class ProductionTerminalVerificationContext(ClosedSignedModel):
    """Independent exact bindings needed to produce one terminal v3 proof."""

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
    delivery_result_loss_closure: ProductionDeliveryResultLossClosureArtifact | None = (
        Field(
            default=None,
            exclude_if=lambda value: value is None,
        )
    )
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
        "pending_permit_artifact_sha256",
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
    signature = private_key.sign(SIGNATURE_DOMAIN_V3 + payload.canonical_bytes())
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
    final = context.permit_chain.entries[-1] if context.permit_chain.entries else None
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
        permit_count=(
            len(context.permit_chain.entries)
            + (1 if context.permit_chain.pending is not None else 0)
        ),
        acknowledged_permit_count=len(context.permit_chain.entries),
        pending_permit_count=(1 if context.permit_chain.pending is not None else 0),
        final_authority_sequence=(
            context.permit_chain.pending.authority_sequence
            if context.permit_chain.pending is not None
            else final.authority_sequence
            if final is not None
            else 0
        ),
        final_runtime_delivery_sequence=(
            final.runtime_delivery_sequence if final is not None else 0
        ),
        workflow_contract_sha256=(prepared.execution_outcome.workflow_contract_sha256),
        execution_outcome=prepared.execution_outcome,
        execution_outcome_sha256=prepared.execution_outcome.artifact_sha256(),
        run_receipt=prepared.run_receipt,
        run_receipt_sha256=prepared.run_receipt_sha256,
        run_report_sha256=prepared.report_sha256,
        run_report_object_version=context.run_report_object_version,
        run_report_object_sha256=prepared.report_sha256,
        evidence_manifests=manifests,
        managed_result_loss=prepared.report.managed_result_loss,
        delivery_result_loss_closure=context.delivery_result_loss_closure,
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
            SIGNATURE_DOMAIN_V3 + payload.canonical_bytes(),
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
    pending_permit_artifact_sha256 = (
        chain.pending.permit_artifact_sha256 if chain.pending is not None else None
    )
    if pending_permit_artifact_sha256 != expected.pending_permit_artifact_sha256:
        raise ProductionTerminalVerificationError(
            "production terminal pending permit does not match live state"
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
    *,
    pending: ProductionPendingDeliveryPermit | None = None,
) -> ProductionDeliveryPermitChain:
    """Rebuild the permit chain from independently retained permit records.

    The acceptor must not trust either artifact digest or the chain digest
    carried inside the proof. It loads each exact retained permit and receipt
    envelope, verifies both authority signatures, recomputes their digests,
    rebuilds the chain, and passes the rebuilt digest as the expected value.
    """

    if not permits and pending is None:
        raise ProductionTerminalVerificationError(
            "retained production delivery artifact count is invalid"
        )
    if len(permits) + (1 if pending is not None else 0) > 10_000:
        raise ProductionTerminalVerificationError(
            "retained production delivery artifact count is invalid"
        )
    return ProductionDeliveryPermitChain.build(permits, pending=pending)


def rebuild_production_delivery_permit_chain_from_artifacts(
    artifacts: tuple[tuple[bytes, bytes], ...],
    *,
    pending_permit_artifact: bytes | None = None,
    receipt_absence_observed_at: str | None = None,
) -> ProductionDeliveryPermitChain:
    """Rebuild a chain from exact retained permit and receipt envelope bytes."""

    has_pending = pending_permit_artifact is not None
    if has_pending != (receipt_absence_observed_at is not None):
        raise ProductionTerminalVerificationError(
            "retained pending delivery binding is incomplete"
        )
    if (not artifacts and not has_pending) or len(artifacts) + (
        1 if has_pending else 0
    ) > 10_000:
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
    pending: ProductionPendingDeliveryPermit | None = None
    if pending_permit_artifact is not None:
        if (
            len(pending_permit_artifact) == 0
            or len(pending_permit_artifact) > MAX_DELIVERY_ARTIFACT_BYTES
        ):
            raise ProductionTerminalVerificationError(
                "retained pending delivery artifact size is invalid"
            )
        try:
            permit = ProductionDeliveryPermitArtifact.model_validate_json(
                pending_permit_artifact
            )
        except ValueError as exc:
            raise ProductionTerminalVerificationError(
                "retained pending delivery artifact is invalid"
            ) from exc
        if permit.canonical_bytes() != pending_permit_artifact:
            raise ProductionTerminalVerificationError(
                "retained pending delivery artifact is not canonical"
            )
        assert receipt_absence_observed_at is not None
        pending = ProductionPendingDeliveryPermit.build(
            permit,
            receipt_absence_observed_at=receipt_absence_observed_at,
        )
    return ProductionDeliveryPermitChain.build(tuple(entries), pending=pending)


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
