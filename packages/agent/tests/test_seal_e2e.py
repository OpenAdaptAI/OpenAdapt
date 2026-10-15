"""End to end with a throwaway test key, real Flow attach code, and a local fake Cloud.

run -> VERIFIED -> Cloud signs the receipt -> Flow writes it into report.json
-> the agent reports sealed:true. A tampered receipt -> sealed:false.

The fake Cloud signs exactly as Cloud's src/lib/sealSignature.ts does
(openadapt.seal-signature/v1 over the canonical receipt without
seal_signature). Skips until the installed openadapt-flow ships cloud_seal.
"""

from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from conftest import FlowCliStub
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import openadapt_agent.runner as runner_mod
from openadapt_agent.runner import FlowRunner
from openadapt_agent.seal import _canonical, _key_id, receipt_sha256

cloud_seal = pytest.importorskip("openadapt_flow.cloud_seal")

EXEC = "0d9c1d7e-5b8a-4f7e-9a51-3f1c2b7d9e10"
API_KEY = "oa_exec_test_only"


def _cloud_receipt(private, key_id):
    receipt = {
        "schema_version": "openadapt.execute-evidence-receipt/v1",
        "receipt_id": f"receipt:{EXEC}",
        "execution_id": EXEC,
        "outcome": "verified",
        "evidence_digest": "a" * 64,
        "issued_at": "2026-10-14T12:00:00Z",
    }
    sig = {
        "schema": "openadapt.seal-signature/v1", "issuer": "openadapt-cloud", "algorithm": "ed25519",
        "key_id": key_id, "receipt_sha256": receipt_sha256(receipt), "run_id": EXEC,
        "issued_at": "2026-10-14T12:00:01Z",
    }
    sig["signature"] = base64.b64encode(private.sign(b"openadapt.seal-signature/v1\0" + _canonical(sig).encode())).decode()
    return {**receipt, "seal_signature": sig}


@pytest.fixture()
def fake_cloud():
    private = Ed25519PrivateKey.generate()
    raw = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    entry = {"key_id": _key_id(raw), "public_key": base64.b64encode(raw).decode(), "status": "active"}
    state = {"receipt": _cloud_receipt(private, entry["key_id"]), "pending": 1}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/api/public/seal-keys":
                return self._send(200, {"keys": [entry]})
            if self.headers.get("Authorization") != f"Bearer {API_KEY}":
                return self._send(401, {"error": "unauthorized"})
            if self.path == f"/api/v1/executions/{EXEC}/receipt":
                if state["pending"]:
                    state["pending"] -= 1
                    return self._send(409, {"error": "receipt_unavailable", "code": "trusted_evidence_pending"})
                return self._send(200, state["receipt"])
            return self._send(404, {"error": "execution_not_found"})

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", entry, state
    server.shutdown()


class ConnectedFlow(FlowCliStub):
    """`openadapt-flow run` that ends VERIFIED and then runs Flow's real attach step."""

    def __init__(self, report, cloud_url):
        super().__init__(exit_code=0, report=report)
        self.cloud_url = cloud_url

    def __call__(self, cmd, **kwargs):
        from pathlib import Path

        result = super().__call__(cmd, **kwargs)
        if "--cloud-execution-id" in cmd:
            run_dir = Path(cmd[cmd.index("--run-dir") + 1])
            execution_id = cmd[cmd.index("--cloud-execution-id") + 1]
            attached = cloud_seal.attach_from_cloud(
                run_dir, execution_id=execution_id, api_key=API_KEY, cloud_url=self.cloud_url, sleep=lambda s: None
            )
            assert attached.attached, attached.code
        return result


def _run(monkeypatch, runner_config, bundle_dir, report, cloud_url):
    flow = ConnectedFlow(report, cloud_url)
    monkeypatch.setattr(runner_mod, "_run_flow_process", flow)
    outcome = FlowRunner(runner_config).run(
        workflow="demo_triage", bundle_dir=bundle_dir, params={"note": "hello"}, cloud_execution_id=EXEC
    )
    assert "--cloud-execution-id" in flow.calls[0]
    return outcome


def test_connected_verified_run_is_sealed_and_tamper_is_not(
    monkeypatch, tmp_path, runner_config, bundle_dir, success_report, fake_cloud
):
    import argparse
    import io

    from openadapt_agent.seal_cli import cmd_keys_pin, cmd_verify

    cloud_url, entry, _ = fake_cloud
    keys = tmp_path / "seal-keys.json"
    # A person pins the test key on first use (scripted confirmation of the id).
    pin = argparse.Namespace(url=f"{cloud_url}/api/public/seal-keys", path=str(keys), confirm=[entry["key_id"]])
    assert cmd_keys_pin(pin, is_tty=lambda: False, out=io.StringIO()) == 0
    monkeypatch.setenv("OPENADAPT_SEAL_KEYS_PATH", str(keys))

    success_report.update({"execution_profile": "standard", "execution_outcome": "VERIFIED", "production_eligible": True})
    outcome = _run(monkeypatch, runner_config, bundle_dir, success_report, cloud_url)
    assert outcome.status == "success"
    assert outcome.report["execute_receipt"]["execution_id"] == EXEC
    assert outcome.report["cloud_execution_id"] == EXEC
    payload = outcome.to_dict()
    assert payload["sealed"] is True and payload["proof"] == "sealed" and payload["outcome"] == "done"

    # Anyone can check the written report offline.
    out = io.StringIO()
    target = argparse.Namespace(target=outcome.report_path, keys=str(keys), json=False)
    assert cmd_verify(target, out=out) == 0, out.getvalue()

    # Tamper with the receipt in the report: no longer sealed, and verify refuses.
    tampered = dict(outcome.report)
    tampered["execute_receipt"] = dict(tampered["execute_receipt"], evidence_digest="b" * 64)
    outcome.report = tampered
    payload = outcome.to_dict()
    assert payload["sealed"] is False and payload["proof"] == "local"

    # Without pinned keys a genuine Seal still reads sealed:false.
    outcome.report = json.loads(open(outcome.report_path).read())
    monkeypatch.delenv("OPENADAPT_SEAL_KEYS_PATH")
    assert outcome.to_dict()["sealed"] is False


def test_unverified_run_gets_no_receipt(monkeypatch, runner_config, bundle_dir, halt_report, fake_cloud):
    cloud_url, _, state = fake_cloud
    flow = FlowCliStub(exit_code=1, report=halt_report)
    monkeypatch.setattr(runner_mod, "_run_flow_process", flow)
    outcome = FlowRunner(runner_config).run(
        workflow="demo_triage", bundle_dir=bundle_dir, params={}, cloud_execution_id=EXEC
    )
    from pathlib import Path

    result = cloud_seal.attach_from_cloud(
        Path(outcome.run_dir), execution_id=EXEC, api_key=API_KEY, cloud_url=cloud_url
    )
    assert result.code == "local_not_verified" and state["pending"] == 1
    assert outcome.to_dict()["sealed"] is False
