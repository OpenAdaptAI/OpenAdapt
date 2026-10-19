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

import openadapt_flow.runtime.effects.adapter as adapter_mod
import openadapt_flow.verify_run as verify_run_mod
from openadapt_types.execute import ExecuteEvidenceReceiptV1
from openadapt_types.seal import (
    parse_key_registry,
    seal_key_id,
    signing_payload,
    verify_sealed_receipt,
)
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
    baseline_digest,
    capture_baseline,
    check_effect_receipt,
    effect_signing_payload,
    load_verify_config,
    receipt_sha256,
    salted_digest,
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


def _run(cfg, tmp_path, *, baseline=True, record=AGENT_RECORD, pre=None, **kw):
    path = _baseline(cfg, tmp_path) if baseline else None
    if pre is not None:
        pre()
    return verify_run(
        cfg,
        params=PARAMS,
        run_record=record,
        authorization_ref="ticket-0000-test",
        baseline_path=path,
        private_dir=tmp_path / "private",
        **kw,
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
    """Stand in for the issuer: openadapt.effect-seal-signature/v1 (RFC-0002)."""

    key = Ed25519PrivateKey.from_private_bytes(TEST_SEED)
    unsigned = {
        "schema": "openadapt.effect-seal-signature/v1",
        "issuer": "openadapt-cloud:unattested-verifier",
        "algorithm": "ed25519",
        "key_id": _registry()[0]["key_id"],
        "receipt_schema": "openadapt.effect-receipt/v1",
        "receipt_sha256": receipt_sha256(receipt),
        "run_id": receipt["execution_id"],
        "issued_at": receipt["issued_at"],
    }
    signature = base64.b64encode(key.sign(effect_signing_payload(unsigned))).decode()
    return {**receipt, "seal_signature": {**unsigned, "signature": signature}}


def _old_domain_sign(receipt: dict) -> dict:
    """The pre-RFC Execute Seal signature, which an effect receipt must refuse."""

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
    assert receipt["verification_tier"] == "independent_readback"
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


def test_absent_after_a_known_dispatch_needs_reconciliation(cfg, sor, tmp_path):
    base, _db = sor
    # The caller knows the save was sent, so an empty store proves nothing:
    # the write may still land, and a retry could save it twice.
    result = _run(
        cfg,
        tmp_path,
        pre=lambda: _actor_writes(base, fault="no_persist"),
        write_attempted=True,
    )
    receipt = result["receipt"]
    assert receipt["effect_verdict"] == "REFUTED"
    assert receipt["outcome"] == "reconciliation_required"
    assert receipt["delivery_uncertain"] is True
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
        private_dir=tmp_path,
    )
    assert result["receipt"]["effect_verdict"] == "INDETERMINATE"
    assert result["receipt"]["outcome"] == "reconciliation_required"
    assert result["receipt"]["verification_tier"] == "unchecked"


def test_screen_readback_never_becomes_verified(clean_registry, tmp_path):
    register_verifier_factory(
        "fake-screen",
        lambda cfg, params: _FakeVerifier(
            VerificationTier.PERSISTED_STATE_REACQUISITION
        ),
    )
    result = verify_run(
        _fake_cfg("fake-screen"),
        params=PARAMS,
        run_record=AGENT_RECORD,
        authorization_ref="ticket-0000-test",
        private_dir=tmp_path,
    )
    receipt = result["receipt"]
    assert receipt["effect_verdict"] == "CONFIRMED"
    assert receipt["verification_tier"] == "screen_readback"
    assert receipt["outcome"] == "reconciliation_required"


def test_verified_without_independent_readback_fails_the_check(cfg, sor, tmp_path):
    base, _db = sor
    result = _run(cfg, tmp_path, pre=lambda: _actor_writes(base))
    forged = _issuer_sign({**result["receipt"], "verification_tier": "screen_readback"})
    verdict = check_effect_receipt({"receipt": forged}, _registry())
    assert verdict["verified"] is False
    assert verdict["reason"] == "malformed"


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
            cfg,
            params=PARAMS,
            run_record={},
            authorization_ref="ticket-0000-test",
            private_dir=Path("/nonexistent"),
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


