"""Build and validate private qualification campaign evidence.

The schemas in this module describe the open mechanism.  Campaign payloads, failure
corpora, system-specific recipes, and empirical tuning remain inside the approved
private evidence boundary.  A public lifecycle consumer must use only the separate
remote-safe decision receipt and public qualification admission.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence, TypedDict

CAMPAIGN_SCHEMA = "openadapt.qualification-campaign/v3"
TRIAL_SCHEMA = "openadapt.qualification-trial-row/v3"
TRIAL_RECEIPT_SCHEMA = "openadapt.qualification-trial-receipt/v3"
CAMPAIGN_SUMMARY_SCHEMA = "openadapt.qualification-campaign-summary/v3"
SOURCE_CAMPAIGN_PROJECTION_SCHEMA = "openadapt.qualification-source-campaign-projection/v1"
TRIAL_RECEIPT_SIGNATURE_DOMAIN = b"OpenAdapt qualification trial receipt v3\0"
TRIAL_EVIDENCE_IDENTITY_DOMAIN = b"OpenAdapt qualification trial evidence projection v3\0"
QUALIFICATION_CONTRACT_IDENTITY_DOMAIN = b"OpenAdapt qualification qualification contract v1\0"
ORACLE_CONTRACT_IDENTITY_DOMAIN = b"OpenAdapt qualification oracle contract v1\0"
AUTHORITY_CONTRACT_IDENTITY_DOMAIN = b"OpenAdapt qualification authority contract v1\0"

QUALIFICATION_CLASSES = frozenset(
    {
        "healthy",
        "safe_halt",
        "idempotency_replay",
        "uncertain_delivery",
        "declared_attended",
        "governed_repair",
    }
)
EXPECTED_OUTCOME_BY_CLASS: Mapping[str, str] = {
    "healthy": "VERIFIED",
    "safe_halt": "HALTED",
    "idempotency_replay": "VERIFIED",
    "uncertain_delivery": "RECONCILIATION_REQUIRED",
    "declared_attended": "VERIFIED",
    "governed_repair": "VERIFIED",
}
DELIVERY_TUPLE_BY_CLASS: Mapping[str, tuple[str, str, str]] = {
    "declared_attended": ("dispatched", "delivered", "single_effect_verified"),
    "governed_repair": ("dispatched", "delivered", "single_effect_verified"),
    "healthy": ("dispatched", "delivered", "single_effect_verified"),
    "idempotency_replay": ("dispatched", "delivered", "duplicate_suppressed"),
    "safe_halt": ("not_dispatched", "not_delivered", "not_applicable"),
    "uncertain_delivery": ("dispatched", "uncertain", "unverifiable"),
}
TERMINAL_OUTCOMES = frozenset({"VERIFIED", "HALTED", "RECONCILIATION_REQUIRED", "PLATFORM_FAILURE"})
RECEIPT_TYPES = frozenset(
    {
        "runner",
        "observer",
        "delivery",
        "decision",
        "policy",
        "repair",
        "cleanup",
        "cleanup_absence",
    }
)

_CAMPAIGN_KEYS = frozenset(
    {
        "schema_version",
        "campaign_id",
        "campaign_permit_sha256",
        "project_contract_sha256",
        "bundle_artifact_sha256",
        "runtime_identity_sha256",
        "evidence_identity_sha256",
        "qualification_contract",
        "qualification_contract_sha256",
        "oracle_contract",
        "oracle_contract_sha256",
        "authority_contract",
        "authority_contract_sha256",
        "conditions",
        "invariants",
        "excluded_trials",
        "receipt_envelopes",
        "generated_at",
    }
)
_CONDITION_KEYS = frozenset(
    {
        "task",
        "condition",
        "qualification_class",
        "expected_terminal_outcome",
        "required_trials",
        "trials",
    }
)
_TRIAL_KEYS = frozenset(
    {
        "schema_version",
        "task",
        "condition",
        "qualification_class",
        "trial_index",
        "attempt_id_sha256",
        "run_id_sha256",
        "campaign_permit_sha256",
        "bundle_artifact_sha256",
        "runtime_identity_sha256",
        "evidence_identity_sha256",
        "started_at",
        "completed_at",
        "observed_terminal_outcome",
        "runner_receipt_sha256",
        "observer_receipt_sha256",
        "delivery_receipt_sha256",
        "policy_receipt_sha256",
        "decision_receipt_sha256",
        "repair_receipt_sha256",
        "cleanup_receipt_sha256",
        "cleanup_absence_proof_sha256",
    }
)
_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "receipt_type",
        "issuer",
        "issuer_key_id",
        "algorithm",
        "source_artifact_sha256",
        "verified_projection",
        "verified_at",
        "signature",
    }
)
_RECEIPT_UNSIGNED_KEYS = _RECEIPT_KEYS - {"signature"}
_RECEIPT_ISSUER_KEYS = frozenset(
    {
        "repository",
        "repository_id",
        "repository_owner_id",
        "workflow",
        "ref",
        "source_commit",
        "environment",
        "run_id",
        "run_attempt",
        "runner_identity_sha256",
    }
)
_RECEIPT_AUTHORITY_KEYS = _RECEIPT_ISSUER_KEYS - {"run_id", "run_attempt"}
_PROJECTION_KEYS = frozenset(
    {
        "campaign_id",
        "task",
        "condition",
        "qualification_class",
        "trial_index",
        "attempt_id_sha256",
        "run_id_sha256",
        "campaign_permit_sha256",
        "bundle_artifact_sha256",
        "runtime_identity_sha256",
        "evidence_identity_sha256",
        "verdict",
        "evidence_sha256",
        "facts",
    }
)
_ROW_RECEIPT_FIELDS: Mapping[str, str] = {
    "runner": "runner_receipt_sha256",
    "observer": "observer_receipt_sha256",
    "delivery": "delivery_receipt_sha256",
    "decision": "decision_receipt_sha256",
    "policy": "policy_receipt_sha256",
    "repair": "repair_receipt_sha256",
    "cleanup": "cleanup_receipt_sha256",
    "cleanup_absence": "cleanup_absence_proof_sha256",
}
_COMMON_RECEIPTS = frozenset({"runner", "observer", "delivery", "cleanup", "cleanup_absence"})
_FACT_KEYS: Mapping[str, frozenset[str]] = {
    "runner": frozenset(
        {
            "observed_terminal_outcome",
            "model_call_count",
            "unplanned_intervention_count",
            "unsafe_effect_count",
            "unverified_direct_action_count",
        }
    ),
    "observer": frozenset(
        {
            "independent_verdict",
            "intended_effect_count",
            "wrong_effect_count",
            "wrong_record_count",
            "duplicate_effect_count",
            "collateral_effect_count",
        }
    ),
    "delivery": frozenset(
        {
            "dispatch_state",
            "blind_retry_count",
            "replay_dispatch_count",
            "idempotency_result",
            "delivery_certainty",
        }
    ),
    "decision": frozenset({"authenticated_typed_bound_decision", "live_target_revalidated"}),
    "policy": frozenset({"policy_approved_model_path"}),
    "repair": frozenset(
        {
            "human_approval_verified",
            "retained_evidence_verified",
            "target_revalidated",
        }
    ),
    "cleanup": frozenset({"cleanup_completed"}),
    "cleanup_absence": frozenset({"absence_verified"}),
}
_COUNT_FACTS: Mapping[str, frozenset[str]] = {
    "runner": frozenset(
        {
            "model_call_count",
            "unplanned_intervention_count",
            "unsafe_effect_count",
            "unverified_direct_action_count",
        }
    ),
    "observer": frozenset(
        {
            "intended_effect_count",
            "wrong_effect_count",
            "wrong_record_count",
            "duplicate_effect_count",
            "collateral_effect_count",
        }
    ),
    "delivery": frozenset({"blind_retry_count", "replay_dispatch_count"}),
    "repair": frozenset(),
}
_BOOL_FACTS: Mapping[str, frozenset[str]] = {
    "decision": frozenset({"authenticated_typed_bound_decision", "live_target_revalidated"}),
    "policy": frozenset({"policy_approved_model_path"}),
    "repair": frozenset(
        {
            "human_approval_verified",
            "retained_evidence_verified",
            "target_revalidated",
        }
    ),
    "cleanup": frozenset({"cleanup_completed"}),
    "cleanup_absence": frozenset({"absence_verified"}),
}
_DISPATCH_STATES = frozenset({"dispatched", "not_dispatched"})
_DELIVERY_CERTAINTY = frozenset({"delivered", "not_delivered", "uncertain"})
_IDEMPOTENCY_RESULTS = frozenset(
    {
        "not_applicable",
        "single_effect_verified",
        "duplicate_suppressed",
        "violated",
        "unverifiable",
    }
)
_OBSERVER_VERDICTS = frozenset({"PROVED", "REFUTED", "UNVERIFIABLE"})
CELL_COUNTERS = (
    "approved_repair_count",
    "authenticated_bound_decision_count",
    "blind_retry_count",
    "collateral_effect_count",
    "cleanup_absence_verified_count",
    "cleanup_verified_count",
    "duplicate_effect_count",
    "duplicate_suppressed_count",
    "dispatch_count",
    "intended_effect_count",
    "live_target_revalidation_count",
    "model_call_count",
    "over_halt_count",
    "policy_approved_repair_count",
    "replay_dispatch_count",
    "retained_repair_evidence_count",
    "silent_incorrect_success_count",
    "unplanned_intervention_count",
    "unsafe_effect_count",
    "uncertain_delivery_evidence_count",
    "unverified_direct_action_count",
    "wrong_effect_count",
    "wrong_record_count",
)
_SHA256 = re.compile(r"^sha256:[a-f0-9]{64}$")
_WHOLE_SECOND_UTC = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")


class TrialReceiptIssuer(TypedDict):
    """The protected run identity bound into one trial receipt."""

    repository: str
    repository_id: str
    repository_owner_id: str
    workflow: str
    ref: str
    source_commit: str
    environment: str
    run_id: str
    run_attempt: int
    runner_identity_sha256: str


class TrialReceiptAuthority(TypedDict):
    """The permit-bound static identity for one trial receipt authority."""

    repository: str
    repository_id: str
    repository_owner_id: str
    workflow: str
    ref: str
    source_commit: str
    environment: str
    runner_identity_sha256: str


ReceiptSigner = Callable[[bytes], bytes]
ReceiptAuthorityResolver = Callable[[str, str], TrialReceiptAuthority]
ReceiptSignatureVerifier = Callable[[str, TrialReceiptIssuer, bytes, bytes], bool]


class QualificationEvidenceError(ValueError):
    """A campaign or signed receipt violates the qualification contract."""


def canonical_json_bytes(value: Any) -> bytes:
    """Return deterministic compact JSON bytes for signatures and commitments."""

    _validate_json_value(value, "object")
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_digest(payload: bytes) -> str:
    """Return a lowercase, algorithm-prefixed SHA-256 digest."""

    return "sha256:" + hashlib.sha256(payload).hexdigest()


def identity_sha256(domain: bytes, value: Mapping[str, Any]) -> str:
    """Commit to a canonical object under an explicit identity domain."""

    return sha256_digest(domain + canonical_json_bytes(dict(value)))


def receipt_sha256(receipt: Mapping[str, Any]) -> str:
    """Commit to the complete signed receipt envelope."""

    return sha256_digest(canonical_json_bytes(dict(receipt)))


def build_signed_trial_receipt(
    *,
    receipt_type: str,
    issuer: TrialReceiptIssuer,
    issuer_key_id: str,
    source_artifact_sha256: str,
    verified_projection: Mapping[str, Any],
    verified_at: str,
    signer: ReceiptSigner,
) -> dict[str, Any]:
    """Build an Ed25519 receipt without binding this package to a key store."""

    if receipt_type not in RECEIPT_TYPES:
        raise QualificationEvidenceError(f"receipt_type is invalid: {receipt_type!r}")
    if not isinstance(issuer_key_id, str) or not issuer_key_id:
        raise QualificationEvidenceError("issuer_key_id must be a non-empty string")
    _require_digest(source_artifact_sha256, "source_artifact_sha256")
    _validate_timestamp(verified_at, "verified_at")
    issuer_value = dict(issuer)
    _validate_receipt_issuer(issuer_value, "issuer")
    projection = dict(verified_projection)
    if "evidence_sha256" in projection:
        raise QualificationEvidenceError(
            "verified_projection.evidence_sha256 is derived by the receipt builder"
        )
    projection["evidence_sha256"] = _trial_evidence_sha256(
        receipt_type,
        source_artifact_sha256,
        projection,
    )
    _validate_projection(projection, receipt_type)
    unsigned = {
        "schema_version": TRIAL_RECEIPT_SCHEMA,
        "receipt_type": receipt_type,
        "issuer": issuer_value,
        "issuer_key_id": issuer_key_id,
        "algorithm": "ed25519",
        "source_artifact_sha256": source_artifact_sha256,
        "verified_projection": projection,
        "verified_at": verified_at,
    }
    signature = signer(TRIAL_RECEIPT_SIGNATURE_DOMAIN + canonical_json_bytes(unsigned))
    if not isinstance(signature, bytes) or len(signature) != 64:
        raise QualificationEvidenceError(
            "the Ed25519 receipt signer must return a 64-byte signature"
        )
    return {**unsigned, "signature": base64.b64encode(signature).decode("ascii")}


def build_qualification_campaign(
    *,
    campaign_id: str,
    campaign_permit_sha256: str,
    project_contract_sha256: str,
    bundle_artifact_sha256: str,
    runtime_identity_sha256: str,
    evidence_identity_sha256: str,
    qualification_contract: Mapping[str, Any],
    oracle_contract: Mapping[str, Any],
    authority_contract: Mapping[str, Any],
    conditions: Sequence[Mapping[str, Any]],
    invariants: Sequence[Mapping[str, Any]],
    excluded_trials: Sequence[Mapping[str, Any]],
    receipt_envelopes: Sequence[Mapping[str, Any]],
    generated_at: str,
    verify_receipt_signature: ReceiptSignatureVerifier,
    resolve_receipt_authority: ReceiptAuthorityResolver,
    require_admissible: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build a canonical v3 campaign and return its derived summary."""

    ordered_conditions = sorted(
        (dict(condition) for condition in conditions),
        key=lambda item: (
            str(item.get("task", "")),
            str(item.get("condition", "")),
            str(item.get("qualification_class", "")),
        ),
    )
    ordered_receipts = sorted((dict(receipt) for receipt in receipt_envelopes), key=receipt_sha256)
    campaign = {
        "schema_version": CAMPAIGN_SCHEMA,
        "campaign_id": campaign_id,
        "campaign_permit_sha256": campaign_permit_sha256,
        "project_contract_sha256": project_contract_sha256,
        "bundle_artifact_sha256": bundle_artifact_sha256,
        "runtime_identity_sha256": runtime_identity_sha256,
        "evidence_identity_sha256": evidence_identity_sha256,
        "qualification_contract": dict(qualification_contract),
        "qualification_contract_sha256": identity_sha256(
            QUALIFICATION_CONTRACT_IDENTITY_DOMAIN, qualification_contract
        ),
        "oracle_contract": dict(oracle_contract),
        "oracle_contract_sha256": identity_sha256(ORACLE_CONTRACT_IDENTITY_DOMAIN, oracle_contract),
        "authority_contract": dict(authority_contract),
        "authority_contract_sha256": identity_sha256(
            AUTHORITY_CONTRACT_IDENTITY_DOMAIN, authority_contract
        ),
        "conditions": ordered_conditions,
        "invariants": [dict(invariant) for invariant in invariants],
        "excluded_trials": [dict(trial) for trial in excluded_trials],
        "receipt_envelopes": ordered_receipts,
        "generated_at": generated_at,
    }
    summary = validate_qualification_campaign(
        campaign,
        verify_receipt_signature=verify_receipt_signature,
        resolve_receipt_authority=resolve_receipt_authority,
        require_admissible=require_admissible,
    )
    return campaign, summary


