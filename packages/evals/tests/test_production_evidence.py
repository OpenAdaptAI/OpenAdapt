from __future__ import annotations

import base64
import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from openadapt_evals import production_evidence as evidence

ROOT = Path(__file__).resolve().parents[1]
_EVALUATION_TIME = datetime(2026, 8, 27, 12, 30, tzinfo=timezone.utc)
_DECISION_PRIVATE_KEY = Ed25519PrivateKey.from_private_bytes(b"\x19" * 32)
_DECISION_PUBLIC_KEY = _DECISION_PRIVATE_KEY.public_key()
_DECISION_PUBLIC_BYTES = _DECISION_PUBLIC_KEY.public_bytes(
    encoding=serialization.Encoding.Raw,
    format=serialization.PublicFormat.Raw,
)
_DECISION_KEY_ID = "qa-ed25519-" + hashlib.sha256(_DECISION_PUBLIC_BYTES).hexdigest()[:16]


class _CryptographicReceiptVerifier:
    def verify(
        self,
        receipt: dict[str, object],
        *,
        reference: dict[str, object],
        bundle_reference: dict[str, object],
        object_sha256: str,
        semantic_identity_sha256: str,
        authority_state_sha256: str,
        signer_registry_sha256: str,
        revocation_state_sha256: str,
        evaluation_time: datetime,
    ) -> None:
        assert object_sha256 == evidence.sha256_digest(
            evidence.canonical_json_bytes(receipt) + b"\n"
        )
        assert semantic_identity_sha256 == evidence._regular_semantic_identity(
            kind="qualification-evidence-decision-receipt",
            object_schema_version=evidence.DECISION_RECEIPT_SCHEMA,
            object_value=receipt,
        )
        assert authority_state_sha256 == _digest("6")
        assert signer_registry_sha256 == _digest("8")
        assert revocation_state_sha256 == _digest("7")
        assert evaluation_time == _EVALUATION_TIME
        assert reference["object_sha256"] == object_sha256
        assert reference["semantic_identity_sha256"] == semantic_identity_sha256
        assert bundle_reference["subject_sha256"] == object_sha256
        statement_bytes = evidence.canonical_json_bytes(receipt["signing_statement"]) + b"\n"
        signature = base64.b64decode(str(receipt["signature"]), validate=True)
        try:
            _DECISION_PUBLIC_KEY.verify(signature, statement_bytes)
        except Exception as exc:
            raise evidence.ProductionEvidenceError(
                "test decision receipt signature verification failed"
            ) from exc

    def verify_registered_object(
        self,
        value: dict[str, object],
        *,
        reference: dict[str, object],
        bundle_reference: dict[str, object],
        kind: str,
        authority_state_sha256: str,
        signer_registry_sha256: str,
        revocation_state_sha256: str,
        evaluation_time: datetime,
    ) -> None:
        raw = evidence.canonical_json_bytes(value) + b"\n"
        assert reference["kind"] == kind
        assert reference["object_sha256"] == evidence.sha256_digest(raw)
        assert reference["semantic_identity_sha256"] == evidence._regular_semantic_identity(
            kind=kind,
            object_schema_version=str(reference["object_schema_version"]),
            object_value=value,
        )
        assert bundle_reference["subject_sha256"] == reference["object_sha256"]
        assert authority_state_sha256 == _digest("6")
        assert signer_registry_sha256 == _digest("8")
        assert revocation_state_sha256 == _digest("7")
        assert evaluation_time == _EVALUATION_TIME


def _verifier() -> _CryptographicReceiptVerifier:
    return _CryptographicReceiptVerifier()


def _validate_summary(
    summary: dict[str, object],
    *,
    qualification_evidence_decision_receipt: dict[str, object],
    qualification_admission: dict[str, object],
    production_acceptance_manifest: dict[str, object],
    expected_publication_assets: list[dict[str, object]],
    decision_receipt_verifier: object | None = None,
) -> None:
    evidence.validate_production_acceptance_summary(
        summary,
        qualification_evidence_decision_receipt=qualification_evidence_decision_receipt,
        qualification_admission=qualification_admission,
        production_acceptance_manifest=production_acceptance_manifest,
        expected_publication_assets=expected_publication_assets,
        decision_receipt_verifier=decision_receipt_verifier or _verifier(),
        evaluation_time=_EVALUATION_TIME,
    )


def _digest(character: str) -> str:
    return "sha256:" + character * 64


def _object(kind: str) -> dict[str, object]:
    return {
        "schema_version": evidence.REGULAR_EVIDENCE_KINDS[kind].schema_version,
        "value": kind,
    }


def _pair(
    kind: str,
    *,
    revision: int = 4,
    object_value: dict[str, object] | None = None,
) -> evidence.EvidenceObjectPair:
    return evidence.build_evidence_object_pair(
        kind=kind,
        object_value=object_value or _object(kind),
        sigstore_bundle=(b'{ "mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json" }\n'),
        registry_source_commit="a" * 40,
        registry_revision=revision,
        registry_head_sha256=_digest("b"),
    )


def _campaign_summary() -> dict[str, dict[str, int]]:
    fields = evidence._CAMPAIGN_CLASS_SUMMARY_KEYS
    summary = {
        qualification_class: {field: 0 for field in fields}
        for qualification_class in evidence._CAMPAIGN_CLASSES
    }
    for counts in summary.values():
        counts["task_condition_cell_count"] = 1
        counts["minimum_trials_per_cell"] = 3
        counts["observed_trial_count"] = 3
    summary["uncertain_delivery"]["reconciliation_required_count"] = 3
    summary["declared_attended"]["authenticated_bound_decision_count"] = 3
    summary["declared_attended"]["live_target_revalidation_count"] = 3
    for field in (
        "policy_approved_repair_count",
        "approved_repair_count",
        "retained_repair_evidence_count",
        "live_target_revalidation_count",
    ):
        summary["governed_repair"][field] = 3
    return summary


