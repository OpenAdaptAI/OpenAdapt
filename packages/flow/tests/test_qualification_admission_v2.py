from __future__ import annotations

import json
from base64 import b64encode
from datetime import datetime, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from openadapt_flow.qualification_admission_v2 import (
    JS_MAX_SAFE_INTEGER,
    PRODUCTION_ACCEPTANCE_ADMISSION_POLICY_SHA256,
    AdmittedRuntimeBuildIdentity,
    ManagedBrowserBuild,
    ProductionAcceptanceEvidenceIdentity,
    QualificationAdmissionError,
    QualificationAdmissionExpected,
    QualificationAdmissionPayload,
    QualificationCampaignBinding,
    QualificationCondition,
    QualificationIssuer,
    QualificationPermitTrustSnapshot,
    QualificationSignerRegistry,
    QualificationSignerRegistryCheckpoint,
    RuntimeComponent,
    RuntimeComponentManifest,
    initialize_local_qualification_signer_checkpoint,
    initialize_qualification_signer_registry_checkpoint,
    load_local_qualification_signer_registry,
    qualification_signer_key_id,
    read_qualification_signer_checkpoint_file,
    sign_qualification_admission,
    validate_qualification_signer_registry_transition,
    verify_qualification_admission,
    verify_qualification_admission_for_actuation,
)
from openadapt_flow.qualification_campaign_permit import (
    QUALIFICATION_CAMPAIGN_EXECUTION_POLICY_SHA256,
    QualificationCampaignPermitError,
    QualificationCampaignPermitExpected,
    QualificationCampaignPermitPayload,
    QualificationCampaignTrialBinding,
    sign_qualification_campaign_permit,
    verify_qualification_campaign_permit,
)

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64
COMMIT = "a" * 40
NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)

IDS = {
    "admission_id": "00000000-0000-4000-8000-000000000001",
    "tenant_id": "00000000-0000-4000-8000-000000000002",
    "workflow_id": "00000000-0000-4000-8000-000000000003",
    "workflow_version_id": "00000000-0000-4000-8000-000000000004",
    "bundle_version_id": "00000000-0000-4000-8000-000000000004",
    "runtime_validation_id": "00000000-0000-4000-8000-000000000005",
    "campaign_id": "00000000-0000-4000-8000-000000000006",
}


def _private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(bytes(range(32)))


def _public_key() -> bytes:
    return (
        _private_key()
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    )


def _component(
    name: str,
    *,
    roles: tuple[str, ...],
    capabilities: tuple[str, ...],
    artifact_sha256: str = SHA_A,
    version: str = "1.2.3",
) -> RuntimeComponent:
    return RuntimeComponent.model_validate(
        {
            "name": name,
            "version": version,
            "release_commit": COMMIT,
            "artifact_sha256": artifact_sha256,
            "roles": roles,
            "capabilities": capabilities,
        }
    )


