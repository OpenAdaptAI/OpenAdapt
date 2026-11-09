from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

import openadapt_flow.qualification_campaign_authority as campaign
from openadapt_flow.production_qualification import ProductionQualificationGuard
from openadapt_flow.qualification_campaign_authority import (
    QualificationCampaignAuthority,
    QualificationCampaignAuthorityError,
    QualificationCampaignGuard,
    load_qualification_campaign_authority,
)
from openadapt_flow.qualification_campaign_permit import (
    QualificationCampaignPermitError,
    sign_qualification_campaign_permit,
)
from openadapt_flow.runtime.authorization import GovernedRunAuthorization
from openadapt_flow.runtime.replayer import Replayer
from tests.test_qualification_admission_v2 import (
    IDS,
    SHA_A,
    _campaign_expected,
    _campaign_permit_payload,
    _private_key,
    _registry,
)
from tests.test_replayer import FakeBackend, FakeVision


def _authority() -> QualificationCampaignAuthority:
    registry = _registry()
    permit = sign_qualification_campaign_permit(
        _campaign_permit_payload(registry), _private_key()
    )
    return QualificationCampaignAuthority(
        qualification_campaign_permit=permit,
        qualification_campaign_permit_sha256=permit.artifact_sha256(),
        expected=_campaign_expected(permit.payload),
        qualification_signer_registry=registry,
        qualification_signer_registry_sha256=registry.artifact_sha256(),
    )


def _write(path, authority: QualificationCampaignAuthority) -> None:
    path.write_text(
        json.dumps(authority.model_dump(mode="json"), sort_keys=True),
        encoding="utf-8",
    )
    if os.name != "nt":
        path.chmod(0o600)


def _stub_guard(monkeypatch, authority: QualificationCampaignAuthority) -> None:
    monkeypatch.setattr(
        campaign, "_workflow_binding_refusal", lambda *_args, **_kw: None
    )
    monkeypatch.setattr(
        campaign,
        "verify_qualification_campaign_permit",
        lambda *_, **__: authority.qualification_campaign_permit_sha256,
    )


def test_campaign_authority_is_private_closed_and_content_bound(tmp_path):
    authority = _authority()
    path = tmp_path / "campaign.json"
    _write(path, authority)

    assert load_qualification_campaign_authority(path) == authority

    raw = authority.model_dump(mode="json")
    raw["unexpected"] = True
    path.write_text(json.dumps(raw), encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)
    with pytest.raises(QualificationCampaignAuthorityError):
        load_qualification_campaign_authority(path)


def test_campaign_guard_binds_the_exact_trial_and_retained_authorization(
    tmp_path, monkeypatch
):
    authority = _authority()
    trial = authority.qualification_campaign_permit.payload.trial
    path = tmp_path / "campaign.json"
    _write(path, authority)
    _stub_guard(monkeypatch, authority)
    guard = QualificationCampaignGuard(
        path,
        workflow=object(),
        case_id=trial.task,
        input_digest=trial.input_digest,
        campaign_id=trial.campaign_id,
        run_id=trial.qualification_run_id,
    )
    binding = guard.authorization_binding(object())
    authorization = SimpleNamespace(
        **binding,
        qualification_case_id=trial.task,
        runtime_inputs_digest=trial.input_digest,
        qualification_campaign_id_sha256=campaign.qualification_campaign_id_sha256(
            trial.campaign_id
        ),
        qualification_run_id_sha256=campaign.qualification_run_id_sha256(
            trial.qualification_run_id
        ),
    )

    assert guard.authorization_refusal(object(), authorization) is None
    authorization.qualification_case_id = "different-case"
    assert guard.authorization_refusal(object(), authorization) == (
        "qualification campaign permit differs from the exact trial"
    )


def test_campaign_consumption_state_can_only_grow(tmp_path, monkeypatch):
    authority = _authority()
    trial = authority.qualification_campaign_permit.payload.trial
    path = tmp_path / "campaign.json"
    _write(path, authority)
    _stub_guard(monkeypatch, authority)
    guard = QualificationCampaignGuard(
        path,
        workflow=object(),
        case_id=trial.task,
        input_digest=trial.input_digest,
    )
    consumed_id = "00000000-0000-4000-8000-000000000099"
    _write(path, authority.model_copy(update={"consumed_permit_ids": (consumed_id,)}))
    guard._load_current()
    _write(path, authority)

    with pytest.raises(
        QualificationCampaignAuthorityError,
        match="consumption state rolled back",
    ):
        guard._load_current()


def test_consumed_campaign_permit_refuses_before_input(tmp_path, monkeypatch):
    authority = _authority()
    permit_id = authority.qualification_campaign_permit.payload.permit_id
    authority = authority.model_copy(update={"consumed_permit_ids": (permit_id,)})
    trial = authority.qualification_campaign_permit.payload.trial
    path = tmp_path / "campaign.json"
    _write(path, authority)
    monkeypatch.setattr(
        campaign, "_workflow_binding_refusal", lambda *_args, **_kw: None
    )

    def _refuse(*_, consumed_permit_ids, **__):
        if permit_id in consumed_permit_ids:
            raise QualificationCampaignPermitError("consumed")
        return authority.qualification_campaign_permit_sha256

    monkeypatch.setattr(campaign, "verify_qualification_campaign_permit", _refuse)
    guard = QualificationCampaignGuard(
        path,
        workflow=object(),
        case_id=trial.task,
        input_digest=trial.input_digest,
    )

    assert guard.refusal(object()) == "qualification campaign permit is not active"


def test_forged_case_id_cannot_bypass_production_admission():
    authorization = GovernedRunAuthorization(
        bundle_content_digest=SHA_A,
        runtime_inputs_digest=SHA_A,
        admitted_policy_name="permissive",
        execution_profile="standard",
        approval_source="qualification-campaign",
        qualification_project_id="project-1",
        qualification_project_revision=1,
        qualification_project_contract_sha256=SHA_A,
        qualification_case_id="forged-case",
        qualification_campaign_id_sha256=SHA_A,
        qualification_case_input_sha256=SHA_A,
        qualification_run_id_sha256=SHA_A,
        qualification_case_kind="representative",
        qualification_case_action_paths={"write": "gui"},
        qualification_campaign_permit_id=IDS["admission_id"],
        qualification_campaign_permit_sha256=SHA_A,
        qualification_campaign_signer_registry_sha256=SHA_A,
        qualification_campaign_signer_registry_revision=1,
        qualification_campaign_signer_registry_expires_at="2099-01-01T00:00:00Z",
        qualification_campaign_authority_sha256=SHA_A,
    )
    production_guard = ProductionQualificationGuard.__new__(
        ProductionQualificationGuard
    )

    with pytest.raises(
        ValueError, match="requires the signed non-production campaign authority"
    ):
        Replayer(
            FakeBackend(),
            vision=FakeVision(),
            governed_authorization=authorization,
            production_qualification_guard=production_guard,
        )