def test_local_wire_profile_matches_pinned_central_fixture() -> None:
    contract = json.loads(
        (ROOT / "tests/fixtures/central-production-trust-989681f6.json").read_text(encoding="utf-8")
    )
    assert contract["source_commit"] == evidence.CENTRAL_TRUST_CONTRACT_COMMIT
    assert contract["schemas"] == {
        "qualification_release": evidence.QUALIFICATION_RELEASE_SCHEMA,
        "qualification_admission": evidence.QUALIFICATION_ADMISSION_SCHEMA,
        "decision_receipt": evidence.DECISION_RECEIPT_SCHEMA,
        "acceptance_manifest": evidence.PRODUCTION_ACCEPTANCE_MANIFEST_SCHEMA,
        "acceptance_summary": evidence.PRODUCTION_ACCEPTANCE_SUMMARY_SCHEMA,
        "flow_release_verification": evidence.RELEASE_VERIFICATION_RECEIPT_SCHEMA,
    }
    assert contract["decision_campaign"] == {
        "minimum_trials_per_task_condition": 3,
        "classes": sorted(evidence._CAMPAIGN_CLASSES),
    }
    assert contract["release_candidate"]["required"] == sorted(evidence._RELEASE_CANDIDATE_KEYS)
    assert {key: list(value) for key, value in evidence._TARGET_RELEASE_CONTRACTS.items()} == (
        contract["targets"]
    )
    assert {
        key: [media_type, list(destinations)]
        for key, (media_type, destinations) in evidence._TARGET_ARTIFACT_CONTRACTS["flow"].items()
    } == contract["flow_artifacts"]


def _decision_receipt(campaign: dict[str, dict[str, int]]) -> dict[str, object]:
    receipt: dict[str, object] = {
        "schema_version": evidence.DECISION_RECEIPT_SCHEMA,
        "evidence_class": "remote-safe-synthetic",
        "decision_identity_sha256": _digest("d"),
        "decision_revision": 1,
        "decision_commitment_sha256": _digest("a"),
        "evidence_manifest_sha256": _digest("b"),
        "evidence_manifest_readback_sha256": _digest("e"),
        "campaign_artifact_sha256": _digest("c"),
        "organization_id_sha256": _digest("d"),
        "workflow_id_sha256": _digest("e"),
        "workflow_version_id_sha256": _digest("f"),
        "bundle_version": "1.0.0",
        "bundle_sha256": _digest("0"),
        "admitted_runtime_sha256": _digest("1"),
        "action_contract_sha256": _digest("2"),
        "application_contract_sha256": _digest("3"),
        "effect_contract_sha256": _digest("4"),
        "environment_contract_sha256": _digest("5"),
        "evidence_authority_contract_sha256": _digest("6"),
        "identity_contract_sha256": _digest("7"),
        "input_contract_sha256": _digest("8"),
        "policy_contract_sha256": _digest("9"),
        "campaign_permit_sha256": _digest("a"),
        "signer_registry_sha256": _digest("8"),
        "revocation_state_sha256": _digest("7"),
        "entity_class": "record",
        "campaign_summary": {
            "schema_version": evidence.DECISION_CAMPAIGN_SUMMARY_SCHEMA,
            "minimum_trials_per_task_condition": 3,
            "task_count": 1,
            "classes": campaign,
        },
        "verdict": "ADMIT",
        "issued_at": "2026-08-27T11:00:00Z",
        "not_before": "2026-08-27T11:00:00Z",
        "expires_at": "2026-09-03T11:00:00Z",
        "issuer_key_id": _DECISION_KEY_ID,
        "algorithm": "ed25519",
        "signing_statement": None,
        "signature": "",
        "issuer": {
            "repository": "OpenAdaptAI/.github",
            "repository_id": "858454062",
            "repository_owner_id": "132681217",
            "workflow": ".github/workflows/issue-synthetic-qualification-evidence-decision.yml",
            "ref": "refs/heads/main",
            "source_commit": evidence.CENTRAL_TRUST_CONTRACT_COMMIT,
            "environment": "synthetic-qualification-evidence-decision",
        },
    }
    unsigned = dict(receipt)
    unsigned.pop("signing_statement")
    unsigned.pop("signature")
    unsigned_bytes = evidence.canonical_json_bytes(unsigned) + b"\n"
    receipt["signing_statement"] = {
        "schema_version": "openadapt.qualification-evidence-signing-statement/v1",
        "object_schema_version": evidence.DECISION_RECEIPT_SCHEMA,
        "signature_domain": evidence.DECISION_RECEIPT_SIGNATURE_DOMAIN.decode(),
        "unsigned_object_sha256": evidence.sha256_digest(unsigned_bytes),
        "unsigned_size_bytes": len(unsigned_bytes),
        "commitment_scheme": "sha256-canonical-json-lf",
    }
    statement_bytes = evidence.canonical_json_bytes(receipt["signing_statement"]) + b"\n"
    receipt["signature"] = base64.b64encode(_DECISION_PRIVATE_KEY.sign(statement_bytes)).decode(
        "ascii"
    )
    return receipt