def _runtime_build(substrate: str = "web") -> AdmittedRuntimeBuildIdentity:
    flow = _component(
        "openadapt-flow",
        roles=("orchestrator",),
        capabilities=("flow_runtime",),
    )
    if substrate == "web":
        components = (
            flow,
            _component(
                "openadapt-web-runner",
                roles=("actuator", "observer", "orchestrator"),
                capabilities=(
                    "browser_actuation",
                    "browser_observation",
                    "runner_wrapper",
                ),
                artifact_sha256=SHA_B,
                version="report-v5",
            ),
        )
        browser = ManagedBrowserBuild(
            playwright_version="1.55.0",
            browser_base_image=f"openadapt/browser@sha256:{SHA_A}",
        )
        os_family = "linux"
        transport = "browser"
    elif substrate in {"windows", "macos", "linux"}:
        observer = {
            "windows": "uia_observation",
            "macos": "accessibility_observation",
            "linux": "atspi_observation",
        }[substrate]
        components = tuple(
            sorted(
                (
                    _component(
                        "openadapt-agent",
                        roles=("actuator", "orchestrator"),
                        capabilities=("native_actuation", "runner_wrapper"),
                        artifact_sha256=SHA_B,
                        version="report-v5",
                    ),
                    _component(
                        "openadapt-capture",
                        roles=("observer",),
                        capabilities=("native_capture", "pixel_observation"),
                    ),
                    flow,
                    _component(
                        f"openadapt-{observer}",
                        roles=("observer",),
                        capabilities=(observer,),
                    ),
                ),
                key=lambda item: item.name,
            )
        )
        browser = None
        os_family = substrate
        transport = "local"
    else:
        components = (
            _component(
                "openadapt-agent",
                roles=("actuator", "orchestrator"),
                capabilities=("native_actuation", "runner_wrapper"),
                artifact_sha256=SHA_B,
                version="report-v5",
            ),
            _component(
                "openadapt-capture",
                roles=("observer",),
                capabilities=("native_capture", "pixel_observation"),
            ),
            flow,
            _component(
                "openadapt-remote-transport",
                roles=("transport",),
                capabilities=("remote_transport",),
            ),
        )
        browser = None
        os_family = "windows"
        transport = substrate
    manifest = RuntimeComponentManifest(components=components)
    return AdmittedRuntimeBuildIdentity.model_validate(
        {
            "substrate": substrate,
            "flow_version": "1.2.3",
            "flow_release_commit": COMMIT,
            "flow_wheel_sha256": SHA_A,
            "runtime_manifest": manifest,
            "runtime_manifest_sha256": manifest.artifact_sha256(),
            "runner_build": "report-v5",
            "runner_artifact_sha256": SHA_B,
            "managed_browser": browser,
            "substrate_runtime": {
                "os_family": os_family,
                "transport": transport,
                "runtime_boundary_sha256": SHA_C,
            },
        }
    )


def _registry(
    *, revision: int = 7, status: str = "active"
) -> QualificationSignerRegistry:
    key = _public_key()
    return QualificationSignerRegistry.model_validate(
        {
            "revision": revision,
            "generated_at": "2026-08-18T11:00:00Z",
            "expires_at": "2026-08-20T11:00:00Z",
            "signers": [
                {
                    "algorithm": "ed25519",
                    "key_id": qualification_signer_key_id(key),
                    "public_key": b64encode(key).decode("ascii"),
                    "allowed_workflows": [
                        "OpenAdaptAI/openadapt-internal/.github/workflows/issue-qualification-campaign-permit.yml",
                        "OpenAdaptAI/openadapt-internal/.github/workflows/production-qualification-admission.yml",
                    ],
                    "allowed_ref_prefixes": ["refs/heads/main@"],
                    "status": status,
                    "revoked_at": (
                        None if status == "active" else "2026-08-18T10:00:00Z"
                    ),
                }
            ],
        }
    )


