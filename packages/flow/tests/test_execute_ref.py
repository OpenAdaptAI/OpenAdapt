"""MIT reference Execute server: contract, halt, idempotency, receipt, boundary."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

pytest.importorskip("fastapi")
pytest.importorskip("openadapt_types")

from fastapi.testclient import TestClient  # noqa: E402
from openadapt_types.execute import (  # noqa: E402
    ExecuteAcceptedV1,
    ExecuteEvidenceReceiptV1,
    ExecuteRequestV1,
    ExecuteStatusV1,
    ExecuteTerminalOutcomeV1,
)

from openadapt_flow.execute.app import create_app  # noqa: E402
from openadapt_flow.execute.dispatch import (  # noqa: E402
    DispatchResult,
    synthetic_mockmed,
)
from openadapt_flow.execute.keys import (  # noqa: E402
    fingerprint_of,
    load_or_create_private_key,
    verify_signature,
)
from openadapt_flow.execute.models import (  # noqa: E402
    FORBIDDEN_RECEIPT_KEYS,
    SelfSignedSealV1,
    assert_no_forbidden_keys,
)
from openadapt_flow.execute.registry import (  # noqa: E402
    MOCKMED_EFFECT_STRENGTH,
    MOCKMED_ENVIRONMENT_LIE,
    MOCKMED_ENVIRONMENT_OK,
    MOCKMED_QUALIFICATION_ID,
    MOCKMED_WORKFLOW_DIGEST,
    MOCKMED_WORKFLOW_VERSION,
)
from openadapt_flow.execute.service import ExecuteService  # noqa: E402
from openadapt_flow.receipt import RunReceipt  # noqa: E402

AUTH_CTX = {
    "actor_id": "caller_agent_12345678",
    "authorization_reference": "authorization_12345678",
}


def _request(**updates: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "schema_version": "openadapt.execute-request/v1",
        "qualification_id": MOCKMED_QUALIFICATION_ID,
        "workflow_version": MOCKMED_WORKFLOW_VERSION,
        "workflow_digest": MOCKMED_WORKFLOW_DIGEST,
        "environment_id": MOCKMED_ENVIRONMENT_OK,
        "parameters": {"date": "2026-08-15", "record": {"id": "12345"}},
        "idempotency_key": "caller_key_12345678",
        "authorization_context": AUTH_CTX,
        "effect_strength_schema_version": "1",
        "minimum_effect_strength": MOCKMED_EFFECT_STRENGTH,
    }
    fields.update(updates)
    return fields


def _client(tmp_path: Path, **kwargs: Any) -> tuple[TestClient, ExecuteService]:
    kwargs.setdefault("seed_mockmed", True)
    kwargs.setdefault("process_inline", True)
    app = create_app(tmp_path, token="test-token", **kwargs)
    store: ExecuteService = app.state.execute
    client = TestClient(app)
    client.headers["Authorization"] = "Bearer test-token"
    return client, store


def test_cli_wires_serve_execute() -> None:
    from openadapt_flow.__main__ import build_parser

    parser = build_parser()
    args = parser.parse_args(
        ["serve-execute", "--port", "8787", "--seed-mockmed", "--data-dir", "/tmp/x"]
    )
    assert args.command == "serve-execute"
    assert args.port == 8787
    assert args.seed_mockmed is True
    assert args.func.__name__ == "_cmd_serve_execute"


def test_health_needs_no_token(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    bare = TestClient(client.app)
    response = bare.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["issuer"] == "self_signed"
    assert body["production_seal"] is False
    assert body["issuer_key_fingerprint"] == store.fingerprint


def test_post_mockmed_verified_receipt(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    accepted = ExecuteAcceptedV1.model_validate(
        client.post("/v1/executions", json=_request()).json()
    )
    assert accepted.state.value == "queued"
    status = ExecuteStatusV1.model_validate(
        client.get(f"/v1/executions/{accepted.execution_id}").json()
    )
    assert status.state.value == "terminal"
    assert status.terminal_outcome is ExecuteTerminalOutcomeV1.VERIFIED
    receipt_response = client.get(f"/v1/executions/{accepted.execution_id}/receipt")
    receipt = ExecuteEvidenceReceiptV1.model_validate(receipt_response.json())
    assert receipt.execution_id == accepted.execution_id
    assert receipt.receipt_id == status.evidence_receipt_id
    assert receipt.workflow_digest == MOCKMED_WORKFLOW_DIGEST
    assert receipt.outcome is ExecuteTerminalOutcomeV1.VERIFIED
    assert receipt_response.headers["X-OpenAdapt-Issuer"] == "self_signed"
    assert receipt_response.headers["X-OpenAdapt-Production-Seal"] == "false"
    assert (
        receipt_response.headers["X-OpenAdapt-Issuer-Fingerprint"] == store.fingerprint
    )
    seal = SelfSignedSealV1.model_validate(
        client.get(f"/seals/{receipt.receipt_id}?format=json").json()
    )
    assert seal.issuer == "self_signed"
    assert seal.production_seal is False
    assert seal.meter_usd == 0.0
    assert seal.verify_host == "local"
    key = load_or_create_private_key(tmp_path)
    assert fingerprint_of(key.public_key()) == seal.issuer_key_fingerprint
    canonical = json.dumps(
        seal.receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    assert verify_signature(key.public_key(), canonical, seal.signature)


def test_break_it_banner_lie_is_not_a_production_seal(tmp_path: Path) -> None:
    client, _store = _client(tmp_path)
    accepted = ExecuteAcceptedV1.model_validate(
        client.post(
            "/v1/executions",
            json=_request(
                environment_id=MOCKMED_ENVIRONMENT_LIE,
                idempotency_key="caller_key_break_it1",
            ),
        ).json()
    )
    status = ExecuteStatusV1.model_validate(
        client.get(f"/v1/executions/{accepted.execution_id}").json()
    )
    assert status.state.value == "terminal"
    assert status.terminal_outcome is ExecuteTerminalOutcomeV1.RECONCILIATION_REQUIRED
    receipt = ExecuteEvidenceReceiptV1.model_validate(
        client.get(f"/v1/executions/{accepted.execution_id}/receipt").json()
    )
    assert receipt.outcome is ExecuteTerminalOutcomeV1.RECONCILIATION_REQUIRED
    assert receipt.outcome is not ExecuteTerminalOutcomeV1.VERIFIED
    assert receipt.contracts.effect_passed is False
    assert receipt.contracts.postcondition_passed is True
    assert receipt.delivery_uncertain is True
    seal = SelfSignedSealV1.model_validate(
        client.get(f"/seals/{receipt.receipt_id}?format=json").json()
    )
    assert seal.meter_usd == 0.0
    assert seal.production_seal is False
    assert seal.issuer == "self_signed"
    html_page = client.get(
        f"/seals/{receipt.receipt_id}", headers={"Accept": "text/html"}
    )
    assert html_page.status_code == 200
    text = html_page.text
    assert "Self-signed" in text
    assert "not an OpenAdapt production Seal" in text
    assert "0" in text


def test_idempotency_returns_the_same_execution(tmp_path: Path) -> None:
    client, _store = _client(tmp_path)
    body = _request(idempotency_key="caller_key_same_body1")
    first = client.post("/v1/executions", json=body)
    second = client.post("/v1/executions", json=body)
    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["execution_id"] == second.json()["execution_id"]
    changed = dict(body)
    changed["parameters"] = {"date": "2026-08-16"}
    conflict = client.post("/v1/executions", json=changed)
    assert conflict.status_code == 409
    assert conflict.json()["error"] == "idempotency_conflict"


def test_receipt_409_until_terminal(tmp_path: Path) -> None:
    gate = threading.Event()

    def runner(admission, request, run_dir):
        gate.wait(timeout=5)
        return synthetic_mockmed(admission, request)

    client, _store = _client(tmp_path, runner=runner, process_inline=False)
    accepted = client.post(
        "/v1/executions", json=_request(idempotency_key="caller_key_slowrun01")
    ).json()
    execution_id = accepted["execution_id"]
    early = client.get(f"/v1/executions/{execution_id}/receipt")
    assert early.status_code == 409
    gate.set()
    deadline = time.time() + 5
    status = None
    while time.time() < deadline:
        status = client.get(f"/v1/executions/{execution_id}").json()
        if status["state"] == "terminal":
            break
        time.sleep(0.05)
    assert status is not None and status["state"] == "terminal"
    receipt = client.get(f"/v1/executions/{execution_id}/receipt")
    assert receipt.status_code == 200
    ExecuteEvidenceReceiptV1.model_validate(receipt.json())


def test_receipt_refuses_extra_keys_and_screenshots() -> None:
    payload = {
        "schema_version": "openadapt.execute-evidence-receipt/v1",
        "receipt_id": "receipt_12345678",
        "execution_id": "execution_12345678",
        "workflow_digest": MOCKMED_WORKFLOW_DIGEST,
        "workflow_version": MOCKMED_WORKFLOW_VERSION,
        "qualification_id": MOCKMED_QUALIFICATION_ID,
        "environment_id": MOCKMED_ENVIRONMENT_OK,
        "runner_id": "runner_12345678",
        "nonce": "nonce_12345678",
        "oracle_tier": 2,
        "outcome": "verified",
        "contracts": {
            "authorization_passed": True,
            "identity_passed": True,
            "postcondition_passed": True,
            "effect_passed": True,
            "minimum_effect_strength": MOCKMED_EFFECT_STRENGTH,
            "observed_effect_strength": MOCKMED_EFFECT_STRENGTH,
            "model_used": False,
            "external_network_used": False,
        },
        "delivery_uncertain": False,
        "evidence_digest": "sha256:" + "b" * 64,
        "issued_at": "2026-07-29T12:00:00Z",
    }
    ExecuteEvidenceReceiptV1.model_validate(payload)
    with pytest.raises(ValidationError):
        ExecuteEvidenceReceiptV1.model_validate({**payload, "screenshot": "x.png"})
    with pytest.raises(ValidationError):
        ExecuteEvidenceReceiptV1.model_validate({**payload, "ocr_text": "secret"})
    with pytest.raises(ValueError, match="forbids"):
        assert_no_forbidden_keys({**payload, "screenshot": "x.png"})
    for key in FORBIDDEN_RECEIPT_KEYS:
        assert key not in ExecuteEvidenceReceiptV1.model_fields
    assert RunReceipt.model_config.get("extra") == "forbid"
    assert ExecuteEvidenceReceiptV1.model_config.get("extra") == "forbid"


def test_stored_receipt_has_no_screenshot_fields(tmp_path: Path) -> None:
    client, _store = _client(tmp_path)
    execution_id = client.post("/v1/executions", json=_request()).json()["execution_id"]
    receipt = client.get(f"/v1/executions/{execution_id}/receipt").json()
    assert FORBIDDEN_RECEIPT_KEYS.isdisjoint(receipt)
    assert FORBIDDEN_RECEIPT_KEYS.isdisjoint(receipt["contracts"])
    text = json.dumps(receipt)
    assert "screenshot" not in text
    assert ".png" not in text
    assert "Encountersaved" not in text
    assert "http://" not in text
    seal = client.get(f"/seals/{receipt['receipt_id']}?format=json").json()
    assert "screenshot" not in json.dumps(seal)


def test_mismatching_qualification_is_refused(tmp_path: Path) -> None:
    client, _store = _client(tmp_path)
    response = client.post(
        "/v1/executions",
        json=_request(qualification_id="qualification_unknown1"),
    )
    assert response.status_code == 422
    assert response.json()["error"] == "qualification_mismatch"


def test_unauthorized_v1_is_401(tmp_path: Path) -> None:
    client, _store = _client(tmp_path)
    bare = TestClient(client.app)
    response = bare.post("/v1/executions", json=_request())
    assert response.status_code == 401


def test_mcp_create_and_read(tmp_path: Path) -> None:
    client, _store = _client(tmp_path)
    listed = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    ).json()
    names = {tool["name"] for tool in listed["result"]["tools"]}
    assert names == {
        "create_execution",
        "get_execution",
        "get_execution_receipt",
    }
    created = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "create_execution",
                "arguments": _request(idempotency_key="caller_key_mcp_tool1"),
            },
        },
    ).json()
    accepted = json.loads(created["result"]["content"][0]["text"])
    execution_id = accepted["execution_id"]
    receipt_rpc = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "get_execution_receipt",
                "arguments": {"execution_id": execution_id},
            },
        },
    ).json()
    receipt = json.loads(receipt_rpc["result"]["content"][0]["text"])
    assert receipt["outcome"] == "verified"


def test_source_boundary_has_no_cloud_tenant_or_billing_modules() -> None:
    root = Path(__file__).resolve().parents[1] / "openadapt_flow" / "execute"
    names = {path.name for path in root.glob("*.py")}
    forbidden_files = {
        "tenant.py",
        "billing.py",
        "stripe.py",
        "control_plane.py",
        "metering.py",
        "orgs.py",
    }
    assert names.isdisjoint(forbidden_files)
    joined = "\n".join(path.read_text(encoding="utf-8") for path in root.glob("*.py"))
    assert "openadapt_cloud" not in joined
    assert "openadapt-cloud" not in joined
    assert "stripe" not in joined.lower()
    assert "class Tenant" not in joined
    assert "class Billing" not in joined
    assert "app.openadapt.ai/seals" not in joined
    assert "EXECUTE_LANE" not in joined


def test_synthetic_break_it_dispatch_is_zero_dollars() -> None:
    request = ExecuteRequestV1.model_validate(
        _request(environment_id=MOCKMED_ENVIRONMENT_LIE)
    )
    from openadapt_flow.execute.models import AdmittedBundle

    admission = AdmittedBundle(
        qualification_id=MOCKMED_QUALIFICATION_ID,
        workflow_version=MOCKMED_WORKFLOW_VERSION,
        workflow_digest=MOCKMED_WORKFLOW_DIGEST,
        environment_id=MOCKMED_ENVIRONMENT_LIE,
        minimum_effect_strength=MOCKMED_EFFECT_STRENGTH,
        synthetic=True,
        break_it=True,
    )
    result = synthetic_mockmed(admission, request)
    assert result.outcome is ExecuteTerminalOutcomeV1.RECONCILIATION_REQUIRED
    assert isinstance(result, DispatchResult)
    assert result.effect_passed is False