def _publication_staging() -> tuple[dict[str, object], list[dict[str, object]]]:
    expected_assets = [
        {
            "name": "openadapt_flow-1.35.0.tar.gz",
            "kind": "python-sdist",
            "sha256": _digest("a"),
            "size_bytes": 100,
            "media_type": "application/gzip",
            "publish_destinations": ["github-release", "pypi"],
        },
        {
            "name": "openadapt_flow-1.35.0-py3-none-any.whl",
            "kind": "python-wheel",
            "sha256": _digest("b"),
            "size_bytes": 200,
            "media_type": "application/zip",
            "publish_destinations": ["github-release", "pypi"],
        },
    ]
    assets = [
        {
            **asset,
            "asset_id": str(index),
            "uploader_id": "321543906",
            "uploader_login": "openadapt-release[bot]",
        }
        for index, asset in enumerate(
            sorted(expected_assets, key=lambda item: item["name"]), start=11
        )
    ]

    def ruleset(
        role: str, ruleset_id: str, rules: list[str], *, creation_bypass: bool
    ) -> dict[str, object]:
        return {
            "schema_version": evidence.TAG_RULESET_SCHEMA,
            "role": role,
            "repository": "OpenAdaptAI/openadapt-flow",
            "repository_id": "1291376938",
            "ruleset_id": ruleset_id,
            "name": (
                "OpenAdapt policy: release tag creation"
                if role == "creation_authority"
                else "OpenAdapt policy: immutable release tags"
            ),
            "target": "tag",
            "enforcement": "active",
            "bypass_actors": (
                [
                    {
                        "actor_id": "4730708",
                        "actor_type": "Integration",
                        "bypass_mode": "always",
                    }
                ]
                if creation_bypass
                else []
            ),
            "conditions": {"ref_name": {"include": ["refs/tags/v*"], "exclude": []}},
            "rules": [
                (
                    {
                        "type": "update",
                        "parameters": {"update_allows_fetch_and_merge": False},
                    }
                    if rule == "update"
                    else {"type": rule}
                )
                for rule in rules
            ],
        }

    tag_rulesets = [
        ruleset("creation_authority", "71", ["creation"], creation_bypass=True),
        ruleset(
            "immutability",
            "72",
            ["deletion", "non_fast_forward", "update"],
            creation_bypass=False,
        ),
    ]
    staging = {
        "schema_version": evidence.PUBLICATION_STAGING_SCHEMA,
        "repository": "OpenAdaptAI/openadapt-flow",
        "repository_id": "1291376938",
        "draft_release_id": "456789",
        "tag": "v1.35.0",
        "target_commitish": "c" * 40,
        "draft": True,
        "prerelease": False,
        "release_app_id": "4730708",
        "release_app_installation_id": "156835568",
        "release_app_bot_user_id": "321543906",
        "release_author_login": "openadapt-release[bot]",
        "assets": assets,
        "immutable_releases": {"enabled": True, "enforced_by_owner": False},
        "immutable_releases_sha256": evidence.sha256_digest(
            evidence.IMMUTABLE_RELEASES_DIGEST_DOMAIN
            + evidence.canonical_json_bytes({"enabled": True, "enforced_by_owner": False})
        ),
        "tag_rulesets": tag_rulesets,
        "tag_rulesets_sha256": evidence.sha256_digest(
            evidence.TAG_RULESETS_DIGEST_DOMAIN + evidence.canonical_json_bytes(tag_rulesets)
        ),
        "tag_ref_state": {"ref": "refs/tags/v1.35.0", "exists": False},
        "tag_ref_state_sha256": evidence.sha256_digest(
            evidence.TAG_REF_STATE_DIGEST_DOMAIN
            + evidence.canonical_json_bytes({"ref": "refs/tags/v1.35.0", "exists": False})
        ),
        "observed_at": "2026-08-27T11:30:00Z",
    }
    return staging, expected_assets


