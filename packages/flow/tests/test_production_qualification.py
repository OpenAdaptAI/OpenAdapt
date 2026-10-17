from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

import openadapt_flow.production_qualification as production
from openadapt_flow.production_qualification import (
    ProductionQualificationAuthority,
    ProductionQualificationAuthorityError,
    ProductionQualificationGuard,
    _workflow_binding_refusal,
    load_production_qualification_authority,
)
from openadapt_flow.qualification_admission_v2 import (
    QualificationAdmissionError,
    VerifiedQualificationAdmission,
    sign_qualification_admission,
)
from tests.test_qualification_admission_v2 import (
    IDS,
    SHA_A,
    SHA_C,
    SHA_E,
    _expected,
    _payload,
    _private_key,
    _registry,
    _snapshot,
)


def _authority(*, snapshot: bool = True) -> ProductionQualificationAuthority:
    registry = _registry()
    admission = sign_qualification_admission(_payload(registry), _private_key())
    return ProductionQualificationAuthority(
        qualification_admission=admission,
        qualification_admission_sha256=admission.artifact_sha256(),
        expected=_expected(admission.payload),
        qualification_signer_registry=registry,
        qualification_signer_registry_sha256=registry.artifact_sha256(),
        permit_trust_snapshot=_snapshot(registry) if snapshot else None,
    )


def _write_authority(path, authority: ProductionQualificationAuthority) -> None:
    path.write_text(
        json.dumps(authority.model_dump(mode="json"), sort_keys=True),
        encoding="utf-8",
    )
    if os.name != "nt":
        path.chmod(0o600)


def _verified(
    authority: ProductionQualificationAuthority,
) -> VerifiedQualificationAdmission:
    return VerifiedQualificationAdmission(
        admission_artifact_sha256=authority.qualification_admission_sha256,
        evidence_identity_sha256=(
            authority.qualification_admission.payload.evidence_identity.artifact_sha256()
        ),
        registry_sha256=authority.qualification_signer_registry_sha256,
        registry_revision=authority.qualification_signer_registry.revision,
        registry_expires_at=authority.qualification_signer_registry.expires_at,
        issuer_key_id=authority.qualification_admission.payload.issuer.key_id,
        admission_id=authority.qualification_admission.payload.admission_id,
        runtime_validation_id=(
            authority.qualification_admission.payload.runtime_validation_id
        ),
    )


def _stub_verification(
    monkeypatch, authority: ProductionQualificationAuthority
) -> None:
    verified = _verified(authority)
    monkeypatch.setattr(production, "_workflow_binding_refusal", lambda *_: None)
    monkeypatch.setattr(
        production,
        "verify_qualification_admission",
        lambda *_, **__: verified,
    )
    monkeypatch.setattr(
        production,
        "verify_qualification_admission_for_actuation",
        lambda *_, **__: verified,
    )


