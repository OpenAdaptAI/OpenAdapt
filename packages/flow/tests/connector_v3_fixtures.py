"""Public synthetic BYOC contracts with real signatures and no external service.

Signing keys are generated only in test-local author storage. The exposed
customer trust directory contains public keys/registrations, never a signer.
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import zipfile
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from openadapt_flow.execute.keys import fingerprint_of, load_or_create_private_key
from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    Postcondition,
    PostconditionKind,
    Step,
    Workflow,
)
from openadapt_flow.jit import authority, hosted_authority, lifecycle
from openadapt_flow.runner.dispatch_envelope import ManagedDispatchEnvelope
from openadapt_flow.runner.protocol import dispatch_binding_sha256
from openadapt_flow.runtime.authorization import (
    GovernedRunAuthorization,
    runtime_inputs_digest,
)
from openadapt_flow.runtime.effects.effect import Effect, EffectKind

ORG_ID = "ac100000-0000-4000-8000-000000000001"
WORKFLOW_ID = "ac100000-0000-4000-8000-000000000002"
RUN_ID = "ac100000-0000-4000-8000-000000000003"
BUNDLE_VERSION_ID = "ac100000-0000-4000-8000-000000000004"
VALIDATION_ID = "ac100000-0000-4000-8000-000000000005"
SESSION_ID = "ac100000-0000-4000-8000-000000000006"
CONTROL_PLANE = "https://control.example.test"
PERMIT_URL = CONTROL_PLANE + "/api/internal/managed-delivery-permit"
ADMITTER_ID = "ci:contract-test-admitter"
CHECKS = (
    "schema_version: openadapt.verify/v1\n"
    "verifier:\n  kind: rest\n  base_url: https://record.example.test\n"
    "  records_path: /records\n"
    "effects:\n  - kind: record_written\n"
    "    match: {type: synthetic-record}\n    field: value\n"
)


def archive_tree(root: Path) -> bytes:
    """Return deterministic bytes for this test's immutable bundle inventory."""
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                member = zipfile.ZipInfo(path.relative_to(root).as_posix())
                archive.writestr(member, path.read_bytes())
    return stream.getvalue()