def _summary() -> tuple[
    dict[str, object],
    dict[str, object],
    dict[str, object],
    dict[str, object],
    list[dict[str, object]],
]:
    campaign = _campaign_summary()
    receipt = _decision_receipt(campaign)
    decision = _pair("qualification-evidence-decision-receipt", object_value=receipt)
    admission_value: dict[str, object] = {
        "schema_version": evidence.QUALIFICATION_ADMISSION_SCHEMA,
        "admission_id_sha256": _digest("0"),
        "evidence_class": receipt["evidence_class"],
        "organization_id_sha256": receipt["organization_id_sha256"],
        "workflow_id_sha256": receipt["workflow_id_sha256"],
        "workflow_version_id_sha256": receipt["workflow_version_id_sha256"],
        "bundle_version": receipt["bundle_version"],
        "bundle_sha256": receipt["bundle_sha256"],
        "admitted_runtime_sha256": receipt["admitted_runtime_sha256"],
        "application_contract_sha256": receipt["application_contract_sha256"],
        "environment_contract_sha256": receipt["environment_contract_sha256"],
        "input_contract_sha256": receipt["input_contract_sha256"],
        "action_contract_sha256": receipt["action_contract_sha256"],
        "identity_contract_sha256": receipt["identity_contract_sha256"],
        "effect_contract_sha256": receipt["effect_contract_sha256"],
        "policy_contract_sha256": receipt["policy_contract_sha256"],
        "evidence_authority_sha256": receipt["evidence_authority_contract_sha256"],
        "campaign_artifact_sha256": receipt["campaign_artifact_sha256"],
        "campaign_permit_sha256": receipt["campaign_permit_sha256"],
        "decision_receipt_reference": decision.references[0],
        "decision_receipt_bundle_reference": decision.references[1],
        "signer_registry_sha256": receipt["signer_registry_sha256"],
        "revocation_state_sha256": receipt["revocation_state_sha256"],
        "entity_class": receipt["entity_class"],
        "campaign_summary": campaign,
        "local_identity_opening": {
            "schema_version": "openadapt.qualification-local-identity-opening/v1",
            "algorithm": "hmac-sha256",
            "required": True,
            "customer_controlled_secret_required": True,
            "exact_contract_match_required": True,
            "revalidation_before_actuation": True,
            "maximum_age_seconds": 60,
        },
        "verdict": "accepted",
        "issued_at": "2026-08-27T11:05:00Z",
        "not_before": "2026-08-27T11:05:00Z",
        "expires_at": "2026-09-03T10:00:00Z",
        "issuer": {
            "repository": "OpenAdaptAI/.github",
            "repository_id": "858454062",
            "repository_owner_id": "132681217",
            "workflow": ".github/workflows/issue-qualification-admission.yml",
            "ref": "refs/heads/main",
            "source_commit": evidence.CENTRAL_TRUST_CONTRACT_COMMIT,
            "environment": "qualification-admission",
        },
    }
    admission_projection = dict(admission_value)
    admission_projection.pop("admission_id_sha256")
    admission_value["admission_id_sha256"] = evidence.sha256_digest(
        evidence.QUALIFICATION_ADMISSION_DIGEST_DOMAIN
        + evidence.canonical_json_bytes(admission_projection)
    )
    admission = _pair("qualification-admission", object_value=admission_value)
    staging, expected_assets = _publication_staging()
    release = {
        "schema_version": "openadapt.production-release-candidate/v1",
        "kind": "package",
        "source_repository": "OpenAdaptAI/openadapt-flow",
        "source_repository_id": "1291376938",
        "source_commit": "c" * 40,
        "version": "1.35.0",
        "tag": "v1.35.0",
        "deployment_id": None,
        "deployment_sha256": None,
        "artifacts": expected_assets,
    }
    release_identity = {
        "schema_version": "openadapt.monotonic-production-release/v1",
        "channel": "production",
        "sequence": 1,
        "previous_admission_sha256": None,
    }
    release_sha256 = evidence.sha256_digest(
        evidence.RELEASE_CANDIDATE_DIGEST_DOMAIN
        + evidence.canonical_json_bytes(
            {"target": "flow", "claim_scope": "production_flow", "release": release}
        )
    )
    inventory = {
        "schema_version": "openadapt.production-release-artifact-inventory/v1",
        "target": "flow",
        "claim_scope": "production_flow",
        "artifacts": expected_assets,
    }
    inventory_sha256 = evidence.sha256_digest(
        evidence.ARTIFACT_INVENTORY_DIGEST_DOMAIN
        + evidence.canonical_json_bytes(
            {
                "target": "flow",
                "claim_scope": "production_flow",
                "artifacts": expected_assets,
            }
        )
    )
    issuer = {
        "repository": "OpenAdaptAI/openadapt-evals",
        "repository_id": "1135998197",
        "repository_owner_id": "132681217",
        "workflow": ".github/workflows/issue-production-acceptance.yml",
        "ref": "refs/heads/main",
        "source_commit": "f" * 40,
        "environment": "production-acceptance",
    }
    common = {
        "target": "flow",
        "verdict": "accepted",
        "claim_scope": "production_flow",
        "acceptance_policy_sha256": _digest("1"),
        "lifecycle_policy_sha256": _digest("2"),
        "release_identity": release_identity,
        "release_sha256": release_sha256,
        "artifact_inventory_sha256": inventory_sha256,
        "publication_staging": staging,
        "publication_staging_sha256": evidence.sha256_digest(
            evidence.PUBLICATION_STAGING_DIGEST_DOMAIN + evidence.canonical_json_bytes(staging)
        ),
        "qualification_evidence_decision_receipt_reference": decision.references[0],
        "qualification_evidence_decision_receipt_bundle_reference": decision.references[1],
        "qualification_admission_reference": admission.references[0],
        "qualification_admission_bundle_reference": admission.references[1],
        "campaign_summary": campaign,
        "authority_state_sha256": _digest("6"),
        "revocation_state_sha256": _digest("7"),
        "signer_registry_sha256": _digest("8"),
        "issued_at": "2026-08-27T12:00:00Z",
        "not_before": "2026-08-27T12:00:00Z",
        "expires_at": "2026-09-03T10:00:00Z",
        "issuer": issuer,
    }
    manifest_value = {
        "schema_version": evidence.PRODUCTION_ACCEPTANCE_MANIFEST_SCHEMA,
        **common,
        "release": release,
        "artifact_inventory": inventory,
    }
    manifest = _pair("production-acceptance-manifest", object_value=manifest_value)
    summary = evidence.build_production_acceptance_summary(
        target="flow",
        claim_scope="production_flow",
        acceptance_policy_sha256=_digest("1"),
        lifecycle_policy_sha256=_digest("2"),
        release_identity=release_identity,
        release_sha256=release_sha256,
        artifact_inventory_sha256=inventory_sha256,
        publication_staging=staging,
        expected_publication_assets=expected_assets,
        qualification_evidence_decision_receipt=receipt,
        qualification_evidence_decision_receipt_references=decision.references,
        qualification_admission=admission_value,
        qualification_admission_references=admission.references,
        production_acceptance_manifest=manifest_value,
        production_acceptance_manifest_references=manifest.references,
        authority_state_sha256=_digest("6"),
        revocation_state_sha256=_digest("7"),
        signer_registry_sha256=_digest("8"),
        issued_at="2026-08-27T12:00:00Z",
        not_before="2026-08-27T12:00:00Z",
        expires_at="2026-09-03T10:00:00Z",
        issuer=issuer,
        decision_receipt_verifier=_verifier(),
        evaluation_time=_EVALUATION_TIME,
    )
    return summary, receipt, admission_value, manifest_value, expected_assets


def test_approved_kind_map_is_exact() -> None:
    assert set(evidence.REGULAR_EVIDENCE_KINDS) == {
        "qualification-release",
        "production-acceptance-manifest",
        "production-acceptance-summary",
        "qualification-authority-state-receipt",
        "qualification-revocation-state-receipt",
        "production-current-default",
        "production-deployment-observation",
        "production-lifecycle-checkpoint",
        "production-cloud-deploy-authorization",
        "production-cloud-deployment-result",
        "qualification-campaign-permit-policy",
        "qualification-campaign-permit-request",
        "qualification-campaign-permit",
        "qualification-campaign-permit-receipt",
        "qualification-evidence-decision-receipt",
        "qualification-admission",
        "support-release-admission",
    }
    assert evidence.CENTRAL_TRUST_CONTRACT_COMMIT == ("989681f6f475616b7e2cb72360c716db0927f7ad")
    assert evidence.REGULAR_EVIDENCE_KINDS["production-acceptance-summary"] == (
        evidence.EvidenceKind(
            "openadapt.production-lifecycle-evidence-summary/v3",
            "application/vnd.openadapt.production-lifecycle-evidence-summary+json;version=3",
        )
    )
    assert evidence.REGULAR_EVIDENCE_KINDS["qualification-admission"] == (
        evidence.EvidenceKind(
            "openadapt.qualification-admission/v4",
            "application/vnd.openadapt.qualification-admission+json;version=4",
        )
    )


