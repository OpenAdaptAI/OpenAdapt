"""verify-run: an external actor writes to MockMed; the store decides.

The "executor" here is test code posting straight to MockMed's encounter API,
the way a computer-use agent or a script would. Flow replay never runs. The
verdict comes from ``RestRecordVerifier`` reading ``GET /api/db``. No model
calls, no network beyond localhost.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path

import pytest
import requests
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from openadapt_types.execute import ExecuteEvidenceReceiptV1
from openadapt_types.seal import (
    parse_key_registry,
    receipt_sha256,
    seal_key_id,
    signing_payload,
    verify_sealed_receipt,
)

import openadapt_flow.runtime.effects.adapter as adapter_mod
from openadapt_flow.mockmed.fault_server import serve as fault_serve
from openadapt_flow.runtime.effects import (
    ConnectionProbe,
    EffectState,
    EffectVerdict,
    Verdict,
    register_verifier_factory,
)
from openadapt_flow.verification import VerificationTier
from openadapt_flow.verify_run import (
    EFFECT_RECEIPT_SCHEMA,
    VerifyConfig,
    VerifyRunError,
    capture_baseline,
    check_effect_receipt,
    load_verify_config,
    verify_run,
)

PARAMS = {"patient_id": "p1", "note": "Follow-up in two weeks"}
AGENT_RECORD = {
    "executor_kind": "agent",
    "executor_id": "cua-test-agent",
    # What the agent says. None of it may count as evidence.
    "claims": [{"wrote": "encounter", "status": "saved"}],
    "self_reported_verdict": "CONFIRMED",
}

# The fixed TEST seed openadapt-cloud's sealSignature tests use. Not a real key.
TEST_SEED = bytes([7]) * 32


def _verify_yaml(base_url: str, **extra: object) -> dict:
    doc = {
        "schema_version": "openadapt.verify/v1",
        "verifier": {"kind": "rest", "base_url": base_url, "records_path": "/api/db"},
        "effects": [
            {
                "kind": "record_written",
                "match": {
                    "patient_id": {"param": "patient_id"},
                    "type": "Triage",
                    "note": {"param": "note"},
                },
                "expected_count": 1,
                "count_new_only": True,
                # The verifier polls for a late write; keep absent cases short.
                "timeout_s": 0.5,
            }
        ],
    }
    doc.update(extra)
    return doc


@pytest.fixture
def sor():
    url, db, stop = fault_serve()
    try:
        yield url.rstrip("/"), db
    finally:
        stop()


@pytest.fixture
def cfg(sor):
    base, _db = sor
    return VerifyConfig.model_validate(_verify_yaml(base))


def _actor_writes(
    base: str, *, patient: str = "p1", note: str = PARAMS["note"], fault: str = ""
) -> None:
    url = f"{base}/api/encounter" + (f"?fault={fault}" if fault else "")
    requests.post(
        url, json={"patient_id": patient, "type": "Triage", "note": note}, timeout=5
    )


def _baseline(cfg: VerifyConfig, tmp_path: Path) -> Path:
    path = tmp_path / "baseline.json"
    capture_baseline(cfg, PARAMS, path)
    return path


def _run(cfg, tmp_path, *, baseline=True, record=AGENT_RECORD, pre=None):
    path = _baseline(cfg, tmp_path) if baseline else None
    if pre is not None:
        pre()
    return verify_run(
        cfg,
        params=PARAMS,
        run_record=record,
        authorization_ref="ticket-0000-test",
        baseline_path=path,
    )


def _registry() -> list[dict]:
    raw = (
        Ed25519PrivateKey.from_private_bytes(TEST_SEED)
        .public_key()
        .public_bytes(Encoding.Raw, PublicFormat.Raw)
    )
    return [
        {
            "key_id": seal_key_id(raw),
            "public_key": base64.b64encode(raw).decode(),
            "status": "active",
        }
    ]


def _issuer_sign(receipt: dict) -> dict:
    """Stand in for the issuer: the same steps Cloud's signSeal takes."""

    key = Ed25519PrivateKey.from_private_bytes(TEST_SEED)
    unsigned = {
        "schema": "openadapt.seal-signature/v1",
        "issuer": "openadapt-cloud",
        "algorithm": "ed25519",
        "key_id": _registry()[0]["key_id"],
        "receipt_sha256": receipt_sha256(receipt),
        "run_id": receipt["execution_id"],
        "issued_at": receipt["issued_at"],
    }
    signature = base64.b64encode(key.sign(signing_payload(unsigned))).decode()
    return {**receipt, "seal_signature": {**unsigned, "signature": signature}}


