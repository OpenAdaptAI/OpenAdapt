#!/usr/bin/env python3
"""Derive one bounded production-acceptance result from signed evidence.

The Cloud certificate proves one live authenticated transaction.  An external
qualification authority signs an admission for the full sanitized campaign.
The campaign retains every trial and a signed normalized receipt for each
runner, observer, webhook, replay, fault, cleanup, and cleanup-absence claim.
This importer verifies all signatures and binds all artifacts by canonical
SHA-256 before it derives counts from complete trial rows.

It never promotes an author-supplied maturity class, production boolean,
summary count, or trial classification.  The only successful output is scoped
to the exact qualified browser workflow and environment in the certificate.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit

CERTIFICATE_SCHEMA = "openadapt.execute-live-acceptance-record/v2"
CAMPAIGN_SCHEMA = "openadapt.qualification-campaign/v2"
TRIAL_SCHEMA = "openadapt.qualification-trial-row/v2"
RESULT_SCHEMA = "openadapt.evals-derived-production-acceptance/v1"
CLAIM_SCOPE = "qualified_browser_workflow_on_bound_environment"
PRODUCTION_ACCEPTANCE_SCHEMA = "openadapt.production-acceptance/v1"
PRODUCTION_ACCEPTANCE_POLICY_SCHEMA = "openadapt.production-acceptance-policy/v1"
PRODUCTION_ACCEPTANCE_POLICY_DOMAIN = b"OpenAdapt production acceptance policy v1\0"
PRODUCTION_LIFECYCLE_POLICY_SCHEMA = "openadapt.production-lifecycle-policy/v1"
PRODUCTION_LIFECYCLE_POLICY_PATH = "schemas/production-lifecycle-policy.schema.json"
PRODUCTION_LIFECYCLE_TARGET_RELEASE_DOMAIN = (
    b"OpenAdapt production lifecycle target release v1\0"
)
PRODUCTION_LIFECYCLE_ARTIFACT_INVENTORY_DOMAIN = (
    b"OpenAdapt production lifecycle artifact inventory v1\0"
)
PRODUCTION_ACCEPTANCE_TARGET_SCOPES = {
    "agent": "qualified_agent_bridge_release",
    "capture": "qualified_native_recorder_release",
    "cloud": "qualified_workflow_control_plane_deployment",
    "desktop": "qualified_native_workflow_desktop_release",
    "docs": "production_documentation_deployment",
    "flow": "qualified_workflow_runtime_release",
    "openadapt": "qualified_workflow_launcher_release",
}
_BROWSER_SOURCE_TARGETS = {"cloud", "flow"}
ADMISSION_SCHEMA = "openadapt.qualification-admission/v2"
ADMISSION_SIGNATURE_DOMAIN_TEXT = "openadapt-qualification-admission-v2\\0"
ADMISSION_SIGNATURE_DOMAIN = b"openadapt-qualification-admission-v2\0"
RECEIPT_SCHEMA = "openadapt.qualification-evidence-receipt/v2"
RECEIPT_SIGNATURE_DOMAIN_TEXT = "openadapt-qualification-evidence-receipt-v2\\0"
RECEIPT_SIGNATURE_DOMAIN = b"openadapt-qualification-evidence-receipt-v2\0"
EVIDENCE_IDENTITY_SCHEMA = "openadapt.production-acceptance-evidence-identity/v2"
EVIDENCE_IDENTITY_DOMAIN = b"OpenAdapt production acceptance evidence identity v2\0"
RUNTIME_BUILD_IDENTITY_SCHEMA = "openadapt.admitted-runtime-build/v1"
ADMISSION_POLICY_SCHEMA = "openadapt.production-acceptance-admission-policy/v1"
ADMISSION_POLICY_DOMAIN = b"OpenAdapt production acceptance admission policy v1\0"
SIGNER_REGISTRY_SCHEMA = "openadapt.qualification-signer-registry/v2"
SIGNER_REGISTRY_DOMAIN = b"OpenAdapt qualification signer registry v2\0"
ADMISSION_ISSUER_WORKFLOW = (
    "OpenAdaptAI/openadapt-internal/.github/workflows/"
    "production-qualification-admission.yml"
)
ADMISSION_REF_PREFIX = "refs/heads/main@"

CLOUD_REPOSITORY = "OpenAdaptAI/openadapt-cloud"
CLOUD_WORKFLOW = ".github/workflows/execute-live-acceptance.yml"
CLOUD_CERTIFICATE_IDENTITY = (
    "https://github.com/OpenAdaptAI/openadapt-cloud/"
    ".github/workflows/execute-live-acceptance.yml@refs/heads/main"
)
GITHUB_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
RETENTION_PROVENANCE_ROUTE = "sigstore-public-good-slsa-provenance-v1"
RETENTION_MEDIUM = "signed-git-commit-v1"
PRIVATE_EXPORT_CONTRACT_SCHEMA = "openadapt.private-export-contract/v1"
GITHUB_HOSTNAME = "github.com"
PUBLIC_TRANSPARENCY_LOG = "https://rekor.sigstore.dev"
PUBLIC_TIMESTAMP_AUTHORITY = "https://timestamp.sigstore.dev/api/v1/timestamp"
REVIEWED_GITHUB_CLI_VERSION = "2.67.0"

_PUBLIC_TRANSPARENCY_LOG = ("Tlog", PUBLIC_TRANSPARENCY_LOG)
_PUBLIC_TIMESTAMP_AUTHORITY = ("TimestampAuthority", PUBLIC_TIMESTAMP_AUTHORITY)
_APPROVED_OBSERVERS = frozenset({_PUBLIC_TRANSPARENCY_LOG, _PUBLIC_TIMESTAMP_AUTHORITY})

_HEX_40 = re.compile(r"^[a-f0-9]{40}$")
_SHA256 = re.compile(r"^sha256:[a-f0-9]{64}$")
_SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
_POSITIVE_DECIMAL = re.compile(r"^[1-9][0-9]*$")
_UNPREFIXED_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_PINNED_IMAGE = re.compile(r"^[a-z0-9][a-z0-9._/:-]{0,255}@sha256:[a-f0-9]{64}$")
_ADMISSION_REF = re.compile(r"^refs/heads/main@[a-f0-9]{40}$")
_ADMISSION_KEY_ID = re.compile(r"^qa-ed25519-[a-f0-9]{16}$")
_RECEIPT_KEY_ID = re.compile(r"^qe-ed25519-[a-f0-9]{16}$")
_WHOLE_SECOND_UTC = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
)
_ARTIFACT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")

_CERTIFICATE_KEYS = {
    "schema_version",
    "verdict",
    "claim_scope",
    "product",
    "qualification",
    "transaction",
    "outcomes",
    "contracts",
    "identities",
    "retention",
}
_PRODUCT_KEYS = {"cloud", "flow", "managed_runtime"}
_CLOUD_KEYS = {"source_commit", "target_build_sha256"}
_FLOW_KEYS = {"version", "release_commit", "wheel_sha256"}
_RUNTIME_KEYS = {
    "manifest_sha256",
    "runner_build",
    "runner_artifact_sha256",
    "substrate",
    "playwright_version",
    "browser_base_image",
}
_QUALIFICATION_KEYS = {
    "qualification_admission_sha256",
    "campaign_artifact_sha256",
    "campaign_contract_sha256",
    "campaign_outcomes_sha256",
    "oracle_contract_sha256",
    "task_count",
    "condition_count",
    "required_trial_count",
    "observed_trial_count",
    "minimum_trials_per_condition",
    "admission_signer",
    "runtime_validation_id_sha256",
    "admission_id_sha256",
    "campaign_id_sha256",
    "workflow_version_id_sha256",
    "workflow_digest",
    "environment_digest",
    "evidence_identity_sha256",
}
_ADMISSION_SIGNER_KEYS = {
    "algorithm",
    "key_id",
    "issuer_workflow",
    "issuer_ref",
}
_TRANSACTION_KEYS = {
    "acceptance_envelope_sha256",
    "accepted_response_sha256",
    "duplicate_response_sha256",
    "receipt_sha256",
    "receipt_evidence_digest",
    "target_attestation_sha256",
    "observer_evidence_sha256",
    "webhook_event_sha256",
    "runner_transaction_identity_sha256",
    "runner_report_sha256",
    "runner_result_sha256",
    "runner_permit_sha256",
    "acceptance_challenge_sha256",
    "execution_id_sha256",
    "receipt_id_sha256",
    "idempotency_key_sha256",
    "request_sha256",
}
_OUTCOME_KEYS = {
    "transaction",
    "independent_effect",
    "webhook_delivery",
    "entitlement_and_metering",
}
_CONTRACT_KEYS = {
    "exact_source_attested",
    "authenticated_submission",
    "qualification_bound",
    "idempotency_verified",
    "independent_effect_verified",
    "single_delivery_verified",
    "single_permit_consumption_verified",
    "entitlement_and_metering_verified",
    "receipt_contract_verified",
    "target_attestation_signature_verified",
    "observer_evidence_signature_verified",
    "runner_transaction_identity_verified",
    "runner_signature_verified",
    "signed_webhook_verified",
    "zero_model_healthy_path",
    "quiet_period_stable",
}
_IDENTITY_KEYS = {
    "signer_fingerprint_scheme",
    "producer",
    "target_observer_signer_sha256",
    "target_attestation_signer_sha256",
    "evidence_runner_signer_sha256",
    "organization_id_sha256",
    "workflow_id_sha256",
    "verifier",
}
_PRODUCER_KEYS = {
    "kind",
    "repository",
    "workflow",
    "source_ref",
    "run_id",
    "run_attempt",
}
_VERIFIER_KEYS = {
    "kind",
    "repository",
    "workflow",
    "certificate_identity",
    "oidc_issuer",
    "hosted_runner_required",
}
_RETENTION_KEYS = {
    "receipt_id",
    "ciphertext_sha256",
    "candidate_sha256",
    "private_envelope_sha256",
    "store_attestation_sha256",
    "storage_identity_sha256",
    "private_locator_version_sha256",
    "encryption_recipient_sha256",
    "uploader_identity_sha256",
    "retained_at",
    "visibility_verified",
    "private_locator_recorded",
    "acceptance_verified_at",
    "provenance_attestation",
}
_PRODUCTION_ACCEPTANCE_KEYS = {
    "schema_version",
    "target",
    "claim_scope",
    "verdict",
    "acceptance_policy_sha256",
    "lifecycle_policy_sha256",
    "target_release_sha256",
    "target_artifact_inventory_sha256",
    "evidence_identity_sha256",
    "source_evidence",
    "qualification",
    "failure_taxonomy_counts",
    "reliability",
    "retention",
}
_PRODUCTION_SOURCE_EVIDENCE_KEYS = {
    "source_result_sha256",
    "certificate_sha256",
    "campaign_sha256",
    "qualification_admission_sha256",
    "attestation_sha256",
    "attestation_bundle_sha256",
}
_PRODUCTION_QUALIFICATION_KEYS = {
    "campaign_contract_sha256",
    "campaign_outcomes_sha256",
    "oracle_contract_sha256",
    "task_count",
    "condition_count",
    "required_trial_count",
    "observed_trial_count",
    "minimum_trials_per_condition",
    "excluded_trial_count",
    "task_condition_inventory_sha256",
}
_RELIABILITY_KEYS = {
    "silent_incorrect_success_count",
    "over_halt_count",
    "wrong_record_count",
    "duplicate_effect_count",
    "collateral_effect_count",
    "operator_intervention_count",
    "uncertain_delivery_count",
    "model_call_count",
}

_CAMPAIGN_KEYS = {
    "schema_version",
    "campaign_id",
    "admission_id",
    "runtime_validation_id",
    "evidence_identity_sha256",
    "qualification_contract",
    "oracle_contract",
    "authority_contract",
    "conditions",
    "invariants",
    "excluded_trials",
    "receipt_envelopes",
    "decision",
    "generated_at",
}
_CONDITION_KEYS = {
    "condition_id",
    "expected_runtime_outcome",
    "required_trials",
    "trials",
}
_TRIAL_KEYS = {
    "schema_version",
    "task",
    "condition",
    "trial_index",
    "attempt_id_sha256",
    "run_id_sha256",
    "workflow_version_id",
    "admission_id",
    "bundle_artifact_sha256",
    "runtime_validation_id",
    "evidence_identity_sha256",
    "started_at",
    "completed_at",
    "execution_outcome",
    "oracle_verdict",
    "failure_class",
    "runner_receipt_sha256",
    "observer_receipt_sha256",
    "webhook_receipt_sha256",
    "replay_report_sha256",
    "fault_receipt_sha256",
    "cleanup_receipt_sha256",
    "cleanup_absence_proof_sha256",
}
_INVARIANT_KEYS = {"id", "holds", "observations", "violations"}
_ORACLE_KEYS = {
    "schema_version",
    "runtime_outcome_source",
    "effect_outcome_source",
    "delivery_outcome_source",
    "attempt_accounting",
    "fault_evidence_source",
    "cleanup_evidence_source",
}

_RECEIPT_TYPES = (
    "runner",
    "observer",
    "webhook",
    "replay",
    "fault",
    "cleanup",
    "cleanup_absence",
)
_AUTHORITY_KEYS = {
    "algorithm",
    "key_id",
    "public_key",
    "signature_domain",
    "schema_version",
}
_RECEIPT_ENVELOPE_KEYS = {
    "schema_version",
    "receipt_type",
    "issuer_key_id",
    "algorithm",
    "source_artifact_sha256",
    "verified_projection",
    "verified_at",
    "signature",
}
_RECEIPT_SIGNED_KEYS = _RECEIPT_ENVELOPE_KEYS - {"signature"}
_RECEIPT_PROJECTION_KEYS = {
    "campaign_id",
    "task",
    "condition",
    "trial_index",
    "attempt_id_sha256",
    "run_id_sha256",
    "workflow_version_id",
    "admission_id",
    "bundle_artifact_sha256",
    "runtime_validation_id",
    "evidence_identity_sha256",
    "verdict",
    "evidence_sha256",
    "facts",
}
_RECEIPT_ROW_FIELDS = {
    "runner": "runner_receipt_sha256",
    "observer": "observer_receipt_sha256",
    "webhook": "webhook_receipt_sha256",
    "replay": "replay_report_sha256",
    "fault": "fault_receipt_sha256",
    "cleanup": "cleanup_receipt_sha256",
    "cleanup_absence": "cleanup_absence_proof_sha256",
}
_RECEIPT_VERDICTS = {
    "runner": {"verified", "halted", "failed"},
    "observer": {"satisfied", "refuted", "unverifiable"},
    "webhook": {"delivered", "duplicate", "missing"},
    "replay": {"verified", "halted", "failed"},
    "fault": {"injected", "not_injected", "unverifiable"},
    "cleanup": {"completed", "failed", "unverifiable"},
    "cleanup_absence": {"absent", "present", "unverifiable"},
}

_ADMISSION_PAYLOAD_KEYS = {
    "schema_version",
    "admission_id",
    "tenant_id",
    "workflow_id",
    "workflow_version_id",
    "bundle_version_id",
    "runtime_validation_id",
    "bundle_artifact_sha256",
    "bundle_content_digest",
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
    "evidence_identity",
    "campaign",
    "issuer",
    "issued_at",
    "not_before",
    "expires_at",
}
_ADMISSION_UUID_KEYS = {
    "admission_id",
    "tenant_id",
    "workflow_id",
    "workflow_version_id",
    "bundle_version_id",
    "runtime_validation_id",
}
_ADMISSION_DIGEST_KEYS = _ADMISSION_PAYLOAD_KEYS - {
    "schema_version",
    *_ADMISSION_UUID_KEYS,
    "campaign",
    "issuer",
    "issued_at",
    "not_before",
    "expires_at",
    "evidence_identity",
}
_ADMISSION_CAMPAIGN_KEYS = {
    "campaign_id",
    "artifact_sha256",
    "contract_sha256",
    "outcomes_sha256",
    "oracle_id",
    "oracle_contract_sha256",
    "tasks",
    "failure_taxonomy",
    "decision",
}

_EVIDENCE_IDENTITY_KEYS = {
    "schema_version",
    "runtime_build_identity",
    "evidence_runner_signer_sha256",
    "tenant_id",
    "workflow_id",
    "workflow_version_id",
    "workflow_digest",
    "bundle_version_id",
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
    "runtime_validation_id",
    "admission_id",
    "campaign_id",
    "admission_policy_sha256",
    "campaign_contract_sha256",
    "oracle_contract_sha256",
    "qualification_campaign_schema",
    "qualification_trial_schema",
    "qualification_receipt_schema",
    "qualification_signer_registry_sha256",
    "qualification_signer_registry_revision",
}

_RUNTIME_BUILD_COMMON_KEYS = {
    "schema_version",
    "flow_version",
    "flow_release_commit",
    "flow_wheel_sha256",
    "runtime_manifest_sha256",
    "runner_build",
    "runner_artifact_sha256",
    "substrate",
    "managed_browser",
    "native_desktop",
    "remote_display",
}
_MANAGED_BROWSER_DETAIL_KEYS = {"playwright_version", "browser_base_image"}
_NATIVE_DESKTOP_DETAIL_KEYS = {
    "desktop_version",
    "desktop_release_commit",
    "desktop_artifact_sha256",
    "os_family",
    "runtime_boundary_sha256",
}
_REMOTE_DISPLAY_DETAIL_KEYS = {
    "desktop_version",
    "desktop_release_commit",
    "desktop_artifact_sha256",
    "runner_os_family",
    "transport",
    "runtime_boundary_sha256",
}

_SIGNER_REGISTRY_KEYS = {
    "schema_version",
    "revision",
    "generated_at",
    "expires_at",
    "signers",
}
_SIGNER_REGISTRY_ENTRY_KEYS = {
    "algorithm",
    "key_id",
    "public_key",
    "allowed_workflows",
    "allowed_ref_prefixes",
    "status",
    "revoked_at",
}

_RUNNER_FACT_KEYS = {
    "model_call_counter",
    "operator_intervention_ids_sha256",
}
_MODEL_CALL_COUNTER_KEYS = {
    "source",
    "attempted",
    "completed",
    "input_tokens",
    "output_tokens",
    "cost_microusd",
    "call_ids_sha256",
    "provider_models",
    "egress_policy_sha256",
    "report_sha256",
}
_PROVIDER_MODEL_KEYS = {"provider", "model"}
_OBSERVER_FACT_KEYS = {
    "dispatch_state",
    "verifier_method",
    "verifier_tier",
    "pre_state_evidence_sha256",
    "post_state_evidence_sha256",
    "expected_record_id_sha256",
    "expected_transaction_ref_sha256",
    "effect_inventory",
    "derived_classifications",
}
_EFFECT_KEYS = {
    "effect_id_sha256",
    "record_id_sha256",
    "transaction_ref_sha256",
}
_EFFECT_CLASSIFICATION_KEYS = {
    "intended_effect_count",
    "wrong_record_count",
    "duplicate_effect_count",
    "collateral_effect_count",
}

_RUNTIME_OUTCOMES = {
    "verified",
    "halted",
    "delivery_uncertain",
    "platform_failure",
    "operator_intervention",
}
_EXPECTED_OUTCOMES = {"verified", "halted"}
_HALT_REASONS = {
    "none",
    "wrong_identity",
    "stale_identity",
    "ambiguity",
    "missing_effect",
    "weak_effect",
}
_FAILURE_TAXONOMY = (
    "verified",
    "safe_halt",
    "silent_incorrect_success",
    "over_halt",
    "wrong_record",
    "duplicate_effect",
    "collateral_effect",
    "uncertain_delivery",
    "platform_failure",
    "operator_intervention",
    "healthy_path_model_call",
)
_PRODUCTION_FAILURES = set(_FAILURE_TAXONOMY) - {"verified", "safe_halt"}
_DERIVED_RESULT_KEYS = {
    "schema_version",
    "verdict",
    "evidence_class",
    "claim_scope",
    "bindings",
    "source_evidence",
    "trial_inventory",
    "derived_outcomes",
    "reliability",
    "retention",
    "claim_limit",
}
_DERIVED_BINDING_KEYS = {
    "runtime_validation_id_sha256",
    "admission_id_sha256",
    "campaign_id_sha256",
    "workflow_version_id_sha256",
    "workflow_digest",
    "environment_digest",
    "evidence_identity_sha256",
    "cloud_source_commit",
    "cloud_target_build_sha256",
    "flow_version",
    "flow_release_commit",
    "flow_wheel_sha256",
    "managed_runtime_manifest_sha256",
    "runner_artifact_sha256",
    "runner_build",
    "substrate",
    "playwright_version",
    "browser_base_image",
    "campaign_contract_sha256",
    "campaign_outcomes_sha256",
    "oracle_contract_sha256",
    "task_count",
    "condition_count",
    "required_trial_count",
    "observed_trial_count",
    "evidence_runner_signer_sha256",
    "target_attestation_signer_sha256",
    "target_observer_signer_sha256",
    "target_attestation_sha256",
    "organization_id_sha256",
    "workflow_id_sha256",
}
_DERIVED_SOURCE_EVIDENCE_KEYS = {
    "certificate_sha256",
    "campaign_sha256",
    "qualification_admission_sha256",
    "qualification_authority",
    "attestation",
    "approved_cloud_source_commit",
}
_DERIVED_TRIAL_INVENTORY_KEYS = {
    "task_count",
    "condition_count",
    "required_trial_count",
    "observed_trial_count",
    "trial_count",
    "minimum_trials_per_condition",
    "conditions",
    "excluded_trial_count",
}
_DERIVED_CONDITION_KEYS = {
    "task_id_sha256",
    "condition_id_sha256",
    "required_trial_count",
    "observed_trial_count",
}


class AcceptanceError(ValueError):
    """The supplied artifacts do not prove bounded production acceptance."""


def canonical_json(value: Any) -> str:
    """Return the cross-repository canonical JSON representation."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class VerifiedProductionLifecycleRelease:
    """An immutable release that passed the lifecycle and authority checks."""

    __slots__ = (
        "_artifacts_json",
        "_claim_scope",
        "_lifecycle_policy_sha256",
        "_release_json",
        "_target",
    )
    _CONSTRUCTION_SEAL = object()

    def __init__(
        self,
        *,
        target: str,
        claim_scope: str,
        lifecycle_policy_sha256: str,
        release: Mapping[str, Any],
        artifacts: Sequence[Mapping[str, Any]],
        _seal: object,
    ) -> None:
        if _seal is not self._CONSTRUCTION_SEAL:
            raise TypeError(
                "VerifiedProductionLifecycleRelease can only be created by the "
                "lifecycle verifier"
            )
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_claim_scope", claim_scope)
        object.__setattr__(self, "_lifecycle_policy_sha256", lifecycle_policy_sha256)
        object.__setattr__(self, "_release_json", canonical_json(release))
        object.__setattr__(self, "_artifacts_json", canonical_json(artifacts))

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("verified lifecycle releases are immutable")

    @property
    def target(self) -> str:
        return self._target

    @property
    def claim_scope(self) -> str:
        return self._claim_scope

    @property
    def lifecycle_policy_sha256(self) -> str:
        return self._lifecycle_policy_sha256

    def release(self) -> dict[str, Any]:
        return json.loads(self._release_json)

    def artifacts(self) -> list[dict[str, Any]]:
        return json.loads(self._artifacts_json)