def test_separate_session_read_is_screen_readback_and_never_verified(
    clean_registry, tmp_path
):
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
        private_dir=tmp_path,
    )
    receipt = result["receipt"]
    assert receipt["effect_verdict"] == "CONFIRMED"
    # RFC-0002 decision 6: a separate UI session (Flow tier 2) is a screen
    # read, so it can never be independent_readback.
    assert receipt["verification_tier"] == "screen_readback"
    assert receipt["outcome"] == "reconciliation_required"
    assert receipt["oracle_tier"] == 1
    assert result["evidence"]["readback"]["effects"][0]["flow_verification_tier"] == 2


def test_untyped_verifier_is_refused(clean_registry, tmp_path):
    register_verifier_factory("fake-untyped", lambda cfg, params: _FakeVerifier(None))
    with pytest.raises(VerifyRunError, match="verification_tier"):
        verify_run(
            _fake_cfg("fake-untyped"),
            params=PARAMS,
            run_record=AGENT_RECORD,
            authorization_ref="ticket-0000-test",
            private_dir=tmp_path,
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
        "verification_tier",
    }
    assert receipt["schema_version"] == EFFECT_RECEIPT_SCHEMA
    # A strict Execute consumer refuses it rather than mistaking it for a
    # Flow-governed Execute receipt.
    with pytest.raises(Exception):
        ExecuteEvidenceReceiptV1.model_validate(receipt)


def test_signed_receipt_verifies_offline_under_the_effect_seal_domain(
    cfg, sor, tmp_path
):
    base, _db = sor
    result = _run(cfg, tmp_path, pre=lambda: _actor_writes(base))
    signed = _issuer_sign(result["receipt"])
    registry = _registry()
    # A pre-RFC Execute Seal verifier refuses it instead of passing it.
    old = verify_sealed_receipt(signed, parse_key_registry(registry))
    assert not old.ok and old.reason == "malformed"
    document = {**result, "receipt": signed}
    verdict = check_effect_receipt(document, registry)
    assert verdict["verified"] is True
    assert verdict["verified_outcome"] is True
    assert verdict["ok"] is True
    assert verdict["issuer"] == "openadapt-cloud:unattested-verifier"
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


def test_execute_seal_signature_on_an_effect_receipt_is_refused(cfg, sor, tmp_path):
    base, _db = sor
    result = _run(cfg, tmp_path, pre=lambda: _actor_writes(base))
    old = _old_domain_sign(result["receipt"])
    verdict = check_effect_receipt({**result, "receipt": old}, _registry())
    assert verdict["verified"] is False
    assert verdict["reason"] == "malformed"
    # Same fields under the new schema but signed over the old domain.
    forged = _issuer_sign(result["receipt"])
    sig = dict(forged["seal_signature"])
    key = Ed25519PrivateKey.from_private_bytes(TEST_SEED)
    sig["signature"] = base64.b64encode(key.sign(signing_payload(sig))).decode()
    verdict = check_effect_receipt(
        {**result, "receipt": {**forged, "seal_signature": sig}}, _registry()
    )
    assert verdict["reason"] == "bad_signature"


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


# -- RFC-0002: run salt, baseline binding, tiers, linking ---------------------


def _hmac(salt: bytes, label: str, value) -> str:
    import hashlib
    import hmac as hmac_mod

    message = (
        label.encode()
        + b"\0"
        + json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    )
    return "hmac-sha256:" + hmac_mod.new(salt, message, hashlib.sha256).hexdigest()