# -- the three outcomes ------------------------------------------------------


def test_correct_external_write_is_confirmed_and_verified(cfg, sor, tmp_path):
    base, _db = sor
    result = _run(cfg, tmp_path, pre=lambda: _actor_writes(base))
    receipt = result["receipt"]
    assert receipt["effect_verdict"] == "CONFIRMED"
    assert receipt["outcome"] == "verified"
    assert receipt["oracle_tier"] == 2
    assert receipt["executor_kind"] == "agent"
    assert (
        receipt["contracts"]["observed_effect_strength"]
        == "independent_system_of_record"
    )
    # The executor's model use is unknown, so the receipt cannot claim none.
    assert receipt["contracts"]["model_used"] is True
    assert result["evidence"]["verifier"]["model_calls"] == 0


def test_incorrect_external_write_is_refuted(cfg, sor, tmp_path):
    base, _db = sor
    # The note the agent typed is not the note the task named.
    result = _run(cfg, tmp_path, pre=lambda: _actor_writes(base, note="Wrong note"))
    assert result["receipt"]["effect_verdict"] == "REFUTED"
    assert result["receipt"]["outcome"] != "verified"


def test_wrong_patient_is_refuted(cfg, sor, tmp_path):
    base, _db = sor
    result = _run(cfg, tmp_path, pre=lambda: _actor_writes(base, fault="wrong_record"))
    assert result["receipt"]["effect_verdict"] == "REFUTED"
    assert result["receipt"]["outcome"] == "halted_before_effect"


def test_duplicate_write_needs_reconciliation(cfg, sor, tmp_path):
    base, _db = sor

    def twice() -> None:
        _actor_writes(base)
        _actor_writes(base)

    result = _run(cfg, tmp_path, pre=twice)
    receipt = result["receipt"]
    assert receipt["effect_verdict"] == "REFUTED"
    assert receipt["outcome"] == "reconciliation_required"
    assert receipt["delivery_uncertain"] is True
    assert (
        result["evidence"]["readback"]["effects"][0]["observed_effect"] == "conflicting"
    )


def test_no_record_with_baseline_is_refuted_absent(cfg, sor, tmp_path):
    base, _db = sor
    # The agent got a 2xx and reported success; nothing persisted.
    result = _run(cfg, tmp_path, pre=lambda: _actor_writes(base, fault="no_persist"))
    assert result["receipt"]["effect_verdict"] == "REFUTED"
    assert result["receipt"]["outcome"] == "halted_before_effect"
    assert result["evidence"]["readback"]["effects"][0]["observed_effect"] == "absent"


def test_no_baseline_means_indeterminate_never_confirmed(cfg, sor, tmp_path):
    base, _db = sor
    _actor_writes(base)  # a correct write, but nobody read the store before it
    result = _run(cfg, tmp_path, baseline=False)
    receipt = result["receipt"]
    assert receipt["effect_verdict"] == "INDETERMINATE"
    assert receipt["outcome"] == "reconciliation_required"
    assert result["evidence"]["readback"]["baseline_sha256"] is None


def test_unreachable_store_is_indeterminate(tmp_path):
    cfg = VerifyConfig.model_validate(_verify_yaml("http://127.0.0.1:1"))
    result = verify_run(
        cfg,
        params=PARAMS,
        run_record=AGENT_RECORD,
        authorization_ref="ticket-0000-test",
    )
    assert result["receipt"]["effect_verdict"] == "INDETERMINATE"
    assert result["receipt"]["outcome"] == "reconciliation_required"


# -- the executor's claims never count ---------------------------------------