def _payload(
    registry: QualificationSignerRegistry,
    *,
    runtime_build: AdmittedRuntimeBuildIdentity | None = None,
) -> QualificationAdmissionPayload:
    runtime_build = runtime_build or _runtime_build()
    identity = ProductionAcceptanceEvidenceIdentity.model_validate(
        {
            "runtime_build_identity": runtime_build,
            "evidence_runner_signer_sha256": SHA_B,
            "deployment_manifest_sha256": SHA_D,
            "qualification_signer_registry_sha256": registry.artifact_sha256(),
            "qualification_signer_registry_revision": registry.revision,
            **IDS,
            "workflow_digest": SHA_A,
            "bundle_artifact_sha256": SHA_B,
            "bundle_content_digest": SHA_A,
            "environment_digest": SHA_E,
            "governed_authorization_template_sha256": SHA_A,
            "application_contract_sha256": SHA_A,
            "substrate_contract_sha256": SHA_A,
            "environment_contract_sha256": SHA_A,
            "runtime_environment_sha256": SHA_C,
            "runtime_contract_sha256": SHA_A,
            "input_policy_sha256": SHA_A,
            "action_policy_sha256": SHA_A,
            "network_policy_sha256": SHA_A,
            "identity_contract_sha256": SHA_A,
            "effect_contract_sha256": SHA_A,
            "operator_contract_sha256": SHA_A,
            "campaign_contract_sha256": SHA_C,
            "oracle_contract_sha256": SHA_D,
            "qualification_campaign_schema": "openadapt.qualification-campaign/v2",
            "qualification_trial_schema": "openadapt.qualification-trial-row/v2",
            "qualification_receipt_schema": "openadapt.qualification-evidence-receipt/v2",
            "admission_policy_sha256": PRODUCTION_ACCEPTANCE_ADMISSION_POLICY_SHA256,
        }
    )
    campaign = QualificationCampaignBinding(
        campaign_id=IDS["campaign_id"],
        artifact_sha256=SHA_B,
        contract_sha256=SHA_C,
        outcomes_sha256=SHA_D,
        oracle_id="independent-postcondition-oracle",
        oracle_contract_sha256=SHA_D,
        tasks=(
            QualificationCondition(
                task="submit-record",
                condition="healthy",
                required_trials=3,
                observed_trials=3,
            ),
        ),
        failure_taxonomy=(
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
        ),
    )
    return QualificationAdmissionPayload.model_validate(
        {
            **{key: value for key, value in IDS.items() if key != "campaign_id"},
            "bundle_artifact_sha256": SHA_B,
            "bundle_content_digest": SHA_A,
            "environment_digest": SHA_E,
            "governed_authorization_template_sha256": SHA_A,
            "application_contract_sha256": SHA_A,
            "substrate_contract_sha256": SHA_A,
            "environment_contract_sha256": SHA_A,
            "runtime_environment_sha256": SHA_C,
            "runtime_contract_sha256": SHA_A,
            "input_policy_sha256": SHA_A,
            "action_policy_sha256": SHA_A,
            "network_policy_sha256": SHA_A,
            "identity_contract_sha256": SHA_A,
            "effect_contract_sha256": SHA_A,
            "operator_contract_sha256": SHA_A,
            "evidence_identity": identity,
            "campaign": campaign,
            "issuer": QualificationIssuer(
                key_id=qualification_signer_key_id(_public_key()),
                workflow=(
                    "OpenAdaptAI/openadapt-internal/.github/workflows/"
                    "production-qualification-admission.yml"
                ),
                ref="refs/heads/main@" + COMMIT,
            ),
            "issued_at": "2026-08-18T11:30:00Z",
            "not_before": "2026-08-18T11:30:00Z",
            "expires_at": "2026-08-20T11:30:00Z",
        }
    )


def _expected(payload: QualificationAdmissionPayload) -> QualificationAdmissionExpected:
    return QualificationAdmissionExpected.model_validate(
        {
            "tenant_id": IDS["tenant_id"],
            "workflow_id": IDS["workflow_id"],
            "workflow_version_id": IDS["workflow_version_id"],
            "bundle_version_id": IDS["bundle_version_id"],
            "runtime_validation_id": IDS["runtime_validation_id"],
            "bundle_artifact_sha256": SHA_B,
            "bundle_content_digest": SHA_A,
            "environment_digest": SHA_E,
            "governed_authorization_template_sha256": SHA_A,
            "application_contract_sha256": SHA_A,
            "substrate_contract_sha256": SHA_A,
            "environment_contract_sha256": SHA_A,
            "runtime_environment_sha256": SHA_C,
            "runtime_contract_sha256": SHA_A,
            "input_policy_sha256": SHA_A,
            "action_policy_sha256": SHA_A,
            "network_policy_sha256": SHA_A,
            "identity_contract_sha256": SHA_A,
            "effect_contract_sha256": SHA_A,
            "operator_contract_sha256": SHA_A,
            "runtime_build_identity": payload.evidence_identity.runtime_build_identity,
            "evidence_runner_signer_sha256": SHA_B,
            "deployment_manifest_sha256": SHA_D,
        }
    )


def _snapshot(
    registry: QualificationSignerRegistry,
) -> QualificationPermitTrustSnapshot:
    return QualificationPermitTrustSnapshot(
        qualification_signer_registry_sha256=registry.artifact_sha256(),
        qualification_signer_registry_revision=registry.revision,
        qualification_signer_registry_checked_at="2026-08-18T11:59:30Z",
        qualification_signer_registry_expires_at=registry.expires_at,
    )