def build_source_campaign_projection(campaign: Mapping[str, Any]) -> dict[str, Any]:
    """Wrap a validated campaign for the private evidence decision boundary."""

    return {
        "schema_version": SOURCE_CAMPAIGN_PROJECTION_SCHEMA,
        "campaign": dict(campaign),
    }


def validate_qualification_campaign(
    campaign: Mapping[str, Any],
    *,
    verify_receipt_signature: ReceiptSignatureVerifier,
    resolve_receipt_authority: ReceiptAuthorityResolver,
    require_admissible: bool = True,
) -> dict[str, Any]:
    """Verify all rows and receipts, derive counts, and enforce launch admission."""

    value = dict(campaign)
    _exact_keys(value, _CAMPAIGN_KEYS, "campaign")
    if value["schema_version"] != CAMPAIGN_SCHEMA:
        raise QualificationEvidenceError(f"campaign schema must be {CAMPAIGN_SCHEMA!r}")
    campaign_id = _require_nonempty(value["campaign_id"], "campaign_id")
    for field in (
        "campaign_permit_sha256",
        "project_contract_sha256",
        "bundle_artifact_sha256",
        "runtime_identity_sha256",
        "evidence_identity_sha256",
        "qualification_contract_sha256",
        "oracle_contract_sha256",
        "authority_contract_sha256",
    ):
        _require_digest(value[field], field)
    contract_identities = (
        (
            "qualification_contract",
            "qualification_contract_sha256",
            QUALIFICATION_CONTRACT_IDENTITY_DOMAIN,
        ),
        ("oracle_contract", "oracle_contract_sha256", ORACLE_CONTRACT_IDENTITY_DOMAIN),
        (
            "authority_contract",
            "authority_contract_sha256",
            AUTHORITY_CONTRACT_IDENTITY_DOMAIN,
        ),
    )
    for contract_field, digest_field, domain in contract_identities:
        contract = _require_mapping(value[contract_field], contract_field)
        if identity_sha256(domain, contract) != value[digest_field]:
            raise QualificationEvidenceError(f"{digest_field} does not bind {contract_field}")
    observed_invariants = _require_sequence(value["invariants"], "invariants")
    excluded_trials = _require_sequence(value["excluded_trials"], "excluded_trials")
    if excluded_trials:
        raise QualificationEvidenceError("an admissible campaign must not hide excluded trials")
    campaign_generated_at = _validate_timestamp(value["generated_at"], "generated_at")

    receipts = _receipt_index(
        value["receipt_envelopes"],
        verify_receipt_signature=verify_receipt_signature,
        resolve_receipt_authority=resolve_receipt_authority,
    )
    conditions = _require_sequence(value["conditions"], "conditions")
    if not conditions:
        raise QualificationEvidenceError("campaign must contain conditions")

    failures: list[str] = []
    classes_by_task: dict[str, set[str]] = defaultdict(set)
    condition_ids: set[tuple[str, str, str]] = set()
    required_trial_count = 0
    observed_trial_count = 0
    minimum_trials_per_condition: int | None = None
    class_counts: dict[str, Counter[str]] = {
        name: Counter() for name in sorted(QUALIFICATION_CLASSES)
    }
    cells: list[dict[str, Any]] = []
    reliability = Counter(
        {
            "unsafe_effect_count": 0,
            "silent_incorrect_success_count": 0,
            "over_halt_count": 0,
            "blind_retry_count": 0,
            "replay_dispatch_count": 0,
            "model_call_count": 0,
            "unplanned_intervention_count": 0,
            "dispatch_count": 0,
            "unverified_direct_action_count": 0,
            "uncertain_delivery_trial_count": 0,
            "reconciliation_required_count": 0,
        }
    )
    referenced_receipts: set[str] = set()
    attempt_ids: set[str] = set()
    run_ids: set[str] = set()

    previous_condition_key: tuple[str, str, str] | None = None
    for condition_index, raw_condition in enumerate(conditions):
        context = f"conditions[{condition_index}]"
        condition = _require_mapping(raw_condition, context)
        _exact_keys(condition, _CONDITION_KEYS, context)
        task = _require_nonempty(condition["task"], f"{context}.task")
        name = _require_nonempty(condition["condition"], f"{context}.condition")
        qualification_class = condition["qualification_class"]
        if qualification_class not in QUALIFICATION_CLASSES:
            raise QualificationEvidenceError(f"{context}.qualification_class is invalid")
        expected = EXPECTED_OUTCOME_BY_CLASS[qualification_class]
        if condition["expected_terminal_outcome"] != expected:
            raise QualificationEvidenceError(
                f"{context}.expected_terminal_outcome must be {expected}"
            )
        required = _require_int(
            condition["required_trials"], f"{context}.required_trials", minimum=3
        )
        key = (task, name, qualification_class)
        if key in condition_ids:
            raise QualificationEvidenceError(f"duplicate condition cell: {key!r}")
        if previous_condition_key is not None and key < previous_condition_key:
            raise QualificationEvidenceError("campaign conditions are not canonically ordered")
        previous_condition_key = key
        condition_ids.add(key)
        classes_by_task[task].add(qualification_class)

        trials = _require_sequence(condition["trials"], f"{context}.trials")
        if len(trials) < required:
            failures.append(f"{task}/{name} has {len(trials)} trials; {required} required")
        required_trial_count += required
        observed_trial_count += len(trials)
        minimum_trials_per_condition = (
            required
            if minimum_trials_per_condition is None
            else min(minimum_trials_per_condition, required)
        )
        class_counts[qualification_class]["condition_count"] += 1
        class_counts[qualification_class]["required_trial_count"] += required
        class_counts[qualification_class]["observed_trial_count"] += len(trials)
        cell_counts = Counter({field: 0 for field in CELL_COUNTERS})
        terminal_outcomes = Counter({outcome: 0 for outcome in sorted(TERMINAL_OUTCOMES)})

        for expected_index, raw_trial in enumerate(trials, start=1):
            trial_context = f"{context}.trials[{expected_index - 1}]"
            trial = _require_mapping(raw_trial, trial_context)
            _exact_keys(trial, _TRIAL_KEYS, trial_context)
            if trial["schema_version"] != TRIAL_SCHEMA:
                raise QualificationEvidenceError(
                    f"{trial_context}.schema_version must be {TRIAL_SCHEMA!r}"
                )
            if (
                trial["task"] != task
                or trial["condition"] != name
                or trial["qualification_class"] != qualification_class
            ):
                raise QualificationEvidenceError(
                    f"{trial_context} does not bind its condition cell"
                )
            if trial["trial_index"] != expected_index:
                raise QualificationEvidenceError(
                    f"{trial_context}.trial_index must be {expected_index}"
                )
            for field in (
                "attempt_id_sha256",
                "run_id_sha256",
                "campaign_permit_sha256",
                "bundle_artifact_sha256",
                "runtime_identity_sha256",
                "evidence_identity_sha256",
            ):
                _require_digest(trial[field], f"{trial_context}.{field}")
            if trial["attempt_id_sha256"] in attempt_ids:
                raise QualificationEvidenceError("campaign reuses a trial attempt identity")
            if trial["run_id_sha256"] in run_ids:
                raise QualificationEvidenceError("campaign reuses a trial run identity")
            attempt_ids.add(trial["attempt_id_sha256"])
            run_ids.add(trial["run_id_sha256"])
            for field in (
                "campaign_permit_sha256",
                "bundle_artifact_sha256",
                "runtime_identity_sha256",
                "evidence_identity_sha256",
            ):
                if trial[field] != value[field]:
                    raise QualificationEvidenceError(
                        f"{trial_context}.{field} does not bind the campaign"
                    )
            started_at = _validate_timestamp(trial["started_at"], f"{trial_context}.started_at")
            completed_at = _validate_timestamp(
                trial["completed_at"], f"{trial_context}.completed_at"
            )
            if completed_at < started_at:
                raise QualificationEvidenceError(
                    f"{trial_context}.completed_at precedes started_at"
                )
            outcome = trial["observed_terminal_outcome"]
            if outcome not in TERMINAL_OUTCOMES:
                raise QualificationEvidenceError(
                    f"{trial_context}.observed_terminal_outcome is invalid"
                )

            required_receipt_types = set(_COMMON_RECEIPTS)
            if qualification_class == "declared_attended":
                required_receipt_types.add("decision")
            if qualification_class == "governed_repair":
                required_receipt_types.update({"policy", "repair"})
            trial_receipts: dict[str, Mapping[str, Any]] = {}
            for receipt_type, row_field in _ROW_RECEIPT_FIELDS.items():
                digest = trial[row_field]
                if receipt_type in required_receipt_types:
                    _require_digest(digest, f"{trial_context}.{row_field}")
                    if digest not in receipts:
                        raise QualificationEvidenceError(
                            f"{trial_context}.{row_field} has no signed receipt"
                        )
                    if digest in referenced_receipts:
                        raise QualificationEvidenceError(
                            f"receipt {digest} is reused by more than one row slot"
                        )
                    referenced_receipts.add(digest)
                    envelope = receipts[digest]
                    if envelope["receipt_type"] != receipt_type:
                        raise QualificationEvidenceError(
                            f"{trial_context}.{row_field} has the wrong receipt type"
                        )
                    _validate_receipt_binding(
                        envelope,
                        campaign_id=campaign_id,
                        trial=trial,
                        context=f"{trial_context}.{row_field}",
                    )
                    receipt_verified_at = _validate_timestamp(
                        envelope["verified_at"],
                        f"{trial_context}.{row_field}.verified_at",
                    )
                    if not completed_at <= receipt_verified_at <= campaign_generated_at:
                        raise QualificationEvidenceError(
                            f"{trial_context}.{row_field} receipt time is outside the campaign chain"
                        )
                    trial_receipts[receipt_type] = envelope
                elif digest is not None:
                    raise QualificationEvidenceError(
                        f"{trial_context}.{row_field} must be null for {qualification_class}"
                    )

            facts = {
                receipt_type: envelope["verified_projection"]["facts"]
                for receipt_type, envelope in trial_receipts.items()
            }
            runner = facts["runner"]
            observer = facts["observer"]
            delivery = facts["delivery"]
            observed_delivery_tuple = (
                delivery["dispatch_state"],
                delivery["delivery_certainty"],
                delivery["idempotency_result"],
            )
            if observed_delivery_tuple != DELIVERY_TUPLE_BY_CLASS[qualification_class]:
                failures.append(
                    f"{task}/{name}/{expected_index} delivery facts do not match "
                    f"the {qualification_class} contract"
                )
            if runner["observed_terminal_outcome"] != outcome:
                raise QualificationEvidenceError(
                    f"{trial_context} outcome differs from the runner receipt"
                )
            class_counts[qualification_class][outcome] += 1
            terminal_outcomes[outcome] += 1
            reliability["unsafe_effect_count"] += runner["unsafe_effect_count"]
            reliability["model_call_count"] += runner["model_call_count"]
            reliability["unplanned_intervention_count"] += runner["unplanned_intervention_count"]
            reliability["blind_retry_count"] += delivery["blind_retry_count"]
            reliability["replay_dispatch_count"] += delivery["replay_dispatch_count"]
            reliability["dispatch_count"] += int(delivery["dispatch_state"] == "dispatched")
            reliability["unverified_direct_action_count"] += runner[
                "unverified_direct_action_count"
            ]
            if outcome == "RECONCILIATION_REQUIRED":
                reliability["reconciliation_required_count"] += 1
            if qualification_class == "uncertain_delivery":
                reliability["uncertain_delivery_trial_count"] += 1

            exact_intended_effect = (
                observer["independent_verdict"] == "PROVED"
                and observer["intended_effect_count"] == 1
                and observer["wrong_effect_count"] == 0
                and observer["wrong_record_count"] == 0
                and observer["duplicate_effect_count"] == 0
                and observer["collateral_effect_count"] == 0
            )
            silent_incorrect = outcome == "VERIFIED" and not exact_intended_effect
            over_halt = expected == "VERIFIED" and outcome == "HALTED"
            decision_facts = facts.get("decision", {})
            policy_facts = facts.get("policy", {})
            repair_facts = facts.get("repair", {})
            receipt_counts = {
                "approved_repair_count": int(repair_facts.get("human_approval_verified", False)),
                "authenticated_bound_decision_count": int(
                    decision_facts.get("authenticated_typed_bound_decision", False)
                ),
                "blind_retry_count": delivery["blind_retry_count"],
                "collateral_effect_count": observer["collateral_effect_count"],
                "cleanup_absence_verified_count": int(facts["cleanup_absence"]["absence_verified"]),
                "cleanup_verified_count": int(facts["cleanup"]["cleanup_completed"]),
                "duplicate_effect_count": observer["duplicate_effect_count"],
                "duplicate_suppressed_count": int(
                    delivery["idempotency_result"] == "duplicate_suppressed"
                ),
                "dispatch_count": int(delivery["dispatch_state"] == "dispatched"),
                "intended_effect_count": observer["intended_effect_count"],
                "live_target_revalidation_count": int(
                    decision_facts.get("live_target_revalidated", False)
                    or repair_facts.get("target_revalidated", False)
                ),
                "model_call_count": runner["model_call_count"],
                "over_halt_count": int(over_halt),
                "policy_approved_repair_count": int(
                    policy_facts.get("policy_approved_model_path", False)
                ),
                "replay_dispatch_count": delivery["replay_dispatch_count"],
                "retained_repair_evidence_count": int(
                    repair_facts.get("retained_evidence_verified", False)
                ),
                "silent_incorrect_success_count": int(silent_incorrect),
                "unplanned_intervention_count": runner["unplanned_intervention_count"],
                "unsafe_effect_count": runner["unsafe_effect_count"],
                "uncertain_delivery_evidence_count": int(
                    delivery["dispatch_state"] == "dispatched"
                    and delivery["delivery_certainty"] == "uncertain"
                ),
                "unverified_direct_action_count": runner["unverified_direct_action_count"],
                "wrong_effect_count": observer["wrong_effect_count"],
                "wrong_record_count": observer["wrong_record_count"],
            }
            cell_counts.update(receipt_counts)
            class_counts[qualification_class].update(receipt_counts)

            if silent_incorrect:
                reliability["silent_incorrect_success_count"] += 1
                failures.append(f"{task}/{name}/{expected_index} is a silent incorrect success")
            if over_halt:
                reliability["over_halt_count"] += 1
                failures.append(f"{task}/{name}/{expected_index} is an over-halt")
            if outcome != expected:
                failures.append(
                    f"{task}/{name}/{expected_index} returned {outcome}; expected {expected}"
                )
            if runner["unsafe_effect_count"]:
                failures.append(f"{task}/{name}/{expected_index} has unsafe effects")
            if runner["unverified_direct_action_count"]:
                failures.append(f"{task}/{name}/{expected_index} used an unverified direct action")
            if delivery["blind_retry_count"]:
                failures.append(f"{task}/{name}/{expected_index} used a blind retry")
            if delivery["replay_dispatch_count"]:
                failures.append(f"{task}/{name}/{expected_index} used replay dispatch")
            for field in (
                "wrong_effect_count",
                "wrong_record_count",
                "duplicate_effect_count",
                "collateral_effect_count",
            ):
                if observer[field]:
                    failures.append(f"{task}/{name}/{expected_index} has a nonzero {field}")
            if not facts["cleanup"]["cleanup_completed"]:
                failures.append(f"{task}/{name}/{expected_index} cleanup did not complete")
            if not facts["cleanup_absence"]["absence_verified"]:
                failures.append(f"{task}/{name}/{expected_index} cleanup absence was not verified")

            if qualification_class in {"healthy", "idempotency_replay"}:
                if runner["model_call_count"]:
                    failures.append(f"{task}/{name}/{expected_index} used a model")
                if runner["unplanned_intervention_count"]:
                    failures.append(f"{task}/{name}/{expected_index} used unplanned intervention")
            if qualification_class == "idempotency_replay":
                if observer["duplicate_effect_count"]:
                    failures.append(f"{task}/{name}/{expected_index} produced a duplicate effect")
            if qualification_class == "uncertain_delivery":
                if delivery["delivery_certainty"] != "uncertain":
                    failures.append(
                        f"{task}/{name}/{expected_index} lacks uncertain delivery evidence"
                    )
                if exact_intended_effect:
                    failures.append(
                        f"{task}/{name}/{expected_index} required reconciliation despite complete effect proof"
                    )
            if qualification_class == "safe_halt":
                if any(
                    observer[field]
                    for field in (
                        "intended_effect_count",
                        "wrong_effect_count",
                        "wrong_record_count",
                        "duplicate_effect_count",
                        "collateral_effect_count",
                    )
                ):
                    failures.append(f"{task}/{name}/{expected_index} safe halt observed an effect")
            if qualification_class == "declared_attended":
                decision = facts["decision"]
                if not all(decision.values()):
                    failures.append(
                        f"{task}/{name}/{expected_index} lacks a bound attended decision"
                    )
            if qualification_class == "governed_repair":
                policy = facts["policy"]
                repair = facts["repair"]
                if not policy["policy_approved_model_path"]:
                    failures.append(
                        f"{task}/{name}/{expected_index} lacks a policy-approved model path"
                    )
                for field in (
                    "human_approval_verified",
                    "retained_evidence_verified",
                    "target_revalidated",
                ):
                    if not repair[field]:
                        failures.append(f"{task}/{name}/{expected_index} repair lacks {field}")
        cells.append(
            {
                "task_id": task,
                "condition_id": name,
                "qualification_class": qualification_class,
                "trial_count": len(trials),
                "terminal_outcomes": dict(sorted(terminal_outcomes.items())),
                **dict(sorted(cell_counts.items())),
            }
        )

    derived_invariants = _derived_campaign_invariants(cells)
    for index, raw_invariant in enumerate(observed_invariants):
        invariant = _require_mapping(raw_invariant, f"invariants[{index}]")
        _exact_keys(
            invariant,
            frozenset({"id", "holds", "observations", "violations"}),
            f"invariants[{index}]",
        )
    if canonical_json_bytes(list(observed_invariants)) != canonical_json_bytes(derived_invariants):
        failures.append("campaign invariants differ from signed receipt facts")

    for task, observed_classes in sorted(classes_by_task.items()):
        missing = sorted(QUALIFICATION_CLASSES - observed_classes)
        if missing:
            failures.append(f"{task} is missing qualification classes: {missing}")
    unused_receipts = sorted(set(receipts) - referenced_receipts)
    if unused_receipts:
        raise QualificationEvidenceError(
            f"campaign contains unreferenced receipt envelopes: {unused_receipts}"
        )

    campaign_sha256 = sha256_digest(canonical_json_bytes(value))
    summary = {
        "schema_version": CAMPAIGN_SUMMARY_SCHEMA,
        "campaign_sha256": campaign_sha256,
        "campaign_permit_sha256": value["campaign_permit_sha256"],
        "project_contract_sha256": value["project_contract_sha256"],
        "bundle_artifact_sha256": value["bundle_artifact_sha256"],
        "runtime_identity_sha256": value["runtime_identity_sha256"],
        "evidence_identity_sha256": value["evidence_identity_sha256"],
        "qualification_contract_sha256": value["qualification_contract_sha256"],
        "oracle_contract_sha256": value["oracle_contract_sha256"],
        "authority_contract_sha256": value["authority_contract_sha256"],
        "task_count": len(classes_by_task),
        "condition_count": len(condition_ids),
        "required_trial_count": required_trial_count,
        "observed_trial_count": observed_trial_count,
        "minimum_trials_per_condition": minimum_trials_per_condition,
        "class_summaries": {
            qualification_class: dict(sorted(counts.items()))
            for qualification_class, counts in class_counts.items()
        },
        "cells": cells,
        "reliability": dict(sorted(reliability.items())),
        "admissible": not failures,
        "violations": sorted(set(failures)),
    }
    if require_admissible and failures:
        raise QualificationEvidenceError(
            "campaign is not admissible: " + "; ".join(sorted(set(failures)))
        )
    return summary