def test_customer_digests_are_salted_with_the_run_salt(cfg, sor, tmp_path):
    import hashlib

    base, _db = sor
    path = _baseline(cfg, tmp_path)
    baseline = json.loads(path.read_text())
    salt = bytes.fromhex(baseline["salt"])
    _actor_writes(base)
    result = verify_run(
        cfg,
        params=PARAMS,
        run_record=AGENT_RECORD,
        authorization_ref="ticket-0000-test",
        baseline_path=path,
    )
    evidence = result["evidence"]
    assert evidence["salting"] == {
        "scheme": "openadapt.run-salt/v1",
        "salt_commitment": "sha256:" + hashlib.sha256(salt).hexdigest(),
    }
    assert evidence["executor"]["run_record_sha256"] == _hmac(
        salt, "run_record", AGENT_RECORD
    )
    assert evidence["authorization"]["reference_sha256"] == _hmac(
        salt, "authorization_reference", "ticket-0000-test"
    )
    # The baseline digest covers the five named fields, never the salt.
    five = {
        k: baseline[k]
        for k in (
            "schema_version",
            "execution_id",
            "baseline_read_at",
            "verifier_identity",
            "state",
        )
    }
    assert evidence["readback"]["baseline_sha256"] == _hmac(salt, "baseline", five)
    assert evidence["readback"]["baseline_sha256"] == baseline_digest(baseline, salt)
    # The receipt continues the run the baseline started.
    assert result["receipt"]["execution_id"] == baseline["execution_id"]
    # The salt itself never leaves the private file.
    assert baseline["salt"] not in json.dumps(result)
    # The effect hash binds the parameters only through salted digests.
    effect = cfg.effects[0]
    unsalted = effect.resolve(PARAMS).contract_hash()
    row = evidence["readback"]["effects"][0]
    assert row["effect_contract_hash"] != unsalted
    salted = {
        name: salted_digest(salt, f"effect_param:{name}", PARAMS[name]).split(":", 1)[1]
        for name in effect.referenced_params()
    }
    assert row["effect_contract_hash"] == effect.resolved_contract_hash(
        {}, opaque_param_sha256=salted
    )


def test_two_runs_differ_in_salt_and_execution_but_not_in_contract(cfg, sor, tmp_path):
    seen: list = []
    original = verify_run_mod.build_effect_verifier

    def spy(config, params):
        verifier = original(config, params)
        seen.append(verifier)
        return verifier

    base, _db = sor
    _actor_writes(base)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(verify_run_mod, "build_effect_verifier", spy)
        one = verify_run(
            cfg,
            params=PARAMS,
            run_record=AGENT_RECORD,
            authorization_ref="ticket-0000-test",
            private_dir=tmp_path / "a",
        )
        two = verify_run(
            cfg,
            params=PARAMS,
            run_record=AGENT_RECORD,
            authorization_ref="ticket-0000-test",
            private_dir=tmp_path / "b",
        )
    r1, r2 = one["receipt"], two["receipt"]
    assert r1["execution_id"] != r2["execution_id"]
    assert r1["workflow_digest"] == r2["workflow_digest"]
    assert (
        one["evidence"]["readback"]["verifier_identity"]
        == two["evidence"]["readback"]["verifier_identity"]
    )
    assert (
        one["evidence"]["salting"]["salt_commitment"]
        != two["evidence"]["salting"]["salt_commitment"]
    )
    assert (
        one["evidence"]["executor"]["run_record_sha256"]
        != two["evidence"]["executor"]["run_record_sha256"]
    )
    # Every REST read carries the run's own execution id and trace header.
    for verifier, receipt in zip(seen, (r1, r2)):
        assert verifier.headers["X-OpenAdapt-Execution-Id"] == receipt["execution_id"]
        assert verifier.headers["traceparent"].startswith("00-")
    # The run salt is kept privately, mode 0600.
    private = tmp_path / "a" / "effect-run.private.json"
    assert (private.stat().st_mode & 0o777) == 0o600
    assert json.loads(private.read_text())["execution_id"] == r1["execution_id"]


def test_a_baseline_with_an_extra_key_is_refused(cfg, sor, tmp_path):
    path = _baseline(cfg, tmp_path)
    doc = json.loads(path.read_text())
    doc["note"] = "unbound"
    path.write_text(json.dumps(doc))
    with pytest.raises(VerifyRunError, match="exactly"):
        verify_run(
            cfg,
            params=PARAMS,
            run_record=AGENT_RECORD,
            authorization_ref="ticket-0000-test",
            baseline_path=path,
        )