def _campaign_permit_payload(
    registry: QualificationSignerRegistry,
) -> QualificationCampaignPermitPayload:
    runtime = _runtime_build("web")
    trial = QualificationCampaignTrialBinding(
        campaign_id=IDS["campaign_id"],
        campaign_contract_sha256=SHA_A,
        trial_id="00000000-0000-4000-8000-000000000007",
        qualification_run_id="00000000-0000-4000-8000-000000000008",
        task="submit-record",
        condition="healthy",
        trial_ordinal=1,
        required_trials=3,
        input_digest=SHA_B,
        oracle_contract_sha256=SHA_C,
        evidence_sink_sha256=SHA_D,
    )
    return QualificationCampaignPermitPayload(
        permit_id="00000000-0000-4000-8000-000000000009",
        tenant_id=IDS["tenant_id"],
        workflow_id=IDS["workflow_id"],
        workflow_version_id=IDS["workflow_version_id"],
        bundle_version_id=IDS["bundle_version_id"],
        bundle_artifact_sha256=SHA_B,
        bundle_content_digest=SHA_A,
        environment_digest=SHA_E,
        environment_contract_sha256=SHA_C,
        runtime_environment_sha256=SHA_D,
        runtime_build_identity=runtime,
        runtime_build_identity_sha256=runtime.artifact_sha256(),
        qualification_signer_registry_sha256=registry.artifact_sha256(),
        qualification_signer_registry_revision=registry.revision,
        trial=trial,
        policy_sha256=QUALIFICATION_CAMPAIGN_EXECUTION_POLICY_SHA256,
        issuer=QualificationIssuer(
            key_id=qualification_signer_key_id(_public_key()),
            workflow=(
                "OpenAdaptAI/openadapt-internal/.github/workflows/"
                "issue-qualification-campaign-permit.yml"
            ),
            ref="refs/heads/main@" + COMMIT,
        ),
        issued_at="2026-08-18T11:30:00Z",
        not_before="2026-08-18T11:30:00Z",
        expires_at="2026-08-18T13:30:00Z",
    )


def _campaign_expected(
    payload: QualificationCampaignPermitPayload,
) -> QualificationCampaignPermitExpected:
    return QualificationCampaignPermitExpected(
        tenant_id=payload.tenant_id,
        workflow_id=payload.workflow_id,
        workflow_version_id=payload.workflow_version_id,
        bundle_version_id=payload.bundle_version_id,
        bundle_artifact_sha256=payload.bundle_artifact_sha256,
        bundle_content_digest=payload.bundle_content_digest,
        environment_digest=payload.environment_digest,
        environment_contract_sha256=payload.environment_contract_sha256,
        runtime_environment_sha256=payload.runtime_environment_sha256,
        runtime_build_identity=payload.runtime_build_identity,
        trial=payload.trial,
    )


@pytest.mark.parametrize(
    "substrate", ["web", "windows", "macos", "linux", "rdp", "citrix"]
)
def test_runtime_identity_covers_each_supported_substrate(substrate: str) -> None:
    assert _runtime_build(substrate).substrate == substrate


def test_remote_runtime_rejects_structural_observer() -> None:
    runtime = _runtime_build("rdp")
    observer = _component(
        "openadapt-uia-observer",
        roles=("observer",),
        capabilities=("uia_observation",),
    )
    manifest = runtime.runtime_manifest.model_copy(
        update={
            "components": tuple(
                sorted(
                    (*runtime.runtime_manifest.components, observer),
                    key=lambda item: item.name,
                )
            )
        }
    )
    with pytest.raises(ValidationError, match="invalid capabilities"):
        runtime.model_copy(
            update={
                "runtime_manifest": manifest,
                "runtime_manifest_sha256": manifest.artifact_sha256(),
            }
        ).__class__.model_validate(
            runtime.model_copy(
                update={
                    "runtime_manifest": manifest,
                    "runtime_manifest_sha256": manifest.artifact_sha256(),
                }
            ).model_dump()
        )


def test_signed_admission_matches_live_environment_and_runtime() -> None:
    registry = _registry()
    payload = _payload(registry)
    envelope = sign_qualification_admission(payload, _private_key())
    verified = verify_qualification_admission(
        envelope,
        registry=registry,
        expected=_expected(payload),
        now=NOW,
    )
    assert verified.admission_artifact_sha256 == envelope.artifact_sha256()
    assert verified.registry_revision == 7