def test_executor_claims_do_not_change_the_verdict(cfg, sor, tmp_path):
    base, _db = sor
    loud = {**AGENT_RECORD, "claims": [{"verified": True}] * 5, "outcome": "verified"}
    result = _run(
        cfg, tmp_path, record=loud, pre=lambda: _actor_writes(base, fault="no_persist")
    )
    assert result["receipt"]["effect_verdict"] == "REFUTED"
    evidence = result["evidence"]
    assert evidence["executor"]["claims_counted_as_evidence"] is False
    # The run record is bound by digest only; none of its text is copied.
    blob = json.dumps(result)
    assert "cua-test-agent" not in blob
    assert "saved" not in blob


def test_params_come_from_the_task_not_the_run_record(cfg, sor, tmp_path):
    base, _db = sor
    # The agent wrote for p2 and its record says p2 was the task. The task
    # said p1, so the store holds nothing that satisfies the contract.
    record = {**AGENT_RECORD, "params": {"patient_id": "p2", "note": PARAMS["note"]}}
    result = _run(
        cfg, tmp_path, record=record, pre=lambda: _actor_writes(base, patient="p2")
    )
    assert result["receipt"]["effect_verdict"] == "REFUTED"


def test_run_record_must_name_executor_kind(cfg):
    with pytest.raises(VerifyRunError, match="executor_kind"):
        verify_run(
            cfg, params=PARAMS, run_record={}, authorization_ref="ticket-0000-test"
        )


# -- no VERIFIED without an independent read ---------------------------------


def test_screen_verifier_is_refused(tmp_path, sor):
    base, _db = sor
    doc = _verify_yaml(base)
    doc["verifier"] = {"kind": "onscreen"}
    path = tmp_path / "verify.yaml"
    path.write_text(yaml.safe_dump(doc))
    with pytest.raises(VerifyRunError, match="independently"):
        load_verify_config(path)


class _FakeVerifier:
    """Confirms everything. Only its declared tier differs between tests."""

    substrate = "fake"

    def __init__(self, tier):
        if tier is not None:
            self.verification_tier = tier

    def test_connection(self):
        return ConnectionProbe(ok=True, substrate="fake")

    def capture_pre_state(self, context=None):
        return EffectState(substrate="fake", reachable=True)

    capture_post_state = capture_pre_state

    def verify(self, expected, before, context=None):
        return EffectVerdict(
            verdict=Verdict.CONFIRMED,
            kind=expected.kind,
            substrate="fake",
            observed_count=1,
            expected_count=1,
        )


@pytest.fixture
def clean_registry(monkeypatch):
    monkeypatch.setattr(adapter_mod, "_REGISTRY", {})
    monkeypatch.setattr(adapter_mod, "_ENTRY_POINTS_LOADED", True)
    yield


def _fake_cfg(kind: str, **extra) -> VerifyConfig:
    doc = _verify_yaml("http://unused")
    doc["verifier"] = {"kind": kind}
    doc["effects"][0].pop("count_new_only")
    doc.update(extra)
    return VerifyConfig.model_validate(doc)


def test_separate_session_read_never_becomes_verified(clean_registry):
    register_verifier_factory(
        "fake-session",
        lambda cfg, params: _FakeVerifier(VerificationTier.INDEPENDENT_SESSION),
    )
    cfg = _fake_cfg("fake-session", minimum_effect_strength="independent_session")
    result = verify_run(
        cfg,
        params=PARAMS,
        run_record=AGENT_RECORD,
        authorization_ref="ticket-0000-test",
    )
    receipt = result["receipt"]
    assert receipt["effect_verdict"] == "CONFIRMED"
    assert receipt["outcome"] == "reconciliation_required"
    assert receipt["oracle_tier"] == 1


def test_untyped_verifier_is_refused(clean_registry):
    register_verifier_factory("fake-untyped", lambda cfg, params: _FakeVerifier(None))
    with pytest.raises(VerifyRunError, match="verification_tier"):
        verify_run(
            _fake_cfg("fake-untyped"),
            params=PARAMS,
            run_record=AGENT_RECORD,
            authorization_ref="ticket-0000-test",
        )


# -- the receipt: backward compatible, offline-verifiable ---------------------