def test_builds_exact_content_addressed_v2_reference_pair() -> None:
    pair = _pair("production-acceptance-summary")
    regular, bundle = pair.references

    assert pair.objects[1].startswith(b'{ "mediaType"')
    assert set(regular) == evidence.REFERENCE_KEYS
    assert "url" not in regular
    assert regular["schema_version"] == evidence.OBJECT_REFERENCE_SCHEMA
    assert regular["repository"] == "OpenAdaptAI/.github"
    assert regular["repository_id"] == "858454062"
    assert regular["repository_owner_id"] == "132681217"
    assert regular["object_schema_version"] == (
        "openadapt.production-lifecycle-evidence-summary/v3"
    )
    assert regular["object_media_type"] == (
        "application/vnd.openadapt.production-lifecycle-evidence-summary+json;version=3"
    )
    assert regular["subject_sha256"] is None
    regular_hex = str(regular["object_sha256"]).removeprefix("sha256:")
    assert regular["object_path"] == (
        f"production-evidence/objects/sha256/{regular_hex[:2]}/"
        f"{regular_hex}.production-acceptance-summary.json"
    )
    assert bundle["kind"] == "production-acceptance-summary-sigstore-bundle"
    assert bundle["object_schema_version"] == evidence.SIGSTORE_BUNDLE_MEDIA_TYPE
    assert bundle["object_media_type"] == evidence.SIGSTORE_BUNDLE_MEDIA_TYPE
    assert bundle["subject_sha256"] == regular["object_sha256"]
    evidence.validate_reference_pair(
        pair.references, expected_regular_kind="production-acceptance-summary"
    )


def test_reference_digests_use_frozen_domains_and_projections() -> None:
    pair = _pair("qualification-admission")
    regular, bundle = pair.references
    regular_object = json.loads(pair.objects[0])
    expected_regular_identity = evidence.sha256_digest(
        evidence.REGULAR_SEMANTIC_IDENTITY_DOMAIN
        + evidence.canonical_json_bytes(
            {
                "kind": "qualification-admission",
                "object_schema_version": "openadapt.qualification-admission/v4",
                "object": regular_object,
            }
        )
    )
    assert regular["semantic_identity_sha256"] == expected_regular_identity
    expected_bundle_identity = evidence.sha256_digest(
        evidence.BUNDLE_SEMANTIC_IDENTITY_DOMAIN
        + evidence.canonical_json_bytes(
            {
                "kind": "qualification-admission-sigstore-bundle",
                "object_sha256": bundle["object_sha256"],
                "subject_sha256": regular["object_sha256"],
            }
        )
    )
    assert bundle["semantic_identity_sha256"] == expected_bundle_identity
    for reference in pair.references:
        entry = {field: reference[field] for field in evidence.REGISTRY_ENTRY_KEYS}
        assert reference["registry_entry_sha256"] == evidence.sha256_digest(
            evidence.REGISTRY_ENTRY_DIGEST_DOMAIN + evidence.canonical_json_bytes(entry)
        )


def test_decision_receipt_uses_series_identity_and_canonical_json_lf() -> None:
    receipt = _decision_receipt(_campaign_summary())
    pair = _pair("qualification-evidence-decision-receipt", object_value=receipt)
    assert pair.objects[0] == evidence.canonical_json_bytes(receipt) + b"\n"
    assert pair.references[0]["semantic_identity_sha256"] == evidence.sha256_digest(
        evidence.DECISION_RECEIPT_IDENTITY_DOMAIN
        + evidence.canonical_json_bytes(
            {
                "decision_identity_sha256": receipt["decision_identity_sha256"],
                "decision_revision": receipt["decision_revision"],
                "evidence_class": receipt["evidence_class"],
            }
        )
    )


def test_rejects_bundle_before_regular_or_wrong_subject() -> None:
    pair = _pair("qualification-campaign-permit")
    with pytest.raises(evidence.ProductionEvidenceError):
        evidence.validate_reference_pair(tuple(reversed(pair.references)))

    regular, bundle = copy.deepcopy(pair.references)
    bundle["subject_sha256"] = _digest("f")
    bundle["registry_entry_sha256"] = evidence._registry_entry_sha256(bundle)
    with pytest.raises(evidence.ProductionEvidenceError, match="subject"):
        evidence.validate_reference_pair((regular, bundle))


def test_signer_registry_pointer_has_only_frozen_fields() -> None:
    registry = {
        "schema_version": "openadapt.qualification-signer-registry/v2",
        "revision": 9,
    }
    pointer = evidence.build_signer_registry_pointer(
        object_path="production-evidence/authority/signer-registry.json",
        object_sha256=_digest("a"),
        registry_identity_sha256=evidence.signer_registry_identity(registry),
        registry_revision=9,
    )
    assert pointer == {
        "schema_version": "openadapt.qualification-signer-registry-pointer/v1",
        "object_path": "production-evidence/authority/signer-registry.json",
        "object_sha256": _digest("a"),
        "registry_identity_sha256": evidence.signer_registry_identity(registry),
        "registry_revision": 9,
    }


def test_builds_remote_safe_summary_from_three_prior_reference_pairs() -> None:
    summary, receipt, admission, manifest, expected_assets = _summary()
    assert set(summary) == evidence._SUMMARY_KEYS
    assert summary["schema_version"] == ("openadapt.production-lifecycle-evidence-summary/v3")
    assert summary["verdict"] == "accepted"
    assert set(summary["campaign_summary"]) == evidence._CAMPAIGN_CLASSES
    rendered = json.dumps(summary["campaign_summary"], sort_keys=True)
    for private_field in (
        "task_name",
        "condition_name",
        "application",
        "environment",
        "live_identity",
    ):
        assert private_field not in rendered
    _validate_summary(
        summary,
        qualification_evidence_decision_receipt=receipt,
        qualification_admission=admission,
        production_acceptance_manifest=manifest,
        expected_publication_assets=expected_assets,
    )


