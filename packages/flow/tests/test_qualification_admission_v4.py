from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from openadapt_flow.production_qualification import (
    ProductionQualificationAuthorityError,
    ProductionQualificationAuthorityV4,
    ProductionQualificationGuard,
    _workflow_binding_refusal,
    load_production_qualification_authority,
)
from openadapt_flow.qualification_admission_v4 import (
    LIVE_V4_FIELDS,
    PINNED_KEY_ID,
    PINNED_REGISTRY_IDENTITY_SHA256,
    QualificationAdmissionV4Error,
    admission_is_revoked,
    bind_live_v4_admission,
    extract_live_v4_binding,
    raw_object_sha256,
    revocation_is_monotonic_successor,
    validate_qualification_admission_v4,
    verify_pinned_signer_registry,
    verify_qualification_admission_v4,
)

FIXTURES = Path(__file__).parent / "fixtures" / "v4-qualification"
NOW = datetime(2026, 9, 2, 18, 30, tzinfo=timezone.utc)
ISSUED_BUNDLE_SHA256 = (
    "sha256:33f8c637f3d5d9eb23b92e0fb7ba74d2713a52f7c8ed11495b30f8bbefb1f312"
)


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def issued_admission() -> dict:
    return _load_fixture("qualification-admission.json")


def issued_registry() -> dict:
    return _load_fixture("signer-registry.json")


def issued_receipt() -> dict:
    return _load_fixture("decision-receipt.json")


def issued_revocation() -> dict:
    return _load_fixture("revocation-state.json")


def v4_authority_payload() -> dict:
    admission = issued_admission()
    registry = issued_registry()
    return {
        "schema_version": "openadapt.production-qualification-authority/v4",
        "qualification_admission": admission,
        "qualification_admission_sha256": raw_object_sha256(admission),
        "qualification_signer_registry": registry,
        "qualification_signer_registry_sha256": PINNED_REGISTRY_IDENTITY_SHA256[7:],
        "decision_receipt": issued_receipt(),
        "revocation_state": issued_revocation(),
    }


def _write_authority(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)


def _live_binding(admission: dict | None = None, **overrides: object) -> dict:
    source = admission or issued_admission()
    binding = {field: source[field] for field in LIVE_V4_FIELDS}
    binding.update(overrides)
    return binding


def _matching_live_workflow(**overrides: object) -> SimpleNamespace:
    binding = _live_binding(**overrides)
    digest = str(binding["bundle_sha256"])
    raw = digest[7:] if digest.startswith("sha256:") else digest
    return SimpleNamespace(
        manifest=SimpleNamespace(content_digest=raw),
        qualification=None,
        live_qualification_binding=binding,
    )


def test_issued_v4_admission_verifies_against_pinned_registry() -> None:
    verified = verify_qualification_admission_v4(
        issued_admission(),
        registry=issued_registry(),
        decision_receipt=issued_receipt(),
        revocation_state=issued_revocation(),
        expected_bundle_sha256=ISSUED_BUNDLE_SHA256,
        now=NOW,
    )
    assert verified.issuer_key_id == PINNED_KEY_ID
    assert verified.bundle_sha256 == ISSUED_BUNDLE_SHA256
    assert verified.registry_expires_at is None
    assert verified.evidence_class == "remote-safe-synthetic"


def test_until_revoked_expires_at_null_is_required() -> None:
    admission = issued_admission()
    admission["expires_at"] = "2026-10-01T00:00:00Z"
    with pytest.raises(QualificationAdmissionV4Error, match="until-revoked"):
        validate_qualification_admission_v4(admission, now=NOW)

    registry = issued_registry()
    registry["expires_at"] = "2026-10-01T00:00:00Z"
    with pytest.raises(QualificationAdmissionV4Error, match="until-revoked"):
        verify_pinned_signer_registry(registry)