@pytest.mark.parametrize(
    "replacement",
    [
        [
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
        ],
        [
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
            "unexpected",
        ],
        [
            "collateral_effect",
            "duplicate_effect",
            "healthy_path_model_call",
            "operator_intervention",
            "over_halt",
            "platform_failure",
            "safe_halt",
            "silent_incorrect_success",
            "uncertain_delivery",
            "wrong_record",
            "verified",
        ],
        [
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
            "wrong_item",
        ],
    ],
    ids=("omission", "addition", "reordering", "substitution"),
)
def test_campaign_requires_exact_policy_taxonomy(replacement: list[str]) -> None:
    campaign = _payload(_registry()).campaign.model_dump(mode="json")
    campaign["failure_taxonomy"] = replacement
    with pytest.raises(ValidationError, match="taxonomy does not match policy"):
        QualificationCampaignBinding.model_validate(campaign)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("workflow", "OpenAdaptAI/alternate/.github/workflows/admit.yml"),
        (
            "workflow",
            "OpenAdaptAI/openadapt-internal/.github/workflows/"
            "issue-qualification-campaign-permit.yml",
        ),
        ("ref", "refs/heads/release@" + COMMIT),
        ("ref", "refs/heads/main@" + "a" * 39),
        ("ref", "refs/heads/main@" + "A" * 40),
    ],
)
def test_admission_requires_fixed_policy_issuer(field: str, replacement: str) -> None:
    data = _payload(_registry()).model_dump(mode="json")
    data["issuer"][field] = replacement
    with pytest.raises(ValidationError, match=f"issuer {field} does not match policy"):
        QualificationAdmissionPayload.model_validate(data)


def test_live_environment_mismatch_is_refused() -> None:
    registry = _registry()
    payload = _payload(registry)
    expected = _expected(payload).model_copy(update={"environment_digest": SHA_D})
    with pytest.raises(
        QualificationAdmissionError,
        match="environment_digest does not match the live run",
    ):
        verify_qualification_admission(
            sign_qualification_admission(payload, _private_key()),
            registry=registry,
            expected=expected,
            now=NOW,
        )


def test_revoked_signer_is_refused() -> None:
    admission_registry = _registry()
    payload = _payload(admission_registry)
    revoked_registry = _registry(status="revoked")
    identity = payload.evidence_identity.model_copy(
        update={
            "qualification_signer_registry_sha256": revoked_registry.artifact_sha256()
        }
    )
    resigned = payload.model_copy(update={"evidence_identity": identity})
    with pytest.raises(QualificationAdmissionError, match="signer is revoked"):
        verify_qualification_admission(
            sign_qualification_admission(resigned, _private_key()),
            registry=revoked_registry,
            expected=_expected(resigned),
            now=NOW,
        )


def test_actuation_rejects_stale_permit_registry_snapshot() -> None:
    registry = _registry()
    payload = _payload(registry)
    stale = _snapshot(registry).model_copy(
        update={"qualification_signer_registry_checked_at": "2026-08-18T11:58:00Z"}
    )
    with pytest.raises(QualificationAdmissionError, match="snapshot is stale"):
        verify_qualification_admission_for_actuation(
            sign_qualification_admission(payload, _private_key()),
            registry=registry,
            expected=_expected(payload),
            permit_trust_snapshot=stale,
            now=NOW,
        )


def test_actuation_rechecks_exact_permit_registry_snapshot() -> None:
    registry = _registry()
    payload = _payload(registry)
    verified = verify_qualification_admission_for_actuation(
        sign_qualification_admission(payload, _private_key()),
        registry=registry,
        expected=_expected(payload),
        permit_trust_snapshot=_snapshot(registry),
        now=NOW,
    )
    assert verified.registry_sha256 == registry.artifact_sha256()


def test_v1_admission_is_not_accepted_by_v2() -> None:
    with pytest.raises(ValidationError):
        QualificationAdmissionPayload.model_validate(
            {"schema_version": "openadapt.qualification-admission/v1"}
        )


def _write_registry(path, registry: QualificationSignerRegistry) -> None:
    path.write_text(json.dumps(registry.model_dump(mode="json")))
    path.chmod(0o600)