def test_a_baseline_is_used_once(cfg, sor, tmp_path):
    base, _db = sor
    path = _baseline(cfg, tmp_path)
    _actor_writes(base)
    kw = dict(
        params=PARAMS,
        run_record=AGENT_RECORD,
        authorization_ref="ticket-0000-test",
        baseline_path=path,
    )
    assert verify_run(cfg, **kw)["receipt"]["outcome"] == "verified"
    with pytest.raises(VerifyRunError, match="already used"):
        verify_run(cfg, **kw)


def test_a_baseline_outside_the_window_counts_as_absent(cfg, sor, tmp_path):
    base, _db = sor
    path = _baseline(cfg, tmp_path)
    doc = json.loads(path.read_text())
    doc["baseline_read_at"] = "2020-01-01T00:00:00Z"
    path.write_text(json.dumps(doc))
    _actor_writes(base)
    result = verify_run(
        cfg,
        params=PARAMS,
        run_record=AGENT_RECORD,
        authorization_ref="ticket-0000-test",
        baseline_path=path,
    )
    assert result["receipt"]["effect_verdict"] == "INDETERMINATE"
    assert result["receipt"]["outcome"] == "reconciliation_required"
    readback = result["evidence"]["readback"]
    assert readback["baseline_sha256"] is None
    assert readback["baseline_read_at"] is None


def test_offline_check_refuses_a_broken_baseline_window(cfg, sor, tmp_path):
    base, _db = sor
    result = _run(cfg, tmp_path, pre=lambda: _actor_writes(base))
    evidence = json.loads(json.dumps(result["evidence"]))
    evidence["readback"]["baseline_read_at"] = "2020-01-01T00:00:00Z"
    receipt = dict(result["receipt"])
    receipt["evidence_digest"] = verify_run_mod.sha256_tagged(evidence)
    signed = _issuer_sign(receipt)
    verdict = check_effect_receipt(
        {"receipt": signed, "evidence": evidence}, _registry()
    )
    assert verdict["reason"] == "baseline_window"
    assert verdict["verified"] is False


def test_offline_check_refuses_unsalted_evidence(cfg, sor, tmp_path):
    base, _db = sor
    result = _run(cfg, tmp_path, pre=lambda: _actor_writes(base))
    evidence = json.loads(json.dumps(result["evidence"]))
    evidence["executor"]["run_record_sha256"] = "sha256:" + "0" * 64
    receipt = dict(result["receipt"])
    receipt["evidence_digest"] = verify_run_mod.sha256_tagged(evidence)
    verdict = check_effect_receipt(
        {"receipt": _issuer_sign(receipt), "evidence": evidence}, _registry()
    )
    assert verdict["reason"] == "unsalted"


def test_remediates_and_correlation_id_are_signed_into_the_receipt(cfg, sor, tmp_path):
    base, _db = sor
    earlier = "receipt-" + "a" * 32
    result = _run(
        cfg,
        tmp_path,
        pre=lambda: _actor_writes(base),
        remediates=earlier,
        correlation_id="ticket-INC-4242",
    )
    receipt = result["receipt"]
    assert receipt["remediates"] == earlier
    assert receipt["correlation_id"] == "ticket-INC-4242"
    signed = _issuer_sign(receipt)
    assert check_effect_receipt({**result, "receipt": signed}, _registry())["ok"]
    # Both are inside receipt_sha256, so editing either breaks the seal.
    for field, value in (
        ("remediates", "receipt-" + "b" * 32),
        ("correlation_id", "ticket-INC-9999"),
    ):
        edited = {**signed, field: value}
        verdict = check_effect_receipt({**result, "receipt": edited}, _registry())
        assert verdict["reason"] == "receipt_mismatch"
    # Absent unless given, so a plain receipt keeps the RFC field set.
    plain = _run(cfg, tmp_path / "plain", pre=lambda: _actor_writes(base))
    assert "remediates" not in plain["receipt"]
    assert "correlation_id" not in plain["receipt"]