def test_foreign_key_is_not_trusted() -> None:
    registry = issued_registry()
    registry["signers"] = [
        {**registry["signers"][0], "key_id": "qa-ed25519-0000000000000000"}
    ]
    with pytest.raises(QualificationAdmissionV4Error, match="pinned"):
        verify_pinned_signer_registry(registry)


def test_revoked_pinned_key_fails_closed() -> None:
    registry = issued_registry()
    inner = dict(registry["signers"][0])
    inner["status"] = "revoked"
    inner["revoked_at"] = "2026-09-02T18:24:25Z"
    registry["signers"] = [inner, registry["signers"][1]]
    with pytest.raises(
        QualificationAdmissionV4Error, match="pinned published registry"
    ):
        verify_pinned_signer_registry(registry)


def test_wrong_bundle_digest_is_not_attached() -> None:
    with pytest.raises(QualificationAdmissionV4Error, match="does not bind"):
        verify_qualification_admission_v4(
            issued_admission(),
            registry=issued_registry(),
            decision_receipt=issued_receipt(),
            revocation_state=issued_revocation(),
            expected_bundle_sha256="sha256:" + ("ab" * 32),
            now=NOW,
        )


def test_tampered_receipt_signature_fails_closed() -> None:
    receipt = issued_receipt()
    receipt["signature"] = "A" * 86 + "=="
    with pytest.raises(QualificationAdmissionV4Error, match="signature"):
        verify_qualification_admission_v4(
            issued_admission(),
            registry=issued_registry(),
            decision_receipt=receipt,
            revocation_state=issued_revocation(),
            now=NOW,
        )


def test_unpinned_issuer_fails_closed() -> None:
    admission = issued_admission()
    admission["issuer"] = {
        **admission["issuer"],
        "workflow": ".github/workflows/other.yml",
    }
    with pytest.raises(QualificationAdmissionV4Error, match="pinned .github issuer"):
        validate_qualification_admission_v4(admission, now=NOW)


def test_v4_authority_binds_only_the_admitted_bundle(tmp_path: Path) -> None:
    path = tmp_path / "authority.json"
    _write_authority(path, v4_authority_payload())
    authority = load_production_qualification_authority(path)
    assert isinstance(authority, ProductionQualificationAuthorityV4)

    matching = _matching_live_workflow()
    assert _workflow_binding_refusal(authority, matching) is None

    foreign = _matching_live_workflow(bundle_sha256="sha256:" + ("ab" * 32))
    foreign.manifest.content_digest = "ab" * 32
    assert _workflow_binding_refusal(authority, foreign) == (
        "Production qualification does not bind the sealed workflow contracts"
    )


def test_v4_guard_authorization_is_until_revoked(tmp_path: Path) -> None:
    path = tmp_path / "authority.json"
    _write_authority(path, v4_authority_payload())
    guard = ProductionQualificationGuard(path, remote_permit_revalidation=False)
    workflow = _matching_live_workflow()
    binding = guard.authorization_binding(workflow)
    assert str(binding["production_qualification_admission_id"]).startswith("sha256:")
    assert binding["production_qualification_signer_registry_expires_at"] is None
    assert (
        binding["production_qualification_signer_registry_sha256"]
        == (PINNED_REGISTRY_IDENTITY_SHA256[7:])
    )


def test_v4_guard_refuses_a_foreign_bundle(tmp_path: Path) -> None:
    path = tmp_path / "authority.json"
    _write_authority(path, v4_authority_payload())
    guard = ProductionQualificationGuard(path, remote_permit_revalidation=True)
    workflow = SimpleNamespace(
        manifest=SimpleNamespace(content_digest="cd" * 32),
        qualification=None,
    )
    with pytest.raises(
        ProductionQualificationAuthorityError,
        match="does not bind the sealed workflow contracts",
    ):
        guard.verify(workflow, for_actuation=True)


def test_demo_does_not_require_signed_admission() -> None:
    from openadapt_flow.execution_profiles import (
        ExecutionProfile,
        requires_signed_qualification_admission,
    )

    assert not requires_signed_qualification_admission(
        ExecutionProfile.DEMO, will_actuate=True
    )


