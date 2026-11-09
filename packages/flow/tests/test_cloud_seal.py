"""Cloud-signed Execute receipts are attached only to VERIFIED, connected runs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openadapt_flow.cloud_seal import attach_from_cloud

EXEC = "11111111-2222-4333-8444-555555555555"


def _receipt(**over):
    receipt = {
        "schema_version": "openadapt.execute-evidence-receipt/v1",
        "execution_id": EXEC,
        "outcome": "verified",
        "seal_signature": {"schema": "openadapt.seal-signature/v1", "run_id": EXEC},
    }
    receipt.update(over)
    return receipt


def _run(tmp_path: Path, outcome: str = "VERIFIED") -> Path:
    run = tmp_path / "run"
    run.mkdir()
    (run / "report.json").write_text(
        json.dumps({"workflow_name": "w", "execution_outcome": outcome})
    )
    return run


def _transport(responses):
    calls = []

    def send(method, url, headers):
        calls.append((method, url, headers))
        status, body = responses.pop(0)
        return status, json.dumps(body).encode()

    send.calls = calls
    return send


def test_verified_connected_run_gets_receipt(tmp_path):
    run = _run(tmp_path)
    t = _transport([(409, {"code": "trusted_evidence_pending"}), (200, _receipt())])
    result = attach_from_cloud(
        run, execution_id=EXEC, api_key="k", transport=t, sleep=lambda s: None
    )
    assert result.attached and result.code == "attached"
    report = json.loads((run / "report.json").read_text())
    assert report["execute_receipt"]["execution_id"] == EXEC
    assert report["cloud_execution_id"] == EXEC
    assert (
        json.loads((run / "report.pre-seal.json").read_text()).get("execute_receipt")
        is None
    )
    assert t.calls[0][1].endswith(f"/api/v1/executions/{EXEC}/receipt")
    assert t.calls[0][2]["Authorization"] == "Bearer k"


@pytest.mark.parametrize(
    "receipt,code",
    [
        (_receipt(outcome="halted"), "not_verified"),
        (_receipt(execution_id="other-exec"), "execution_mismatch"),
        ({k: v for k, v in _receipt().items() if k != "seal_signature"}, "unsigned"),
        (_receipt(schema_version="x"), "malformed_receipt"),
    ],
)
def test_bad_receipts_attach_nothing(tmp_path, receipt, code):
    run = _run(tmp_path)
    before = (run / "report.json").read_text()
    result = attach_from_cloud(
        run, execution_id=EXEC, api_key="k", transport=_transport([(200, receipt)])
    )
    assert not result.attached and result.code == code
    assert (run / "report.json").read_text() == before


def test_unverified_local_run_never_calls_cloud(tmp_path):
    run = _run(tmp_path, outcome="HALTED")
    t = _transport([])
    assert (
        attach_from_cloud(run, execution_id=EXEC, api_key="k", transport=t).code
        == "local_not_verified"
    )
    assert t.calls == []


def test_not_connected_is_a_no_op(tmp_path):
    assert attach_from_cloud(_run(tmp_path), env={}).code == "not_connected"


def test_insecure_url_and_network_errors_fail_closed(tmp_path):
    run = _run(tmp_path)
    assert (
        attach_from_cloud(
            run,
            execution_id=EXEC,
            api_key="k",
            cloud_url="http://evil.example",
            transport=_transport([]),
        ).code
        == "insecure_cloud_url"
    )

    def boom(*a):
        raise ConnectionError("down")

    assert (
        attach_from_cloud(run, execution_id=EXEC, api_key="k", transport=boom).code
        == "cloud_unreachable"
    )
    assert "execute_receipt" not in json.loads((run / "report.json").read_text())


def test_cli_parses_seal_attach_and_run_flag():
    from openadapt_flow.__main__ import build_parser

    parser = build_parser()
    args = parser.parse_args(["attach-seal", "runs/r1", "--execution-id", EXEC])
    assert args.run_dir == "runs/r1" and args.execution_id == EXEC
    args = parser.parse_args(["run", "bundle", "--cloud-execution-id", EXEC])
    assert args.cloud_execution_id == EXEC