def test_summary_binds_durable_release_app_staging_and_two_tag_rulesets() -> None:
    summary, receipt, admission, manifest, expected_assets = _summary()
    staging = summary["publication_staging"]
    assert staging["draft"] is True
    assert staging["prerelease"] is False
    assert staging["release_app_id"] == "4730708"
    assert staging["release_app_installation_id"] == "156835568"
    assert staging["release_app_bot_user_id"] == "321543906"
    assert [ruleset["role"] for ruleset in staging["tag_rulesets"]] == [
        "creation_authority",
        "immutability",
    ]
    assert summary["publication_staging_sha256"] == evidence.sha256_digest(
        evidence.PUBLICATION_STAGING_DIGEST_DOMAIN + evidence.canonical_json_bytes(staging)
    )

    mutated = copy.deepcopy(summary)
    mutated["publication_staging"]["immutable_releases"]["enabled"] = False
    with pytest.raises(evidence.ProductionEvidenceError, match="immutable"):
        _validate_summary(
            mutated,
            qualification_evidence_decision_receipt=receipt,
            qualification_admission=admission,
            production_acceptance_manifest=manifest,
            expected_publication_assets=expected_assets,
        )


def test_summary_rejects_draft_asset_or_tag_authority_drift() -> None:
    summary, receipt, admission, manifest, expected_assets = _summary()
    changed_assets = copy.deepcopy(expected_assets)
    changed_assets[0]["size_bytes"] += 1
    with pytest.raises(evidence.ProductionEvidenceError, match="release candidate"):
        _validate_summary(
            summary,
            qualification_evidence_decision_receipt=receipt,
            qualification_admission=admission,
            production_acceptance_manifest=manifest,
            expected_publication_assets=changed_assets,
        )

    changed_assets = copy.deepcopy(expected_assets)
    changed_assets[0]["media_type"] = "application/zip"
    with pytest.raises(evidence.ProductionEvidenceError, match="release candidate"):
        _validate_summary(
            summary,
            qualification_evidence_decision_receipt=receipt,
            qualification_admission=admission,
            production_acceptance_manifest=manifest,
            expected_publication_assets=changed_assets,
        )

    changed_rules = copy.deepcopy(summary)
    changed_rules["publication_staging"]["tag_rulesets"][1]["bypass_actors"] = [
        {
            "actor_id": "4730708",
            "actor_type": "Integration",
            "bypass_mode": "always",
        }
    ]
    with pytest.raises(evidence.ProductionEvidenceError, match="bypass"):
        _validate_summary(
            changed_rules,
            qualification_evidence_decision_receipt=receipt,
            qualification_admission=admission,
            production_acceptance_manifest=manifest,
            expected_publication_assets=expected_assets,
        )


def test_summary_rejects_release_target_and_validity_drift() -> None:
    summary, receipt, admission, manifest, expected_assets = _summary()
    changed_manifest = copy.deepcopy(manifest)
    changed_manifest["release"]["source_repository"] = "OpenAdaptAI/openadapt-desktop"
    with pytest.raises(evidence.ProductionEvidenceError, match="candidate identity"):
        _validate_summary(
            summary,
            qualification_evidence_decision_receipt=receipt,
            qualification_admission=admission,
            production_acceptance_manifest=changed_manifest,
            expected_publication_assets=expected_assets,
        )

    changed_manifest = copy.deepcopy(manifest)
    changed_manifest["release"]["deployment_id"] = "12"
    changed_manifest["release"]["deployment_sha256"] = _digest("d")
    with pytest.raises(evidence.ProductionEvidenceError, match="package release candidate profile"):
        _validate_summary(
            summary,
            qualification_evidence_decision_receipt=receipt,
            qualification_admission=admission,
            production_acceptance_manifest=changed_manifest,
            expected_publication_assets=expected_assets,
        )

    changed_summary = copy.deepcopy(summary)
    changed_summary["expires_at"] = "2026-09-03T10:30:00Z"
    with pytest.raises(evidence.ProductionEvidenceError, match="exceeds the manifest"):
        _validate_summary(
            changed_summary,
            qualification_evidence_decision_receipt=receipt,
            qualification_admission=admission,
            production_acceptance_manifest=manifest,
            expected_publication_assets=expected_assets,
        )


def test_summary_rejects_noncanonical_admission_fields() -> None:
    summary, receipt, admission, manifest, expected_assets = _summary()
    changed_admission = copy.deepcopy(admission)
    changed_admission["bundle_version"] = "v1.35"
    with pytest.raises(evidence.ProductionEvidenceError, match="bundle_version"):
        _validate_summary(
            summary,
            qualification_evidence_decision_receipt=receipt,
            qualification_admission=changed_admission,
            production_acceptance_manifest=manifest,
            expected_publication_assets=expected_assets,
        )


def test_summary_extracts_only_the_remote_safe_receipt_classes() -> None:
    summary, receipt, admission, manifest, expected_assets = _summary()
    changed_receipt = copy.deepcopy(receipt)
    changed_receipt["entity_class"] = "custom customer noun"
    with pytest.raises(evidence.ProductionEvidenceError, match="remote-safe"):
        _validate_summary(
            summary,
            qualification_evidence_decision_receipt=changed_receipt,
            qualification_admission=admission,
            production_acceptance_manifest=manifest,
            expected_publication_assets=expected_assets,
        )


def test_summary_identity_uses_the_exact_frozen_projection() -> None:
    summary, _, _, _, _ = _summary()
    projection = {
        field: summary[field]
        for field in (
            "target",
            "claim_scope",
            "release_identity",
            "release_sha256",
            "artifact_inventory_sha256",
            "publication_staging_sha256",
            "qualification_evidence_decision_receipt_reference",
            "qualification_admission_reference",
            "production_acceptance_manifest_reference",
            "campaign_summary",
            "authority_state_sha256",
            "revocation_state_sha256",
            "signer_registry_sha256",
            "issuer",
        )
    }
    assert summary["evidence_identity_sha256"] == (
        "sha256:"
        + hashlib.sha256(
            evidence.PRODUCTION_EVIDENCE_IDENTITY_DOMAIN + evidence.canonical_json_bytes(projection)
        ).hexdigest()
    )