@pytest.mark.parametrize("profile", ["standard", "regulated"])
def test_cli_production_run_refuses_without_admission(
    tmp_path: Path, capsys, monkeypatch, profile: str
) -> None:
    from openadapt_flow.__main__ import main
    from openadapt_flow.ir import Workflow

    monkeypatch.chdir(tmp_path)
    bundle = tmp_path / "bundle"
    Workflow(name="unsigned", steps=[]).save(bundle)
    rc = main(
        [
            "run",
            str(bundle),
            "--profile",
            profile,
            "--backend",
            "web",
            "--run-dir",
            str(tmp_path / "run"),
        ]
    )
    assert rc == 2
    out = capsys.readouterr().out
    assert "signed qualification admission" in out
    assert "Nothing was executed." in out


def test_cli_standard_dry_run_does_not_require_admission(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    from openadapt_flow.__main__ import main
    from openadapt_flow.ir import Workflow

    monkeypatch.chdir(tmp_path)
    bundle = tmp_path / "bundle"
    Workflow(name="report-only", steps=[]).save(bundle)
    rc = main(
        [
            "run",
            str(bundle),
            "--profile",
            "standard",
            "--backend",
            "web",
            "--dry-run",
            "--run-dir",
            str(tmp_path / "run"),
        ]
    )
    out = capsys.readouterr().out
    assert "signed qualification admission" not in out
    assert rc in {0, 2}


def test_cli_demo_run_does_not_require_admission(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    from openadapt_flow.__main__ import main
    from openadapt_flow.ir import Workflow
    from tests.test_surface_selection import _install_fake_browser

    monkeypatch.chdir(tmp_path)
    captured: dict = {}
    _install_fake_browser(monkeypatch, captured)
    bundle = tmp_path / "bundle"
    Workflow(name="demo", steps=[]).save(bundle)
    rc = main(
        [
            "run",
            str(bundle),
            "--profile",
            "demo",
            "--backend",
            "web",
            "--url",
            "http://app.example/",
            "--run-dir",
            str(tmp_path / "run"),
        ]
    )
    out = capsys.readouterr().out
    assert "signed qualification admission" not in out
    assert rc == 0
    assert "run" in captured


def test_matching_v4_admission_reaches_existing_run_gate(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    from openadapt_flow.__main__ import main
    from openadapt_flow.ir import ActionKind, Step, Workflow
    from openadapt_flow.production_qualification import ProductionQualificationGuard

    monkeypatch.chdir(tmp_path)
    bundle = tmp_path / "bundle"
    Workflow(
        name="synthetic",
        steps=[
            Step(
                id="save",
                intent="save",
                action=ActionKind.CLICK,
                risk="irreversible",
            )
        ],
    ).save(bundle)
    authority = tmp_path / "authority.json"
    _write_authority(authority, v4_authority_payload())

    monkeypatch.setattr(
        ProductionQualificationGuard,
        "verify",
        lambda self, workflow, *, for_actuation: object(),
    )
    rc = main(
        [
            "run",
            str(bundle),
            "--profile",
            "standard",
            "--backend",
            "web",
            "--qualification-authority-file",
            str(authority),
            "--run-dir",
            str(tmp_path / "run"),
        ]
    )
    assert rc == 2
    out = capsys.readouterr().out
    assert "signed qualification admission" not in out
    assert "Nothing was executed." in out
    assert "the Production qualification authority is invalid" not in out


def test_tutorial_standard_path_does_not_construct_a_production_guard(
    monkeypatch,
) -> None:
    from openadapt_flow.tutorial import TutorialError, run_tutorial_workflow

    constructed: list[object] = []

    class _Boom:
        def __init__(self, *args, **kwargs) -> None:
            constructed.append((args, kwargs))
            raise AssertionError("tutorial must not require a signed admission")

    monkeypatch.setattr(
        "openadapt_flow.production_qualification.ProductionQualificationGuard",
        _Boom,
    )
    monkeypatch.setattr(
        "openadapt_flow.tutorial._http_json", lambda *args, **kwargs: {}
    )
    monkeypatch.setattr(
        "openadapt_flow.run_gate.evaluate_run_gate",
        lambda *args, **kwargs: SimpleNamespace(
            passed=False,
            render=lambda: "refused in test",
        ),
    )
    with pytest.raises(TutorialError, match="REFUSED the tutorial bundle"):
        run_tutorial_workflow(
            base_url="http://127.0.0.1:9",
            workflow=SimpleNamespace(params={"note": "x"}),
            bundle_dir=Path("/tmp"),
            run_dir=Path("/tmp"),
        )
    assert constructed == []


def test_live_binding_refuses_when_required_fields_are_missing() -> None:
    admission = issued_admission()
    live = {"bundle_sha256": ISSUED_BUNDLE_SHA256}
    with pytest.raises(QualificationAdmissionV4Error, match="missing required fields"):
        bind_live_v4_admission(admission, live)


def test_live_binding_refuses_a_mismatched_organization() -> None:
    admission = issued_admission()
    live = _live_binding(admission, organization_id_sha256="sha256:" + ("cd" * 32))
    with pytest.raises(QualificationAdmissionV4Error, match="does not bind the live"):
        bind_live_v4_admission(admission, live)


@pytest.mark.parametrize(
    "field",
    [
        "organization_id_sha256",
        "workflow_id_sha256",
        "workflow_version_id_sha256",
        "admitted_runtime_sha256",
        "application_contract_sha256",
        "environment_contract_sha256",
        "input_contract_sha256",
        "action_contract_sha256",
        "identity_contract_sha256",
        "effect_contract_sha256",
        "policy_contract_sha256",
        "bundle_version",
        "local_identity_opening",
    ],
)
def test_live_binding_refuses_each_mismatched_v4_field(field: str) -> None:
    admission = issued_admission()
    if field == "bundle_version":
        live = _live_binding(admission, bundle_version="9.9.9-other")
    elif field == "local_identity_opening":
        live = _live_binding(
            admission,
            local_identity_opening={
                **admission["local_identity_opening"],
                "required": False,
            },
        )
    else:
        live = _live_binding(admission, **{field: "sha256:" + ("ee" * 32)})
    with pytest.raises(QualificationAdmissionV4Error, match="does not bind the live"):
        bind_live_v4_admission(admission, live)


def test_extract_live_binding_does_not_invent_missing_digests() -> None:
    workflow = SimpleNamespace(
        manifest=SimpleNamespace(content_digest=ISSUED_BUNDLE_SHA256[7:]),
        qualification=None,
    )
    live = extract_live_v4_binding(workflow)
    assert live["bundle_sha256"] == ISSUED_BUNDLE_SHA256
    assert "organization_id_sha256" not in live
    assert "admitted_runtime_sha256" not in live
    assert "application_contract_sha256" not in live


def test_v4_verify_binds_live_fields_not_only_the_bundle() -> None:
    live = _live_binding()
    verified = verify_qualification_admission_v4(
        issued_admission(),
        registry=issued_registry(),
        decision_receipt=issued_receipt(),
        revocation_state=issued_revocation(),
        expected_live=live,
        now=NOW,
    )
    assert verified.bundle_sha256 == ISSUED_BUNDLE_SHA256
    live["organization_id_sha256"] = "sha256:" + ("11" * 32)
    with pytest.raises(QualificationAdmissionV4Error, match="does not bind the live"):
        verify_qualification_admission_v4(
            issued_admission(),
            registry=issued_registry(),
            decision_receipt=issued_receipt(),
            revocation_state=issued_revocation(),
            expected_live=live,
            now=NOW,
        )


def test_v4_guard_refuses_missing_live_contract_fields(tmp_path: Path) -> None:
    path = tmp_path / "authority.json"
    _write_authority(path, v4_authority_payload())
    guard = ProductionQualificationGuard(path, remote_permit_revalidation=False)
    workflow = SimpleNamespace(
        manifest=SimpleNamespace(content_digest=ISSUED_BUNDLE_SHA256[7:]),
        qualification=None,
    )
    with pytest.raises(
        ProductionQualificationAuthorityError,
        match="does not bind the sealed workflow contracts",
    ):
        guard.verify(workflow, for_actuation=True)


def test_current_revocation_list_revokes_the_saved_admission() -> None:
    admission = issued_admission()
    reason = admission_is_revoked(
        admission,
        [
            {
                "subject_kind": "qualification-admission",
                "subject_id": admission["admission_id_sha256"],
                "revoked_at": "2026-09-02T19:00:00Z",
            }
        ],
    )
    assert reason == "qualification admission is revoked"
    assert admission_is_revoked(admission, []) is None


def test_revocation_successor_must_be_monotonic() -> None:
    current = issued_revocation()
    digest = current["revocation_state_sha256"]
    assert revocation_is_monotonic_successor(
        previous_digest=digest,
        previous_revision=1,
        previous_observed_at=current["observed_at"],
        current=current,
    )
    later = {
        **current,
        "revision": 2,
        "observed_at": "2026-09-02T19:00:00Z",
        "previous_revocation_state_sha256": digest,
        "revocation_state_sha256": "sha256:" + ("ab" * 32),
    }
    assert revocation_is_monotonic_successor(
        previous_digest=digest,
        previous_revision=1,
        previous_observed_at=current["observed_at"],
        current=later,
    )
    rollback = {
        **current,
        "revision": 1,
        "previous_revocation_state_sha256": "sha256:" + ("cd" * 32),
        "revocation_state_sha256": "sha256:" + ("ab" * 32),
    }
    assert not revocation_is_monotonic_successor(
        previous_digest=digest,
        previous_revision=1,
        previous_observed_at=current["observed_at"],
        current=rollback,
    )


def test_later_revocation_fails_a_saved_authority_file(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "authority.json"
    payload = v4_authority_payload()
    _write_authority(path, payload)
    workflow = _matching_live_workflow()
    guard = ProductionQualificationGuard(path, remote_permit_revalidation=False)
    guard.verify(workflow, for_actuation=False)

    admission = payload["qualification_admission"]
    later = dict(payload["revocation_state"])
    later["revocations"] = [
        {
            "subject_kind": "qualification-admission",
            "subject_id": admission["admission_id_sha256"],
            "revoked_at": "2026-09-02T19:00:00Z",
        }
    ]
    later["revision"] = 2
    later["observed_at"] = "2026-09-02T19:00:00Z"
    later["previous_revocation_state_sha256"] = later["revocation_state_sha256"]
    later["revocation_state_sha256"] = "sha256:" + ("ab" * 32)
    payload["revocation_state"] = later
    _write_authority(path, payload)

    with pytest.raises(ProductionQualificationAuthorityError):
        guard.verify(workflow, for_actuation=True)


def test_guard_rereads_current_revocation_not_only_the_frozen_digest(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "authority.json"
    payload = v4_authority_payload()
    _write_authority(path, payload)
    workflow = _matching_live_workflow()
    guard = ProductionQualificationGuard(path, remote_permit_revalidation=False)
    first = guard._revocation_state_sha256
    monkeypatch.setattr(
        "openadapt_flow.qualification_admission_v4._verify_embedded_signature",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "openadapt_flow.qualification_admission_v4.admission_is_revoked",
        lambda admission, revocations: "qualification admission is revoked",
    )
    with pytest.raises(ProductionQualificationAuthorityError):
        guard.verify(workflow, for_actuation=True)
    assert guard._revocation_state_sha256 == first


@pytest.mark.parametrize(
    "mutate",
    [
        lambda obj: obj.pop("issued_at"),
        lambda obj: obj.pop("not_before"),
        lambda obj: obj.pop("expires_at"),
        lambda obj: obj.pop("issuer"),
        lambda obj: obj["issuer"].pop("repository"),
        lambda obj: obj["issuer"].pop("source_commit"),
    ],
)
def test_malformed_admission_issuer_or_time_is_a_controlled_refusal(mutate) -> None:
    admission = issued_admission()
    mutate(admission)
    with pytest.raises(QualificationAdmissionV4Error):
        validate_qualification_admission_v4(admission, now=NOW)


def test_malformed_receipt_time_is_a_controlled_refusal() -> None:
    receipt = issued_receipt()
    receipt.pop("issued_at")
    with pytest.raises(
        QualificationAdmissionV4Error, match="malformed|validity window"
    ):
        verify_qualification_admission_v4(
            issued_admission(),
            registry=issued_registry(),
            decision_receipt=receipt,
            revocation_state=issued_revocation(),
            now=NOW,
        )


def test_malformed_revocation_issuer_is_a_controlled_refusal() -> None:
    state = issued_revocation()
    state["issuer"].pop("repository")
    with pytest.raises(QualificationAdmissionV4Error, match="malformed|incomplete"):
        verify_qualification_admission_v4(
            issued_admission(),
            registry=issued_registry(),
            decision_receipt=issued_receipt(),
            revocation_state=state,
            now=NOW,
        )


def test_malformed_revocation_observed_at_is_a_controlled_refusal() -> None:
    state = issued_revocation()
    state.pop("observed_at")
    with pytest.raises(
        QualificationAdmissionV4Error, match="malformed|validity window"
    ):
        verify_qualification_admission_v4(
            issued_admission(),
            registry=issued_registry(),
            decision_receipt=issued_receipt(),
            revocation_state=state,
            now=NOW,
        )


def test_managed_dispatch_rechecks_admission_at_an_input_edge() -> None:
    from openadapt_flow.production_qualification import (
        managed_qualification_edge_refusal,
    )

    authorization = SimpleNamespace(
        execution_profile="standard",
        qualification_admission=issued_admission(),
    )
    matching = _matching_live_workflow()
    assert managed_qualification_edge_refusal(authorization, matching) is None

    missing = SimpleNamespace(
        manifest=SimpleNamespace(content_digest=ISSUED_BUNDLE_SHA256[7:]),
        qualification=None,
    )
    assert managed_qualification_edge_refusal(authorization, missing) is not None

    revoked = SimpleNamespace(qualification_admission=None)
    assert managed_qualification_edge_refusal(revoked, matching) is not None


def test_managed_dispatch_malformed_admission_is_a_controlled_refusal() -> None:
    from openadapt_flow.production_qualification import (
        managed_qualification_edge_refusal,
    )

    admission = issued_admission()
    admission.pop("issuer")
    authorization = SimpleNamespace(qualification_admission=admission)
    refusal = managed_qualification_edge_refusal(
        authorization, _matching_live_workflow()
    )
    assert refusal is not None


def test_replayer_rechecks_managed_v4_admission_before_delivery(
    tmp_path: Path,
) -> None:
    from openadapt_flow.runtime.replayer import Replayer

    class _Binding:
        run_id = "run-v4"
        binding_sha256 = "0" * 64
        authorization = SimpleNamespace(
            execution_profile="standard",
            qualification_admission=issued_admission(),
            qualification_case_id=None,
        )

    replayer = Replayer.__new__(Replayer)
    replayer.production_qualification_guard = None
    replayer.managed_dispatch_binding = _Binding()
    replayer.governed_authorization = _Binding.authorization
    replayer.qualification_campaign_guard = None
    missing = SimpleNamespace(
        manifest=SimpleNamespace(content_digest=ISSUED_BUNDLE_SHA256[7:]),
        qualification=None,
    )
    assert replayer._production_qualification_refusal(missing) is not None
    matching = _matching_live_workflow()
    assert replayer._production_qualification_refusal(matching) is None