def canonical_sha256(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def evidence_identity_sha256(value: Mapping[str, Any]) -> str:
    """Return the domain-separated digest for one exact evidence identity."""

    return hashlib.sha256(
        EVIDENCE_IDENTITY_DOMAIN + canonical_json(value).encode("utf-8")
    ).hexdigest()


def qualification_signer_registry_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        SIGNER_REGISTRY_DOMAIN + canonical_json(value).encode("utf-8")
    ).hexdigest()


def admission_policy() -> dict[str, Any]:
    """Return the fixed policy whose digest the qualification authority admits."""

    return {
        "schema_version": ADMISSION_POLICY_SCHEMA,
        "qualification_admission_schema": ADMISSION_SCHEMA,
        "qualification_campaign_schema": CAMPAIGN_SCHEMA,
        "qualification_trial_schema": TRIAL_SCHEMA,
        "qualification_receipt_schema": RECEIPT_SCHEMA,
        "admission_signature_domain": ADMISSION_SIGNATURE_DOMAIN_TEXT,
        "receipt_signature_domain": RECEIPT_SIGNATURE_DOMAIN_TEXT,
        "issuer_workflow": ADMISSION_ISSUER_WORKFLOW,
        "issuer_ref_prefix": ADMISSION_REF_PREFIX,
        "maximum_lifetime_seconds": 2_592_000,
        "minimum_trials_per_condition": 3,
        "failure_taxonomy": sorted(_FAILURE_TAXONOMY),
        "external_signer_registry_required": True,
        "registry_freshness_required_at_actuation": True,
        "revocation_check_required": True,
        "distinct_admission_and_runtime_ids_required": True,
        "shared_evidence_identity_required": True,
    }


def admission_policy_sha256() -> str:
    return hashlib.sha256(
        ADMISSION_POLICY_DOMAIN + canonical_json(admission_policy()).encode("utf-8")
    ).hexdigest()


def production_acceptance_policy() -> dict[str, Any]:
    """Return the fixed target-neutral public-manifest policy."""

    return {
        "schema_version": PRODUCTION_ACCEPTANCE_POLICY_SCHEMA,
        "manifest_schema": PRODUCTION_ACCEPTANCE_SCHEMA,
        "source_result_schema": RESULT_SCHEMA,
        "accepted_verdict": "accepted",
        "target_claim_scopes": dict(sorted(PRODUCTION_ACCEPTANCE_TARGET_SCOPES.items())),
        "failure_taxonomy": sorted(_FAILURE_TAXONOMY),
        "minimum_trials_per_condition": 3,
        "excluded_trial_count": 0,
        "zero_failure_counts_required": sorted(_PRODUCTION_FAILURES),
        "required_retention_medium": RETENTION_MEDIUM,
        "required_retention_provenance": RETENTION_PROVENANCE_ROUTE,
        "minimum_retention_days": 365,
        "maximum_retention_days": 3650,
        "browser_evidence_target_set": sorted(_BROWSER_SOURCE_TARGETS),
        "enabled_browser_builder_targets": ["flow"],
        "pending_target_bindings": {
            "cloud": "reviewed_deployment_manifest_binding_required"
        },
        "release_binding_rules": {
            "flow": "verified_lifecycle_public_package_sdist_and_wheel_v1",
            "cloud": "private_deployment_release_and_reviewed_manifest_v1",
        },
        "acceptance_policy_digest": "domain-separated-canonical-json-sha256-v1",
        "lifecycle_policy_digest": "exact-raw-bytes-sha256-v1",
        "target_release_digest_domain": (
            "OpenAdapt production lifecycle target release v1\\0"
        ),
        "artifact_inventory_digest_domain": (
            "OpenAdapt production lifecycle artifact inventory v1\\0"
        ),
        "lifecycle_authority_metadata_required": True,
        "complete_source_result_required": True,
        "target_evidence_adapter_required": True,
        "privacy_safe_task_condition_identity_required": True,
    }


def production_acceptance_policy_sha256() -> str:
    return "sha256:" + hashlib.sha256(
        PRODUCTION_ACCEPTANCE_POLICY_DOMAIN
        + canonical_json(production_acceptance_policy()).encode("utf-8")
    ).hexdigest()


def _domain_sha256(domain: bytes, value: Any) -> str:
    return "sha256:" + hashlib.sha256(
        domain + canonical_json(value).encode("utf-8")
    ).hexdigest()


def file_sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


_RETENTION_DESTINATION_KEYS = {
    "repository",
    "ref",
    "path_prefix",
    "encryption_recipient",
    "retention_commitment_days",
}
_PRIVATE_EXPORT_CONTRACT_KEYS = {
    "schema_version",
    "destination",
    "uploader_identity",
    "importer_workflow_ref",
    "approval_authority",
    "approved_at",
}
_IMPORTER_WORKFLOW_REF = re.compile(
    r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/\.github/workflows/[A-Za-z0-9._-]+\.ya?ml@refs/heads/[A-Za-z0-9._/-]+$"
)
_REPOSITORY = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_GIT_REF = re.compile(r"^refs/heads/[A-Za-z0-9._/-]+$")
_PATH_PREFIX = re.compile(r"^[A-Za-z0-9._/-]{1,256}$")
# An age X25519 recipient. The private half never reaches CI: the writer only
# ever encrypts, so nothing in any workflow can read the evidence back.
_AGE_RECIPIENT = re.compile(r"^age1[0-9a-z]{58}$")
_UPLOADER_IDENTITY = re.compile(r"^[A-Za-z0-9._/\[\]-]{1,128}$")


def retention_binding_sha256(domain: str, value: str) -> str:
    """Return the digest the retention writer produces for one opaque binding.

    This must stay byte-identical to ``opaqueDigest`` in the Cloud repository's
    ``scripts/retain-execute-private-evidence.mjs``:

        sha256(`OpenAdapt ${domain} v1\\0` + value)

    Note that this is *not* ``opaque_binding_sha256`` above, which inserts the
    word ``acceptance`` into the separator and is used for other bindings.  The
    two produce different digests for the same domain, and only this one matches
    what a real certificate carries.
    """

    payload = f"OpenAdapt {domain} v1\0".encode("utf-8") + value.encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def retention_destination_approval_sha256(destination: Mapping[str, Any]) -> str:
    """Return the destination approval digest the retention writer checks.

    The Cloud writer refuses to retain anything unless
    ``EXECUTE_ACCEPTANCE_RETENTION_DESTINATION_APPROVAL_SHA256`` equals this
    value, so one approval governs both sides instead of two schemes that can
    disagree.
    """

    return retention_binding_sha256(
        "Execute acceptance retention destination",
        canonical_json({key: destination[key] for key in sorted(destination)}),
    )


def _validate_retention_destination(value: Any) -> dict[str, Any]:
    destination = _closed(value, _RETENTION_DESTINATION_KEYS, "retention destination")
    repository = _nonempty(destination["repository"], "retention destination repository")
    ref = _nonempty(destination["ref"], "retention destination ref")
    prefix = _nonempty(destination["path_prefix"], "retention destination path_prefix")
    recipient = _nonempty(
        destination["encryption_recipient"],
        "retention destination encryption_recipient",
    )
    if _REPOSITORY.fullmatch(repository) is None:
        raise AcceptanceError("retention destination repository is invalid")
    if _GIT_REF.fullmatch(ref) is None:
        raise AcceptanceError("retention destination ref is invalid")
    if (
        _PATH_PREFIX.fullmatch(prefix) is None
        or prefix.startswith("/")
        or prefix.endswith("/")
        or ".." in prefix
        or "//" in prefix
    ):
        raise AcceptanceError("retention destination path prefix is invalid")
    if _AGE_RECIPIENT.fullmatch(recipient) is None:
        raise AcceptanceError("retention destination encryption recipient is invalid")
    days = destination["retention_commitment_days"]
    if not isinstance(days, int) or isinstance(days, bool):
        raise AcceptanceError(
            "retention destination retention_commitment_days must be an integer"
        )
    policy = production_acceptance_policy()
    if not policy["minimum_retention_days"] <= days <= policy["maximum_retention_days"]:
        raise AcceptanceError("retention destination retention commitment is outside policy")
    return dict(destination)


def validate_private_export_contract(contract: Any) -> dict[str, Any]:
    """Return the derived expectations from one approved private-export contract.

    The contract carries the destination fields, never the digests.  Every digest
    the importer compares against is derived here, so an approval cannot assert a
    digest whose preimage nobody can see.
    """

    document = _closed(contract, _PRIVATE_EXPORT_CONTRACT_KEYS, "private export contract")
    if document["schema_version"] != PRIVATE_EXPORT_CONTRACT_SCHEMA:
        raise AcceptanceError("private export contract schema is not supported")
    destination = _validate_retention_destination(document["destination"])
    uploader_identity = _nonempty(
        document["uploader_identity"],
        "private export contract uploader_identity",
    )
    if _UPLOADER_IDENTITY.fullmatch(uploader_identity) is None:
        raise AcceptanceError("private export contract uploader identity is invalid")
    workflow_ref = document["importer_workflow_ref"]
    if (
        not isinstance(workflow_ref, str)
        or _IMPORTER_WORKFLOW_REF.fullmatch(workflow_ref) is None
    ):
        raise AcceptanceError("private export contract importer workflow ref is invalid")
    _nonempty(document["approval_authority"], "private export contract approval authority")
    approved_at = _timestamp(document["approved_at"], "private export contract approved_at")
    return {
        "destination_approval_sha256": retention_destination_approval_sha256(destination),
        "storage_identity_sha256": retention_binding_sha256(
            "retention store",
            destination["repository"],
        ),
        "encryption_recipient_sha256": retention_binding_sha256(
            "retention encryption recipient",
            destination["encryption_recipient"],
        ),
        "uploader_identity_sha256": retention_binding_sha256(
            "retention uploader",
            uploader_identity,
        ),
        "importer_workflow_ref": workflow_ref,
        "retention_commitment_days": destination["retention_commitment_days"],
        "approval_authority": document["approval_authority"],
        "approved_at": approved_at,
        "contract_sha256": canonical_sha256(document),
    }


def verify_importer_identity(
    contract: Mapping[str, Any],
    environ: Mapping[str, str],
) -> None:
    """Refuse unless this process is the workflow and ref the approval names.

    GitHub sets ``GITHUB_WORKFLOW_REF`` for the running job.  The evidence
    cannot influence it, which is the whole point: the approval authorises one
    importer, and an importer that cannot prove it is that one gets nothing.
    """

    actual = environ.get("GITHUB_WORKFLOW_REF")
    if not isinstance(actual, str) or not actual:
        raise AcceptanceError("importer workflow ref is absent")
    if actual != contract["importer_workflow_ref"]:
        raise AcceptanceError("importer is not the approved workflow and ref")


def verify_retention_against_contract(
    retention: Mapping[str, Any],
    contract: Mapping[str, Any],
) -> None:
    """Refuse unless the retained evidence went where the approval says.

    Without this the certificate names its own repository, its own key, and its
    own uploader, and the importer only checks that those look like digests.

    There is no period check here, and that is deliberate rather than an
    omission.  A git commit carries no expiry, so retention_commitment_days is a
    commitment this mechanism records and does not enforce.  Enforcing a number
    nothing can hold would be theatre.
    """

    for key in (
        "storage_identity_sha256",
        "encryption_recipient_sha256",
        "uploader_identity_sha256",
    ):
        if retention.get(key) != contract[key]:
            raise AcceptanceError(f"retained evidence {key} is not the approved identity")