def test_local_checkpoint_initializes_once_and_rejects_rollback(tmp_path) -> None:
    registry_path = tmp_path / "qualification-signers.json"
    checkpoint_path = tmp_path / "qualification-signers.checkpoint.json"
    _write_registry(registry_path, _registry(revision=7))

    checkpoint = initialize_local_qualification_signer_checkpoint(
        registry_path, checkpoint_path, now=NOW
    )
    assert checkpoint.revision == 7
    assert read_qualification_signer_checkpoint_file(checkpoint_path) == checkpoint

    # Provisioning is one-time: an existing checkpoint refuses re-initialization.
    with pytest.raises(QualificationAdmissionError, match="already exists"):
        initialize_local_qualification_signer_checkpoint(
            registry_path, checkpoint_path, now=NOW
        )

    # Same revision, same content: idempotent.
    registry, advanced = load_local_qualification_signer_registry(
        registry_path, checkpoint_path, now=NOW
    )
    assert registry.revision == 7
    assert advanced == checkpoint

    # Rollback to a lower revision is refused.
    _write_registry(registry_path, _registry(revision=6))
    with pytest.raises(QualificationAdmissionError, match="revision rolled back"):
        load_local_qualification_signer_registry(
            registry_path, checkpoint_path, now=NOW
        )

    # Same revision with changed content is equivocation.
    _write_registry(
        registry_path,
        _registry(revision=7).model_copy(update={"expires_at": "2026-08-21T11:00:00Z"}),
    )
    with pytest.raises(QualificationAdmissionError, match="changed content"):
        load_local_qualification_signer_registry(
            registry_path, checkpoint_path, now=NOW
        )

    # The durable checkpoint did not move under any refused transition.
    assert read_qualification_signer_checkpoint_file(checkpoint_path) == checkpoint


def test_local_checkpoint_advances_and_survives_reload(tmp_path) -> None:
    registry_path = tmp_path / "qualification-signers.json"
    checkpoint_path = tmp_path / "qualification-signers.checkpoint.json"
    _write_registry(registry_path, _registry(revision=7))
    initialize_local_qualification_signer_checkpoint(
        registry_path, checkpoint_path, now=NOW
    )

    newer = QualificationSignerRegistry.model_validate(
        {
            **_registry(revision=8).model_dump(mode="json"),
            "generated_at": "2026-08-18T11:30:00Z",
            "expires_at": "2026-08-20T11:30:00Z",
        }
    )
    _write_registry(registry_path, newer)
    _registry_loaded, advanced = load_local_qualification_signer_registry(
        registry_path, checkpoint_path, now=NOW
    )
    assert advanced.revision == 8

    # The persisted checkpoint now refuses the older revision after a reload.
    _write_registry(registry_path, _registry(revision=7))
    with pytest.raises(QualificationAdmissionError, match="revision rolled back"):
        load_local_qualification_signer_registry(
            registry_path, checkpoint_path, now=NOW
        )


def test_missing_durable_checkpoint_halts(tmp_path) -> None:
    registry_path = tmp_path / "qualification-signers.json"
    checkpoint_path = tmp_path / "qualification-signers.checkpoint.json"
    _write_registry(registry_path, _registry(revision=7))
    with pytest.raises(QualificationAdmissionError, match="unavailable"):
        load_local_qualification_signer_registry(
            registry_path, checkpoint_path, now=NOW
        )
    with pytest.raises(QualificationAdmissionError):
        validate_qualification_signer_registry_transition(
            _registry(revision=7),
            prior_checkpoint=None,  # type: ignore[arg-type]
            now=NOW,
        )


def test_higher_revision_with_earlier_generation_time_rejects() -> None:
    prior = initialize_qualification_signer_registry_checkpoint(
        _registry(revision=7), now=NOW
    )
    earlier = QualificationSignerRegistry.model_validate(
        {
            **_registry(revision=8).model_dump(mode="json"),
            "generated_at": "2026-08-18T10:00:00Z",
            "expires_at": "2026-08-20T10:00:00Z",
        }
    )
    with pytest.raises(QualificationAdmissionError, match="generation time"):
        validate_qualification_signer_registry_transition(
            earlier, prior_checkpoint=prior, now=NOW
        )


def test_expired_registry_never_advances_the_checkpoint() -> None:
    prior = initialize_qualification_signer_registry_checkpoint(
        _registry(revision=7), now=NOW
    )
    expired_now = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
    with pytest.raises(QualificationAdmissionError, match="expired"):
        validate_qualification_signer_registry_transition(
            _registry(revision=8), prior_checkpoint=prior, now=expired_now
        )