def test_private_authority_file_is_closed_and_owner_only(tmp_path):
    authority = _authority()
    path = tmp_path / "authority.json"
    _write_authority(path, authority)

    assert load_production_qualification_authority(path) == authority

    raw = authority.model_dump(mode="json")
    raw["unexpected"] = True
    path.write_text(json.dumps(raw), encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)
    with pytest.raises(ProductionQualificationAuthorityError):
        load_production_qualification_authority(path)

    if os.name != "nt":
        _write_authority(path, authority)
        path.chmod(0o640)
        with pytest.raises(ProductionQualificationAuthorityError):
            load_production_qualification_authority(path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX link contract")
def test_private_authority_file_refuses_links(tmp_path):
    authority = _authority()
    target = tmp_path / "authority.json"
    link = tmp_path / "authority-link.json"
    _write_authority(target, authority)
    link.symlink_to(target)

    with pytest.raises(ProductionQualificationAuthorityError):
        load_production_qualification_authority(link)


def test_private_authority_file_refuses_a_short_descriptor_read(tmp_path, monkeypatch):
    authority = _authority()
    path = tmp_path / "authority.json"
    _write_authority(path, authority)
    monkeypatch.setattr(production.os, "read", lambda *_: b"")

    with pytest.raises(
        ProductionQualificationAuthorityError,
        match="ended during the safe read",
    ):
        production._read_private_json(path)


def test_windows_authority_file_fails_closed_without_acl_proof(tmp_path, monkeypatch):
    authority = _authority()
    path = tmp_path / "authority.json"
    _write_authority(path, authority)
    monkeypatch.setattr(production.os, "name", "nt")
    monkeypatch.setattr(production, "Path", lambda value: value)

    def _acl_unavailable(_descriptor):
        raise production.PrivateFileAclError("ACL proof unavailable")

    monkeypatch.setattr(
        production,
        "windows_descriptor_has_private_acl",
        _acl_unavailable,
    )
    with pytest.raises(
        ProductionQualificationAuthorityError,
        match="ACL proof unavailable",
    ):
        production._read_private_json(path)


def test_mutable_permit_snapshot_does_not_change_retained_identity(
    tmp_path, monkeypatch
):
    authority = _authority()
    path = tmp_path / "authority.json"
    _write_authority(path, authority)
    _stub_verification(monkeypatch, authority)
    guard = ProductionQualificationGuard(path, remote_permit_revalidation=False)

    first = guard.authorization_binding(object())
    refreshed = authority.model_copy(
        update={
            "permit_trust_snapshot": authority.permit_trust_snapshot.model_copy(
                update={
                    "qualification_signer_registry_checked_at": ("2026-08-18T12:00:00Z")
                }
            )
        }
    )
    _write_authority(path, refreshed)
    second = guard.authorization_binding(object())

    assert first == second
    assert first["production_qualification_authority_sha256"] == (
        authority.immutable_binding_sha256()
    )


def test_revocation_state_can_only_grow(tmp_path):
    authority = _authority()
    path = tmp_path / "authority.json"
    _write_authority(path, authority)
    guard = ProductionQualificationGuard(path, remote_permit_revalidation=True)
    revoked_id = "00000000-0000-4000-8000-000000000099"

    _write_authority(
        path,
        authority.model_copy(update={"revoked_admission_ids": (revoked_id,)}),
    )
    guard._load_current()
    _write_authority(path, authority)

    with pytest.raises(
        ProductionQualificationAuthorityError,
        match="revocation state rolled back",
    ):
        guard._load_current()


def test_changed_immutable_authority_is_refused_after_run_admission(tmp_path):
    authority = _authority()
    path = tmp_path / "authority.json"
    _write_authority(path, authority)
    guard = ProductionQualificationGuard(path, remote_permit_revalidation=True)
    changed = authority.model_copy(
        update={
            "expected": authority.expected.model_copy(
                update={"bundle_artifact_sha256": SHA_C}
            )
        }
    )
    _write_authority(path, changed)

    with pytest.raises(
        ProductionQualificationAuthorityError,
        match="changed after run admission",
    ):
        guard._load_current()


def test_local_actuation_requires_current_permit_snapshot(tmp_path, monkeypatch):
    authority = _authority(snapshot=False)
    path = tmp_path / "authority.json"
    _write_authority(path, authority)
    _stub_verification(monkeypatch, authority)
    guard = ProductionQualificationGuard(path, remote_permit_revalidation=False)

    assert guard.refusal(object()) == (
        "local Production actuation requires a fresh permit trust snapshot"
    )


def test_current_admission_revocation_refuses_before_delivery(tmp_path, monkeypatch):
    authority = _authority()
    current_id = authority.qualification_admission.payload.admission_id
    authority = authority.model_copy(update={"revoked_admission_ids": (current_id,)})
    path = tmp_path / "authority.json"
    _write_authority(path, authority)
    monkeypatch.setattr(production, "_workflow_binding_refusal", lambda *_: None)

    def _refuse_revoked(*_, revoked_admission_ids, **__):
        if current_id in revoked_admission_ids:
            raise QualificationAdmissionError("revoked")
        return _verified(authority)

    monkeypatch.setattr(production, "verify_qualification_admission", _refuse_revoked)
    guard = ProductionQualificationGuard(path, remote_permit_revalidation=True)

    with pytest.raises(ProductionQualificationAuthorityError, match="not active"):
        guard.authorization_binding(object())


def test_workflow_binding_reproduces_sealed_contracts(monkeypatch):
    authority = _authority()
    template = SimpleNamespace(
        template_sha256=SHA_A,
        qualification_environment_contract_sha256=SHA_A,
        parameter_contract_sha256=SHA_A,
        qualification_project_contract_sha256=SHA_A,
        identity_contract_sha256=SHA_A,
        qualified_effect_requirements=(),
    )
    workflow = SimpleNamespace(
        manifest=SimpleNamespace(
            content_digest=SHA_A,
            provenance=SimpleNamespace(governed_authorization_template=template),
        ),
        qualification=SimpleNamespace(
            environment=SimpleNamespace(environment_digest=SHA_E)
        ),
    )
    monkeypatch.setattr(production, "contract_sha256", lambda *_: SHA_A)

    assert _workflow_binding_refusal(authority, workflow) is None

    workflow.manifest.content_digest = SHA_C
    assert _workflow_binding_refusal(authority, workflow) == (
        "Production qualification does not bind the sealed workflow contracts"
    )


def test_governed_authorization_requires_complete_v2_binding():
    from openadapt_flow.runtime.authorization import GovernedRunAuthorization

    with pytest.raises(ValueError, match="Production qualification binding"):
        GovernedRunAuthorization(
            authorization_id=IDS["admission_id"],
            bundle_content_digest=SHA_A,
            runtime_inputs_digest=SHA_A,
            admitted_policy_name="permissive",
            execution_profile="standard",
            production_qualification_admission_id=IDS["admission_id"],
        )