def opaque_binding_sha256(domain: str, value: str) -> str:
    payload = f"OpenAdapt acceptance {domain} v1\0".encode("utf-8") + value.encode(
        "utf-8"
    )
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def privacy_safe_campaign_label_sha256(
    label_kind: str,
    campaign_id: str,
    value: str,
) -> str:
    """Bind a private task or condition label without publishing the label."""

    domain = f"OpenAdapt acceptance private {label_kind} identity v1\0".encode(
        "utf-8"
    )
    payload = canonical_json(
        {
            "campaign_id": campaign_id,
            "value": value,
        }
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(domain + payload).hexdigest()


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise AcceptanceError(f"{label} must be an object")
    return value


def _string_set(value: Any, label: str) -> set[str]:
    if (
        not isinstance(value, list)
        or not all(isinstance(item, str) and item for item in value)
        or len(value) != len(set(value))
    ):
        raise AcceptanceError(f"{label} must be a unique string list")
    return set(value)


def _closed(value: Any, expected: set[str], label: str) -> Mapping[str, Any]:
    mapping = _mapping(value, label)
    keys = set(mapping)
    if keys != expected:
        missing = sorted(expected - keys)
        extra = sorted(keys - expected)
        raise AcceptanceError(f"{label} keys differ: missing={missing}, extra={extra}")
    return mapping


def _nonempty(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AcceptanceError(f"{label} must be a non-empty string")
    return value


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise AcceptanceError(f"{label} must be a sha256 digest")
    return value


def _unprefixed_digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or _UNPREFIXED_SHA256.fullmatch(value) is None:
        raise AcceptanceError(f"{label} must be an unprefixed sha256 digest")
    return value


def _canonical_uuid(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise AcceptanceError(f"{label} must be a canonical UUID")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise AcceptanceError(f"{label} must be a canonical UUID") from exc
    if (
        str(parsed) != value
        or parsed.variant != uuid.RFC_4122
        or parsed.version not in range(1, 9)
    ):
        raise AcceptanceError(f"{label} must be a canonical UUID")
    return value


def _validate_desktop_runtime_detail(value: Mapping[str, Any], *, label: str) -> None:
    if not isinstance(value["desktop_version"], str) or _SEMVER.fullmatch(
        value["desktop_version"]
    ) is None:
        raise AcceptanceError(f"{label} desktop version is not exact")
    if not isinstance(value["desktop_release_commit"], str) or _HEX_40.fullmatch(
        value["desktop_release_commit"]
    ) is None:
        raise AcceptanceError(f"{label} desktop commit is not exact")
    for key in ("desktop_artifact_sha256", "runtime_boundary_sha256"):
        _unprefixed_digest(value[key], f"{label} {key}")


def _validate_evidence_identity(
    value: Any,
    *,
    admission_payload: Mapping[str, Any],
    campaign: Mapping[str, Any],
) -> dict[str, Any]:
    identity = dict(
        _closed(value, _EVIDENCE_IDENTITY_KEYS, "qualification evidence identity")
    )
    if identity["schema_version"] != EVIDENCE_IDENTITY_SCHEMA:
        raise AcceptanceError("qualification evidence identity schema is not supported")
    registry_revision = identity["qualification_signer_registry_revision"]
    if (
        not isinstance(registry_revision, int)
        or isinstance(registry_revision, bool)
        or registry_revision < 1
    ):
        raise AcceptanceError(
            "qualification evidence identity signer registry revision is invalid"
        )
    runtime_value = _mapping(
        identity["runtime_build_identity"],
        "qualification runtime build identity",
    )
    runtime = _closed(
        runtime_value,
        _RUNTIME_BUILD_COMMON_KEYS,
        "qualification runtime build identity",
    )
    selected = [
        name
        for name in ("managed_browser", "native_desktop", "remote_display")
        if runtime[name] is not None
    ]
    if len(selected) != 1:
        raise AcceptanceError(
            "qualification runtime build identity must select exactly one substrate detail"
        )
    selected_detail = selected[0]
    substrate = runtime["substrate"]
    if runtime["schema_version"] != RUNTIME_BUILD_IDENTITY_SCHEMA:
        raise AcceptanceError("qualification runtime build identity schema is not supported")
    if not isinstance(runtime["flow_version"], str) or _SEMVER.fullmatch(
        runtime["flow_version"]
    ) is None:
        raise AcceptanceError("qualification evidence identity Flow version is not exact")
    if not isinstance(runtime["flow_release_commit"], str) or _HEX_40.fullmatch(
        runtime["flow_release_commit"]
    ) is None:
        raise AcceptanceError("qualification evidence identity Flow commit is not exact")
    _nonempty(runtime["runner_build"], "qualification evidence identity runner build")
    for key in (
        "flow_wheel_sha256",
        "runtime_manifest_sha256",
        "runner_artifact_sha256",
    ):
        _unprefixed_digest(runtime[key], f"qualification runtime build identity {key}")
    if selected_detail == "managed_browser":
        if substrate != "web":
            raise AcceptanceError("qualification managed-browser substrate is not web")
        detail = _closed(
            runtime[selected_detail],
            _MANAGED_BROWSER_DETAIL_KEYS,
            "qualification managed-browser identity",
        )
        if not isinstance(detail["playwright_version"], str) or _SEMVER.fullmatch(
            detail["playwright_version"]
        ) is None:
            raise AcceptanceError(
                "qualification evidence identity Playwright version is not exact"
            )
        browser_image = _nonempty(
            detail["browser_base_image"],
            "qualification evidence identity browser image",
        )
        if _PINNED_IMAGE.fullmatch(browser_image) is None:
            raise AcceptanceError(
                "qualification evidence identity browser image is not digest-pinned"
            )
    elif selected_detail == "native_desktop":
        detail = _closed(
            runtime[selected_detail],
            _NATIVE_DESKTOP_DETAIL_KEYS,
            "qualification native-desktop identity",
        )
        _validate_desktop_runtime_detail(detail, label="qualification native-desktop identity")
        if substrate not in {"windows", "macos", "linux"} or detail["os_family"] != substrate:
            raise AcceptanceError("qualification native-desktop substrate differs from OS")
    else:
        detail = _closed(
            runtime[selected_detail],
            _REMOTE_DISPLAY_DETAIL_KEYS,
            "qualification remote-display identity",
        )
        _validate_desktop_runtime_detail(detail, label="qualification remote-display identity")
        if substrate not in {"rdp", "citrix"} or detail["transport"] != substrate:
            raise AcceptanceError("qualification remote-display transport differs")
        if detail["runner_os_family"] not in {"windows", "linux"}:
            raise AcceptanceError("qualification remote-display runner OS is invalid")

    for key in {
        "tenant_id",
        "workflow_id",
        "workflow_version_id",
        "bundle_version_id",
        "runtime_validation_id",
        "admission_id",
        "campaign_id",
    }:
        _canonical_uuid(identity[key], f"qualification evidence identity {key}")
    for key in {
        "evidence_runner_signer_sha256",
        "workflow_digest",
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
        "admission_policy_sha256",
        "campaign_contract_sha256",
        "oracle_contract_sha256",
        "qualification_signer_registry_sha256",
    }:
        _unprefixed_digest(identity[key], f"qualification evidence identity {key}")

    for key in (
        "tenant_id",
        "workflow_id",
        "workflow_version_id",
        "bundle_version_id",
        "bundle_artifact_sha256",
        "bundle_content_digest",
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
        "runtime_validation_id",
        "admission_id",
    ):
        if identity[key] != admission_payload[key]:
            raise AcceptanceError(
                f"qualification evidence identity {key} differs from admission"
            )
    if identity["campaign_id"] != campaign["campaign_id"]:
        raise AcceptanceError("qualification evidence identity campaign differs from campaign")
    if identity["admission_policy_sha256"] != admission_policy_sha256():
        raise AcceptanceError("qualification evidence identity admission policy differs")
    if identity["campaign_contract_sha256"] != canonical_sha256(
        campaign["qualification_contract"]
    ).removeprefix("sha256:"):
        raise AcceptanceError("qualification evidence identity campaign contract differs")
    if identity["oracle_contract_sha256"] != canonical_sha256(
        campaign["oracle_contract"]
    ).removeprefix("sha256:"):
        raise AcceptanceError("qualification evidence identity oracle contract differs")
    for key, expected in {
        "qualification_campaign_schema": CAMPAIGN_SCHEMA,
        "qualification_trial_schema": TRIAL_SCHEMA,
        "qualification_receipt_schema": RECEIPT_SCHEMA,
    }.items():
        if identity[key] != expected:
            raise AcceptanceError(f"qualification evidence identity {key} differs")
    return identity


def _whole_second_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or _WHOLE_SECOND_UTC.fullmatch(value) is None:
        raise AcceptanceError(f"{label} must be a whole-second UTC timestamp")
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00").astimezone(timezone.utc)
    except ValueError as exc:
        raise AcceptanceError(f"{label} is invalid") from exc


def _validate_signer_registry(
    value: Any,
) -> tuple[dict[str, Mapping[str, Any]], datetime, datetime]:
    registry = _closed(value, _SIGNER_REGISTRY_KEYS, "qualification signer registry")
    if registry["schema_version"] != SIGNER_REGISTRY_SCHEMA:
        raise AcceptanceError("qualification signer registry schema is not supported")
    revision = registry["revision"]
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise AcceptanceError("qualification signer registry revision is invalid")
    generated_at = _whole_second_timestamp(
        registry["generated_at"], "qualification signer registry generated_at"
    )
    expires_at = _whole_second_timestamp(
        registry["expires_at"], "qualification signer registry expires_at"
    )
    if expires_at <= generated_at or expires_at > generated_at + timedelta(days=7):
        raise AcceptanceError("qualification signer registry lifetime is invalid")
    entries = registry["signers"]
    if not isinstance(entries, list) or not entries:
        raise AcceptanceError("qualification signer registry signers must be non-empty")
    verified: dict[str, Mapping[str, Any]] = {}
    ordered_key_ids: list[str] = []
    for index, value in enumerate(entries):
        entry = _closed(
            value,
            _SIGNER_REGISTRY_ENTRY_KEYS,
            f"qualification signer registry entry {index}",
        )
        if entry["algorithm"] != "ed25519":
            raise AcceptanceError("qualification signer registry algorithm is not Ed25519")
        key_id = entry["key_id"]
        if not isinstance(key_id, str) or _ADMISSION_KEY_ID.fullmatch(key_id) is None:
            raise AcceptanceError("qualification signer registry key ID is invalid")
        if key_id in verified:
            raise AcceptanceError("qualification signer registry key ID is duplicate")
        public_key = _canonical_base64(
            entry["public_key"], 32, "qualification signer registry public key"
        )
        derived = "qa-ed25519-" + hashlib.sha256(public_key).hexdigest()[:16]
        if key_id != derived:
            raise AcceptanceError("qualification signer registry key differs from key ID")
        if entry["allowed_workflows"] != [ADMISSION_ISSUER_WORKFLOW]:
            raise AcceptanceError("qualification signer registry workflow trust is not exact")
        if entry["allowed_ref_prefixes"] != [ADMISSION_REF_PREFIX]:
            raise AcceptanceError("qualification signer registry ref trust is not exact")
        if entry["status"] == "active":
            if entry["revoked_at"] is not None:
                raise AcceptanceError("active qualification signer has a revocation time")
        elif entry["status"] == "revoked":
            revoked_at = _whole_second_timestamp(
                entry["revoked_at"], "qualification signer registry revoked_at"
            )
            if revoked_at > generated_at:
                raise AcceptanceError("qualification signer revocation postdates registry")
        else:
            raise AcceptanceError("qualification signer registry status is invalid")
        verified[key_id] = entry
        ordered_key_ids.append(key_id)
    if ordered_key_ids != sorted(ordered_key_ids):
        raise AcceptanceError("qualification signer registry entries are not ordered")
    return verified, generated_at, expires_at


def _canonical_base64(value: Any, length: int, label: str) -> bytes:
    if not isinstance(value, str):
        raise AcceptanceError(f"{label} must be canonical base64")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AcceptanceError(f"{label} must be canonical base64") from exc
    if len(decoded) != length or base64.b64encode(decoded).decode("ascii") != value:
        raise AcceptanceError(f"{label} must be canonical base64")
    return decoded


def _verify_ed25519(public_key: bytes, signature: bytes, message: bytes) -> None:
    """Verify Ed25519 with the project library or the OpenSSL 3 CI fallback."""

    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        # SubjectPublicKeyInfo prefix for a raw Ed25519 public key (RFC 8410).
        public_der = bytes.fromhex("302a300506032b6570032100") + public_key
        with tempfile.TemporaryDirectory(prefix="openadapt-admission-") as directory:
            root = Path(directory)
            public_path = root / "public.der"
            signature_path = root / "signature.bin"
            message_path = root / "message.bin"
            public_path.write_bytes(public_der)
            signature_path.write_bytes(signature)
            message_path.write_bytes(message)
            try:
                verified = subprocess.run(
                    [
                        "openssl",
                        "pkeyutl",
                        "-verify",
                        "-pubin",
                        "-inkey",
                        str(public_path),
                        "-keyform",
                        "DER",
                        "-rawin",
                        "-in",
                        str(message_path),
                        "-sigfile",
                        str(signature_path),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                )
            except OSError as exc:
                raise AcceptanceError(
                    "qualification signature verifier is unavailable"
                ) from exc
        if verified.returncode != 0:
            raise AcceptanceError("qualification admission signature is invalid")
        return
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, message)
    except (InvalidSignature, ValueError) as exc:
        raise AcceptanceError("qualification admission signature is invalid") from exc


def validate_qualification_admission(
    envelope: Mapping[str, Any],
    campaign: Mapping[str, Any],
    *,
    trusted_signers: Mapping[str, Any],
    revoked_admission_ids: set[str] | frozenset[str] = frozenset(),
    revoked_signer_key_ids: set[str] | frozenset[str] = frozenset(),
    now: datetime | None = None,
) -> dict[str, Any]:
    """Verify the external qualification authority and its exact campaign."""

    trusted_signers = _mapping(trusted_signers, "qualification signer trust registry")
    signer_entries, registry_generated_at, registry_expires_at = (
        _validate_signer_registry(trusted_signers)
    )
    if not isinstance(revoked_admission_ids, (set, frozenset)) or not all(
        isinstance(value, str) and value for value in revoked_admission_ids
    ):
        raise AcceptanceError("qualification admission revocations must be a string set")
    if not isinstance(revoked_signer_key_ids, (set, frozenset)) or not all(
        isinstance(value, str) and value for value in revoked_signer_key_ids
    ):
        raise AcceptanceError("qualification signer revocations must be a string set")
    campaign = _closed(campaign, _CAMPAIGN_KEYS, "campaign")
    envelope = _closed(envelope, {"payload", "algorithm", "signature"}, "admission")
    if envelope["algorithm"] != "ed25519":
        raise AcceptanceError("qualification admission algorithm is not ed25519")
    payload = _closed(
        envelope["payload"],
        _ADMISSION_PAYLOAD_KEYS,
        "qualification admission payload",
    )
    if payload["schema_version"] != ADMISSION_SCHEMA:
        raise AcceptanceError("qualification admission schema is not supported")
    for key in _ADMISSION_UUID_KEYS:
        _canonical_uuid(payload[key], f"qualification admission {key}")
    if payload["admission_id"] == payload["runtime_validation_id"]:
        raise AcceptanceError(
            "qualification admission ID equals the runtime-validation ID"
        )
    for key in _ADMISSION_DIGEST_KEYS:
        _unprefixed_digest(payload[key], f"qualification admission {key}")

    evidence_identity = _validate_evidence_identity(
        payload["evidence_identity"],
        admission_payload=payload,
        campaign=campaign,
    )
    if evidence_identity["qualification_signer_registry_sha256"] != (
        qualification_signer_registry_sha256(trusted_signers)
    ):
        raise AcceptanceError("qualification signer registry differs from admission")
    if evidence_identity["qualification_signer_registry_revision"] != trusted_signers[
        "revision"
    ]:
        raise AcceptanceError("qualification signer registry revision differs from admission")
    identity_digest = evidence_identity_sha256(evidence_identity)
    if campaign["admission_id"] != payload["admission_id"]:
        raise AcceptanceError("campaign admission ID differs from admission")
    if campaign["runtime_validation_id"] != payload["runtime_validation_id"]:
        raise AcceptanceError("campaign runtime-validation ID differs from admission")
    if campaign["evidence_identity_sha256"] != identity_digest:
        raise AcceptanceError("campaign evidence identity differs from admission")

    issuer = _closed(
        payload["issuer"],
        {"key_id", "workflow", "ref"},
        "qualification admission issuer",
    )
    key_id = issuer["key_id"]
    if not isinstance(key_id, str) or _ADMISSION_KEY_ID.fullmatch(key_id) is None:
        raise AcceptanceError("qualification admission signer key ID is invalid")
    if key_id in revoked_signer_key_ids:
        raise AcceptanceError("qualification admission signer is revoked")
    if issuer["workflow"] != ADMISSION_ISSUER_WORKFLOW:
        raise AcceptanceError("qualification admission issuer workflow is not approved")
    if not isinstance(issuer["ref"], str) or _ADMISSION_REF.fullmatch(issuer["ref"]) is None:
        raise AcceptanceError("qualification admission issuer ref is not approved")

    trust = signer_entries.get(key_id)
    if trust is None or trust["status"] != "active":
        raise AcceptanceError("qualification admission signer is not trusted")
    workflows = trust["allowed_workflows"]
    ref_prefixes = trust["allowed_ref_prefixes"]
    if workflows != [ADMISSION_ISSUER_WORKFLOW] and workflows != (ADMISSION_ISSUER_WORKFLOW,):
        raise AcceptanceError("qualification signer workflow trust is not exact")
    if ref_prefixes != [ADMISSION_REF_PREFIX] and ref_prefixes != (ADMISSION_REF_PREFIX,):
        raise AcceptanceError("qualification signer ref trust is not exact")
    public_key = _canonical_base64(
        trust["public_key"],
        32,
        "qualification signer public key",
    )
    derived_key_id = "qa-ed25519-" + hashlib.sha256(public_key).hexdigest()[:16]
    if key_id != derived_key_id:
        raise AcceptanceError("qualification signer key ID differs from its public key")
    signature = _canonical_base64(
        envelope["signature"],
        64,
        "qualification admission signature",
    )
    _verify_ed25519(
        public_key,
        signature,
        ADMISSION_SIGNATURE_DOMAIN + canonical_json(payload).encode("utf-8"),
    )

    issued_at = _whole_second_timestamp(
        payload["issued_at"], "qualification admission issued_at"
    )
    not_before = _whole_second_timestamp(
        payload["not_before"], "qualification admission not_before"
    )
    expires_at = _whole_second_timestamp(
        payload["expires_at"], "qualification admission expires_at"
    )
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise AcceptanceError("qualification import time must include a timezone")
    current = current.astimezone(timezone.utc)
    if not issued_at - timedelta(minutes=5) <= not_before <= issued_at + timedelta(minutes=5):
        raise AcceptanceError("qualification admission start is not bound to issuance")
    if expires_at <= not_before or expires_at > issued_at + timedelta(days=30):
        raise AcceptanceError("qualification admission lifetime is invalid")
    if issued_at > current + timedelta(minutes=5):
        raise AcceptanceError("qualification admission is future-issued")
    if current < not_before:
        raise AcceptanceError("qualification admission is not active")
    if current >= expires_at:
        raise AcceptanceError("qualification admission has expired")
    if not registry_generated_at <= issued_at < registry_expires_at:
        raise AcceptanceError("qualification signer registry was not active at issuance")
    if payload["admission_id"] in revoked_admission_ids:
        raise AcceptanceError("qualification admission is revoked")

    campaign_binding = _closed(
        payload["campaign"],
        _ADMISSION_CAMPAIGN_KEYS,
        "qualification admission campaign",
    )
    if campaign_binding["campaign_id"] != campaign["campaign_id"]:
        raise AcceptanceError("qualification admission campaign ID differs")
    for key in (
        "artifact_sha256",
        "contract_sha256",
        "outcomes_sha256",
        "oracle_contract_sha256",
    ):
        _unprefixed_digest(
            campaign_binding[key],
            f"qualification admission campaign {key}",
        )
    if campaign_binding["decision"] != "admitted":
        raise AcceptanceError("qualification campaign was not admitted")
    campaign_artifact_digest = canonical_sha256(campaign).removeprefix("sha256:")
    contract_digest = canonical_sha256(campaign["qualification_contract"]).removeprefix(
        "sha256:"
    )
    outcomes_projection = {
        "conditions": campaign["conditions"],
        "invariants": campaign["invariants"],
        "excluded_trials": campaign["excluded_trials"],
    }
    outcomes_digest = canonical_sha256(outcomes_projection).removeprefix("sha256:")
    oracle_digest = canonical_sha256(campaign["oracle_contract"]).removeprefix("sha256:")
    if campaign_binding["artifact_sha256"] != campaign_artifact_digest:
        raise AcceptanceError("qualification admission campaign artifact digest differs")
    if campaign_binding["contract_sha256"] != contract_digest:
        raise AcceptanceError("qualification admission campaign contract digest differs")
    if campaign_binding["outcomes_sha256"] != outcomes_digest:
        raise AcceptanceError("qualification admission campaign outcomes digest differs")
    oracle = _mapping(campaign["oracle_contract"], "campaign oracle contract")
    if campaign_binding["oracle_id"] != oracle.get("schema_version"):
        raise AcceptanceError("qualification admission campaign oracle identity differs")
    if campaign_binding["oracle_contract_sha256"] != oracle_digest:
        raise AcceptanceError("qualification admission campaign oracle digest differs")

    expected_tasks = []
    contract = _mapping(campaign["qualification_contract"], "qualification contract")
    task_id = contract.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise AcceptanceError("qualification campaign task is missing")
    conditions = campaign["conditions"]
    if not isinstance(conditions, list) or not conditions:
        raise AcceptanceError("qualification campaign conditions must be a non-empty list")
    for index, condition_value in enumerate(conditions):
        condition = _closed(
            condition_value,
            _CONDITION_KEYS,
            f"qualification campaign condition {index}",
        )
        required_trials = condition["required_trials"]
        trials = condition["trials"]
        if (
            not isinstance(required_trials, int)
            or isinstance(required_trials, bool)
            or required_trials < 3
            or not isinstance(trials, list)
        ):
            raise AcceptanceError("qualification admission task inventory is malformed")
        expected_tasks.append(
            {
                "task": task_id,
                "condition": condition["condition_id"],
                "required_trials": required_trials,
                "observed_trials": len(trials),
            }
        )
    expected_tasks.sort(key=lambda value: (value["task"], value["condition"]))
    tasks = campaign_binding["tasks"]
    if not isinstance(tasks, list):
        raise AcceptanceError("qualification admission task inventory must be a list")
    if tasks != expected_tasks:
        raise AcceptanceError("qualification admission task inventory differs from campaign rows")
    taxonomy = campaign_binding["failure_taxonomy"]
    expected_taxonomy = sorted(_FAILURE_TAXONOMY)
    if taxonomy != expected_taxonomy:
        raise AcceptanceError("qualification admission failure taxonomy is incomplete")
    campaign_generated_at = _whole_second_timestamp(
        campaign["generated_at"],
        "qualification campaign generated_at",
    )
    if campaign_generated_at > issued_at:
        raise AcceptanceError("qualification admission predates its campaign")
    return {
        "artifact_sha256": canonical_sha256(envelope),
        "signer_key_id": key_id,
        "issuer_workflow": issuer["workflow"],
        "issuer_ref": issuer["ref"],
        "expires_at": payload["expires_at"],
        "evidence_identity_sha256": identity_digest,
    }


def _count(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise AcceptanceError(f"{label} must be a non-negative integer")
    return value


def _timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise AcceptanceError(f"{label} must be a canonical UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise AcceptanceError(f"{label} must be a canonical UTC timestamp") from exc
    if parsed.tzinfo != timezone.utc:
        raise AcceptanceError(f"{label} must be a canonical UTC timestamp")
    milliseconds = parsed.microsecond // 1000
    expected = parsed.replace(microsecond=milliseconds * 1000).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")
    if value != expected:
        raise AcceptanceError(f"{label} must use canonical millisecond UTC form")
    return parsed


def _observed_timestamp(value: Any, label: str) -> datetime:
    """Read one RFC 3339 time that an external verifier wrote.

    ``_timestamp`` enforces the canonical millisecond UTC form that OpenAdapt
    itself writes, and no external tool owes us that form.  GitHub CLI
    serializes a Go ``time.Time``, so it emits the offset of the runner's own
    location and omits zero sub-second digits: a real
    ``gh attestation verify --format json`` run emits
    ``2026-07-28T03:33:58-04:00``.  Require an explicit offset, then normalize
    to UTC, so the fifteen-minute issuance binding compares real instants.
    """

    if not isinstance(value, str):
        raise AcceptanceError(f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AcceptanceError(f"{label} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AcceptanceError(f"{label} has no timezone")
    return parsed.astimezone(timezone.utc)


def _validate_certificate(
    certificate: Mapping[str, Any],
    *,
    now: datetime,
    expected_cloud_source_commit: str,
) -> dict[str, Any]:
    certificate = _closed(certificate, _CERTIFICATE_KEYS, "certificate")
    if certificate["schema_version"] != CERTIFICATE_SCHEMA:
        raise AcceptanceError("certificate schema is not supported")
    if certificate["verdict"] != "passed":
        raise AcceptanceError("certificate verdict is not passed")
    if certificate["claim_scope"] != "single_authenticated_qualified_browser_transaction":
        raise AcceptanceError("certificate claim scope is not the live browser transaction")

    product = _closed(certificate["product"], _PRODUCT_KEYS, "certificate.product")
    cloud = _closed(product["cloud"], _CLOUD_KEYS, "certificate.product.cloud")
    flow = _closed(product["flow"], _FLOW_KEYS, "certificate.product.flow")
    runtime = _closed(
        product["managed_runtime"],
        _RUNTIME_KEYS,
        "certificate.product.managed_runtime",
    )
    if not isinstance(cloud["source_commit"], str) or _HEX_40.fullmatch(
        cloud["source_commit"]
    ) is None:
        raise AcceptanceError("certificate Cloud source commit is not exact")
    if _HEX_40.fullmatch(expected_cloud_source_commit) is None:
        raise AcceptanceError("approved Cloud source commit is not exact")
    if cloud["source_commit"] != expected_cloud_source_commit:
        raise AcceptanceError("certificate Cloud source commit is not the approved commit")
    _digest(cloud["target_build_sha256"], "certificate Cloud target build")
    if not isinstance(flow["version"], str) or _SEMVER.fullmatch(flow["version"]) is None:
        raise AcceptanceError("certificate Flow version is not exact")
    if not isinstance(flow["release_commit"], str) or _HEX_40.fullmatch(
        flow["release_commit"]
    ) is None:
        raise AcceptanceError("certificate Flow release commit is not exact")
    _digest(flow["wheel_sha256"], "certificate Flow wheel")
    _digest(runtime["manifest_sha256"], "certificate managed-runtime manifest")
    _digest(runtime["runner_artifact_sha256"], "certificate runner artifact")
    _nonempty(runtime["runner_build"], "certificate runner build")
    if runtime["substrate"] != "web":
        raise AcceptanceError("certificate managed-runtime substrate is not web")
    if not isinstance(runtime["playwright_version"], str) or _SEMVER.fullmatch(
        runtime["playwright_version"]
    ) is None:
        raise AcceptanceError("certificate Playwright version is not exact")
    browser_image = _nonempty(runtime["browser_base_image"], "certificate browser image")
    if _PINNED_IMAGE.fullmatch(browser_image) is None:
        raise AcceptanceError("certificate browser image is not digest-pinned")

    qualification = _closed(
        certificate["qualification"],
        _QUALIFICATION_KEYS,
        "certificate.qualification",
    )
    for key in (
        "qualification_admission_sha256",
        "campaign_artifact_sha256",
        "campaign_contract_sha256",
        "campaign_outcomes_sha256",
        "oracle_contract_sha256",
        "runtime_validation_id_sha256",
        "admission_id_sha256",
        "campaign_id_sha256",
        "workflow_version_id_sha256",
        "workflow_digest",
        "environment_digest",
        "evidence_identity_sha256",
    ):
        _digest(qualification[key], f"certificate qualification {key}")
    for key in (
        "task_count",
        "condition_count",
        "required_trial_count",
        "observed_trial_count",
        "minimum_trials_per_condition",
    ):
        value = _count(qualification[key], f"certificate qualification {key}")
        if value < 1:
            raise AcceptanceError(f"certificate qualification {key} must be positive")
    if (
        qualification["minimum_trials_per_condition"] < 3
        or qualification["condition_count"] < qualification["task_count"]
        or qualification["observed_trial_count"] < qualification["required_trial_count"]
    ):
        raise AcceptanceError("certificate qualification counts are not internally consistent")
    admission_signer = _closed(
        qualification["admission_signer"],
        _ADMISSION_SIGNER_KEYS,
        "certificate qualification admission signer",
    )
    if (
        admission_signer["algorithm"] != "ed25519"
        or not isinstance(admission_signer["key_id"], str)
        or _ADMISSION_KEY_ID.fullmatch(admission_signer["key_id"]) is None
        or admission_signer["issuer_workflow"] != ADMISSION_ISSUER_WORKFLOW
        or not isinstance(admission_signer["issuer_ref"], str)
        or _ADMISSION_REF.fullmatch(admission_signer["issuer_ref"]) is None
    ):
        raise AcceptanceError("certificate qualification admission signer is not approved")
    if qualification["runtime_validation_id_sha256"] == qualification[
        "admission_id_sha256"
    ]:
        raise AcceptanceError("runtime-validation and qualification bindings are not separated")

    transaction = _closed(
        certificate["transaction"],
        _TRANSACTION_KEYS,
        "certificate.transaction",
    )
    for key, value in transaction.items():
        _digest(value, f"certificate transaction {key}")
    if transaction["accepted_response_sha256"] != transaction["duplicate_response_sha256"]:
        raise AcceptanceError("idempotent accepted responses have different digests")

    outcomes = _closed(certificate["outcomes"], _OUTCOME_KEYS, "certificate.outcomes")
    if any(value != "verified" for value in outcomes.values()):
        raise AcceptanceError("certificate outcomes are not all verified")

    contracts = _closed(certificate["contracts"], _CONTRACT_KEYS, "certificate.contracts")
    if any(value is not True for value in contracts.values()):
        raise AcceptanceError("certificate contracts are not all verified")

    identities = _closed(certificate["identities"], _IDENTITY_KEYS, "certificate.identities")
    if identities["signer_fingerprint_scheme"] != "sha256-ed25519-public-key-raw-v1":
        raise AcceptanceError("certificate signer fingerprint scheme is not supported")
    producer = _closed(identities["producer"], _PRODUCER_KEYS, "certificate producer")
    verifier = _closed(identities["verifier"], _VERIFIER_KEYS, "certificate verifier")
    if producer != {
        "kind": "github_oidc_attested_workflow",
        "repository": CLOUD_REPOSITORY,
        "workflow": CLOUD_WORKFLOW,
        "source_ref": "refs/heads/main",
        "run_id": producer["run_id"],
        "run_attempt": producer["run_attempt"],
    }:
        raise AcceptanceError("certificate producer identity is not the reviewed Cloud workflow")
    for field in ("run_id", "run_attempt"):
        if not isinstance(producer[field], str) or _POSITIVE_DECIMAL.fullmatch(
            producer[field]
        ) is None:
            raise AcceptanceError(f"certificate producer {field} is invalid")
    if verifier != {
        "kind": "github_artifact_attestation",
        "repository": CLOUD_REPOSITORY,
        "workflow": CLOUD_WORKFLOW,
        "certificate_identity": CLOUD_CERTIFICATE_IDENTITY,
        "oidc_issuer": GITHUB_OIDC_ISSUER,
        "hosted_runner_required": True,
    }:
        raise AcceptanceError("certificate verifier identity is not the reviewed Cloud workflow")
    observer_signer = _digest(
        identities["target_observer_signer_sha256"],
        "certificate target observer signer",
    )
    attestation_signer = _digest(
        identities["target_attestation_signer_sha256"],
        "certificate target attestation signer",
    )
    runner_signer = _digest(
        identities["evidence_runner_signer_sha256"],
        "certificate evidence runner signer",
    )
    _digest(identities["organization_id_sha256"], "certificate organization identity")
    _digest(identities["workflow_id_sha256"], "certificate workflow identity")
    if len({observer_signer, attestation_signer, runner_signer}) != 3:
        raise AcceptanceError(
            "target attestation, observer, and runner signing identities are not separate"
        )

    retention = _closed(certificate["retention"], _RETENTION_KEYS, "certificate.retention")
    for key in (
        "ciphertext_sha256",
        "candidate_sha256",
        "private_envelope_sha256",
        "store_attestation_sha256",
        "storage_identity_sha256",
        "private_locator_version_sha256",
        "encryption_recipient_sha256",
        "uploader_identity_sha256",
    ):
        _digest(retention[key], f"certificate retention {key}")
    if not isinstance(retention["receipt_id"], str) or re.fullmatch(
        r"retention:[a-f0-9]{32}", retention["receipt_id"]
    ) is None:
        raise AcceptanceError("certificate retention receipt ID is invalid")
    for key in (
        "visibility_verified",
        "private_locator_recorded",
    ):
        if retention[key] is not True:
            raise AcceptanceError(f"certificate retention {key} is not verified")
    if retention["provenance_attestation"] != RETENTION_PROVENANCE_ROUTE:
        raise AcceptanceError("certificate provenance attestation version is not reviewed")
    acceptance_verified_at = _timestamp(
        retention["acceptance_verified_at"],
        "certificate retention acceptance_verified_at",
    )
    retained_at = _timestamp(retention["retained_at"], "certificate retention retained_at")
    if not acceptance_verified_at <= retained_at:
        raise AcceptanceError("certificate retention chronology is invalid")
    if now.tzinfo is None:
        raise AcceptanceError("import time must include a timezone")
    if retained_at > now.astimezone(timezone.utc):
        raise AcceptanceError("certificate retention is dated in the future")

    return {
        "product": {"cloud": dict(cloud), "flow": dict(flow), "managed_runtime": dict(runtime)},
        "qualification": dict(qualification),
        "retention": dict(retention),
    }


def _case_contract(contract: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    contract = _closed(
        contract,
        {
            "schema_version",
            "task_id",
            "classification",
            "target",
            "runtime",
            "effect_contract",
            "required_capabilities",
            "cases",
            "fault_case_receipts",
            "cleanup",
            "post_campaign_invariant",
        },
        "qualification contract",
    )
    if contract["schema_version"] != "openadapt.execute-acceptance-qualification/v1":
        raise AcceptanceError("qualification contract schema is not supported")
    if contract["task_id"] != "execute-acceptance":
        raise AcceptanceError("qualification task is not the reviewed Execute acceptance task")
    if contract["classification"] != "private-product-owned-synthetic":
        raise AcceptanceError("qualification contract classification is not reviewed")
    target = _closed(
        contract["target"],
        {
            "entry_url",
            "browser_only_write",
            "reference_input_selector",
            "transaction_ref_parameter",
        },
        "qualification target",
    )
    if target != {
        "entry_url": "https://app.openadapt.ai/execute-acceptance/target",
        "browser_only_write": True,
        "reference_input_selector": "input[data-testid=transaction-ref]",
        "transaction_ref_parameter": "transaction_ref",
    }:
        raise AcceptanceError("qualification target is not the reviewed synthetic target")
    runtime = _closed(contract["runtime"], {"openadapt_flow"}, "qualification runtime")
    flow_runtime = _closed(
        runtime["openadapt_flow"],
        {
            "release_identity_required",
            "release_channel",
            "healthy_path_model_call_limit",
        },
        "qualification Flow runtime",
    )
    if flow_runtime["release_identity_required"] is not True:
        raise AcceptanceError("qualification does not require a Flow release identity")
    _nonempty(flow_runtime["release_channel"], "qualification Flow release channel")
    if flow_runtime["healthy_path_model_call_limit"] != 0:
        raise AcceptanceError("qualification healthy path permits model calls")
    effect = _closed(
        contract["effect_contract"],
        {"kind", "match", "required_strength", "observer", "observer_request"},
        "qualification effect contract",
    )
    match = _closed(effect["match"], {"transaction_ref"}, "qualification effect match")
    observer_request = _closed(
        effect["observer_request"],
        {"method", "path", "records_key", "auth"},
        "qualification observer request",
    )
    if (
        effect["kind"] != "record_written"
        or match["transaction_ref"] != "$param:transaction_ref"
        or effect["required_strength"] != "independent_system_of_record"
        or effect["observer"] != "separate read-only synthetic transaction observer"
        or observer_request
        != {
            "method": "GET",
            "path": (
                "/api/internal/execute-acceptance/observer?"
                "transaction_ref={transaction_ref}"
            ),
            "records_key": "records",
            "auth": "bearer environment reference",
        }
    ):
        raise AcceptanceError("qualification effect contract is not independently verified")
    if contract["required_capabilities"] != [
        "browser_observation",
        "browser_actuation",
        "identity_verification",
        "independent_effect_verification",
    ]:
        raise AcceptanceError("qualification capability contract is not reviewed")

    fault_receipts = _closed(
        contract["fault_case_receipts"],
        {"required", "signer", "case_kinds"},
        "qualification fault-receipt contract",
    )
    if fault_receipts != {
        "required": True,
        "signer": "named trusted fault driver",
        "case_kinds": [
            "wrong_identity",
            "stale_identity",
            "ambiguity",
            "missing_effect",
            "weak_effect",
        ],
    }:
        raise AcceptanceError("qualification fault-receipt contract is not reviewed")
    cleanup = _closed(
        contract["cleanup"],
        {"required", "scope", "required_evidence", "failure_outcome"},
        "qualification cleanup contract",
    )
    if cleanup != {
        "required": True,
        "scope": "every synthetic transaction reference issued for this campaign",
        "required_evidence": [
            "signed cleanup receipt",
            "independent observer absence proof",
        ],
        "failure_outcome": "reconciliation_required",
    }:
        raise AcceptanceError("qualification cleanup contract is not reviewed")
    if contract["post_campaign_invariant"] != (
        "the observer confirms that every synthetic transaction ref used by this "
        "campaign is absent after cleanup"
    ):
        raise AcceptanceError("qualification post-campaign invariant is not reviewed")

    cases = contract["cases"]
    if not isinstance(cases, list) or not cases:
        raise AcceptanceError("qualification contract has no cases")
    reviewed_cases = {
        "healthy-01": ("representative", "verified"),
        "healthy-02": ("representative", "verified"),
        "healthy-03": ("representative", "verified"),
        "idempotency-replay": ("representative", "verified"),
        "wrong-reference": ("wrong_identity", "halted"),
        "stale-session": ("stale_identity", "halted"),
        "ambiguous-target": ("ambiguity", "halted"),
        "verifier-unavailable": ("missing_effect", "halted"),
        "weak-effect-only": ("weak_effect", "halted"),
    }
    expected: dict[str, dict[str, str]] = {}
    for index, case_value in enumerate(cases):
        case = _mapping(case_value, f"qualification contract case {index}")
        case_id = _nonempty(case.get("id"), f"qualification contract case {index} id")
        expected_keys = {"id", "kind", "expected_outcome"}
        if case_id == "idempotency-replay":
            expected_keys |= {
                "replays_case",
                "idempotency_key_parameter",
                "invariant",
            }
        case = _closed(case, expected_keys, f"qualification contract case {case_id!r}")
        outcome = case.get("expected_outcome")
        if outcome not in _EXPECTED_OUTCOMES:
            raise AcceptanceError(f"qualification contract case {case_id!r} has invalid outcome")
        if case_id in expected:
            raise AcceptanceError(f"qualification contract case is duplicate: {case_id!r}")
        kind = _nonempty(case.get("kind"), f"qualification contract case {case_id!r} kind")
        if reviewed_cases.get(case_id) != (kind, outcome):
            raise AcceptanceError(f"qualification contract case is not reviewed: {case_id!r}")
        if case_id == "idempotency-replay" and (
            case["replays_case"] != "healthy-01"
            or case["idempotency_key_parameter"] != "transaction_ref"
            or case["invariant"]
            != (
                "the exact healthy-01 canonical input and transaction_ref replay "
                "returns the same execution and creates no second effect"
            )
        ):
            raise AcceptanceError("qualification replay contract is not reviewed")
        halt_reason = "none" if outcome == "verified" else kind
        if halt_reason not in _HALT_REASONS:
            raise AcceptanceError(
                f"qualification contract case {case_id!r} has an unsupported halt reason"
            )
        expected[case_id] = {
            "outcome": outcome,
            "halt_reason": halt_reason,
            "kind": kind,
        }
    if set(expected) != set(reviewed_cases):
        raise AcceptanceError("qualification contract does not contain the nine reviewed cases")
    return expected


def _binding_pairs(certificate: Mapping[str, Any]) -> dict[str, Any]:
    product = certificate["product"]
    qualification = certificate["qualification"]
    return {
        "runtime_validation_id_sha256": qualification["runtime_validation_id_sha256"],
        "admission_id_sha256": qualification["admission_id_sha256"],
        "campaign_id_sha256": qualification["campaign_id_sha256"],
        "workflow_version_id_sha256": qualification["workflow_version_id_sha256"],
        "workflow_digest": qualification["workflow_digest"],
        "environment_digest": qualification["environment_digest"],
        "evidence_identity_sha256": qualification["evidence_identity_sha256"],
        "cloud_source_commit": product["cloud"]["source_commit"],
        "cloud_target_build_sha256": product["cloud"]["target_build_sha256"],
        "flow_version": product["flow"]["version"],
        "flow_release_commit": product["flow"]["release_commit"],
        "flow_wheel_sha256": product["flow"]["wheel_sha256"],
        "managed_runtime_manifest_sha256": product["managed_runtime"]["manifest_sha256"],
        "runner_artifact_sha256": product["managed_runtime"]["runner_artifact_sha256"],
        "runner_build": product["managed_runtime"]["runner_build"],
        "substrate": product["managed_runtime"]["substrate"],
        "playwright_version": product["managed_runtime"]["playwright_version"],
        "browser_base_image": product["managed_runtime"]["browser_base_image"],
        "campaign_contract_sha256": qualification["campaign_contract_sha256"],
        "campaign_outcomes_sha256": qualification["campaign_outcomes_sha256"],
        "oracle_contract_sha256": qualification["oracle_contract_sha256"],
        "task_count": qualification["task_count"],
        "condition_count": qualification["condition_count"],
        "required_trial_count": qualification["required_trial_count"],
        "observed_trial_count": qualification["observed_trial_count"],
        "evidence_runner_signer_sha256": certificate["identities"][
            "evidence_runner_signer_sha256"
        ],
        "target_attestation_signer_sha256": certificate["identities"][
            "target_attestation_signer_sha256"
        ],
        "target_observer_signer_sha256": certificate["identities"][
            "target_observer_signer_sha256"
        ],
        "target_attestation_sha256": certificate["transaction"][
            "target_attestation_sha256"
        ],
        "organization_id_sha256": certificate["identities"]["organization_id_sha256"],
        "workflow_id_sha256": certificate["identities"]["workflow_id_sha256"],
    }


def _validate_authority_contract(value: Any) -> dict[str, tuple[str, bytes]]:
    authorities = _closed(value, set(_RECEIPT_TYPES), "campaign authority contract")
    verified: dict[str, tuple[str, bytes]] = {}
    for receipt_type in _RECEIPT_TYPES:
        authority = _closed(
            authorities[receipt_type],
            _AUTHORITY_KEYS,
            f"campaign {receipt_type} authority",
        )
        if authority["algorithm"] != "ed25519":
            raise AcceptanceError(f"campaign {receipt_type} authority is not Ed25519")
        if authority["schema_version"] != RECEIPT_SCHEMA:
            raise AcceptanceError(f"campaign {receipt_type} receipt schema is not supported")
        if authority["signature_domain"] != RECEIPT_SIGNATURE_DOMAIN_TEXT:
            raise AcceptanceError(f"campaign {receipt_type} signature domain is not exact")
        key_id = authority["key_id"]
        if not isinstance(key_id, str) or _RECEIPT_KEY_ID.fullmatch(key_id) is None:
            raise AcceptanceError(f"campaign {receipt_type} key ID is invalid")
        public_key = _canonical_base64(
            authority["public_key"],
            32,
            f"campaign {receipt_type} public key",
        )
        derived = "qe-ed25519-" + hashlib.sha256(public_key).hexdigest()[:16]
        if key_id != derived:
            raise AcceptanceError(f"campaign {receipt_type} key ID differs from its key")
        verified[receipt_type] = (key_id, public_key)
    if len({key_id for key_id, _ in verified.values()}) != len(_RECEIPT_TYPES):
        raise AcceptanceError("campaign receipt authorities are not separated by type")
    return verified


def _unique_digest_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list):
        raise AcceptanceError(f"{label} must be a list")
    digests = [_unprefixed_digest(item, f"{label} item") for item in value]
    if len(digests) != len(set(digests)):
        raise AcceptanceError(f"{label} must not contain duplicates")
    return digests


def _validate_runner_facts(
    value: Any,
    *,
    source_digest: str,
    evidence_identity: Mapping[str, Any],
    label: str,
) -> dict[str, Any]:
    facts = _closed(value, _RUNNER_FACT_KEYS, f"{label} runner facts")
    counter = _closed(
        facts["model_call_counter"],
        _MODEL_CALL_COUNTER_KEYS,
        f"{label} model-call counter",
    )
    if counter["source"] != "runner_signed_flow_report_and_enforced_egress_policy":
        raise AcceptanceError(f"{label} model-call counter source is not reviewed")
    counts = {
        key: _count(counter[key], f"{label} model-call {key}")
        for key in (
            "attempted",
            "completed",
            "input_tokens",
            "output_tokens",
            "cost_microusd",
        )
    }
    call_ids = _unique_digest_list(
        counter["call_ids_sha256"], f"{label} model-call IDs"
    )
    if len(call_ids) != counts["attempted"]:
        raise AcceptanceError(f"{label} model-call IDs differ from attempted count")
    if counts["completed"] > counts["attempted"]:
        raise AcceptanceError(f"{label} completed model calls exceed attempted calls")
    provider_models = counter["provider_models"]
    if not isinstance(provider_models, list):
        raise AcceptanceError(f"{label} provider-model inventory must be a list")
    normalized_provider_models: list[tuple[str, str]] = []
    for index, entry in enumerate(provider_models):
        item = _closed(
            entry,
            _PROVIDER_MODEL_KEYS,
            f"{label} provider-model {index}",
        )
        normalized_provider_models.append(
            (
                _nonempty(item["provider"], f"{label} provider-model {index} provider"),
                _nonempty(item["model"], f"{label} provider-model {index} model"),
            )
        )
    if len(normalized_provider_models) != len(set(normalized_provider_models)):
        raise AcceptanceError(f"{label} provider-model inventory contains duplicates")
    if counts["attempted"] == 0 and normalized_provider_models:
        raise AcceptanceError(f"{label} provider-model inventory exists without a model call")
    if counter["egress_policy_sha256"] != evidence_identity["network_policy_sha256"]:
        raise AcceptanceError(f"{label} model-call egress policy differs from admission")
    if counter["report_sha256"] != source_digest:
        raise AcceptanceError(f"{label} model-call report differs from source evidence")
    operator_ids = _unique_digest_list(
        facts["operator_intervention_ids_sha256"],
        f"{label} operator intervention IDs",
    )
    return {
        "model_call_counter": {**counts, "call_ids": call_ids},
        "operator_intervention_count": len(operator_ids),
    }


def _validate_observer_facts(value: Any, *, label: str) -> dict[str, Any]:
    facts = _closed(value, _OBSERVER_FACT_KEYS, f"{label} observer facts")
    if facts["dispatch_state"] not in {"dispatched", "not_dispatched"}:
        raise AcceptanceError(f"{label} observer dispatch state is invalid")
    if facts["verifier_method"] != "read_only_system_of_record_query":
        raise AcceptanceError(f"{label} observer verifier method is not reviewed")
    if facts["verifier_tier"] != "independent_system_of_record":
        raise AcceptanceError(f"{label} observer verifier tier is not independent")
    for key in (
        "pre_state_evidence_sha256",
        "post_state_evidence_sha256",
        "expected_record_id_sha256",
        "expected_transaction_ref_sha256",
    ):
        _unprefixed_digest(facts[key], f"{label} observer {key}")
    inventory = facts["effect_inventory"]
    if not isinstance(inventory, list):
        raise AcceptanceError(f"{label} observer effect inventory must be a list")
    expected_record = facts["expected_record_id_sha256"]
    expected_transaction = facts["expected_transaction_ref_sha256"]
    effect_ids: set[str] = set()
    intended = 0
    wrong_record = 0
    collateral = 0
    for index, value in enumerate(inventory):
        effect = _closed(value, _EFFECT_KEYS, f"{label} observer effect {index}")
        for key in _EFFECT_KEYS:
            _unprefixed_digest(effect[key], f"{label} observer effect {index} {key}")
        if effect["effect_id_sha256"] in effect_ids:
            raise AcceptanceError(f"{label} observer effect inventory contains duplicates")
        effect_ids.add(effect["effect_id_sha256"])
        if effect["transaction_ref_sha256"] != expected_transaction:
            collateral += 1
        elif effect["record_id_sha256"] != expected_record:
            wrong_record += 1
        else:
            intended += 1
    if facts["dispatch_state"] == "not_dispatched" and inventory:
        raise AcceptanceError(f"{label} observer found effects for an undispatched action")
    derived = {
        "intended_effect_count": intended,
        "wrong_record_count": wrong_record,
        "duplicate_effect_count": max(0, intended - 1),
        "collateral_effect_count": collateral,
    }
    declared = _closed(
        facts["derived_classifications"],
        _EFFECT_CLASSIFICATION_KEYS,
        f"{label} observer derived classifications",
    )
    normalized_declared = {
        key: _count(declared[key], f"{label} observer {key}") for key in declared
    }
    if normalized_declared != derived:
        raise AcceptanceError(
            f"{label} observer effect classifications differ from its inventory"
        )
    return {"dispatch_state": facts["dispatch_state"], **derived}


def _validate_receipt_facts(
    value: Any,
    *,
    receipt_type: str,
    source_digest: str,
    evidence_identity: Mapping[str, Any],
    label: str,
) -> dict[str, Any]:
    if receipt_type == "runner":
        return _validate_runner_facts(
            value,
            source_digest=source_digest,
            evidence_identity=evidence_identity,
            label=label,
        )
    if receipt_type == "observer":
        return _validate_observer_facts(value, label=label)
    if value != {}:
        raise AcceptanceError(f"{label} {receipt_type} facts must be empty")
    return {}


def _verified_receipt(
    receipt_digest: Any,
    *,
    receipt_type: str,
    campaign_id: str,
    task_id: str,
    condition_id: str,
    trial: Mapping[str, Any],
    envelopes: Mapping[str, Any],
    authorities: Mapping[str, tuple[str, bytes]],
    evidence_identity: Mapping[str, Any],
    generated_at: datetime,
    label: str,
) -> tuple[str, str, dict[str, Any]]:
    digest = _unprefixed_digest(receipt_digest, f"{label} {receipt_type} receipt")
    if digest not in envelopes:
        raise AcceptanceError(f"{label} {receipt_type} receipt body is missing")
    envelope = _closed(
        envelopes[digest],
        _RECEIPT_ENVELOPE_KEYS,
        f"{label} {receipt_type} receipt envelope",
    )
    if canonical_sha256(envelope).removeprefix("sha256:") != digest:
        raise AcceptanceError(f"{label} {receipt_type} receipt digest differs from its body")
    if envelope["schema_version"] != RECEIPT_SCHEMA:
        raise AcceptanceError(f"{label} {receipt_type} receipt schema is not supported")
    if envelope["receipt_type"] != receipt_type:
        raise AcceptanceError(f"{label} receipt type differs from its row field")
    if envelope["algorithm"] != "ed25519":
        raise AcceptanceError(f"{label} {receipt_type} receipt is not Ed25519")
    key_id, public_key = authorities[receipt_type]
    if envelope["issuer_key_id"] != key_id:
        raise AcceptanceError(f"{label} {receipt_type} receipt signer is not authorized")
    source_digest = _unprefixed_digest(
        envelope["source_artifact_sha256"],
        f"{label} {receipt_type} source artifact",
    )
    projection = _closed(
        envelope["verified_projection"],
        _RECEIPT_PROJECTION_KEYS,
        f"{label} {receipt_type} verified projection",
    )
    expected_projection = {
        "campaign_id": campaign_id,
        "task": task_id,
        "condition": condition_id,
        "trial_index": trial["trial_index"],
        "attempt_id_sha256": trial["attempt_id_sha256"],
        "run_id_sha256": trial["run_id_sha256"],
        "workflow_version_id": trial["workflow_version_id"],
        "admission_id": trial["admission_id"],
        "bundle_artifact_sha256": trial["bundle_artifact_sha256"],
        "runtime_validation_id": trial["runtime_validation_id"],
        "evidence_identity_sha256": trial["evidence_identity_sha256"],
        "verdict": projection["verdict"],
        "evidence_sha256": source_digest,
        "facts": projection["facts"],
    }
    if projection != expected_projection:
        raise AcceptanceError(f"{label} {receipt_type} projection differs from its row")
    if projection["verdict"] not in _RECEIPT_VERDICTS[receipt_type]:
        raise AcceptanceError(f"{label} {receipt_type} verdict is invalid")
    verified_at = _whole_second_timestamp(
        envelope["verified_at"],
        f"{label} {receipt_type} verified_at",
    )
    completed_at = _whole_second_timestamp(trial["completed_at"], f"{label} completed_at")
    if not completed_at <= verified_at <= generated_at:
        raise AcceptanceError(f"{label} {receipt_type} verification time is outside the campaign")
    signature = _canonical_base64(
        envelope["signature"],
        64,
        f"{label} {receipt_type} signature",
    )
    signed = {key: envelope[key] for key in _RECEIPT_SIGNED_KEYS}
    _verify_ed25519(
        public_key,
        signature,
        RECEIPT_SIGNATURE_DOMAIN + canonical_json(signed).encode("utf-8"),
    )
    facts = _validate_receipt_facts(
        projection["facts"],
        receipt_type=receipt_type,
        source_digest=source_digest,
        evidence_identity=evidence_identity,
        label=label,
    )
    return projection["verdict"], source_digest, facts


def _derive_trial_classification(
    *,
    expected: str,
    verdicts: Mapping[str, str],
    receipt_facts: Mapping[str, Mapping[str, Any]],
) -> str:
    runner_facts = receipt_facts["runner"]
    observer_facts = receipt_facts["observer"]
    if runner_facts["operator_intervention_count"]:
        return "operator_intervention"
    if observer_facts["wrong_record_count"]:
        return "wrong_record"
    if observer_facts["duplicate_effect_count"]:
        return "duplicate_effect"
    if observer_facts["collateral_effect_count"]:
        return "collateral_effect"
    if expected == "verified" and runner_facts["model_call_counter"]["attempted"]:
        return "healthy_path_model_call"
    if verdicts["webhook"] != "delivered":
        return "uncertain_delivery"
    if verdicts["cleanup"] != "completed" or verdicts["cleanup_absence"] != "absent":
        return "platform_failure"
    if verdicts["runner"] == "failed" or verdicts["replay"] == "failed":
        return "platform_failure"
    if expected == "verified":
        if verdicts["runner"] == "halted":
            return "over_halt"
        if (
            verdicts["runner"] == "verified"
            and verdicts["observer"] == "satisfied"
            and verdicts["replay"] == "verified"
            and "fault" not in verdicts
            and observer_facts["dispatch_state"] == "dispatched"
            and observer_facts["intended_effect_count"] == 1
        ):
            return "verified"
        return "silent_incorrect_success"
    if (
        verdicts["runner"] == "halted"
        and verdicts["observer"] == "satisfied"
        and verdicts["replay"] == "halted"
        and verdicts.get("fault") == "injected"
        and observer_facts["dispatch_state"] == "not_dispatched"
        and observer_facts["intended_effect_count"] == 0
    ):
        return "safe_halt"
    return "silent_incorrect_success"


def _validate_campaign(
    campaign: Mapping[str, Any],
    admission_payload: Mapping[str, Any],
) -> tuple[Counter[str], dict[str, int], Counter[str]]:
    campaign = _closed(campaign, _CAMPAIGN_KEYS, "campaign")
    if campaign["schema_version"] != CAMPAIGN_SCHEMA:
        raise AcceptanceError("campaign schema is not supported")
    campaign_id = _canonical_uuid(campaign["campaign_id"], "campaign ID")
    if campaign["decision"] != "admitted":
        raise AcceptanceError("campaign decision is not admitted")
    for key in ("admission_id", "runtime_validation_id"):
        _canonical_uuid(campaign[key], f"campaign {key}")
        if campaign[key] != admission_payload[key]:
            raise AcceptanceError(f"campaign {key} differs from admission")
    evidence_identity = _mapping(
        admission_payload["evidence_identity"],
        "qualification evidence identity",
    )
    identity_digest = evidence_identity_sha256(evidence_identity)
    if campaign["evidence_identity_sha256"] != identity_digest:
        raise AcceptanceError("campaign evidence identity differs from admission")
    generated_at = _whole_second_timestamp(campaign["generated_at"], "campaign generated_at")
    oracle = _closed(campaign["oracle_contract"], _ORACLE_KEYS, "campaign oracle contract")
    if oracle != {
        "schema_version": "openadapt.execute-acceptance-campaign-oracle/v1",
        "runtime_outcome_source": "signed_flow_run_receipt",
        "effect_outcome_source": "independent_system_of_record_observer",
        "delivery_outcome_source": "signed_managed_runner_and_webhook_evidence",
        "attempt_accounting": "signed_campaign_attempt_index",
        "fault_evidence_source": "signed_named_fault_driver_receipt",
        "cleanup_evidence_source": "signed_cleanup_receipt_and_observer_absence_proof",
    }:
        raise AcceptanceError("campaign oracle contract is not reviewed")
    if campaign["excluded_trials"] != []:
        raise AcceptanceError("campaign contains excluded or hidden trials")

    contract = _mapping(campaign["qualification_contract"], "qualification contract")
    expected_cases = _case_contract(contract)
    task_id = contract["task_id"]
    authorities = _validate_authority_contract(campaign["authority_contract"])
    runner_public_key = authorities["runner"][1]
    runner_fingerprint = hashlib.sha256(runner_public_key).hexdigest()
    if runner_fingerprint != evidence_identity["evidence_runner_signer_sha256"]:
        raise AcceptanceError("campaign runner signer differs from admission")
    envelopes = _mapping(campaign["receipt_envelopes"], "campaign receipt envelopes")
    for digest in envelopes:
        _unprefixed_digest(digest, "campaign receipt envelope key")

    conditions = campaign["conditions"]
    if not isinstance(conditions, list) or not conditions:
        raise AcceptanceError("campaign conditions must be a non-empty list")
    seen_conditions: set[str] = set()
    seen_attempts: set[str] = set()
    seen_runs: set[str] = set()
    used_receipts: set[str] = set()
    seen_source_artifacts: set[str] = set()
    taxonomy: Counter[str] = Counter()
    fact_totals: Counter[str] = Counter()
    trials_per_condition: dict[str, int] = {}
    effect_invariant_observations = 0
    healthy_model_observations = 0
    for condition_index, condition_value in enumerate(conditions):
        condition = _closed(
            condition_value,
            _CONDITION_KEYS,
            f"campaign condition {condition_index}",
        )
        condition_id = _nonempty(
            condition["condition_id"],
            f"campaign condition {condition_index} id",
        )
        if condition_id in seen_conditions:
            raise AcceptanceError(f"campaign condition is duplicate: {condition_id!r}")
        seen_conditions.add(condition_id)
        if condition_id not in expected_cases:
            raise AcceptanceError(f"campaign condition is outside the contract: {condition_id!r}")
        expected = condition["expected_runtime_outcome"]
        case = expected_cases[condition_id]
        if expected != case["outcome"]:
            raise AcceptanceError(f"campaign condition outcome differs from contract: {condition_id!r}")
        trials = condition["trials"]
        required_trials = condition["required_trials"]
        if (
            not isinstance(required_trials, int)
            or isinstance(required_trials, bool)
            or required_trials < 3
            or required_trials > 10_000
        ):
            raise AcceptanceError(
                f"campaign condition has an invalid required trial count: {condition_id!r}"
            )
        if not isinstance(trials, list) or len(trials) < required_trials:
            raise AcceptanceError(
                "campaign condition has fewer than 3 retained trials or its required count: "
                f"{condition_id!r}"
            )
        trials_per_condition[condition_id] = len(trials)
        for trial_index, trial_value in enumerate(trials, start=1):
            label = f"campaign condition {condition_id!r} trial {trial_index}"
            trial = _closed(trial_value, _TRIAL_KEYS, label)
            if trial["schema_version"] != TRIAL_SCHEMA:
                raise AcceptanceError(f"{label} schema is not supported")
            if trial["task"] != task_id or trial["condition"] != condition_id:
                raise AcceptanceError(f"{label} task or condition differs from its container")
            if (
                not isinstance(trial["trial_index"], int)
                or isinstance(trial["trial_index"], bool)
                or trial["trial_index"] != trial_index
            ):
                raise AcceptanceError(f"{label} index is not one-based and contiguous")
            for key in ("attempt_id_sha256", "run_id_sha256", "bundle_artifact_sha256"):
                _unprefixed_digest(trial[key], f"{label} {key}")
            if trial["attempt_id_sha256"] in seen_attempts:
                raise AcceptanceError(f"{label} attempt ID is duplicate")
            if trial["run_id_sha256"] in seen_runs:
                raise AcceptanceError(f"{label} run ID is duplicate")
            seen_attempts.add(trial["attempt_id_sha256"])
            seen_runs.add(trial["run_id_sha256"])
            _canonical_uuid(trial["workflow_version_id"], f"{label} workflow version")
            _canonical_uuid(trial["admission_id"], f"{label} admission")
            _canonical_uuid(trial["runtime_validation_id"], f"{label} runtime validation")
            if trial["workflow_version_id"] != admission_payload["workflow_version_id"]:
                raise AcceptanceError(f"{label} workflow version differs from admission")
            if trial["runtime_validation_id"] != admission_payload["runtime_validation_id"]:
                raise AcceptanceError(f"{label} runtime validation differs from admission")
            if trial["admission_id"] != admission_payload["admission_id"]:
                raise AcceptanceError(f"{label} admission ID differs from admission")
            if trial["evidence_identity_sha256"] != identity_digest:
                raise AcceptanceError(f"{label} evidence identity differs from admission")
            if trial["bundle_artifact_sha256"] != admission_payload["bundle_artifact_sha256"]:
                raise AcceptanceError(f"{label} bundle artifact differs from admission")
            started_at = _whole_second_timestamp(trial["started_at"], f"{label} started_at")
            completed_at = _whole_second_timestamp(trial["completed_at"], f"{label} completed_at")
            if completed_at < started_at or completed_at > generated_at:
                raise AcceptanceError(f"{label} time interval is invalid")
            if trial["execution_outcome"] not in {"verified", "halted", "failed"}:
                raise AcceptanceError(f"{label} execution outcome is invalid")
            if trial["oracle_verdict"] not in {"satisfied", "refuted", "unverifiable"}:
                raise AcceptanceError(f"{label} oracle verdict is invalid")
            if trial["failure_class"] not in _FAILURE_TAXONOMY:
                raise AcceptanceError(f"{label} failure class is invalid")
            verdicts: dict[str, str] = {}
            receipt_facts: dict[str, dict[str, Any]] = {}
            for receipt_type, row_field in _RECEIPT_ROW_FIELDS.items():
                receipt_digest = trial[row_field]
                if receipt_type == "fault" and case["kind"] == "representative":
                    if receipt_digest is not None:
                        raise AcceptanceError(f"{label} has a fault receipt for a nonfault case")
                    continue
                if receipt_digest is None:
                    raise AcceptanceError(f"{label} {receipt_type} receipt is required")
                receipt_digest = _unprefixed_digest(
                    receipt_digest,
                    f"{label} {receipt_type} receipt",
                )
                if receipt_digest in used_receipts:
                    raise AcceptanceError(f"{label} reuses a receipt envelope")
                used_receipts.add(receipt_digest)
                verdict, source_digest, facts = _verified_receipt(
                    receipt_digest,
                    receipt_type=receipt_type,
                    campaign_id=campaign_id,
                    task_id=task_id,
                    condition_id=condition_id,
                    trial=trial,
                    envelopes=envelopes,
                    authorities=authorities,
                    evidence_identity=evidence_identity,
                    generated_at=generated_at,
                    label=label,
                )
                if source_digest in seen_source_artifacts:
                    raise AcceptanceError(f"{label} reuses a source evidence artifact")
                seen_source_artifacts.add(source_digest)
                verdicts[receipt_type] = verdict
                receipt_facts[receipt_type] = facts
            if verdicts["runner"] != trial["execution_outcome"]:
                raise AcceptanceError(f"{label} execution outcome differs from signed evidence")
            if verdicts["observer"] != trial["oracle_verdict"]:
                raise AcceptanceError(f"{label} oracle verdict differs from signed evidence")
            classification = _derive_trial_classification(
                expected=expected,
                verdicts=verdicts,
                receipt_facts=receipt_facts,
            )
            if trial["failure_class"] != classification:
                raise AcceptanceError(f"{label} failure class differs from signed evidence")
            taxonomy[classification] += 1
            observer_facts = receipt_facts["observer"]
            runner_facts = receipt_facts["runner"]
            fact_totals["wrong_record_count"] += observer_facts["wrong_record_count"]
            fact_totals["duplicate_effect_count"] += observer_facts[
                "duplicate_effect_count"
            ]
            fact_totals["collateral_effect_count"] += observer_facts[
                "collateral_effect_count"
            ]
            fact_totals["operator_intervention_count"] += runner_facts[
                "operator_intervention_count"
            ]
            attempted_model_calls = runner_facts["model_call_counter"]["attempted"]
            fact_totals["model_call_count"] += attempted_model_calls
            if expected == "verified":
                fact_totals["healthy_path_model_call_count"] += attempted_model_calls
            effect_invariant_observations += 1
            if expected == "verified":
                healthy_model_observations += 1
    if seen_conditions != set(expected_cases):
        missing = sorted(set(expected_cases) - seen_conditions)
        raise AcceptanceError(f"campaign does not retain every contract condition: {missing}")
    if set(envelopes) != used_receipts:
        raise AcceptanceError("campaign contains unreferenced or hidden receipt envelopes")
    failures = {name: taxonomy.get(name, 0) for name in _PRODUCTION_FAILURES}
    present = {name: count for name, count in failures.items() if count}
    if present:
        raise AcceptanceError(f"campaign contains production-acceptance failures: {present}")

    invariants = campaign["invariants"]
    if not isinstance(invariants, list) or not invariants:
        raise AcceptanceError("campaign invariants must be a non-empty list")
    derived_invariants = {
        "no_wrong_or_duplicate_effect": {
            "holds": (
                fact_totals["wrong_record_count"]
                + fact_totals["duplicate_effect_count"]
                + fact_totals["collateral_effect_count"]
                == 0
            ),
            "observations": effect_invariant_observations,
            "violations": (
                fact_totals["wrong_record_count"]
                + fact_totals["duplicate_effect_count"]
                + fact_totals["collateral_effect_count"]
            ),
        },
        "zero_model_healthy_path": {
            "holds": fact_totals["healthy_path_model_call_count"] == 0,
            "observations": healthy_model_observations,
            "violations": fact_totals["healthy_path_model_call_count"],
        },
    }
    seen_invariants: set[str] = set()
    for index, invariant_value in enumerate(invariants):
        invariant = _closed(invariant_value, _INVARIANT_KEYS, f"campaign invariant {index}")
        invariant_id = _nonempty(invariant["id"], f"campaign invariant {index} id")
        if invariant_id in seen_invariants:
            raise AcceptanceError(f"campaign invariant is duplicate: {invariant_id!r}")
        seen_invariants.add(invariant_id)
        if invariant_id not in derived_invariants:
            raise AcceptanceError(f"campaign invariant is not derived: {invariant_id!r}")
        observations = _count(invariant["observations"], f"campaign invariant {invariant_id}")
        violations = _count(invariant["violations"], f"campaign invariant {invariant_id}")
        if {
            "holds": invariant["holds"],
            "observations": observations,
            "violations": violations,
        } != derived_invariants[invariant_id]:
            raise AcceptanceError(f"campaign invariant differs from retained trials: {invariant_id!r}")
    if seen_invariants != set(derived_invariants):
        raise AcceptanceError("campaign does not contain the exact derived invariant set")
    return taxonomy, trials_per_condition, fact_totals


def verify_github_attestation(
    certificate_path: Path,
    bundle_path: Path,
    expected_cloud_source_commit: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, str]:
    """Verify the certificate bytes against the exact protected Cloud workflow."""

    if not certificate_path.is_file():
        raise AcceptanceError("certificate file is missing")
    if not bundle_path.is_file():
        raise AcceptanceError("certificate attestation bundle is missing")
    if _HEX_40.fullmatch(expected_cloud_source_commit) is None:
        raise AcceptanceError("approved Cloud source commit is not exact")
    try:
        certificate = json.loads(certificate_path.read_text(encoding="utf-8"))
        source_commit = certificate["product"]["cloud"]["source_commit"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise AcceptanceError("certificate Cloud source commit cannot be read") from exc
    if not isinstance(source_commit, str) or _HEX_40.fullmatch(source_commit) is None:
        raise AcceptanceError("certificate Cloud source commit is not exact")
    if source_commit != expected_cloud_source_commit:
        raise AcceptanceError("certificate Cloud source commit is not the approved commit")
    version_command = ["gh", "--version"]
    try:
        version_result = run(
            version_command,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise AcceptanceError(f"reviewed GitHub CLI could not run: {exc}") from exc
    version_lines = version_result.stdout.strip().splitlines()
    expected_release_url = (
        "https://github.com/cli/cli/releases/tag/v"
        f"{REVIEWED_GITHUB_CLI_VERSION}"
    )
    if (
        version_result.returncode != 0
        or version_result.stderr.strip()
        or len(version_lines) != 2
        or re.fullmatch(
            rf"gh version {re.escape(REVIEWED_GITHUB_CLI_VERSION)} "
            r"\([0-9]{4}-[0-9]{2}-[0-9]{2}\)",
            version_lines[0],
        )
        is None
        or version_lines[1] != expected_release_url
    ):
        raise AcceptanceError(
            "GitHub attestation verification requires reviewed gh version "
            f"{REVIEWED_GITHUB_CLI_VERSION}"
        )
    command = [
        "gh",
        "attestation",
        "verify",
        str(certificate_path),
        "--repo",
        CLOUD_REPOSITORY,
        "--bundle",
        str(bundle_path),
        "--cert-identity",
        CLOUD_CERTIFICATE_IDENTITY,
        "--cert-oidc-issuer",
        GITHUB_OIDC_ISSUER,
        "--hostname",
        GITHUB_HOSTNAME,
        "--deny-self-hosted-runners",
        "--format",
        "json",
    ]
    try:
        completed = run(command, check=False, capture_output=True, text=True)
    except OSError as exc:
        raise AcceptanceError(f"GitHub attestation verifier could not run: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or "verification failed").strip().splitlines()[-1]
        raise AcceptanceError(f"GitHub certificate attestation is invalid: {detail}")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise AcceptanceError("GitHub attestation verifier returned invalid JSON") from exc
    _validate_verified_provenance(
        result,
        certificate_path=certificate_path,
        expected_cloud_source_commit=expected_cloud_source_commit,
    )
    return {
        "repository": CLOUD_REPOSITORY,
        "workflow": CLOUD_WORKFLOW,
        "certificate_identity": CLOUD_CERTIFICATE_IDENTITY,
        "source_commit": expected_cloud_source_commit,
        "bundle_sha256": file_sha256(bundle_path),
    }


def _validate_verified_provenance(
    result: Any,
    *,
    certificate_path: Path,
    expected_cloud_source_commit: str,
) -> None:
    """Check the source binding inside one already verified GitHub statement."""

    try:
        acceptance_record = json.loads(certificate_path.read_text(encoding="utf-8"))
        producer = acceptance_record["identities"]["producer"]
        issued_at = _timestamp(
            acceptance_record["retention"]["acceptance_verified_at"],
            "certificate retention acceptance_verified_at",
        )
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise AcceptanceError("certificate provenance bindings cannot be read") from exc
    run_id = producer.get("run_id")
    run_attempt = producer.get("run_attempt")
    if not isinstance(run_id, str) or _POSITIVE_DECIMAL.fullmatch(run_id) is None:
        raise AcceptanceError("certificate provenance run ID is invalid")
    if not isinstance(run_attempt, str) or _POSITIVE_DECIMAL.fullmatch(run_attempt) is None:
        raise AcceptanceError("certificate provenance run attempt is invalid")
    expected_invocation = (
        f"https://github.com/{CLOUD_REPOSITORY}/actions/runs/{run_id}/attempts/{run_attempt}"
    )

    if not isinstance(result, list) or len(result) != 1:
        raise AcceptanceError("GitHub attestation verifier must return one verified statement")
    item = _closed(
        result[0],
        {"attestation", "verificationResult"},
        "GitHub attestation result",
    )
    verification = _mapping(
        item["verificationResult"],
        "GitHub attestation verification result",
    )
    signature = _mapping(
        verification.get("signature"),
        "GitHub attestation signature",
    )
    certificate = _mapping(
        signature.get("certificate"),
        "GitHub attestation signing certificate",
    )
    expected_certificate_fields = {
        "subjectAlternativeName": CLOUD_CERTIFICATE_IDENTITY,
        "issuer": GITHUB_OIDC_ISSUER,
        "githubWorkflowTrigger": "workflow_dispatch",
        "githubWorkflowSHA": expected_cloud_source_commit,
        "githubWorkflowRepository": CLOUD_REPOSITORY,
        "githubWorkflowRef": "refs/heads/main",
        "buildSignerURI": CLOUD_CERTIFICATE_IDENTITY,
        "buildSignerDigest": expected_cloud_source_commit,
        "runnerEnvironment": "github-hosted",
        "sourceRepositoryURI": f"https://github.com/{CLOUD_REPOSITORY}",
        "sourceRepositoryDigest": expected_cloud_source_commit,
        "sourceRepositoryRef": "refs/heads/main",
        "sourceRepositoryOwnerURI": "https://github.com/OpenAdaptAI",
        "buildConfigURI": CLOUD_CERTIFICATE_IDENTITY,
        "buildConfigDigest": expected_cloud_source_commit,
        "buildTrigger": "workflow_dispatch",
        "runInvocationURI": expected_invocation,
        "sourceRepositoryVisibilityAtSigning": "private",
    }
    for key, expected in expected_certificate_fields.items():
        if certificate.get(key) != expected:
            raise AcceptanceError(f"GitHub signing certificate {key} is not approved")
    for key in ("sourceRepositoryIdentifier", "sourceRepositoryOwnerIdentifier"):
        value = certificate.get(key)
        if not isinstance(value, str) or _POSITIVE_DECIMAL.fullmatch(value) is None:
            raise AcceptanceError(f"GitHub signing certificate {key} is invalid")

    timestamps = verification.get("verifiedTimestamps")
    if not isinstance(timestamps, list) or not timestamps:
        raise AcceptanceError("GitHub attestation has no verified observed timestamp")
    log_times: list[datetime] = []
    authority_times: list[datetime] = []
    for value in timestamps:
        timestamp = _closed(
            value,
            {"type", "uri", "timestamp"},
            "GitHub verified timestamp",
        )
        observer = (timestamp["type"], timestamp["uri"])
        if observer not in _APPROVED_OBSERVERS:
            raise AcceptanceError("GitHub attestation carries an unapproved timestamp observer")
        observed_time = _observed_timestamp(
            timestamp["timestamp"],
            "GitHub observed timestamp",
        )
        if observer == _PUBLIC_TRANSPARENCY_LOG:
            log_times.append(observed_time)
        else:
            authority_times.append(observed_time)
    if len(log_times) != 1 or len(authority_times) > 1:
        raise AcceptanceError("GitHub attestation must have one public transparency-log time")
    for observed_time in log_times + authority_times:
        if not issued_at <= observed_time <= issued_at + timedelta(minutes=15):
            raise AcceptanceError("GitHub observed timestamp is not bound to record issuance")

    statement = _closed(
        verification.get("statement"),
        {"_type", "subject", "predicateType", "predicate"},
        "GitHub provenance statement",
    )
    if statement["_type"] != "https://in-toto.io/Statement/v1":
        raise AcceptanceError("GitHub provenance statement type is not supported")
    if statement["predicateType"] != "https://slsa.dev/provenance/v1":
        raise AcceptanceError("GitHub provenance predicate type is not supported")
    subjects = statement["subject"]
    if not isinstance(subjects, list) or not subjects:
        raise AcceptanceError("GitHub provenance has no certificate subject")
    expected_subject = {
        "name": certificate_path.name,
        "sha256": file_sha256(certificate_path).removeprefix("sha256:"),
    }
    matching_subjects = 0
    for index, value in enumerate(subjects):
        subject = _closed(value, {"name", "digest"}, f"GitHub provenance subject {index}")
        digest = _mapping(subject["digest"], f"GitHub provenance subject {index} digest")
        if digest.get("sha256") == expected_subject["sha256"]:
            if subject["name"] != expected_subject["name"] or set(digest) != {"sha256"}:
                raise AcceptanceError("GitHub provenance certificate subject is malformed")
            matching_subjects += 1
    if matching_subjects != 1:
        raise AcceptanceError(
            "GitHub provenance must contain exactly one subject for the certificate bytes"
        )

    predicate = _mapping(statement["predicate"], "GitHub provenance predicate")
    build = _mapping(predicate.get("buildDefinition"), "GitHub provenance build definition")
    if build.get("buildType") != (
        "https://actions.github.io/buildtypes/workflow/v1"
    ):
        raise AcceptanceError("GitHub provenance build type is not supported")
    external = _mapping(
        build.get("externalParameters"),
        "GitHub provenance external parameters",
    )
    workflow = _closed(
        external.get("workflow"),
        {"ref", "repository", "path"},
        "GitHub provenance source workflow",
    )
    if workflow != {
        "ref": "refs/heads/main",
        "repository": f"https://github.com/{CLOUD_REPOSITORY}",
        "path": CLOUD_WORKFLOW,
    }:
        raise AcceptanceError("GitHub provenance source workflow is not the approved main workflow")
    dependencies = build.get("resolvedDependencies")
    if not isinstance(dependencies, list) or len(dependencies) != 1:
        raise AcceptanceError("GitHub provenance must contain one resolved source dependency")
    dependency = _closed(
        dependencies[0],
        {"uri", "digest"},
        "GitHub provenance resolved source dependency",
    )
    if dependency["uri"] != (
        f"git+https://github.com/{CLOUD_REPOSITORY}@refs/heads/main"
    ):
        raise AcceptanceError("GitHub provenance resolved source ref is not approved")
    dependency_digest = _closed(
        dependency["digest"],
        {"gitCommit"},
        "GitHub provenance resolved source digest",
    )
    if dependency_digest["gitCommit"] != expected_cloud_source_commit:
        raise AcceptanceError("GitHub provenance resolved source commit is not approved")

    internal = _closed(
        build.get("internalParameters"),
        {"github"},
        "GitHub provenance internal parameters",
    )
    github = _closed(
        internal["github"],
        {
            "event_name",
            "repository_id",
            "repository_owner_id",
            "runner_environment",
        },
        "GitHub provenance runner parameters",
    )
    if github["event_name"] != "workflow_dispatch":
        raise AcceptanceError("GitHub provenance event is not the reviewed manual acceptance gate")
    if github["runner_environment"] != "github-hosted":
        raise AcceptanceError("GitHub provenance runner is not GitHub-hosted")
    for key in ("repository_id", "repository_owner_id"):
        if not isinstance(github[key], str) or _POSITIVE_DECIMAL.fullmatch(github[key]) is None:
            raise AcceptanceError(f"GitHub provenance {key} is invalid")

    run_details = _mapping(predicate.get("runDetails"), "GitHub provenance run details")
    builder = _closed(
        run_details.get("builder"),
        {"id"},
        "GitHub provenance builder",
    )
    if builder["id"] != CLOUD_CERTIFICATE_IDENTITY:
        raise AcceptanceError("GitHub provenance builder is not the reviewed Cloud workflow")
    metadata = _closed(
        run_details.get("metadata"),
        {"invocationId"},
        "GitHub provenance run metadata",
    )
    if metadata["invocationId"] != expected_invocation:
        raise AcceptanceError("GitHub provenance invocation differs from the certificate run")


def derive_production_acceptance(
    certificate: Mapping[str, Any],
    campaign: Mapping[str, Any],
    admission: Mapping[str, Any],
    *,
    attestation: Mapping[str, str],
    expected_cloud_source_commit: str,
    trusted_admission_signers: Mapping[str, Any],
    revoked_admission_ids: set[str] | frozenset[str] = frozenset(),
    revoked_admission_signer_key_ids: set[str] | frozenset[str] = frozenset(),
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return a derived result or raise when any required proof is absent."""

    now = now or datetime.now(timezone.utc)
    facts = _validate_certificate(
        certificate,
        now=now,
        expected_cloud_source_commit=expected_cloud_source_commit,
    )
    expected_attestation = {
        "repository": CLOUD_REPOSITORY,
        "workflow": CLOUD_WORKFLOW,
        "certificate_identity": CLOUD_CERTIFICATE_IDENTITY,
        "source_commit": expected_cloud_source_commit,
        "bundle_sha256": attestation.get("bundle_sha256"),
    }
    if attestation != expected_attestation:
        raise AcceptanceError("GitHub attestation identity is not the reviewed Cloud workflow")
    _digest(attestation.get("bundle_sha256"), "GitHub attestation bundle")

    admission_facts = validate_qualification_admission(
        admission,
        campaign,
        trusted_signers=trusted_admission_signers,
        revoked_admission_ids=revoked_admission_ids,
        revoked_signer_key_ids=revoked_admission_signer_key_ids,
        now=now,
    )
    admission_digest = canonical_sha256(admission)
    if admission_digest != certificate["qualification"]["qualification_admission_sha256"]:
        raise AcceptanceError("qualification admission digest differs from certificate")
    admission_payload = _mapping(admission["payload"], "qualification admission payload")
    evidence_identity = _mapping(
        admission_payload["evidence_identity"],
        "qualification evidence identity",
    )
    identity_digest = evidence_identity_sha256(evidence_identity)
    if certificate["qualification"]["evidence_identity_sha256"] != (
        "sha256:" + identity_digest
    ):
        raise AcceptanceError("certificate evidence identity differs from admission")
    runtime_identity = _mapping(
        evidence_identity["runtime_build_identity"],
        "qualification runtime build identity",
    )
    managed_browser = _mapping(
        runtime_identity["managed_browser"],
        "qualification managed-browser identity",
    )
    product = certificate["product"]
    certificate_identity_projection = {
        "flow_version": product["flow"]["version"],
        "flow_release_commit": product["flow"]["release_commit"],
        "flow_wheel_sha256": product["flow"]["wheel_sha256"].removeprefix("sha256:"),
        "runtime_manifest_sha256": product["managed_runtime"][
            "manifest_sha256"
        ].removeprefix("sha256:"),
        "runner_build": product["managed_runtime"]["runner_build"],
        "runner_artifact_sha256": product["managed_runtime"][
            "runner_artifact_sha256"
        ].removeprefix("sha256:"),
        "substrate": product["managed_runtime"]["substrate"],
        "playwright_version": product["managed_runtime"]["playwright_version"],
        "browser_base_image": product["managed_runtime"]["browser_base_image"],
        "workflow_digest": certificate["qualification"]["workflow_digest"].removeprefix(
            "sha256:"
        ),
        "environment_digest": certificate["qualification"][
            "environment_digest"
        ].removeprefix("sha256:"),
        "evidence_runner_signer_sha256": certificate["identities"][
            "evidence_runner_signer_sha256"
        ].removeprefix("sha256:"),
    }
    expected_identity_projection = {
        "flow_version": runtime_identity["flow_version"],
        "flow_release_commit": runtime_identity["flow_release_commit"],
        "flow_wheel_sha256": runtime_identity["flow_wheel_sha256"],
        "runtime_manifest_sha256": runtime_identity["runtime_manifest_sha256"],
        "runner_build": runtime_identity["runner_build"],
        "runner_artifact_sha256": runtime_identity["runner_artifact_sha256"],
        "substrate": runtime_identity["substrate"],
        "playwright_version": managed_browser["playwright_version"],
        "browser_base_image": managed_browser["browser_base_image"],
        "workflow_digest": evidence_identity["workflow_digest"],
        "environment_digest": evidence_identity["environment_digest"],
        "evidence_runner_signer_sha256": evidence_identity[
            "evidence_runner_signer_sha256"
        ],
    }
    if certificate_identity_projection != expected_identity_projection:
        raise AcceptanceError("certificate execution identity differs from admission")
    if certificate["qualification"]["admission_signer"] != {
        "algorithm": "ed25519",
        "key_id": admission_facts["signer_key_id"],
        "issuer_workflow": admission_facts["issuer_workflow"],
        "issuer_ref": admission_facts["issuer_ref"],
    }:
        raise AcceptanceError("certificate qualification signer differs from admission")
    admission_campaign = _mapping(
        admission_payload["campaign"],
        "qualification admission campaign",
    )
    opaque_bindings = {
        "runtime_validation_id_sha256": (
            "runtime validation id",
            admission_payload["runtime_validation_id"],
        ),
        "admission_id_sha256": (
            "qualification admission id",
            admission_payload["admission_id"],
        ),
        "campaign_id_sha256": (
            "qualification campaign id",
            admission_campaign["campaign_id"],
        ),
        "workflow_version_id_sha256": (
            "workflow version id",
            admission_payload["workflow_version_id"],
        ),
    }
    for certificate_key, (domain, raw_value) in opaque_bindings.items():
        expected = opaque_binding_sha256(domain, raw_value)
        if certificate["qualification"][certificate_key] != expected:
            raise AcceptanceError(
                f"certificate {certificate_key} differs from the retained admission"
            )
    for certificate_key, domain, payload_key in (
        ("organization_id_sha256", "organization id", "tenant_id"),
        ("workflow_id_sha256", "workflow id", "workflow_id"),
    ):
        expected = opaque_binding_sha256(domain, admission_payload[payload_key])
        if certificate["identities"][certificate_key] != expected:
            raise AcceptanceError(
                f"certificate {certificate_key} differs from the retained admission"
            )
    campaign_digest = canonical_sha256(campaign)
    if campaign_digest != certificate["qualification"]["campaign_artifact_sha256"]:
        raise AcceptanceError("campaign evidence digest differs from certificate")
    campaign_contract_digest = canonical_sha256(campaign["qualification_contract"])
    if campaign_contract_digest != certificate["qualification"]["campaign_contract_sha256"]:
        raise AcceptanceError("campaign qualification-contract digest differs from certificate")
    campaign_outcomes_digest = canonical_sha256(
        {
            "conditions": campaign["conditions"],
            "invariants": campaign["invariants"],
            "excluded_trials": campaign["excluded_trials"],
        }
    )
    if campaign_outcomes_digest != certificate["qualification"]["campaign_outcomes_sha256"]:
        raise AcceptanceError("campaign outcomes digest differs from certificate")
    oracle_digest = canonical_sha256(campaign["oracle_contract"])
    if oracle_digest != certificate["qualification"]["oracle_contract_sha256"]:
        raise AcceptanceError("campaign oracle digest differs from certificate")
    taxonomy, trials_per_condition, fact_totals = _validate_campaign(
        campaign, admission_payload
    )
    conditions = campaign["conditions"]
    required_trial_count = sum(condition["required_trials"] for condition in conditions)
    observed_trial_count = sum(trials_per_condition.values())
    minimum_trials_per_condition = min(trials_per_condition.values())
    expected_certificate_counts = {
        "task_count": 1,
        "condition_count": len(trials_per_condition),
        "required_trial_count": required_trial_count,
        "observed_trial_count": observed_trial_count,
        "minimum_trials_per_condition": minimum_trials_per_condition,
    }
    for key, expected in expected_certificate_counts.items():
        if certificate["qualification"][key] != expected:
            raise AcceptanceError(
                f"certificate qualification {key} differs from retained rows"
            )
    counted_taxonomy = {name: taxonomy.get(name, 0) for name in _FAILURE_TAXONOMY}
    total_trials = observed_trial_count
    privacy_safe_conditions = [
        {
            "task_id_sha256": privacy_safe_campaign_label_sha256(
                "qualification task",
                campaign["campaign_id"],
                campaign["qualification_contract"]["task_id"],
            ),
            "condition_id_sha256": privacy_safe_campaign_label_sha256(
                "qualification condition",
                campaign["campaign_id"],
                condition["condition_id"],
            ),
            "required_trial_count": condition["required_trials"],
            "observed_trial_count": trials_per_condition[condition["condition_id"]],
        }
        for condition in sorted(
            campaign["conditions"],
            key=lambda value: value["condition_id"],
        )
    ]
    return {
        "schema_version": RESULT_SCHEMA,
        "verdict": "accepted",
        "evidence_class": "qualified_browser_production_acceptance",
        "claim_scope": CLAIM_SCOPE,
        "bindings": _binding_pairs(certificate),
        "source_evidence": {
            "certificate_sha256": canonical_sha256(certificate),
            "campaign_sha256": campaign_digest,
            "qualification_admission_sha256": admission_digest,
            "qualification_authority": admission_facts,
            "attestation": dict(attestation),
            "approved_cloud_source_commit": expected_cloud_source_commit,
        },
        "trial_inventory": {
            "task_count": expected_certificate_counts["task_count"],
            "condition_count": len(trials_per_condition),
            "required_trial_count": required_trial_count,
            "observed_trial_count": observed_trial_count,
            "trial_count": total_trials,
            "minimum_trials_per_condition": min(trials_per_condition.values()),
            "conditions": privacy_safe_conditions,
            "excluded_trial_count": 0,
        },
        "derived_outcomes": counted_taxonomy,
        "reliability": {
            "silent_incorrect_success_count": counted_taxonomy[
                "silent_incorrect_success"
            ],
            "over_halt_count": counted_taxonomy["over_halt"],
            "wrong_record_count": fact_totals["wrong_record_count"],
            "duplicate_effect_count": fact_totals["duplicate_effect_count"],
            "collateral_effect_count": fact_totals["collateral_effect_count"],
            "operator_intervention_count": fact_totals[
                "operator_intervention_count"
            ],
            "uncertain_delivery_count": counted_taxonomy["uncertain_delivery"],
            "model_call_count": fact_totals["model_call_count"],
        },
        "retention": facts["retention"],
        "claim_limit": "not_general_product_production_readiness",
    }


def _validated_manifest_source(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the complete private result used by the public manifest builder."""

    source = dict(_closed(value, _DERIVED_RESULT_KEYS, "production acceptance source"))
    if (
        source["schema_version"] != RESULT_SCHEMA
        or source["verdict"] != "accepted"
        or source["evidence_class"] != "qualified_browser_production_acceptance"
        or source["claim_scope"] != CLAIM_SCOPE
        or source["claim_limit"] != "not_general_product_production_readiness"
    ):
        raise AcceptanceError("production acceptance source is not an accepted browser result")

    bindings = _closed(
        source["bindings"],
        _DERIVED_BINDING_KEYS,
        "production acceptance source bindings",
    )
    for key in (
        "runtime_validation_id_sha256",
        "admission_id_sha256",
        "campaign_id_sha256",
        "workflow_version_id_sha256",
        "workflow_digest",
        "environment_digest",
        "evidence_identity_sha256",
        "cloud_target_build_sha256",
        "flow_wheel_sha256",
        "managed_runtime_manifest_sha256",
        "runner_artifact_sha256",
        "campaign_contract_sha256",
        "campaign_outcomes_sha256",
        "oracle_contract_sha256",
        "evidence_runner_signer_sha256",
        "target_attestation_signer_sha256",
        "target_observer_signer_sha256",
        "target_attestation_sha256",
        "organization_id_sha256",
        "workflow_id_sha256",
    ):
        _digest(bindings[key], f"production acceptance source binding {key}")
    if not isinstance(bindings["cloud_source_commit"], str) or _HEX_40.fullmatch(
        bindings["cloud_source_commit"]
    ) is None:
        raise AcceptanceError("production acceptance source Cloud commit is invalid")
    if not isinstance(bindings["flow_release_commit"], str) or _HEX_40.fullmatch(
        bindings["flow_release_commit"]
    ) is None:
        raise AcceptanceError("production acceptance source Flow commit is invalid")
    if not isinstance(bindings["flow_version"], str) or _SEMVER.fullmatch(
        bindings["flow_version"]
    ) is None:
        raise AcceptanceError("production acceptance source Flow version is invalid")
    if bindings["substrate"] != "web":
        raise AcceptanceError("production acceptance source substrate is not web")
    if not isinstance(bindings["browser_base_image"], str) or _PINNED_IMAGE.fullmatch(
        bindings["browser_base_image"]
    ) is None:
        raise AcceptanceError("production acceptance source browser image is invalid")

    evidence = _closed(
        source["source_evidence"],
        _DERIVED_SOURCE_EVIDENCE_KEYS,
        "production acceptance source evidence",
    )
    for key in (
        "certificate_sha256",
        "campaign_sha256",
        "qualification_admission_sha256",
    ):
        _digest(evidence[key], f"production acceptance source evidence {key}")
    if evidence["approved_cloud_source_commit"] != bindings["cloud_source_commit"]:
        raise AcceptanceError("production acceptance source Cloud approval differs")
    attestation = _closed(
        evidence["attestation"],
        {
            "repository",
            "workflow",
            "certificate_identity",
            "source_commit",
            "bundle_sha256",
        },
        "production acceptance source attestation",
    )
    if (
        attestation["repository"] != CLOUD_REPOSITORY
        or attestation["workflow"] != CLOUD_WORKFLOW
        or attestation["certificate_identity"] != CLOUD_CERTIFICATE_IDENTITY
        or attestation["source_commit"] != bindings["cloud_source_commit"]
    ):
        raise AcceptanceError("production acceptance source attestation differs")
    _digest(
        attestation["bundle_sha256"],
        "production acceptance source attestation bundle",
    )
    authority = _closed(
        evidence["qualification_authority"],
        {
            "artifact_sha256",
            "signer_key_id",
            "issuer_workflow",
            "issuer_ref",
            "expires_at",
            "evidence_identity_sha256",
        },
        "production acceptance source qualification authority",
    )
    _digest(
        authority["artifact_sha256"],
        "production acceptance source qualification authority artifact",
    )
    if authority["artifact_sha256"] != evidence["qualification_admission_sha256"]:
        raise AcceptanceError("production acceptance source admission digest differs")
    if authority["evidence_identity_sha256"] != bindings[
        "evidence_identity_sha256"
    ].removeprefix("sha256:"):
        raise AcceptanceError("production acceptance source evidence identity differs")

    inventory = _closed(
        source["trial_inventory"],
        _DERIVED_TRIAL_INVENTORY_KEYS,
        "production acceptance source trial inventory",
    )
    counts = {
        key: _count(inventory[key], f"production acceptance source inventory {key}")
        for key in (
            "task_count",
            "condition_count",
            "required_trial_count",
            "observed_trial_count",
            "trial_count",
            "minimum_trials_per_condition",
            "excluded_trial_count",
        )
    }
    if (
        counts["task_count"] < 1
        or counts["condition_count"] < counts["task_count"]
        or counts["required_trial_count"] < 3
        or counts["observed_trial_count"] < counts["required_trial_count"]
        or counts["trial_count"] != counts["observed_trial_count"]
        or counts["minimum_trials_per_condition"] < 3
        or counts["excluded_trial_count"] != 0
    ):
        raise AcceptanceError("production acceptance source inventory is incomplete")
    for key in (
        "task_count",
        "condition_count",
        "required_trial_count",
        "observed_trial_count",
    ):
        if bindings[key] != counts[key]:
            raise AcceptanceError(f"production acceptance source binding {key} differs")
    conditions = inventory["conditions"]
    if not isinstance(conditions, list) or len(conditions) != counts["condition_count"]:
        raise AcceptanceError("production acceptance source condition inventory is incomplete")
    seen_conditions: set[str] = set()
    seen_tasks: set[str] = set()
    required_total = 0
    observed_total = 0
    observed_minimum: int | None = None
    for index, item in enumerate(conditions):
        condition = _closed(
            item,
            _DERIVED_CONDITION_KEYS,
            f"production acceptance source condition {index}",
        )
        task_digest = _digest(
            condition["task_id_sha256"],
            f"production acceptance source condition {index} task",
        )
        condition_digest = _digest(
            condition["condition_id_sha256"],
            f"production acceptance source condition {index} identity",
        )
        if condition_digest in seen_conditions:
            raise AcceptanceError("production acceptance source condition is duplicate")
        seen_conditions.add(condition_digest)
        seen_tasks.add(task_digest)
        required = _count(
            condition["required_trial_count"],
            f"production acceptance source condition {index} required trials",
        )
        observed = _count(
            condition["observed_trial_count"],
            f"production acceptance source condition {index} observed trials",
        )
        if required < 3 or observed < required:
            raise AcceptanceError("production acceptance source condition is incomplete")
        required_total += required
        observed_total += observed
        observed_minimum = observed if observed_minimum is None else min(
            observed_minimum,
            observed,
        )
    if (
        len(seen_tasks) != counts["task_count"]
        or required_total != counts["required_trial_count"]
        or observed_total != counts["observed_trial_count"]
        or observed_minimum != counts["minimum_trials_per_condition"]
    ):
        raise AcceptanceError("production acceptance source condition counts differ")

    taxonomy = _closed(
        source["derived_outcomes"],
        set(_FAILURE_TAXONOMY),
        "production acceptance source failure taxonomy",
    )
    counted_taxonomy = {
        key: _count(value, f"production acceptance source failure taxonomy {key}")
        for key, value in taxonomy.items()
    }
    if sum(counted_taxonomy.values()) != counts["observed_trial_count"]:
        raise AcceptanceError("production acceptance source taxonomy count differs")
    if any(counted_taxonomy[name] for name in _PRODUCTION_FAILURES):
        raise AcceptanceError("production acceptance source contains a production failure")

    reliability = _closed(
        source["reliability"],
        _RELIABILITY_KEYS,
        "production acceptance source reliability",
    )
    for key, value in reliability.items():
        _count(value, f"production acceptance source reliability {key}")
    expected_reliability = {
        "silent_incorrect_success_count": counted_taxonomy[
            "silent_incorrect_success"
        ],
        "over_halt_count": counted_taxonomy["over_halt"],
        "wrong_record_count": counted_taxonomy["wrong_record"],
        "duplicate_effect_count": counted_taxonomy["duplicate_effect"],
        "collateral_effect_count": counted_taxonomy["collateral_effect"],
        "operator_intervention_count": counted_taxonomy["operator_intervention"],
        "uncertain_delivery_count": counted_taxonomy["uncertain_delivery"],
    }
    if any(reliability[key] != value for key, value in expected_reliability.items()):
        raise AcceptanceError("production acceptance source reliability differs")

    retention = _closed(
        source["retention"],
        _RETENTION_KEYS,
        "production acceptance source retention",
    )
    for key in (
        "ciphertext_sha256",
        "candidate_sha256",
        "private_envelope_sha256",
        "store_attestation_sha256",
        "storage_identity_sha256",
        "private_locator_version_sha256",
        "encryption_recipient_sha256",
        "uploader_identity_sha256",
    ):
        _digest(retention[key], f"production acceptance source retention {key}")
    for key in ("visibility_verified", "private_locator_recorded"):
        if retention[key] is not True:
            raise AcceptanceError(f"production acceptance source retention {key} is false")
    if not isinstance(retention["receipt_id"], str) or re.fullmatch(
        r"retention:[a-f0-9]{32}", retention["receipt_id"]
    ) is None:
        raise AcceptanceError("production acceptance source retention receipt ID is invalid")
    if retention["provenance_attestation"] != RETENTION_PROVENANCE_ROUTE:
        raise AcceptanceError(
            "production acceptance source retention provenance is invalid"
        )
    acceptance_verified_at = _timestamp(
        retention["acceptance_verified_at"],
        "production acceptance source retention acceptance_verified_at",
    )
    retained_at = _timestamp(
        retention["retained_at"],
        "production acceptance source retention retained_at",
    )
    if not acceptance_verified_at <= retained_at:
        raise AcceptanceError("production acceptance source retention chronology is invalid")
    return source


def _reject_duplicate_json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise AcceptanceError(
                f"production lifecycle policy contains duplicate key {key!r}"
            )
        value[key] = item
    return value


def _validated_flow_lifecycle_policy(policy_bytes: bytes) -> dict[str, Any]:
    if not isinstance(policy_bytes, bytes) or not policy_bytes:
        raise AcceptanceError("production lifecycle policy must be non-empty bytes")
    try:
        value = json.loads(
            policy_bytes.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_pairs,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AcceptanceError("production lifecycle policy is not valid UTF-8 JSON") from exc
    policy = _closed(
        value,
        {
            "$schema",
            "schema_version",
            "revision",
            "maximum_admission_days",
            "summary_authority",
            "targets",
        },
        "production lifecycle policy",
    )
    if (
        policy["$schema"] != PRODUCTION_LIFECYCLE_POLICY_PATH
        or policy["schema_version"] != PRODUCTION_LIFECYCLE_POLICY_SCHEMA
    ):
        raise AcceptanceError("production lifecycle policy schema is not supported")
    if (
        not isinstance(policy["revision"], int)
        or isinstance(policy["revision"], bool)
        or policy["revision"] < 1
    ):
        raise AcceptanceError("production lifecycle policy revision is invalid")
    maximum_days = policy["maximum_admission_days"]
    if (
        not isinstance(maximum_days, int)
        or isinstance(maximum_days, bool)
        or not 1 <= maximum_days <= 30
    ):
        raise AcceptanceError("production lifecycle admission duration is invalid")
    if not isinstance(policy["summary_authority"], dict):
        raise AcceptanceError("production lifecycle summary authority is invalid")
    targets = policy["targets"]
    if not isinstance(targets, list) or len(targets) != len(
        PRODUCTION_ACCEPTANCE_TARGET_SCOPES
    ):
        raise AcceptanceError("production lifecycle target inventory is incomplete")
    target_keys = {
        "id",
        "display_name",
        "lifecycle_scope",
        "lifecycle_subject",
        "source_repository",
        "release_kind",
        "required_claim_scope",
        "required_artifact_kinds",
        "package_index_project",
        "artifact_authority_by_kind",
    }
    target_map: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(targets):
        declared = dict(
            _closed(item, target_keys, f"production lifecycle target {index}")
        )
        target_id = declared["id"]
        if not isinstance(target_id, str) or target_id in target_map:
            raise AcceptanceError("production lifecycle target identity is invalid")
        target_map[target_id] = declared
    if set(target_map) != set(PRODUCTION_ACCEPTANCE_TARGET_SCOPES):
        raise AcceptanceError("production lifecycle target inventory differs")
    expected_flow = {
        "id": "flow",
        "display_name": "OpenAdapt Flow",
        "lifecycle_scope": "repository",
        "lifecycle_subject": "openadapt-flow",
        "source_repository": "OpenAdaptAI/openadapt-flow",
        "release_kind": "public_package",
        "required_claim_scope": PRODUCTION_ACCEPTANCE_TARGET_SCOPES["flow"],
        "required_artifact_kinds": ["sdist", "wheel"],
        "package_index_project": "openadapt-flow",
        "artifact_authority_by_kind": {"sdist": "pypi", "wheel": "pypi"},
    }
    if target_map["flow"] != expected_flow:
        raise AcceptanceError("Flow production lifecycle policy differs")
    return target_map["flow"]


def _clean_https_url(value: object, label: str) -> str:
    url = _nonempty(value, label)
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise AcceptanceError(f"{label} must be a clean HTTPS URL")
    return url


def verify_production_lifecycle_release(
    lifecycle_policy_bytes: bytes,
    target: str,
    target_release: Mapping[str, Any],
    *,
    pypi_release_metadata: Mapping[str, Any],
) -> VerifiedProductionLifecycleRelease:
    """Verify one exact release against lifecycle policy and PyPI metadata."""

    if target != "flow":
        if target in PRODUCTION_ACCEPTANCE_TARGET_SCOPES:
            raise AcceptanceError(
                f"production lifecycle target {target!r} requires its own verifier"
            )
        raise AcceptanceError("production lifecycle target is not supported")
    target_policy = _validated_flow_lifecycle_policy(lifecycle_policy_bytes)
    release = dict(
        _closed(
            target_release,
            {
                "kind",
                "version",
                "tag",
                "source_commit",
                "immutable_release_url",
                "artifacts",
            },
            "Flow production lifecycle release",
        )
    )
    version = release["version"]
    if (
        release["kind"] != "public_package"
        or not isinstance(version, str)
        or _SEMVER.fullmatch(version) is None
        or release["tag"] not in {version, f"v{version}"}
    ):
        raise AcceptanceError("Flow production lifecycle package identity is invalid")
    source_commit = release["source_commit"]
    if not isinstance(source_commit, str) or _HEX_40.fullmatch(source_commit) is None:
        raise AcceptanceError("Flow production lifecycle source commit is invalid")
    immutable_url = _clean_https_url(
        release["immutable_release_url"],
        "Flow production lifecycle immutable release URL",
    )
    if immutable_url != (
        "https://github.com/OpenAdaptAI/openadapt-flow/commit/" + source_commit
    ):
        raise AcceptanceError(
            "Flow production lifecycle immutable release URL is not the exact commit"
        )
    artifacts_value = release["artifacts"]
    if not isinstance(artifacts_value, list) or len(artifacts_value) != 2:
        raise AcceptanceError(
            "Flow production lifecycle release must contain one sdist and one wheel"
        )
    artifacts: list[dict[str, Any]] = []
    artifact_identities: list[tuple[str, str]] = []
    for index, item in enumerate(artifacts_value):
        artifact = dict(
            _closed(
                item,
                {"authority", "kind", "name", "sha256", "size_bytes", "url"},
                f"Flow production lifecycle artifact {index}",
            )
        )
        name = _nonempty(artifact["name"], f"Flow lifecycle artifact {index} name")
        if _ARTIFACT_NAME.fullmatch(name) is None:
            raise AcceptanceError(f"Flow lifecycle artifact {index} name is invalid")
        kind = artifact["kind"]
        if kind not in {"sdist", "wheel"} or artifact["authority"] != "pypi":
            raise AcceptanceError(
                f"Flow lifecycle artifact {index} kind or authority is invalid"
            )
        _digest(artifact["sha256"], f"Flow lifecycle artifact {index} digest")
        size = artifact["size_bytes"]
        if not isinstance(size, int) or isinstance(size, bool) or size < 1:
            raise AcceptanceError(f"Flow lifecycle artifact {index} size is invalid")
        artifact_url = _clean_https_url(
            artifact["url"], f"Flow lifecycle artifact {index} URL"
        )
        if urlsplit(artifact_url).netloc != "files.pythonhosted.org":
            raise AcceptanceError(f"Flow lifecycle artifact {index} is not from PyPI")
        artifact_identities.append((kind, name))
        artifacts.append(artifact)
    if artifact_identities != sorted(artifact_identities) or {
        kind for kind, _name in artifact_identities
    } != set(target_policy["required_artifact_kinds"]):
        raise AcceptanceError(
            "Flow production lifecycle artifacts must be sorted sdist and wheel"
        )
    if len(set(artifact_identities)) != len(artifact_identities):
        raise AcceptanceError("Flow production lifecycle artifact is duplicate")
    if not isinstance(pypi_release_metadata, Mapping):
        raise AcceptanceError("PyPI release metadata must be an object")
    info = pypi_release_metadata.get("info")
    urls = pypi_release_metadata.get("urls")
    if not isinstance(info, Mapping) or info.get("version") != version:
        raise AcceptanceError("PyPI release metadata version differs")
    if not isinstance(urls, list):
        raise AcceptanceError("PyPI release metadata files are invalid")
    for artifact in artifacts:
        matches = [
            item
            for item in urls
            if isinstance(item, Mapping)
            and item.get("filename") == artifact["name"]
            and item.get("url") == artifact["url"]
            and item.get("size") == artifact["size_bytes"]
            and isinstance(item.get("digests"), Mapping)
            and item["digests"].get("sha256")
            == artifact["sha256"].removeprefix("sha256:")
            and item.get("yanked") is False
        ]
        if len(matches) != 1:
            raise AcceptanceError(
                f"PyPI does not verify exact artifact {artifact['name']}"
            )
    return VerifiedProductionLifecycleRelease(
        target=target,
        claim_scope=target_policy["required_claim_scope"],
        lifecycle_policy_sha256=(
            "sha256:" + hashlib.sha256(lifecycle_policy_bytes).hexdigest()
        ),
        release=release,
        artifacts=artifacts,
        _seal=VerifiedProductionLifecycleRelease._CONSTRUCTION_SEAL,
    )


def _validated_target_lifecycle(
    source: Mapping[str, Any],
    target: str,
    lifecycle_release: VerifiedProductionLifecycleRelease,
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    if type(lifecycle_release) is not VerifiedProductionLifecycleRelease:
        raise AcceptanceError(
            "production acceptance requires a verifier-derived lifecycle release"
        )
    if (
        lifecycle_release.target != target
        or lifecycle_release.claim_scope != PRODUCTION_ACCEPTANCE_TARGET_SCOPES[target]
    ):
        raise AcceptanceError("production acceptance lifecycle target differs")
    release = lifecycle_release.release()
    artifacts = lifecycle_release.artifacts()
    if release.get("artifacts") != artifacts:
        raise AcceptanceError("production acceptance lifecycle artifacts differ")
    bindings = source["bindings"]
    if (
        release["version"] != bindings["flow_version"]
        or release["source_commit"] != bindings["flow_release_commit"]
    ):
        raise AcceptanceError(
            "Flow production acceptance lifecycle release differs from verified evidence"
        )
    wheels = [item for item in artifacts if item["kind"] == "wheel"]
    if (
        len(wheels) != 1
        or wheels[0]["sha256"] != bindings["flow_wheel_sha256"]
    ):
        raise AcceptanceError(
            "Flow production acceptance wheel differs from verified evidence"
        )
    _digest(
        lifecycle_release.lifecycle_policy_sha256,
        "production lifecycle policy digest",
    )
    return release, artifacts, lifecycle_release.lifecycle_policy_sha256


def _build_production_acceptance_manifest(
    source: Mapping[str, Any],
    target: str,
    lifecycle_release: VerifiedProductionLifecycleRelease,
) -> dict[str, Any]:
    if target not in _BROWSER_SOURCE_TARGETS:
        if target in PRODUCTION_ACCEPTANCE_TARGET_SCOPES:
            raise AcceptanceError(
                f"production acceptance target {target!r} requires its own evidence adapter"
            )
        raise AcceptanceError("production acceptance target is not supported")
    if target == "cloud":
        raise AcceptanceError(
            "Cloud production acceptance requires a reviewed deployment-manifest binding"
        )
    bindings = source["bindings"]
    evidence = source["source_evidence"]
    inventory = source["trial_inventory"]
    release_descriptor, artifact_inventory, lifecycle_policy_sha256 = (
        _validated_target_lifecycle(
            source,
            target,
            lifecycle_release,
        )
    )
    return {
        "schema_version": PRODUCTION_ACCEPTANCE_SCHEMA,
        "target": target,
        "claim_scope": PRODUCTION_ACCEPTANCE_TARGET_SCOPES[target],
        "verdict": "accepted",
        "acceptance_policy_sha256": production_acceptance_policy_sha256(),
        "lifecycle_policy_sha256": lifecycle_policy_sha256,
        "target_release_sha256": _domain_sha256(
            PRODUCTION_LIFECYCLE_TARGET_RELEASE_DOMAIN,
            {
                "target": target,
                "claim_scope": PRODUCTION_ACCEPTANCE_TARGET_SCOPES[target],
                "release": release_descriptor,
            },
        ),
        "target_artifact_inventory_sha256": _domain_sha256(
            PRODUCTION_LIFECYCLE_ARTIFACT_INVENTORY_DOMAIN,
            {
                "target": target,
                "claim_scope": PRODUCTION_ACCEPTANCE_TARGET_SCOPES[target],
                "artifacts": artifact_inventory,
            },
        ),
        "evidence_identity_sha256": bindings["evidence_identity_sha256"],
        "source_evidence": {
            "source_result_sha256": canonical_sha256(source),
            "certificate_sha256": evidence["certificate_sha256"],
            "campaign_sha256": evidence["campaign_sha256"],
            "qualification_admission_sha256": evidence[
                "qualification_admission_sha256"
            ],
            "attestation_sha256": canonical_sha256(evidence["attestation"]),
            "attestation_bundle_sha256": evidence["attestation"]["bundle_sha256"],
        },
        "qualification": {
            "campaign_contract_sha256": bindings["campaign_contract_sha256"],
            "campaign_outcomes_sha256": bindings["campaign_outcomes_sha256"],
            "oracle_contract_sha256": bindings["oracle_contract_sha256"],
            "task_count": inventory["task_count"],
            "condition_count": inventory["condition_count"],
            "required_trial_count": inventory["required_trial_count"],
            "observed_trial_count": inventory["observed_trial_count"],
            "minimum_trials_per_condition": inventory[
                "minimum_trials_per_condition"
            ],
            "excluded_trial_count": inventory["excluded_trial_count"],
            "task_condition_inventory_sha256": _domain_sha256(
                b"OpenAdapt production acceptance task-condition inventory v1\0",
                inventory["conditions"],
            ),
        },
        "failure_taxonomy_counts": dict(source["derived_outcomes"]),
        "reliability": dict(source["reliability"]),
        "retention": dict(source["retention"]),
    }


def validate_production_acceptance_manifest(
    value: Mapping[str, Any],
    verified_source_result: Mapping[str, Any],
    *,
    lifecycle_release: VerifiedProductionLifecycleRelease,
) -> dict[str, Any]:
    """Validate one target manifest against the complete verified private result."""

    source = _validated_manifest_source(verified_source_result)
    manifest = dict(
        _closed(value, _PRODUCTION_ACCEPTANCE_KEYS, "production acceptance manifest")
    )
    target = manifest["target"]
    if not isinstance(target, str):
        raise AcceptanceError("production acceptance manifest target is invalid")
    expected = _build_production_acceptance_manifest(
        source,
        target,
        lifecycle_release,
    )
    if manifest != expected:
        raise AcceptanceError(
            "production acceptance manifest differs from its verified source result"
        )
    for key in (
        "acceptance_policy_sha256",
        "lifecycle_policy_sha256",
        "target_release_sha256",
        "target_artifact_inventory_sha256",
        "evidence_identity_sha256",
    ):
        _digest(manifest[key], f"production acceptance manifest {key}")
    _closed(
        manifest["source_evidence"],
        _PRODUCTION_SOURCE_EVIDENCE_KEYS,
        "production acceptance manifest source evidence",
    )
    _closed(
        manifest["qualification"],
        _PRODUCTION_QUALIFICATION_KEYS,
        "production acceptance manifest qualification",
    )
    _closed(
        manifest["failure_taxonomy_counts"],
        set(_FAILURE_TAXONOMY),
        "production acceptance manifest failure taxonomy",
    )
    _closed(
        manifest["reliability"],
        _RELIABILITY_KEYS,
        "production acceptance manifest reliability",
    )
    _closed(
        manifest["retention"],
        _RETENTION_KEYS,
        "production acceptance manifest retention",
    )
    return manifest


def build_production_acceptance_manifest(
    verified_source_result: Mapping[str, Any],
    target: str,
    *,
    lifecycle_release: VerifiedProductionLifecycleRelease,
) -> dict[str, Any]:
    """Build, without I/O, one accepted target manifest from verified evidence."""

    source = _validated_manifest_source(verified_source_result)
    manifest = _build_production_acceptance_manifest(
        source,
        target,
        lifecycle_release,
    )
    return validate_production_acceptance_manifest(
        manifest,
        source,
        lifecycle_release=lifecycle_release,
    )


def import_files(
    certificate_path: Path,
    campaign_path: Path,
    admission_path: Path,
    bundle_path: Path,
    expected_cloud_source_commit: str,
    *,
    trusted_admission_signers: Mapping[str, Any],
    revoked_admission_ids: set[str] | frozenset[str] = frozenset(),
    revoked_admission_signer_key_ids: set[str] | frozenset[str] = frozenset(),
    now: datetime | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    raise AcceptanceError(
        "full admission/campaign import is pending an approved private-export contract"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Derive one scoped browser-workflow production-acceptance result."
    )
    parser.add_argument("--certificate", type=Path, required=True)
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--qualification-admission", type=Path, required=True)
    parser.add_argument("--attestation-bundle", type=Path, required=True)
    parser.add_argument("--expected-cloud-source-commit", required=True)
    parser.add_argument("--trusted-admission-signers", type=Path, required=True)
    parser.add_argument("--revoked-admission-ids", type=Path)
    parser.add_argument("--revoked-admission-signer-key-ids", type=Path)
    parser.add_argument("--private-export-contract", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.private_export_contract:
            contract = validate_private_export_contract(
                json.loads(args.private_export_contract.read_text(encoding="utf-8"))
            )
            verify_importer_identity(contract, os.environ)
        trusted_admission_signers = _mapping(
            json.loads(args.trusted_admission_signers.read_text(encoding="utf-8")),
            "qualification signer trust registry",
        )
        revoked_admission_ids = (
            _string_set(
                json.loads(args.revoked_admission_ids.read_text(encoding="utf-8")),
                "qualification admission revocations",
            )
            if args.revoked_admission_ids
            else set()
        )
        revoked_admission_signer_key_ids = (
            _string_set(
                json.loads(
                    args.revoked_admission_signer_key_ids.read_text(encoding="utf-8")
                ),
                "qualification signer revocations",
            )
            if args.revoked_admission_signer_key_ids
            else set()
        )
        result = import_files(
            args.certificate,
            args.campaign,
            args.qualification_admission,
            args.attestation_bundle,
            args.expected_cloud_source_commit,
            trusted_admission_signers=trusted_admission_signers,
            revoked_admission_ids=revoked_admission_ids,
            revoked_admission_signer_key_ids=revoked_admission_signer_key_ids,
        )
        with args.output.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    except (AcceptanceError, FileExistsError, OSError, json.JSONDecodeError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    print(f"Wrote scoped acceptance result to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