def test_link_ids_must_be_opaque(cfg, tmp_path):
    with pytest.raises(VerifyRunError, match="correlation_id"):
        verify_run(
            cfg,
            params=PARAMS,
            run_record=AGENT_RECORD,
            authorization_ref="ticket-0000-test",
            private_dir=tmp_path,
            correlation_id="jane.doe@example.com x",
        )


def test_a_self_approved_write_is_rejected_policy(cfg, sor, tmp_path):
    base, _db = sor
    approver = {
        "kind": "agent",
        "id": AGENT_RECORD["executor_id"],
        "policy_ref": "sha256:" + "1" * 64,
    }
    result = _run(cfg, tmp_path, pre=lambda: _actor_writes(base), approver=approver)
    receipt = result["receipt"]
    assert receipt["effect_verdict"] == "CONFIRMED"
    assert receipt["outcome"] == "rejected_policy"
    assert receipt["contracts"]["authorization_passed"] is False
    assert receipt["contracts"]["effect_passed"] is True
    assert result["evidence"]["authorization"]["self_approved"] is True
    other = {**approver, "kind": "person", "id": "staff-00000042"}
    ok = _run(cfg, tmp_path / "other", pre=lambda: _actor_writes(base), approver=other)
    assert ok["evidence"]["authorization"]["self_approved"] is False


def test_a_genuine_signature_over_a_refutation_is_not_a_pass(cfg, sor, tmp_path):
    base, _db = sor
    result = _run(cfg, tmp_path, pre=lambda: _actor_writes(base, fault="no_persist"))
    assert result["receipt"]["outcome"] == "halted_before_effect"
    document = {**result, "receipt": _issuer_sign(result["receipt"])}
    verdict = check_effect_receipt(document, _registry())
    assert verdict["verified"] is True
    assert verdict["verified_outcome"] is False
    assert verdict["ok"] is False
    accepted = check_effect_receipt(
        document, _registry(), accept_outcomes=["halted_before_effect"]
    )
    assert accepted["ok"] is True


# -- one verify.yaml for qualify and verify-run --------------------------------


def test_the_qualify_example_loads_and_hashes_like_qualify(monkeypatch):
    from openadapt_flow.qualify.campaign import record_effects
    from openadapt_flow.qualify.config import (
        load_verify_config as load_qualify_config,
    )
    from openadapt_flow.qualify.result import verify_config_sha256

    path = Path(__file__).resolve().parents[1] / "benchmark/openemr_local/verify.yaml"
    for name in ("OPENEMR_UI_URL", "OPENEMR_API_URL"):
        monkeypatch.delenv(name, raising=False)
    ours = load_verify_config(path)
    theirs = load_qualify_config(path)
    # Qualify-only sections and verifier.session are ignored, not refused.
    assert ours.verifier == theirs.verifier.verifier
    assert ours.environment_id == theirs.environment_id
    assert [e.model_dump(mode="json") for e in ours.effects] == [
        e.model_dump(mode="json") for e in record_effects(theirs)
    ]
    identity = "rest:example"
    assert verify_config_sha256(identity, list(ours.effects)) == verify_config_sha256(
        identity, record_effects(theirs)
    )


def test_record_and_effects_together_are_refused():
    from openadapt_flow.verify_run import parse_verify_config

    data = _verify_yaml("http://127.0.0.1:1")
    data["record"] = {
        "name": "note",
        "match": ["patient_id"],
        "fields": {"patient_id": "patient_id"},
    }
    with pytest.raises(VerifyRunError, match="either effects or record"):
        parse_verify_config(data)


def test_unset_environment_reference_is_refused(monkeypatch):
    from openadapt_flow.verify_run import parse_verify_config

    monkeypatch.delenv("OA_TEST_UNSET_SOR_URL", raising=False)
    data = _verify_yaml("${OA_TEST_UNSET_SOR_URL}")
    with pytest.raises(VerifyRunError, match="OA_TEST_UNSET_SOR_URL"):
        parse_verify_config(data)
