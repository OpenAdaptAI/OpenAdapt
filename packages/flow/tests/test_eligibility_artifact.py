"""Atomic, idempotent, PHI-bound eligibility artifact tests."""

from __future__ import annotations

import base64
import csv
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from openadapt_flow.eligibility.artifact import (
    BOUNDARY_FILE,
    RESULTS_CSV,
    TRANSACTIONS_DIR,
    ArtifactEncryption,
    PracticeArtifactPolicy,
    all_confirmed,
    purge_expired_eligibility_artifacts,
    write_and_verify,
    write_eligibility_artifacts,
)
from openadapt_flow.eligibility.client import (
    ApplicationMode,
    BenefitSelection,
    EligibilityRequest,
    EligibilityStatus,
    parse_271,
)
from openadapt_flow.runtime.effects.document_hash import DocumentHashVerifier
from openadapt_flow.runtime.effects.effect import Verdict

ACTIVE = {
    "meta": {"applicationMode": "test"},
    "tradingPartnerServiceId": "62308",
    "subscriber": {"memberId": "U3141592653"},
    "provider": {"npi": "1999999984"},
    "payer": {"name": "Cigna"},
    "planInformation": {"groupDescription": "DENTAL PPO"},
    "planDateInformation": {"planBegin": "20260101", "planEnd": "20261231"},
    "benefitsInformation": [
        {"code": "1", "serviceTypeCodes": ["35"]},
        {
            "code": "C",
            "benefitAmount": "50",
            "serviceTypeCodes": ["35"],
            "coverageLevelCode": "IND",
            "inPlanNetworkIndicatorCode": "Y",
            "timeQualifierCode": "23",
        },
    ],
}


def request(operation_id="artifact-check-1") -> EligibilityRequest:
    return EligibilityRequest(
        operation_id=operation_id,
        payer_id="62308",
        provider_npi="1999999984",
        provider_organization="One",
        member_id="U3141592653",
        service_type_codes=["35"],
        date_of_service="20260721",
        benefit_selection=BenefitSelection(network_code="Y", coverage_level_code="IND"),
    )


def active_result(operation_id="artifact-check-1"):
    return parse_271(
        request(operation_id),
        json.dumps(ACTIVE).encode(),
        expected_mode=ApplicationMode.TEST,
    )


def volume_policy(
    boundary="practice-1", mode=ApplicationMode.TEST
) -> PracticeArtifactPolicy:
    return PracticeArtifactPolicy(
        boundary_id=boundary,
        application_mode=mode,
        encryption=ArtifactEncryption.PLATFORM_VOLUME,
        volume_encryption_attested=True,
        retention_days=30,
    )


def encrypted_policy() -> PracticeArtifactPolicy:
    return PracticeArtifactPolicy(
        boundary_id="practice-encrypted",
        application_mode=ApplicationMode.TEST,
        encryption=ArtifactEncryption.APPLICATION_AES256_GCM,
        encryption_key_env="ELIGIBILITY_ARTIFACT_KEY",
        retention_days=30,
    )


def test_raw_and_normalized_records_promote_together_and_verify(tmp_path):
    artifact, verdicts = write_and_verify(
        active_result(),
        tmp_path,
        request=request(),
        policy=volume_policy(),
    )
    assert artifact.created
    assert len(verdicts) == 4
    assert all_confirmed(verdicts), [v.reason for v in verdicts]
    tx = Path(artifact.transaction_dir)
    assert tx.is_dir()
    assert {p.name for p in tx.iterdir()} == {
        Path(artifact.raw_271_file).name,
        Path(artifact.normalized_file).name,
        "manifest.json",
    }
    manifest = json.loads((tx / "manifest.json").read_text())
    assert manifest["raw_plain_sha256"] == artifact.raw_271_sha256
    assert manifest["normalized_plain_sha256"] == artifact.normalized_sha256
    assert (
        manifest["response_subject_sha256"] == active_result().response_subject_sha256
    )
    assert manifest["egress"] == "none"
    assert manifest["application_mode"] == "test"
    assert manifest["http_status"] == 200
    assert manifest["committed_at"] == artifact.committed_at
    assert manifest["retention_expires_at"].endswith("Z")
    boundary = json.loads((tmp_path / BOUNDARY_FILE).read_text())
    assert boundary["application_mode"] == "test"