def test_receipt_keeps_every_execute_v1_field(cfg, sor, tmp_path):
    base, _db = sor
    receipt = _run(cfg, tmp_path, pre=lambda: _actor_writes(base))["receipt"]
    v1_fields = set(ExecuteEvidenceReceiptV1.model_fields) - {"seal_signature"}
    assert v1_fields <= set(receipt)
    assert set(receipt) - v1_fields == {
        "executor_kind",
        "executor_ref_sha256",
        "effect_verdict",
    }
    assert receipt["schema_version"] == EFFECT_RECEIPT_SCHEMA
    # A strict Execute consumer refuses it rather than mistaking it for a
    # Flow-governed Execute receipt.
    with pytest.raises(Exception):
        ExecuteEvidenceReceiptV1.model_validate(receipt)


def test_signed_receipt_verifies_offline_with_the_existing_seal_code(
    cfg, sor, tmp_path
):
    base, _db = sor
    result = _run(cfg, tmp_path, pre=lambda: _actor_writes(base))
    signed = _issuer_sign(result["receipt"])
    registry = _registry()
    # The existing Seal check in openadapt-types, unchanged.
    assert verify_sealed_receipt(signed, parse_key_registry(registry)).ok
    document = {**result, "receipt": signed}
    verdict = check_effect_receipt(document, registry)
    assert verdict["verified"] is True
    assert verdict["evidence_bound"] is True
    assert verdict["executor_kind"] == "agent"
    assert verdict["effect_verdict"] == "CONFIRMED"


def test_tampering_is_caught(cfg, sor, tmp_path):
    base, _db = sor
    result = _run(cfg, tmp_path, pre=lambda: _actor_writes(base))
    signed = _issuer_sign(result["receipt"])
    registry = _registry()
    relabeled = {**result, "receipt": {**signed, "executor_kind": "flow_replay"}}
    assert check_effect_receipt(relabeled, registry)["reason"] in {
        "receipt_mismatch",
        "evidence_mismatch",
    }
    evidence = json.loads(json.dumps(result["evidence"]))
    evidence["readback"]["effects"][0]["verdict"] = "REFUTED"
    assert (
        check_effect_receipt(
            {**result, "receipt": signed, "evidence": evidence}, registry
        )["reason"]
        == "evidence_mismatch"
    )
    assert check_effect_receipt(result, registry)["reason"] == "unsigned"
    assert check_effect_receipt(result, [])["verified"] is False


def test_receipt_carries_no_record_values(cfg, sor, tmp_path):
    base, _db = sor
    result = _run(cfg, tmp_path, pre=lambda: _actor_writes(base))
    blob = json.dumps(result)
    for value in (PARAMS["note"], "Triage", "patient_id"):
        assert value not in blob


# -- CLI --------------------------------------------------------------------


def test_cli_end_to_end(sor, tmp_path):
    base, _db = sor
    verify = tmp_path / "verify.yaml"
    verify.write_text(yaml.safe_dump(_verify_yaml(base)))
    params = tmp_path / "params.json"
    params.write_text(json.dumps(PARAMS))
    record = tmp_path / "run.json"
    record.write_text(json.dumps(AGENT_RECORD))
    baseline = tmp_path / "before.json"
    cli = [
        sys.executable,
        "-m",
        "openadapt_flow",
        "verify-run",
        "--verify",
        str(verify),
        "--params",
        str(params),
    ]

    done = subprocess.run(
        [*cli, "--capture-baseline", str(baseline)], capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr
    assert (baseline.stat().st_mode & 0o777) == 0o600
    _actor_writes(base)
    out = tmp_path / "out"
    done = subprocess.run(
        [
            *cli,
            "--run-record",
            str(record),
            "--authorization-ref",
            "ticket-0000-test",
            "--baseline",
            str(baseline),
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    assert "effect verdict: CONFIRMED" in done.stdout
    document = json.loads((out / "effect-receipt.json").read_text())
    signed = tmp_path / "signed.json"
    signed.write_text(
        json.dumps({**document, "receipt": _issuer_sign(document["receipt"])})
    )
    keys = tmp_path / "keys.json"
    keys.write_text(json.dumps({"keys": _registry()}))
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "openadapt_flow",
            "verify-run",
            "--check",
            str(signed),
            "--keys",
            str(keys),
        ],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert json.loads(done.stdout)["verified"] is True