def test_summary_rejects_private_counts_or_receipt_drift() -> None:
    summary, receipt, admission, manifest, expected_assets = _summary()
    mutated = copy.deepcopy(summary)
    mutated["campaign_summary"]["healthy"]["task_name"] = 1
    with pytest.raises(evidence.ProductionEvidenceError, match="keys"):
        _validate_summary(
            mutated,
            qualification_evidence_decision_receipt=receipt,
            qualification_admission=admission,
            production_acceptance_manifest=manifest,
            expected_publication_assets=expected_assets,
        )

    different_receipt = copy.deepcopy(receipt)
    different_receipt["campaign_summary"]["classes"]["healthy"]["observed_trial_count"] = 4
    with pytest.raises(evidence.ProductionEvidenceError, match="signing_statement"):
        _validate_summary(
            summary,
            qualification_evidence_decision_receipt=different_receipt,
            qualification_admission=admission,
            production_acceptance_manifest=manifest,
            expected_publication_assets=expected_assets,
        )


@pytest.mark.parametrize(
    "qualification_class,field,value",
    [
        ("healthy", "silent_incorrect_success_count", 1),
        ("safe_halt", "over_halt_count", 1),
        ("uncertain_delivery", "replay_dispatch_count", 1),
        ("uncertain_delivery", "reconciliation_required_count", 2),
        ("declared_attended", "authenticated_bound_decision_count", 2),
        ("governed_repair", "unverified_direct_action_count", 1),
    ],
)
def test_summary_rejects_nonqualifying_class_counts(
    qualification_class: str, field: str, value: int
) -> None:
    summary, receipt, admission, manifest, expected_assets = _summary()
    summary["campaign_summary"][qualification_class][field] = value
    with pytest.raises(evidence.ProductionEvidenceError):
        _validate_summary(
            summary,
            qualification_evidence_decision_receipt=receipt,
            qualification_admission=admission,
            production_acceptance_manifest=manifest,
            expected_publication_assets=expected_assets,
        )


def test_summary_can_delegate_receipt_signature_verification() -> None:
    summary, receipt, admission, manifest, expected_assets = _summary()

    class Verifier:
        calls: list[tuple[str, str, str, str, str, datetime]] = []
        registered_calls: list[str] = []

        def verify(
            self,
            value: dict[str, object],
            *,
            reference: dict[str, object],
            bundle_reference: dict[str, object],
            object_sha256: str,
            semantic_identity_sha256: str,
            authority_state_sha256: str,
            signer_registry_sha256: str,
            revocation_state_sha256: str,
            evaluation_time: datetime,
        ) -> None:
            assert value == receipt
            assert reference["object_sha256"] == object_sha256
            assert bundle_reference["subject_sha256"] == object_sha256
            self.calls.append(
                (
                    object_sha256,
                    semantic_identity_sha256,
                    authority_state_sha256,
                    signer_registry_sha256,
                    revocation_state_sha256,
                    evaluation_time,
                )
            )

        def verify_registered_object(
            self,
            value: dict[str, object],
            *,
            reference: dict[str, object],
            bundle_reference: dict[str, object],
            kind: str,
            authority_state_sha256: str,
            signer_registry_sha256: str,
            revocation_state_sha256: str,
            evaluation_time: datetime,
        ) -> None:
            assert reference["kind"] == kind
            assert bundle_reference["subject_sha256"] == reference["object_sha256"]
            assert authority_state_sha256 == summary["authority_state_sha256"]
            assert signer_registry_sha256 == summary["signer_registry_sha256"]
            assert revocation_state_sha256 == summary["revocation_state_sha256"]
            assert evaluation_time == _EVALUATION_TIME
            assert (
                evidence.sha256_digest(evidence.canonical_json_bytes(value) + b"\n")
                == (reference["object_sha256"])
            )
            self.registered_calls.append(kind)

    verifier = Verifier()
    _validate_summary(
        summary,
        qualification_evidence_decision_receipt=receipt,
        qualification_admission=admission,
        production_acceptance_manifest=manifest,
        expected_publication_assets=expected_assets,
        decision_receipt_verifier=verifier,
    )
    assert verifier.calls == [
        (
            summary["qualification_evidence_decision_receipt_reference"]["object_sha256"],
            summary["qualification_evidence_decision_receipt_reference"][
                "semantic_identity_sha256"
            ],
            summary["authority_state_sha256"],
            summary["signer_registry_sha256"],
            summary["revocation_state_sha256"],
            _EVALUATION_TIME,
        )
    ]
    assert verifier.registered_calls == [
        "qualification-admission",
        "production-acceptance-manifest",
    ]


def test_placeholder_decision_signature_cannot_produce_accepted_evidence() -> None:
    receipt = _decision_receipt(_campaign_summary())
    receipt["signature"] = base64.b64encode(b"placeholder-signature".ljust(64, b"!")).decode()
    pair = _pair("qualification-evidence-decision-receipt", object_value=receipt)
    with pytest.raises(evidence.ProductionEvidenceError, match="signature verification failed"):
        evidence._validate_decision_receipt(
            receipt,
            reference=pair.references[0],
            bundle_reference=pair.references[1],
            expected_authority_state_sha256=_digest("6"),
            expected_signer_registry_sha256=_digest("8"),
            expected_revocation_state_sha256=_digest("7"),
            verifier=_verifier(),
            evaluation_time=_EVALUATION_TIME,
        )