def test_csv_is_derived_and_formula_neutralized(tmp_path):
    bound_request = request().model_copy(update={"member_id": '=HYPERLINK("bad")'})
    response = json.loads(json.dumps(ACTIVE))
    response["subscriber"]["memberId"] = bound_request.member_id
    response["payer"]["name"] = "+malicious"
    result = parse_271(
        bound_request,
        json.dumps(response).encode(),
        expected_mode=ApplicationMode.TEST,
    )
    artifact, verdicts = write_and_verify(
        result,
        tmp_path,
        request=bound_request,
        policy=volume_policy(),
    )
    assert all_confirmed(verdicts)
    with Path(artifact.results_csv).open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["member_id"].startswith("'=")
    assert rows[0]["payer"].startswith("'+")
    assert rows[0]["deductible_total"] == "50"
    assert rows[0]["status"] == EligibilityStatus.ACTIVE.value
    assert rows[0]["application_mode"] == "test"
    assert rows[0]["http_status"] == "200"
    assert rows[0]["committed_at"]


def test_same_operation_and_content_is_idempotent(tmp_path):
    first, first_verdicts = write_and_verify(
        active_result(), tmp_path, request=request(), policy=volume_policy()
    )
    second, second_verdicts = write_and_verify(
        active_result(), tmp_path, request=request(), policy=volume_policy()
    )
    assert first.created and not second.created
    assert first.transaction_dir == second.transaction_dir
    assert all_confirmed(first_verdicts) and all_confirmed(second_verdicts)
    with (tmp_path / RESULTS_CSV).open(newline="") as handle:
        assert len(list(csv.DictReader(handle))) == 1


def test_operation_id_reuse_with_changed_content_refuses(tmp_path):
    write_eligibility_artifacts(
        active_result(), tmp_path, request=request(), policy=volume_policy()
    )
    changed = {**ACTIVE, "controlNumber": "changed"}
    changed_result = parse_271(
        request(), json.dumps(changed).encode(), expected_mode=ApplicationMode.TEST
    )
    with pytest.raises(FileExistsError, match="different content"):
        write_eligibility_artifacts(
            changed_result, tmp_path, request=request(), policy=volume_policy()
        )


def test_tampered_storage_is_refuted(tmp_path):
    artifact, verdicts = write_and_verify(
        active_result(), tmp_path, request=request(), policy=volume_policy()
    )
    assert all_confirmed(verdicts)
    raw = Path(artifact.raw_271_file)
    raw.write_bytes(raw.read_bytes() + b"tampered")
    verifier = DocumentHashVerifier(tmp_path, glob="transactions/tx_*/*")
    state = verifier.capture_pre_state()
    digest_effect = artifact.effects[1]
    verdict = verifier.verify(digest_effect, state)
    assert verdict.verdict is Verdict.REFUTED


def test_application_encryption_leaks_no_member_or_raw_payload(tmp_path):
    key = base64.urlsafe_b64encode(b"k" * 32).decode()
    artifact, verdicts = write_and_verify(
        active_result(),
        tmp_path,
        request=request(),
        policy=encrypted_policy(),
        env={"ELIGIBILITY_ARTIFACT_KEY": key},
    )
    assert all_confirmed(verdicts)
    assert artifact.results_csv.endswith(".enc")
    for path in tmp_path.rglob("*"):
        if path.is_file():
            payload = path.read_bytes()
            assert b"U3141592653" not in payload
            assert b"benefitsInformation" not in payload


def test_application_encryption_requires_real_32_byte_key(tmp_path):
    with pytest.raises(ValueError, match="decode to 32 bytes"):
        write_eligibility_artifacts(
            active_result(),
            tmp_path,
            request=request(),
            policy=encrypted_policy(),
            env={
                "ELIGIBILITY_ARTIFACT_KEY": base64.urlsafe_b64encode(b"short").decode()
            },
        )


def test_boundary_policy_mismatch_fails_loud(tmp_path):
    write_eligibility_artifacts(
        active_result(), tmp_path, request=request(), policy=volume_policy()
    )
    with pytest.raises(ValueError, match="different PHI policy"):
        write_eligibility_artifacts(
            active_result("other-operation"),
            tmp_path,
            request=request("other-operation"),
            policy=volume_policy("practice-2"),
        )
    assert (tmp_path / BOUNDARY_FILE).exists()


