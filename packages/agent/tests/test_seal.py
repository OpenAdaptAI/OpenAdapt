"""sealed:true only for a verified run whose Cloud Seal verifies against pinned keys."""

from __future__ import annotations

import base64
import json

from conftest import FlowCliStub
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import openadapt_agent.runner as runner_mod
from openadapt_agent.runner import FlowRunner
from openadapt_agent.seal import _canonical, _key_id, parse_registry, receipt_sha256, run_is_sealed

RUN = "run-0001-test"


def _key():
    private = Ed25519PrivateKey.generate()
    raw = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return private, {"key_id": _key_id(raw), "public_key": base64.b64encode(raw).decode(), "status": "active"}


def _sealed_receipt(private, entry, *, execution_id=RUN, outcome="verified"):
    receipt = {"schema_version": "openadapt.execute-evidence-receipt/v1", "execution_id": execution_id, "outcome": outcome}
    sig = {
        "schema": "openadapt.seal-signature/v1", "issuer": "openadapt-cloud", "algorithm": "ed25519",
        "key_id": entry["key_id"], "receipt_sha256": receipt_sha256(receipt), "run_id": execution_id,
        "issued_at": "2026-10-14T12:00:00Z",
    }
    payload = b"openadapt.seal-signature/v1\0" + _canonical(sig).encode()
    sig["signature"] = base64.b64encode(private.sign(payload)).decode()
    return {**receipt, "seal_signature": sig}


def test_only_a_verifying_seal_counts() -> None:
    private, entry = _key()
    registry = parse_registry({"keys": [entry]})
    good = {"execute_receipt": _sealed_receipt(private, entry)}
    assert run_is_sealed(good, run_id=RUN, execution_outcome="VERIFIED", registry=registry)
    # Every other case is unsealed.
    assert not run_is_sealed(good, run_id=RUN, execution_outcome="VERIFIED", registry=[])
    assert not run_is_sealed(good, run_id=RUN, execution_outcome="HALTED", registry=registry)
    assert not run_is_sealed(good, run_id="other-run", execution_outcome="VERIFIED", registry=registry)
    tampered = {"execute_receipt": {**good["execute_receipt"], "outcome": "rolled_back_verified"}}
    assert not run_is_sealed(tampered, run_id=RUN, execution_outcome="VERIFIED", registry=registry)
    other_private, _ = _key()
    forged = {"execute_receipt": _sealed_receipt(other_private, entry)}
    assert not run_is_sealed(forged, run_id=RUN, execution_outcome="VERIFIED", registry=registry)
    unsigned = {"execute_receipt": {k: v for k, v in good["execute_receipt"].items() if k != "seal_signature"}}
    assert not run_is_sealed(unsigned, run_id=RUN, execution_outcome="VERIFIED", registry=registry)
    revoked = parse_registry([dict(entry, status="revoked", revoked_at="2026-01-01T00:00:00Z")])
    assert not run_is_sealed(good, run_id=RUN, execution_outcome="VERIFIED", registry=revoked)


def test_runner_reports_sealed_only_with_pinned_keys(monkeypatch, tmp_path, runner_config, bundle_dir, success_report) -> None:
    private, entry = _key()
    success_report.update({"execution_profile": "standard", "execution_outcome": "VERIFIED", "production_eligible": True})
    monkeypatch.setattr(runner_mod.subprocess, "run", FlowCliStub(exit_code=0, report=success_report))
    outcome = FlowRunner(runner_config).run(workflow="demo_triage", bundle_dir=bundle_dir, params={"note": "hello"})
    assert outcome.status == "success"
    outcome.report = dict(outcome.report or {}, execute_receipt=_sealed_receipt(private, entry, execution_id=outcome.run_id))

    monkeypatch.delenv("OPENADAPT_SEAL_KEYS_PATH", raising=False)
    monkeypatch.delenv("OPENADAPT_SEAL_KEYS_JSON", raising=False)
    payload = outcome.to_dict()
    assert payload["sealed"] is False and payload["proof"] == "local" and payload["outcome"] == "done"

    keys = tmp_path / "seal-keys.json"
    keys.write_text(json.dumps({"keys": [entry]}))
    monkeypatch.setenv("OPENADAPT_SEAL_KEYS_PATH", str(keys))
    payload = outcome.to_dict()
    assert payload["sealed"] is True and payload["proof"] == "sealed" and payload["outcome"] == "done"
