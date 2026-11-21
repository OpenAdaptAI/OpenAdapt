"""`openadapt verify` and the run receipt Flow writes (record/run/v1).

Real signing and real verification with a throwaway key under tmp_path:
receipts for stopped runs, tamper detection, unknown signers, embedded
content, and older formats that must still verify.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

pytest.importorskip("openadapt_flow.receipt_record")

from openadapt_flow.receipt_record import (  # noqa: E402
    build_statement,
    local_registry,
    receipt_after_run,
    verify_run_receipt,
    write_receipt_record,
)
from openadapt_types import receipt as r  # noqa: E402

from openadapt.cli import main  # noqa: E402

BUNDLE = "2" * 64


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENADAPT_SEAL_HOME", str(tmp_path / "seal-home"))
    monkeypatch.delenv("OPENADAPT_SEAL_KEY", raising=False)
    monkeypatch.delenv("OPENADAPT_RECEIPT", raising=False)


def _run(tmp_path: Path, outcome: str = "HALTED", name: str = "run") -> Path:
    run = tmp_path / name
    run.mkdir()
    report = {
        "run_id": f"{name}-1",
        "workflow_name": "MockMed referral",
        "execution_outcome": outcome,
        "started_at": "2026-11-20T23:14:00Z",
        "bundle_content_digest": BUNDLE,
        "model_calls": 0,
        "results": [
            {"ok": True, "effect_verified": True, "postconditions_ok": True},
            {"ok": outcome == "VERIFIED", "effect_verified": outcome == "VERIFIED"},
            {"ok": True},
        ],
    }
    (run / "report.json").write_text(json.dumps(report))
    return run


def _verify(*args: str):
    return CliRunner().invoke(main, ["verify", *args], catch_exceptions=False)


def test_a_stopped_run_gets_a_receipt_and_stays_stopped(tmp_path):
    run = _run(tmp_path, "HALTED")
    assert receipt_after_run(run) == f"receipt: {run / 'receipt.json'}"
    private = run / "receipt.private.json"
    assert private.stat().st_mode & 0o077 == 0
    result = verify_run_receipt(run, local_registry())
    assert result.ok and result.outcome == "STOPPED"
    assert result.signer_lines == ("Signed by this computer",)
    lines = result.statement["predicate"]["lines"]
    assert [line["check"] for line in lines] == ["confirmed", "refuted"]
    out = _verify(str(run))
    assert out.exit_code == 0, out.output
    assert "Signed by this computer" in out.output
    assert "Outcome: STOPPED" in out.output


def test_the_receipt_never_holds_values_from_the_report(tmp_path):
    run = _run(tmp_path, "VERIFIED")
    write_receipt_record(run)
    text = (run / "receipt.json").read_text()
    payload = base64.b64decode(json.loads(text)["payload"]).decode()
    for secret in ("MockMed", "run-1", "2026-11-20T23:14"):
        assert secret not in payload
    assert "MockMed" in (run / "receipt.private.json").read_text()


@pytest.mark.parametrize(
    "run_outcome", ["HALTED", "COMPLETED_UNVERIFIED", "ROLLED_BACK", None, "WEIRD"]
)
def test_signing_never_upgrades(run_outcome):
    report = {"execution_outcome": run_outcome, "results": []}
    statement, _ = build_statement(report, "1" * 64, flow_version="3.5.0")
    assert statement["predicate"]["outcome"] != "VERIFIED"


def test_a_verified_report_with_a_refuted_write_is_not_verified():
    report = {
        "execution_outcome": "VERIFIED",
        "results": [{"ok": False, "effect_verified": False}],
    }
    statement, _ = build_statement(report, "1" * 64, flow_version="3.5.0")
    assert statement["predicate"]["outcome"] == "UNKNOWN"


def test_tampering_with_the_report_is_detected(tmp_path):
    run = _run(tmp_path, "HALTED")
    write_receipt_record(run)
    report = json.loads((run / "report.json").read_text())
    report["execution_outcome"] = "VERIFIED"
    (run / "report.json").write_text(json.dumps(report))
    out = _verify(str(run))
    assert out.exit_code == 1
    assert "changed after it was signed" in out.output


def test_tampering_with_the_receipt_is_detected(tmp_path):
    run = _run(tmp_path, "HALTED")
    write_receipt_record(run)
    envelope = json.loads((run / "receipt.json").read_text())
    payload = base64.b64decode(envelope["payload"]).replace(b'"STOPPED"', b'"VERIFIED"')
    envelope["payload"] = base64.b64encode(payload).decode()
    (run / "receipt.json").write_text(json.dumps(envelope))
    out = _verify(str(run / "receipt.json"))
    assert out.exit_code == 1
    assert "Not verified" in out.output and "Outcome" not in out.output


def test_an_unknown_signer_fails_closed(tmp_path, monkeypatch):
    run = _run(tmp_path, "VERIFIED")
    write_receipt_record(run)
    monkeypatch.setenv("OPENADAPT_SEAL_HOME", str(tmp_path / "another-computer"))
    out = _verify(str(run / "receipt.json"))
    assert out.exit_code == 1
    assert "Not verified: unknown signer" in out.output


def test_a_receipt_with_an_embedded_image_is_refused(tmp_path):
    run = _run(tmp_path, "VERIFIED")
    write_receipt_record(run)
    private = json.loads((run / "receipt.private.json").read_text())
    private["openings"]["program_name"]["value"] = (
        "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    )
    (run / "receipt.private.json").write_text(json.dumps(private))
    result = verify_run_receipt(run, local_registry())
    assert (result.ok, result.reason) == (False, "embedded_content")


def test_the_builder_refuses_a_screenshot_as_a_value():
    report = {"workflow_name": "iVBORw0KGgo" + "A" * 200, "results": []}
    statement, openings = build_statement(report, "1" * 64, flow_version="3.5.0")
    # The name fails its field pattern, so it is never committed at all.
    assert "program_name" not in openings
    assert r._statement_embedded(statement) is None


def test_receipts_are_on_by_default_and_can_be_turned_off(tmp_path):
    run = _run(tmp_path)
    assert receipt_after_run(run, env={"OPENADAPT_RECEIPT": "off"}) is None
    assert not (run / "receipt.json").exists()


def test_an_older_run_summary_in_receipt_json_moves_aside(tmp_path):
    run = _run(tmp_path, "VERIFIED")
    (run / "receipt.json").write_text(
        json.dumps({"schema_version": "openadapt.run-receipt/v2"})
    )
    write_receipt_record(run)
    assert r.is_receipt_envelope(json.loads((run / "receipt.json").read_text()))
    assert json.loads((run / "run-summary.json").read_text())["schema_version"] == (
        "openadapt.run-receipt/v2"
    )


def test_a_folder_with_only_an_older_seal_still_verifies(tmp_path):
    from openadapt_flow.offline_seal import seal_run

    run = _run(tmp_path, "HALTED", "old")
    seal_run(run, enqueue=False)
    out = _verify(str(run))
    assert "older format" in out.output
    assert out.exit_code == 0, out.output


def test_the_private_openings_are_not_a_receipt(tmp_path):
    run = _run(tmp_path)
    write_receipt_record(run)
    out = _verify(str(run / "receipt.private.json"))
    assert out.exit_code == 1
    assert "private" in out.output


def test_verify_subcommands_still_route(tmp_path):
    out = CliRunner().invoke(main, ["verify", "--help"])
    assert out.exit_code == 0
    assert "ledger" in out.output and "effect" in out.output


def test_the_shared_vectors_verify_through_the_launcher(tmp_path):
    vectors = (
        Path(r.__file__).resolve().parents[1] / "tests" / "vectors" / "record-run-v1"
    )
    if not vectors.is_dir():
        pytest.skip("installed openadapt-types has no vectors")
    vector = json.loads((vectors / "cloud-countersign.json").read_text())
    path = tmp_path / "card.json"
    path.write_text(json.dumps(vector["document"]))
    out = _verify(str(path), "--keys", str(vectors / "registry.json"))
    assert out.exit_code == 0, out.output
    assert "Countersigned by OpenAdapt Cloud" in out.output


def test_the_public_allow_list_is_inside_todays_public_fields():
    """openadapt.public-disclosure/v1 never reveals more than public-card/v1 did."""
    from openadapt_flow.public_seal import IdentityClaim, PublicCardV1

    card_fields = set(PublicCardV1.model_fields) | {
        f"identity_claim.{name}" for name in IdentityClaim.model_fields
    }
    # public-evidence-receipt/v1 (openadapt-cloud src/lib/evidenceReceipts.ts).
    evidence_fields = {"outcome", "runtime.version", "calls.model_call_count"}
    for version, allowed in r.PUBLIC_DISCLOSURE.items():
        for path, source in allowed.items():
            schema, field = source.split(":")
            known = card_fields if schema == "public-card/v1" else evidence_fields
            assert field in known, (version, path, source)