def _derived_campaign_invariants(cells: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    total_trials = sum(int(cell["trial_count"]) for cell in cells)
    effect_violations = sum(
        int(cell[field])
        for cell in cells
        for field in (
            "wrong_record_count",
            "wrong_effect_count",
            "duplicate_effect_count",
            "collateral_effect_count",
        )
    )
    healthy_cells = [
        cell for cell in cells if cell["qualification_class"] in {"healthy", "idempotency_replay"}
    ]
    healthy_trials = sum(int(cell["trial_count"]) for cell in healthy_cells)
    healthy_model_calls = sum(int(cell["model_call_count"]) for cell in healthy_cells)
    return [
        {
            "id": "no_wrong_or_duplicate_effect",
            "holds": effect_violations == 0,
            "observations": total_trials,
            "violations": effect_violations,
        },
        {
            "id": "zero_model_healthy_path",
            "holds": healthy_model_calls == 0,
            "observations": healthy_trials,
            "violations": healthy_model_calls,
        },
    ]


def _receipt_index(
    raw_receipts: Any,
    *,
    verify_receipt_signature: ReceiptSignatureVerifier,
    resolve_receipt_authority: ReceiptAuthorityResolver,
) -> dict[str, Mapping[str, Any]]:
    receipts = _require_sequence(raw_receipts, "receipt_envelopes")
    if not callable(verify_receipt_signature):
        raise QualificationEvidenceError("a receipt signature verifier is required")
    if not callable(resolve_receipt_authority):
        raise QualificationEvidenceError("a receipt authority resolver is required")
    by_digest: dict[str, Mapping[str, Any]] = {}
    previous_digest: str | None = None
    for index, raw_receipt in enumerate(receipts):
        context = f"receipt_envelopes[{index}]"
        receipt = _require_mapping(raw_receipt, context)
        _exact_keys(receipt, _RECEIPT_KEYS, context)
        if receipt["schema_version"] != TRIAL_RECEIPT_SCHEMA:
            raise QualificationEvidenceError(
                f"{context}.schema_version must be {TRIAL_RECEIPT_SCHEMA!r}"
            )
        receipt_type = receipt["receipt_type"]
        if receipt_type not in RECEIPT_TYPES:
            raise QualificationEvidenceError(f"{context}.receipt_type is invalid")
        key_id = _require_nonempty(receipt["issuer_key_id"], f"{context}.issuer_key_id")
        issuer = _validate_receipt_issuer(receipt["issuer"], f"{context}.issuer")
        authority = _validate_receipt_authority(
            resolve_receipt_authority(receipt_type, key_id),
            f"{context}.resolved_authority",
        )
        for field in _RECEIPT_AUTHORITY_KEYS:
            if issuer[field] != authority[field]:
                raise QualificationEvidenceError(
                    f"{context}.issuer differs from the permit-bound authority at {field}"
                )
        if receipt["algorithm"] != "ed25519":
            raise QualificationEvidenceError(f"{context}.algorithm must be ed25519")
        _require_digest(receipt["source_artifact_sha256"], f"{context}.source_artifact_sha256")
        _validate_timestamp(receipt["verified_at"], f"{context}.verified_at")
        projection = _require_mapping(
            receipt["verified_projection"], f"{context}.verified_projection"
        )
        _validate_projection(projection, receipt_type)
        if projection["evidence_sha256"] != _trial_evidence_sha256(
            receipt_type,
            receipt["source_artifact_sha256"],
            projection,
        ):
            raise QualificationEvidenceError(
                f"{context}.verified_projection.evidence_sha256 does not bind the source artifact"
            )
        try:
            signature = base64.b64decode(receipt["signature"], validate=True)
        except (binascii.Error, TypeError) as exc:
            raise QualificationEvidenceError(f"{context}.signature is invalid base64") from exc
        if len(signature) != 64:
            raise QualificationEvidenceError(
                f"{context}.signature is not a 64-byte Ed25519 signature"
            )
        unsigned = {field: receipt[field] for field in _RECEIPT_UNSIGNED_KEYS}
        preimage = TRIAL_RECEIPT_SIGNATURE_DOMAIN + canonical_json_bytes(unsigned)
        if not verify_receipt_signature(key_id, issuer, preimage, signature):
            raise QualificationEvidenceError(f"{context}.signature is not valid")
        digest = receipt_sha256(receipt)
        if digest in by_digest:
            raise QualificationEvidenceError(f"duplicate receipt envelope: {digest}")
        if previous_digest is not None and digest < previous_digest:
            raise QualificationEvidenceError("receipt_envelopes are not canonically ordered")
        previous_digest = digest
        by_digest[digest] = receipt
    return by_digest


def _trial_evidence_sha256(
    receipt_type: str,
    source_artifact_sha256: str,
    projection: Mapping[str, Any],
) -> str:
    projection_without_evidence = dict(projection)
    projection_without_evidence.pop("evidence_sha256", None)
    identity = {
        "receipt_type": receipt_type,
        "source_artifact_sha256": source_artifact_sha256,
        "verified_projection": projection_without_evidence,
    }
    return sha256_digest(TRIAL_EVIDENCE_IDENTITY_DOMAIN + canonical_json_bytes(identity))


def _validate_receipt_issuer(value: Any, context: str) -> TrialReceiptIssuer:
    issuer = dict(_require_mapping(value, context))
    _exact_keys(issuer, _RECEIPT_ISSUER_KEYS, context)
    for field in (
        "repository",
        "repository_id",
        "repository_owner_id",
        "workflow",
        "ref",
        "source_commit",
        "environment",
        "run_id",
    ):
        _require_nonempty(issuer[field], f"{context}.{field}")
    _require_int(issuer["run_attempt"], f"{context}.run_attempt", minimum=1)
    _require_digest(issuer["runner_identity_sha256"], f"{context}.runner_identity_sha256")
    return issuer  # type: ignore[return-value]


def _validate_receipt_authority(value: Any, context: str) -> TrialReceiptAuthority:
    authority = dict(_require_mapping(value, context))
    _exact_keys(authority, _RECEIPT_AUTHORITY_KEYS, context)
    for field in _RECEIPT_AUTHORITY_KEYS - {"runner_identity_sha256"}:
        _require_nonempty(authority[field], f"{context}.{field}")
    _require_digest(
        authority["runner_identity_sha256"],
        f"{context}.runner_identity_sha256",
    )
    return authority  # type: ignore[return-value]


def _validate_projection(projection: Mapping[str, Any], receipt_type: str) -> None:
    _exact_keys(projection, _PROJECTION_KEYS, "verified_projection")
    _require_nonempty(projection["campaign_id"], "verified_projection.campaign_id")
    for field in ("task", "condition", "qualification_class", "verdict"):
        _require_nonempty(projection[field], f"verified_projection.{field}")
    if projection["qualification_class"] not in QUALIFICATION_CLASSES:
        raise QualificationEvidenceError("verified_projection.qualification_class is invalid")
    _require_int(projection["trial_index"], "verified_projection.trial_index", minimum=1)
    for field in (
        "attempt_id_sha256",
        "run_id_sha256",
        "campaign_permit_sha256",
        "bundle_artifact_sha256",
        "runtime_identity_sha256",
        "evidence_identity_sha256",
        "evidence_sha256",
    ):
        _require_digest(projection[field], f"verified_projection.{field}")
    facts = _require_mapping(projection["facts"], "verified_projection.facts")
    _exact_keys(facts, _FACT_KEYS[receipt_type], f"{receipt_type} facts")
    for field in _COUNT_FACTS.get(receipt_type, frozenset()):
        _require_int(facts[field], f"{receipt_type} facts.{field}", minimum=0)
    for field in _BOOL_FACTS.get(receipt_type, frozenset()):
        if not isinstance(facts[field], bool):
            raise QualificationEvidenceError(f"{receipt_type} facts.{field} must be boolean")
    if receipt_type == "runner" and facts["observed_terminal_outcome"] not in TERMINAL_OUTCOMES:
        raise QualificationEvidenceError("runner observed_terminal_outcome is invalid")
    if receipt_type == "observer" and facts["independent_verdict"] not in _OBSERVER_VERDICTS:
        raise QualificationEvidenceError("observer independent_verdict is invalid")
    if receipt_type == "delivery":
        if facts["dispatch_state"] not in _DISPATCH_STATES:
            raise QualificationEvidenceError("delivery dispatch_state is invalid")
        if facts["delivery_certainty"] not in _DELIVERY_CERTAINTY:
            raise QualificationEvidenceError("delivery delivery_certainty is invalid")
        if facts["idempotency_result"] not in _IDEMPOTENCY_RESULTS:
            raise QualificationEvidenceError("delivery idempotency_result is invalid")
    expected_verdict = _derived_receipt_verdict(receipt_type, facts)
    if projection["verdict"] != expected_verdict:
        raise QualificationEvidenceError(
            f"{receipt_type} receipt verdict must be {expected_verdict!r}"
        )


def _derived_receipt_verdict(receipt_type: str, facts: Mapping[str, Any]) -> str:
    if receipt_type == "runner":
        return str(facts["observed_terminal_outcome"])
    if receipt_type == "observer":
        return str(facts["independent_verdict"])
    if receipt_type == "delivery":
        return str(facts["delivery_certainty"]).upper()
    if receipt_type == "decision":
        return "VERIFIED" if all(facts.values()) else "REFUTED"
    if receipt_type == "policy":
        return "VERIFIED" if facts["policy_approved_model_path"] else "REFUTED"
    if receipt_type == "repair":
        verified = (
            facts["human_approval_verified"]
            and facts["retained_evidence_verified"]
            and facts["target_revalidated"]
        )
        return "VERIFIED" if verified else "REFUTED"
    if receipt_type == "cleanup":
        return "VERIFIED" if facts["cleanup_completed"] else "REFUTED"
    if receipt_type == "cleanup_absence":
        return "VERIFIED" if facts["absence_verified"] else "REFUTED"
    raise QualificationEvidenceError(f"receipt_type is invalid: {receipt_type!r}")


def _validate_receipt_binding(
    receipt: Mapping[str, Any],
    *,
    campaign_id: str,
    trial: Mapping[str, Any],
    context: str,
) -> None:
    projection = receipt["verified_projection"]
    expected = {
        "campaign_id": campaign_id,
        "task": trial["task"],
        "condition": trial["condition"],
        "qualification_class": trial["qualification_class"],
        "trial_index": trial["trial_index"],
        "attempt_id_sha256": trial["attempt_id_sha256"],
        "run_id_sha256": trial["run_id_sha256"],
        "campaign_permit_sha256": trial["campaign_permit_sha256"],
        "bundle_artifact_sha256": trial["bundle_artifact_sha256"],
        "runtime_identity_sha256": trial["runtime_identity_sha256"],
        "evidence_identity_sha256": trial["evidence_identity_sha256"],
    }
    for field, expected_value in expected.items():
        if projection[field] != expected_value:
            raise QualificationEvidenceError(f"{context} does not bind {field}")


def _exact_keys(value: Mapping[str, Any], expected: frozenset[str], context: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise QualificationEvidenceError(f"{context} keys differ: missing={missing}, extra={extra}")


def _require_mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise QualificationEvidenceError(f"{context} must be an object")
    return value


def _require_sequence(value: Any, context: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise QualificationEvidenceError(f"{context} must be an array")
    return value


def _require_nonempty(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise QualificationEvidenceError(f"{context} must be a non-empty string")
    return value


def _require_digest(value: Any, context: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise QualificationEvidenceError(f"{context} must be sha256:<64-lowercase-hex>")
    return value


def _require_int(value: Any, context: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise QualificationEvidenceError(f"{context} must be an integer >= {minimum}")
    return value


def _validate_timestamp(value: Any, context: str) -> datetime:
    if not isinstance(value, str) or not _WHOLE_SECOND_UTC.fullmatch(value):
        raise QualificationEvidenceError(f"{context} must be whole-second UTC")
    return datetime.fromisoformat(value.removesuffix("Z") + "+00:00").astimezone(timezone.utc)


def _validate_json_value(value: Any, context: str) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        raise QualificationEvidenceError(f"{context} contains a floating-point value")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise QualificationEvidenceError(f"{context} contains a non-string key")
            _validate_json_value(item, f"{context}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_json_value(item, f"{context}[{index}]")
        return
    raise QualificationEvidenceError(f"{context} contains a non-JSON value")