def test_symlinked_root_and_index_are_refused(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(ValueError, match="not a link"):
        write_eligibility_artifacts(
            active_result(), linked, request=request(), policy=volume_policy()
        )

    root = tmp_path / "root"
    write_eligibility_artifacts(
        active_result(), root, request=request(), policy=volume_policy()
    )
    (root / RESULTS_CSV).unlink()
    (root / RESULTS_CSV).symlink_to(outside / "stolen.csv")
    with pytest.raises(ValueError, match="symlinked"):
        write_eligibility_artifacts(
            active_result("next-operation"),
            root,
            request=request("next-operation"),
            policy=volume_policy(),
        )


def test_existing_broad_directory_is_refused_without_chmod(tmp_path):
    root = tmp_path / "broad"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    with pytest.raises(PermissionError, match="already be owner-only"):
        write_eligibility_artifacts(
            active_result(), root, request=request(), policy=volume_policy()
        )
    assert root.stat().st_mode & 0o777 == 0o755


def test_concurrent_writer_lock_fails_fast(tmp_path):
    tmp_path.chmod(0o700)
    (tmp_path / ".eligibility-write.lock").mkdir()
    with pytest.raises(BlockingIOError, match="holds the lock"):
        write_eligibility_artifacts(
            active_result(), tmp_path, request=request(), policy=volume_policy()
        )


def test_atomic_promotion_failure_leaves_no_consumable_transaction(
    tmp_path, monkeypatch
):
    real_rename = os.rename

    def fail_stage(source, destination):
        if ".staging-" in str(source):
            raise OSError("injected rename failure")
        return real_rename(source, destination)

    monkeypatch.setattr(os, "rename", fail_stage)
    with pytest.raises(OSError, match="injected"):
        write_eligibility_artifacts(
            active_result(), tmp_path, request=request(), policy=volume_policy()
        )
    transactions = tmp_path / "transactions"
    assert transactions.exists()
    assert list(transactions.iterdir()) == []
    assert not (tmp_path / RESULTS_CSV).exists()


@pytest.mark.parametrize("target", ["raw", "normalized", "manifest", "index"])
def test_every_staged_contract_is_verified_before_promotion(
    tmp_path, monkeypatch, target
):
    import openadapt_flow.eligibility.artifact as artifact_module

    real_write = artifact_module._write_exclusive

    def corrupt_after_write(path, payload):
        real_write(path, payload)
        name = path.name
        selected = (
            (target == "raw" and name.startswith("raw_271_"))
            or (target == "normalized" and name.startswith("result_"))
            or (target == "manifest" and name == "manifest.json")
            or (target == "index" and name.startswith(f".{RESULTS_CSV}."))
        )
        if selected:
            with path.open("ab") as handle:
                handle.write(b"corrupt")

    monkeypatch.setattr(artifact_module, "_write_exclusive", corrupt_after_write)
    with pytest.raises((ValueError, json.JSONDecodeError)):
        write_eligibility_artifacts(
            active_result(), tmp_path, request=request(), policy=volume_policy()
        )
    transactions = tmp_path / TRANSACTIONS_DIR
    assert transactions.is_dir()
    assert list(transactions.iterdir()) == []
    assert not (tmp_path / RESULTS_CSV).exists()
    assert not list(tmp_path.glob(".*.tmp"))


def test_index_promotion_failure_rolls_back_promoted_transaction(tmp_path, monkeypatch):
    real_replace = os.replace

    def fail_index(source, destination):
        if Path(destination).name == RESULTS_CSV:
            raise OSError("injected index promotion failure")
        return real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_index)
    with pytest.raises(OSError, match="index promotion"):
        write_eligibility_artifacts(
            active_result(), tmp_path, request=request(), policy=volume_policy()
        )
    assert list((tmp_path / TRANSACTIONS_DIR).iterdir()) == []
    assert not (tmp_path / RESULTS_CSV).exists()


def test_independent_effect_failure_rolls_back_new_transaction(tmp_path, monkeypatch):
    import openadapt_flow.eligibility.artifact as artifact_module

    monkeypatch.setattr(artifact_module, "all_confirmed", lambda _verdicts: False)
    with pytest.raises(RuntimeError, match="did not confirm"):
        write_and_verify(
            active_result(), tmp_path, request=request(), policy=volume_policy()
        )
    assert list((tmp_path / TRANSACTIONS_DIR).iterdir()) == []
    with (tmp_path / RESULTS_CSV).open(newline="") as handle:
        assert list(csv.DictReader(handle)) == []