def build_program_case(root: Path, mode: str = "person") -> dict[str, Any]:
    """Build a real signed person/admitter case for receiver boundary tests."""
    if mode not in {"person", "admitter_key"}:
        raise ValueError("the fixture models only the two declared program forms")
    root.mkdir(parents=True, exist_ok=True)
    bundle = root / "bundle"
    signing_dir = root / "author-signing"
    trust_dir = root / "customer-public-trust"
    author_key = load_or_create_private_key(signing_dir)
    public = author_key.public_key()
    public_hex = public.public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    ).hex()
    workflow = Workflow(
        name="synthetic-contract",
        steps=[
            Step(
                id="step_000",
                intent="enter value",
                action=ActionKind.TYPE,
                text="fixture",
            ),
            Step(
                id="step_001",
                intent="save record",
                action=ActionKind.CLICK,
                risk="irreversible",
                identity_armed=True,
                anchor=Anchor(
                    template="templates/save.png",
                    region=(0, 0, 10, 10),
                    click_point=(5, 5),
                    ocr_text="Save",
                    context_text="Synthetic record",
                ),
                expect=[
                    Postcondition(kind=PostconditionKind.TEXT_PRESENT, text="Saved")
                ],
                effects=[
                    Effect(
                        kind=EffectKind.RECORD_WRITTEN,
                        match={"type": "synthetic-record"},
                        field="value",
                        idempotency_key="synthetic-contract-write",
                        risk="irreversible",
                    )
                ],
            ),
        ],
    )
    from tests.test_replayer import make_png

    (bundle / "templates").mkdir(parents=True, exist_ok=True)
    (bundle / "templates/save.png").write_bytes(make_png((10, 10)))
    workflow.save(bundle)
    readback = bundle / hosted_authority.READBACK_PATH
    readback.parent.mkdir(parents=True, exist_ok=True)
    readback.write_text(CHECKS, encoding="utf-8")
    workflow = Workflow.load(bundle)
    assert workflow.manifest is not None
    body: dict[str, Any] = {
        "schema": lifecycle.ADMISSION_SCHEMA,
        "signing_key": fingerprint_of(public),
        "admitted_by_principal_id": "Example Operator",
        "admission_mode": mode,
        "state": lifecycle.BundleState.ADMITTED.value,
        "digest": workflow.manifest.content_digest,
    }
    registry = authority.PrincipalRegistry()
    admitter_hex = None
    if mode == "admitter_key":
        admitter = Ed25519PrivateKey.generate()
        admitter_hex = (
            admitter.public_key()
            .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
            .hex()
        )
        body.update(
            admitted_by_principal_id=ADMITTER_ID,
            admitter_public_key=admitter_hex,
        )
        body["admitter_signature"] = authority._sign(
            admitter, authority.ADMITTER_DOMAIN, body
        )
        registry = authority.PrincipalRegistry(
            admitter_keys={ADMITTER_ID: admitter_hex}
        )
    registry.save(signing_dir)
    original = {**body, "signature": lifecycle._sign_record(body, signing_dir)}
    hosted = hosted_authority.hosted_admission(
        original,
        key_dir=signing_dir,
        readback_digest=hosted_authority.readback_sha256(bundle),
    )
    (bundle / lifecycle.ADMISSION_FILE).write_text(json.dumps(hosted), encoding="utf-8")
    (trust_dir / "keys").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(signing_dir / "keys/ed25519.pub", trust_dir / "keys/ed25519.pub")
    registry.save(trust_dir)
    # This is deliberately a public projection. The signing PEM stays above.
    assert sorted(
        p.relative_to(trust_dir).as_posix() for p in trust_dir.rglob("*") if p.is_file()
    ) == ["keys/ed25519.pub", "principals.json"]
    lifecycle.verify_admission(bundle, key_dir=trust_dir)
    shape = hosted_authority.run_shape(bundle, workflow, policy_source="clinical-write")
    authorization = GovernedRunAuthorization(
        bundle_content_digest=shape["bundle_content_digest"],
        runtime_inputs_digest=runtime_inputs_digest(
            workflow, {}, None, interstitials=[]
        ),
        admitted_policy_name=shape["admitted_policy_name"],
        admitted_policy_contract_sha256=shape["admitted_policy_contract_sha256"],
        execution_profile="standard",
        minimum_effect_tier=shape["minimum_effect_tier"],
        qualified_effect_requirements=shape["qualified_effect_requirements"],
        required_identity_step_ids=shape["required_identity_step_ids"],
        approval_source=hosted_authority.approval_source(shape["admission_sha256"]),
    )
    envelope = ManagedDispatchEnvelope(
        run_id=RUN_ID,
        bundle_content_digest=authorization.bundle_content_digest,
        runtime_inputs_digest=authorization.runtime_inputs_digest,
        authorization=authorization,
        dispatch_binding_sha256=dispatch_binding_sha256(RUN_ID, authorization),
    )
    reference = {
        "schema_version": "openadapt.program-admission-reference/v1",
        "admission_sha256": shape["admission_sha256"],
        "public_key_hex": public_hex,
    }
    if mode == "admitter_key":
        reference.update(
            schema_version="openadapt.program-admission-reference/v2",
            admission_mode="admitter_key",
            admitted_by_principal_id=ADMITTER_ID,
            admitter_public_key_hex=admitter_hex,
        )
    dispatch = {**envelope.model_dump(mode="json"), "program_admission": reference}
    archive = archive_tree(bundle)
    payload = {
        "mode": "replay",
        "run_id": RUN_ID,
        "org_id": ORG_ID,
        "workflow_id": WORKFLOW_ID,
        "target_kind": "web",
        "storage": {
            "backend": "local",
            "bundle_ref": "bundles/program.zip",
            "report_ref": "reports/run.json",
        },
        "target_url": "https://record.example.test/",
        "allowed_hosts": ["record.example.test"],
        "params": {"target_kind": "web"},
        "run_token": "synthetic-run-capability",
        "bundle_version_id": BUNDLE_VERSION_ID,
        "runtime_validation_id": VALIDATION_ID,
        "bundle_sha256": hashlib.sha256(archive).hexdigest(),
        "safety": {"effects.verify": True},
        "grounding_model": {"enabled": False},
        "managed_dispatch": dispatch,
        "managed_delivery_authority_url": PERMIT_URL,
        "managed_dispatch_session_id": SESSION_ID,
    }
    return {
        "bundle": bundle,
        "bundle_dir": bundle,
        "bundle_bytes": archive,
        "archive": archive,
        "key_dir": trust_dir,
        "public_key_hex": public_hex,
        "admission_sha256": shape["admission_sha256"],
        "admission": hosted,
        "workflow": workflow,
        "shape": shape,
        "dispatch": dispatch,
        "payload": payload,
        "settings_kwargs": {
            "control_plane_url": CONTROL_PLANE,
            "org_id": ORG_ID,
            "trusted_admission_keys": public_hex,
            "admission_key_dir": str(trust_dir),
        },
    }