def test_registry_file_rejects_unsafe_permissions(tmp_path) -> None:
    registry_path = tmp_path / "qualification-signers.json"
    checkpoint_path = tmp_path / "qualification-signers.checkpoint.json"
    registry_path.write_text(json.dumps(_registry().model_dump(mode="json")))
    registry_path.chmod(0o644)
    with pytest.raises(QualificationAdmissionError, match="permissions are unsafe"):
        initialize_local_qualification_signer_checkpoint(
            registry_path, checkpoint_path, now=NOW
        )


def test_registry_revision_rejects_unsafe_and_non_integer_values() -> None:
    data = _registry().model_dump(mode="json")
    for replacement in (JS_MAX_SAFE_INTEGER + 2, 7.0, "7", True):
        data["revision"] = replacement
        with pytest.raises(ValidationError):
            QualificationSignerRegistry.model_validate(data)


def test_checkpoint_serializes_and_reloads_exactly() -> None:
    checkpoint = initialize_qualification_signer_registry_checkpoint(
        _registry(revision=7), now=NOW
    )
    reloaded = QualificationSignerRegistryCheckpoint.model_validate_json(
        checkpoint.model_dump_json()
    )
    assert reloaded == checkpoint


def test_campaign_trial_permit_is_signed_and_nonproduction_only() -> None:
    registry = _registry()
    payload = _campaign_permit_payload(registry)
    envelope = sign_qualification_campaign_permit(payload, _private_key())
    digest = verify_qualification_campaign_permit(
        envelope,
        registry=registry,
        expected=_campaign_expected(payload),
        now=NOW,
    )
    assert digest == envelope.artifact_sha256()
    assert payload.execution_purpose == "qualification_campaign"
    assert payload.production_data_prohibited is True
    assert payload.production_authority_prohibited is True
    assert payload.production_success_prohibited is True


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("execution_purpose", "production"),
        ("environment_class", "production"),
        ("production_data_prohibited", False),
        ("production_authority_prohibited", False),
        ("production_success_prohibited", False),
        # A numeric spelling of the fixed boolean must not pass either.
        ("production_data_prohibited", 1),
        ("production_authority_prohibited", 1),
        ("production_success_prohibited", 1),
    ],
)
def test_campaign_trial_permit_cannot_become_production(
    field: str,
    replacement: object,
) -> None:
    data = _campaign_permit_payload(_registry()).model_dump(mode="json")
    data[field] = replacement
    with pytest.raises(ValidationError):
        QualificationCampaignPermitPayload.model_validate(data)


def test_campaign_trial_permit_cannot_emit_success() -> None:
    data = _campaign_permit_payload(_registry()).model_dump(mode="json")
    data["status"] = "success"
    with pytest.raises(ValidationError):
        QualificationCampaignPermitPayload.model_validate(data)


def test_campaign_trial_permit_rejects_the_retired_issuer_workflow() -> None:
    data = _campaign_permit_payload(_registry()).model_dump(mode="json")
    data["issuer"]["workflow"] = (
        "OpenAdaptAI/openadapt-internal/.github/workflows/"
        "production-qualification-admission.yml"
    )
    with pytest.raises(ValidationError, match="issuer workflow"):
        QualificationCampaignPermitPayload.model_validate(data)


def test_campaign_trial_permit_rejects_live_environment_change() -> None:
    registry = _registry()
    payload = _campaign_permit_payload(registry)
    expected = _campaign_expected(payload).model_copy(
        update={"environment_digest": SHA_A}
    )
    with pytest.raises(
        QualificationCampaignPermitError,
        match="environment_digest does not match live state",
    ):
        verify_qualification_campaign_permit(
            sign_qualification_campaign_permit(payload, _private_key()),
            registry=registry,
            expected=expected,
            now=NOW,
        )


def test_campaign_trial_permit_is_one_shot() -> None:
    registry = _registry()
    payload = _campaign_permit_payload(registry)
    with pytest.raises(
        QualificationCampaignPermitError,
        match="already consumed",
    ):
        verify_qualification_campaign_permit(
            sign_qualification_campaign_permit(payload, _private_key()),
            registry=registry,
            expected=_campaign_expected(payload),
            consumed_permit_ids={payload.permit_id},
            now=NOW,
        )