def test_expired_transaction_is_purged_and_cannot_be_reused(tmp_path):
    policy = PracticeArtifactPolicy(
        boundary_id="practice-retention",
        application_mode=ApplicationMode.TEST,
        encryption=ArtifactEncryption.PLATFORM_VOLUME,
        volume_encryption_attested=True,
        retention_days=1,
    )
    artifact = write_eligibility_artifacts(
        active_result(), tmp_path, request=request(), policy=policy
    )
    manifest = json.loads(
        (Path(artifact.transaction_dir) / "manifest.json").read_text()
    )
    expires = datetime.strptime(
        manifest["retention_expires_at"], "%Y-%m-%dT%H:%M:%SZ"
    ).replace(tzinfo=timezone.utc)
    removed = purge_expired_eligibility_artifacts(
        tmp_path, policy=policy, now=expires + timedelta(seconds=1)
    )
    assert removed == [request().operation_id]
    assert not Path(artifact.transaction_dir).exists()
    with (tmp_path / RESULTS_CSV).open(newline="") as handle:
        assert list(csv.DictReader(handle)) == []

    replacement = write_eligibility_artifacts(
        active_result(), tmp_path, request=request(), policy=policy
    )
    assert replacement.created


def test_retention_index_failure_restores_expired_transaction(tmp_path, monkeypatch):
    policy = PracticeArtifactPolicy(
        boundary_id="practice-retention-rollback",
        application_mode=ApplicationMode.TEST,
        encryption=ArtifactEncryption.PLATFORM_VOLUME,
        volume_encryption_attested=True,
        retention_days=1,
    )
    artifact = write_eligibility_artifacts(
        active_result(), tmp_path, request=request(), policy=policy
    )
    manifest = json.loads(
        (Path(artifact.transaction_dir) / "manifest.json").read_text()
    )
    expires = datetime.strptime(
        manifest["retention_expires_at"], "%Y-%m-%dT%H:%M:%SZ"
    ).replace(tzinfo=timezone.utc)
    real_replace = os.replace

    def fail_index(source, destination):
        if Path(destination).name == RESULTS_CSV:
            raise OSError("injected retention index failure")
        return real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_index)
    with pytest.raises(OSError, match="retention index"):
        purge_expired_eligibility_artifacts(
            tmp_path, policy=policy, now=expires + timedelta(seconds=1)
        )
    assert Path(artifact.transaction_dir).is_dir()
    with (tmp_path / RESULTS_CSV).open(newline="") as handle:
        assert len(list(csv.DictReader(handle))) == 1


@pytest.mark.parametrize(
    ("expiry", "error"),
    [
        ("not-a-timestamp", "retention expiry"),
        ("2099-01-01T00:00:00Z", "bound policy"),
    ],
)
def test_invalid_retention_manifest_denies_purge_without_deleting(
    tmp_path, expiry, error
):
    policy = volume_policy("practice-retention-denial")
    artifact = write_eligibility_artifacts(
        active_result(), tmp_path, request=request(), policy=policy
    )
    manifest_path = Path(artifact.transaction_dir) / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["retention_expires_at"] = expiry
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match=error):
        purge_expired_eligibility_artifacts(
            tmp_path,
            policy=policy,
            now=datetime.now(timezone.utc) + timedelta(days=365),
        )
    assert Path(artifact.transaction_dir).is_dir()


def test_transport_outcome_without_raw_response_is_not_promoted(tmp_path):
    result = active_result().model_copy(
        update={"raw_271_bytes": None, "raw_271_sha256": None}
    )
    with pytest.raises(ValueError, match="raw response"):
        write_eligibility_artifacts(
            result, tmp_path, request=request(), policy=volume_policy()
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("copay", "999999"),
        ("payer_name", "Fabricated Payer"),
        ("response_subject_sha256", "0" * 64),
    ],
)
def test_mutated_normalized_result_cannot_be_promoted(tmp_path, field, value):
    result = active_result().model_copy(update={field: value})
    with pytest.raises(ValueError, match="does not match the exact raw response"):
        write_eligibility_artifacts(
            result, tmp_path, request=request(), policy=volume_policy()
        )