def build_qualification_case(root: Path) -> dict[str, Any]:
    """Sign a v2 proof for public synthetic certification/model evidence.

    The operator context is assembled from known fixture state before signing.
    This is cryptographic source-contract evidence, not live qualification.
    """
    from base64 import b64encode
    from datetime import datetime, timedelta, timezone

    from openadapt_flow.policy import load_policy
    from openadapt_flow.production_qualification import (
        ProductionQualificationAuthority,
        ProductionQualificationGuard,
    )
    from openadapt_flow.qualification import certify_project, save_qualified_workflow
    from openadapt_flow.qualification_admission_v2 import (
        QualificationAdmissionExpected,
        QualificationAdmissionPayload,
        QualificationSignerRegistry,
        contract_sha256,
        qualification_signer_key_id,
        sign_qualification_admission,
    )
    from openadapt_flow.verification import VerificationTier
    from tests.test_qualification_admission_v2 import (
        _payload,
        _registry,
        _runtime_build,
    )
    from tests.test_qualification_project import (
        _configure,
        _qualification_visual_fixture,
        _record_passing_campaign,
        _workflow,
    )

    root.mkdir(parents=True, exist_ok=True)
    bundle = root / "bundle"
    (bundle / "templates").mkdir(parents=True)
    (bundle / "templates/save.png").write_bytes(_qualification_visual_fixture()[1])
    workflow = _workflow()
    workflow.save(bundle)
    workflow = Workflow.load(bundle)
    _configure(workflow, tier=VerificationTier.INDEPENDENT_SYSTEM)
    evidence_root = root / "synthetic-evidence"
    _record_passing_campaign(workflow, evidence_root)
    certification = certify_project(
        workflow, policy=load_policy("clinical-write"), evidence_root=evidence_root
    )
    assert certification.passed, certification.render()
    save_qualified_workflow(workflow, bundle)
    workflow = Workflow.load(bundle)
    assert workflow.manifest is not None and workflow.qualification is not None
    template = workflow.manifest.provenance.governed_authorization_template
    assert template is not None
    archive = archive_tree(bundle)
    archive_digest = hashlib.sha256(archive).hexdigest()
    now = datetime.now(timezone.utc).replace(microsecond=0)

    def utc(moment):
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")

    signer = Ed25519PrivateKey.generate()
    public = signer.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    registry_data = _registry().model_dump(mode="json")
    registry_data.update(
        generated_at=utc(now - timedelta(minutes=1)),
        expires_at=utc(now + timedelta(hours=1)),
    )
    registry_data["signers"][0].update(
        key_id=qualification_signer_key_id(public),
        public_key=b64encode(public).decode("ascii"),
    )
    registry = QualificationSignerRegistry.model_validate(registry_data)
    runtime = _runtime_build().model_dump(mode="json")
    known = {
        "tenant_id": ORG_ID,
        "workflow_id": WORKFLOW_ID,
        "workflow_version_id": BUNDLE_VERSION_ID,
        "bundle_version_id": BUNDLE_VERSION_ID,
        "runtime_validation_id": VALIDATION_ID,
        "bundle_artifact_sha256": archive_digest,
        "bundle_content_digest": workflow.manifest.content_digest,
        "environment_digest": workflow.qualification.environment.environment_digest,
        "governed_authorization_template_sha256": template.template_sha256,
        "application_contract_sha256": "a" * 64,
        "substrate_contract_sha256": "a" * 64,
        "environment_contract_sha256": template.qualification_environment_contract_sha256,
        "runtime_environment_sha256": "c" * 64,
        "runtime_contract_sha256": "a" * 64,
        "input_policy_sha256": template.parameter_contract_sha256,
        "action_policy_sha256": template.qualification_project_contract_sha256,
        "network_policy_sha256": "a" * 64,
        "identity_contract_sha256": template.identity_contract_sha256,
        "effect_contract_sha256": contract_sha256(
            [
                item.model_dump(mode="json")
                for item in template.qualified_effect_requirements
            ]
        ),
        "operator_contract_sha256": "a" * 64,
        "runtime_build_identity": runtime,
        "evidence_runner_signer_sha256": "b" * 64,
        "deployment_manifest_sha256": "d" * 64,
    }
    expected = QualificationAdmissionExpected.model_validate(known)
    raw = _payload(registry).model_dump(mode="json")
    raw.update({k: v for k, v in known.items() if k in raw})
    raw.update(
        issued_at=utc(now - timedelta(seconds=1)),
        not_before=utc(now - timedelta(seconds=1)),
        expires_at=utc(now + timedelta(minutes=30)),
    )
    raw["issuer"]["key_id"] = qualification_signer_key_id(public)
    raw["evidence_identity"].update(
        {k: v for k, v in known.items() if k in raw["evidence_identity"]}
    )
    raw["evidence_identity"]["workflow_digest"] = workflow.manifest.content_digest
    proof = sign_qualification_admission(
        QualificationAdmissionPayload.model_validate(raw), signer
    )
    authority_file = root / "operator-authority.json"
    operator = ProductionQualificationAuthority(
        qualification_admission=proof,
        qualification_admission_sha256=proof.artifact_sha256(),
        expected=expected,
        qualification_signer_registry=registry,
        qualification_signer_registry_sha256=registry.artifact_sha256(),
    )
    authority_file.write_text(operator.model_dump_json(), encoding="utf-8")
    authority_file.chmod(0o600)
    ProductionQualificationGuard(
        authority_file, remote_permit_revalidation=True
    ).verify(workflow, for_actuation=False)
    authorization = GovernedRunAuthorization(
        bundle_content_digest=workflow.manifest.content_digest,
        runtime_inputs_digest=runtime_inputs_digest(
            workflow, {}, None, interstitials=[]
        ),
        admitted_policy_name=template.policy_name,
        admitted_policy_contract_sha256=template.policy_contract_sha256,
        execution_profile="standard",
        minimum_effect_tier=template.minimum_effect_tier,
        qualified_effect_requirements=template.qualified_effect_requirements,
        required_identity_step_ids=template.required_identity_step_ids,
        approval_source="hosted:execute-api",
    )
    envelope = ManagedDispatchEnvelope(
        run_id=RUN_ID,
        bundle_content_digest=authorization.bundle_content_digest,
        runtime_inputs_digest=authorization.runtime_inputs_digest,
        authorization=authorization,
        dispatch_binding_sha256=dispatch_binding_sha256(RUN_ID, authorization),
    )
    dispatch = {
        **envelope.model_dump(mode="json"),
        "qualification_admission": proof.model_dump(mode="json"),
        "qualification_admission_sha256": proof.artifact_sha256(),
    }
    payload = {
        "mode": "replay",
        "run_id": RUN_ID,
        "org_id": ORG_ID,
        "workflow_id": WORKFLOW_ID,
        "target_kind": "web",
        "storage": {
            "backend": "local",
            "bundle_ref": "bundles/qualified.zip",
            "report_ref": "reports/run.json",
        },
        "target_url": "https://record.example.test/",
        "allowed_hosts": ["record.example.test"],
        "params": {"target_kind": "web"},
        "run_token": "synthetic-run-capability",
        "bundle_version_id": BUNDLE_VERSION_ID,
        "runtime_validation_id": VALIDATION_ID,
        "bundle_sha256": archive_digest,
        "safety": {"effects.verify": True},
        "grounding_model": {"enabled": False},
        "managed_dispatch": dispatch,
        "managed_delivery_authority_url": PERMIT_URL,
        "managed_dispatch_session_id": SESSION_ID,
    }
    return {
        "bundle": bundle,
        "bundle_dir": bundle,
        "bundle_bytes": archive,
        "archive": archive,
        "payload": payload,
        "dispatch": dispatch,
        "operator": operator,
        "authority_file": authority_file,
        "workflow": workflow,
        "settings_kwargs": {
            "control_plane_url": CONTROL_PLANE,
            "org_id": ORG_ID,
            "qualification_authority_file": str(authority_file),
        },
    }