def test_decision_receipt_must_be_current_and_match_registered_identity() -> None:
    receipt = _decision_receipt(_campaign_summary())
    pair = _pair("qualification-evidence-decision-receipt", object_value=receipt)
    with pytest.raises(evidence.ProductionEvidenceError, match="not active"):
        evidence._validate_decision_receipt(
            receipt,
            reference=pair.references[0],
            bundle_reference=pair.references[1],
            expected_authority_state_sha256=_digest("6"),
            expected_signer_registry_sha256=_digest("8"),
            expected_revocation_state_sha256=_digest("7"),
            verifier=_verifier(),
            evaluation_time=datetime(2026, 9, 3, 11, 0, tzinfo=timezone.utc),
        )

    changed_reference = dict(pair.references[0])
    changed_reference["semantic_identity_sha256"] = _digest("f")
    changed_reference["registry_entry_sha256"] = evidence._registry_entry_sha256(changed_reference)
    with pytest.raises(evidence.ProductionEvidenceError, match="semantic identity differs"):
        evidence._validate_decision_receipt(
            receipt,
            reference=changed_reference,
            bundle_reference=pair.references[1],
            expected_authority_state_sha256=_digest("6"),
            expected_signer_registry_sha256=_digest("8"),
            expected_revocation_state_sha256=_digest("7"),
            verifier=_verifier(),
            evaluation_time=_EVALUATION_TIME,
        )


def test_decision_receipt_minimum_is_exactly_three() -> None:
    receipt = _decision_receipt(_campaign_summary())
    receipt["campaign_summary"]["minimum_trials_per_task_condition"] = 4
    unsigned = dict(receipt)
    unsigned.pop("signature")
    unsigned.pop("signing_statement")
    unsigned_bytes = evidence.canonical_json_bytes(unsigned) + b"\n"
    receipt["signing_statement"] = {
        "schema_version": "openadapt.qualification-evidence-signing-statement/v1",
        "object_schema_version": evidence.DECISION_RECEIPT_SCHEMA,
        "signature_domain": evidence.DECISION_RECEIPT_SIGNATURE_DOMAIN.decode(),
        "unsigned_object_sha256": evidence.sha256_digest(unsigned_bytes),
        "unsigned_size_bytes": len(unsigned_bytes),
        "commitment_scheme": "sha256-canonical-json-lf",
    }
    receipt["signature"] = base64.b64encode(
        _DECISION_PRIVATE_KEY.sign(
            evidence.canonical_json_bytes(receipt["signing_statement"]) + b"\n"
        )
    ).decode()
    pair = _pair("qualification-evidence-decision-receipt", object_value=receipt)
    with pytest.raises(evidence.ProductionEvidenceError, match="exactly three"):
        evidence._validate_decision_receipt(
            receipt,
            reference=pair.references[0],
            bundle_reference=pair.references[1],
            expected_authority_state_sha256=_digest("6"),
            expected_signer_registry_sha256=_digest("8"),
            expected_revocation_state_sha256=_digest("7"),
            verifier=_verifier(),
            evaluation_time=_EVALUATION_TIME,
        )


def test_validates_flow_only_release_verification_receipt_v1() -> None:
    summary, _, admission, _, _ = _summary()
    receipt: dict[str, object] = {
        "schema_version": evidence.RELEASE_VERIFICATION_RECEIPT_SCHEMA,
        "verification_id_sha256": _digest("0"),
        "verdict": "verified",
        "evidence_class": "remote-safe-synthetic",
        "target": "flow",
        "claim_scope": "production_flow",
        "admission_object_sha256": _digest("1"),
        "admission_bundle_object_sha256": _digest("2"),
        "admission_id_sha256": _digest("3"),
        "release_sha256": summary["release_sha256"],
        "artifact_inventory_sha256": summary["artifact_inventory_sha256"],
        "release_identity": summary["release_identity"],
        "source_repository": "OpenAdaptAI/openadapt-flow",
        "source_repository_id": "1291376938",
        "source_commit": "c" * 40,
        "version": "1.35.0",
        "tag": "v1.35.0",
        "draft_release_id": "456789",
        "publication_staging_sha256": summary["publication_staging_sha256"],
        "authority_state_sha256": summary["authority_state_sha256"],
        "revocation_state_sha256": summary["revocation_state_sha256"],
        "signer_registry_sha256": summary["signer_registry_sha256"],
        "acceptance_summary_object_sha256": _digest("4"),
        "acceptance_manifest_object_sha256": summary["production_acceptance_manifest_reference"][
            "object_sha256"
        ],
        "decision_receipt_object_sha256": summary[
            "qualification_evidence_decision_receipt_reference"
        ]["object_sha256"],
        "qualification_admission_object_sha256": summary["qualification_admission_reference"][
            "object_sha256"
        ],
        "qualification_admission_id_sha256": admission["admission_id_sha256"],
        "workflow_version_id_sha256": admission["workflow_version_id_sha256"],
        "workflow_bundle_sha256": admission["bundle_sha256"],
        "admitted_runtime_sha256": admission["admitted_runtime_sha256"],
        "verified_at": "2026-08-27T12:00:00Z",
        "expires_at": "2026-09-03T10:00:00Z",
        "registry_source_commit": "a" * 40,
        "registry_revision": 4,
        "registry_head_sha256": _digest("5"),
        "trust_state_source_commit": evidence.CENTRAL_TRUST_CONTRACT_COMMIT,
    }
    projection = dict(receipt)
    projection.pop("verification_id_sha256")
    receipt["verification_id_sha256"] = evidence.sha256_digest(
        evidence.RELEASE_VERIFICATION_RECEIPT_DOMAIN + evidence.canonical_json_bytes(projection)
    )
    evidence.validate_release_verification_receipt(receipt)

    changed = copy.deepcopy(receipt)
    changed["target"] = "desktop"
    with pytest.raises(evidence.ProductionEvidenceError, match="target"):
        evidence.validate_release_verification_receipt(changed)


def test_accepts_exact_pinned_central_flow_verification_fixture() -> None:
    path = ROOT / "tests/fixtures/central-989681f6-flow-release-verification.json"
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == (
        "bf170018cd1d3519f40ebd2788e158dd34a789c7bfe1e85e30e3a445e5e40af8"
    )
    evidence.validate_release_verification_receipt(json.loads(raw))