def test_non_answer_and_wrong_subject_binding_are_not_consumable(tmp_path):
    ambiguous_body = json.loads(json.dumps(ACTIVE))
    ambiguous_body["benefitsInformation"].append(
        {"code": "6", "serviceTypeCodes": ["35"]}
    )
    ambiguous = parse_271(
        request(),
        json.dumps(ambiguous_body).encode(),
        expected_mode=ApplicationMode.TEST,
    )
    with pytest.raises(ValueError, match="unambiguous"):
        write_eligibility_artifacts(
            ambiguous, tmp_path / "ambiguous", request=request(), policy=volume_policy()
        )

    wrong_subject = request().model_copy(update={"member_id": "OTHER-MEMBER"})
    with pytest.raises(ValueError, match="not bound"):
        write_eligibility_artifacts(
            active_result(),
            tmp_path / "wrong-subject",
            request=wrong_subject,
            policy=volume_policy(),
        )


def test_tampered_manifest_cannot_escape_transaction_directory(tmp_path):
    artifact = write_eligibility_artifacts(
        active_result(), tmp_path, request=request(), policy=volume_policy()
    )
    manifest_path = Path(artifact.transaction_dir) / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["normalized_file"] = "../../../outside.json"
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="normalized_file"):
        write_eligibility_artifacts(
            active_result(), tmp_path, request=request(), policy=volume_policy()
        )


def test_test_answer_cannot_enter_a_production_artifact_boundary(tmp_path):
    with pytest.raises(ValueError, match="application mode"):
        write_eligibility_artifacts(
            active_result(),
            tmp_path,
            request=request(),
            policy=volume_policy(mode=ApplicationMode.PRODUCTION),
        )
    assert not (tmp_path / TRANSACTIONS_DIR).exists()


def test_artifact_boundary_cannot_mix_application_modes(tmp_path):
    write_eligibility_artifacts(
        active_result(), tmp_path, request=request(), policy=volume_policy()
    )
    production_response = json.loads(json.dumps(ACTIVE))
    production_response["meta"]["applicationMode"] = "production"
    production_request = request("production-check")
    production_result = parse_271(
        production_request,
        json.dumps(production_response).encode(),
        expected_mode=ApplicationMode.PRODUCTION,
    )
    assert production_result.is_answer
    with pytest.raises(ValueError, match="different PHI policy"):
        write_eligibility_artifacts(
            production_result,
            tmp_path,
            request=production_request,
            policy=volume_policy(mode=ApplicationMode.PRODUCTION),
        )


def test_caller_timestamp_is_not_persisted_as_audit_truth(tmp_path):
    result = active_result().model_copy(update={"checked_at": "1900-01-01T00:00:00Z"})
    artifact = write_eligibility_artifacts(
        result, tmp_path, request=request(), policy=volume_policy()
    )
    normalized = json.loads(Path(artifact.normalized_file).read_text())
    assert "checked_at" not in normalized
    assert normalized["committed_at"] == artifact.committed_at
    assert normalized["committed_at"] != result.checked_at
    assert normalized["application_mode"] == "test"
    assert normalized["http_status"] == 200


@pytest.mark.parametrize("target", ["transaction", "raw", "normalized", "manifest"])
def test_committed_paths_must_remain_owner_only(tmp_path, target):
    artifact = write_eligibility_artifacts(
        active_result(), tmp_path, request=request(), policy=volume_policy()
    )
    tx = Path(artifact.transaction_dir)
    paths = {
        "transaction": tx,
        "raw": Path(artifact.raw_271_file),
        "normalized": Path(artifact.normalized_file),
        "manifest": tx / "manifest.json",
    }
    paths[target].chmod(0o755 if target == "transaction" else 0o644)
    with pytest.raises(PermissionError, match="owner-only"):
        write_eligibility_artifacts(
            active_result(), tmp_path, request=request(), policy=volume_policy()
        )


def test_committed_file_symlink_substitution_is_refused(tmp_path):
    artifact = write_eligibility_artifacts(
        active_result(), tmp_path, request=request(), policy=volume_policy()
    )
    raw = Path(artifact.raw_271_file)
    outside = tmp_path.parent / f"{tmp_path.name}-outside.json"
    outside.write_bytes(raw.read_bytes())
    raw.unlink()
    try:
        raw.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises((OSError, ValueError)):
        write_eligibility_artifacts(
            active_result(), tmp_path, request=request(), policy=volume_policy()
        )
