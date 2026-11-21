"""`openadapt verify` and the run receipt Flow writes (record/run/v1).

Real signing and real verification with a throwaway key under tmp_path:
receipts for stopped runs, tamper detection, unknown signers, embedded
content, and older formats that must still verify.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

pytest.importorskip("openadapt_flow.receipt_record")

from openadapt_flow.receipt_record import (  # noqa: E402
    ReceiptRecordError,
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
    fixture = "run-1" if outcome == "VERIFIED" else "run-broken"
    data = Path(__file__).parent / "fixtures" / "seal_view" / fixture / "report.json"
    report = json.loads(data.read_bytes())
    report.update(
        run_id=f"{name}-1",
        workflow_name="MockMed referral",
        execution_outcome=outcome,
        started_at="2026-11-20T23:14:00Z",
        bundle_content_digest=BUNDLE,
    )
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
    # Two contracts belong to one attempted write. The stopped run saw one
    # absence but the other check never completed.
    assert [(line["step"], line["check"]) for line in lines] == [(5, "not_checked")]
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
        "results": [
            {"step_id": "a", "intent": "save", "ok": False, "effect_verified": False}
        ],
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


@pytest.mark.parametrize("fixture", ["run-1", "run-broken"])
def test_settled_receipt_covers_both_report_encodings(fixture, tmp_path):
    from openadapt_flow.offline_seal import public_key_entry
    from openadapt_types.receipt_callback import verified_receipt_metrics

    run = tmp_path / fixture
    run.mkdir()
    source = Path(__file__).parent / "fixtures" / "seal_view" / fixture / "report.json"
    raw = source.read_bytes()
    (run / "report.json").write_bytes(raw)
    envelope = write_receipt_record(run)
    entry = public_key_entry()
    pin = {key: entry[key] for key in ("key_id", "public_key")}
    canonical = hashlib.sha256(r.canonical_json(json.loads(raw)).encode()).hexdigest()
    exact = hashlib.sha256(raw).hexdigest()
    assert canonical != exact
    checked = r.verify_receipt(envelope, local_registry())
    assert checked.ok
    subjects = {s["name"]: s["digest"]["sha256"] for s in checked.statement["subject"]}
    assert (
        subjects["report.json"] == exact
        and subjects["report.canonical.json"] == canonical
    )
    lines = checked.statement["predicate"]["lines"]
    assert len(lines) == 1 and lines[0]["step"] == 5
    assert (lines[0]["check"], lines[0]["strength"]) == (
        ("confirmed", "read_back")
        if fixture == "run-1"
        else ("not_checked", "same_screen")
    )
    wire = (run / "receipt.json").read_bytes()
    assert verified_receipt_metrics(
        wire, pinned_signer=pin, report_sha256=canonical, outcome=checked.outcome
    ) == {
        "receipt_record": envelope,
        "receipt_record_signer": pin,
    }
    with pytest.raises(ValueError, match="canonical report"):
        verified_receipt_metrics(
            wire, pinned_signer=pin, report_sha256=exact, outcome=checked.outcome
        )
    with pytest.raises(ValueError, match="outcome"):
        verified_receipt_metrics(
            wire, pinned_signer=pin, report_sha256=canonical, outcome="FAILED"
        )
    if fixture == "run-broken":
        # A stopped execution can still need reconciliation for a possible write.
        precise_outcome = r.receipt_outcome(json.loads(raw)["transaction_outcome"])[0]
        assert (checked.outcome, precise_outcome) == ("STOPPED", "UNKNOWN")
        with pytest.raises(ValueError, match="outcome"):
            verified_receipt_metrics(
                wire,
                pinned_signer=pin,
                report_sha256=canonical,
                outcome=precise_outcome,
            )
    assert write_receipt_record(run) == envelope
    (run / "report.json").write_bytes(raw + b"\n")
    with pytest.raises(ReceiptRecordError, match="already_signed"):
        write_receipt_record(run)
    assert (run / "receipt.json").read_bytes() == wire


def test_incomplete_or_excess_write_evidence_cannot_appear_verified():
    result = {
        "step_id": "save",
        "intent": "save",
        "effect_verified": False,
        "ok": False,
        "risk": "irreversible",
    }
    report = {"execution_outcome": "VERIFIED", "results": [result]}
    statement, _ = build_statement(report, "a" * 64)
    assert statement["predicate"]["outcome"] == "UNKNOWN"
    assert statement["predicate"]["lines"][0]["check"] == "not_checked"
    for key in ("delivery_attempted", "skipped"):
        report["results"] = [{**result, key: key == "skipped"}]
        statement, _ = build_statement(report, "a" * 64)
        assert statement["predicate"]["lines"] == []
    report["results"] = [result] * (r.MAX_LINES + 1)
    with pytest.raises(ReceiptRecordError, match="too_many_writes"):
        build_statement(report, "a" * 64)


def test_signing_preserves_conflicting_legacy_files(tmp_path):
    run = _run(tmp_path)
    old = b'{"schema_version":"openadapt.run-receipt/v2"}'
    other = b'{"schema_version":"openadapt.run-receipt/v2","other":true}'
    (run / "receipt.json").write_bytes(old)
    (run / "run-summary.json").write_bytes(other)
    with pytest.raises(ReceiptRecordError, match="legacy_summary_conflict"):
        write_receipt_record(run)
    assert (run / "receipt.json").read_bytes() == old
    assert (run / "run-summary.json").read_bytes() == other
    assert not (run / r.PRIVATE_FILE).exists()


def test_reuse_requires_the_original_signer_and_intact_openings(tmp_path, monkeypatch):
    run = _run(tmp_path)
    envelope = write_receipt_record(run)
    home = tmp_path / "other-home"
    with monkeypatch.context() as patch:
        patch.setenv("OPENADAPT_SEAL_HOME", str(home))
        with pytest.raises(ReceiptRecordError, match="existing_unverified"):
            write_receipt_record(run)
    (run / r.PRIVATE_FILE).write_text("{}")
    with pytest.raises(ReceiptRecordError, match="existing_unverified"):
        write_receipt_record(run)
    assert json.loads((run / r.RECEIPT_FILE).read_bytes()) == envelope


def test_new_finalizer_requires_explicit_opt_in_and_waits_for_settlement(tmp_path):
    run = _run(tmp_path)
    assert receipt_after_run(run, env={}, require_opt_in=True) is None
    assert not (run / r.RECEIPT_FILE).exists()
    path = run / "report.json"
    report = json.loads(path.read_bytes())
    report["transaction_outcome"] = "SAVED_AWAITING_CHECK"
    path.write_text(json.dumps(report))
    assert "readback_pending" in receipt_after_run(
        run, env={"OPENADAPT_RECEIPT": "on"}, require_opt_in=True
    )
    assert not (run / r.RECEIPT_FILE).exists()


def test_completed_unverified_no_write_uses_actual_report_serialization(tmp_path):
    from argparse import Namespace

    from openadapt_flow.__main__ import _finish_replay
    from openadapt_flow.ir import RunReport, Workflow
    from openadapt_flow.offline_seal import public_key_entry
    from openadapt_flow.transaction import stamp_transaction_outcome
    from openadapt_types.receipt_callback import verified_receipt_metrics

    report = RunReport(
        workflow_name="read",
        started_at="2026-11-20T23:14:00Z",
        execution_profile="demo",
        execution_outcome="COMPLETED_UNVERIFIED",
        results=[],
    )
    stamp_transaction_outcome(report, Workflow(name="read", steps=[]))
    assert report.transaction_outcome == "COMPLETED_UNVERIFIED"
    path = report.save(tmp_path)
    _finish_replay(tmp_path, report, Namespace())
    envelope = json.loads((tmp_path / r.RECEIPT_FILE).read_bytes())
    entry = public_key_entry()
    digest = hashlib.sha256(
        r.canonical_json(json.loads(path.read_bytes())).encode()
    ).hexdigest()
    keys = {name: entry[name] for name in ("key_id", "public_key")}
    checked = r.verify_receipt(envelope, local_registry())
    assert checked.ok and checked.outcome == "UNKNOWN"
    assert checked.statement["predicate"]["lines"] == []
    assert (
        verified_receipt_metrics(
            (tmp_path / r.RECEIPT_FILE).read_bytes(),
            pinned_signer=keys,
            report_sha256=digest,
            outcome=r.receipt_outcome(report.transaction_outcome)[0],
        )["receipt_record"]
        == envelope
    )


def test_cli_refusal_cannot_sign_a_prior_report(tmp_path, monkeypatch):
    import openadapt_flow.__main__ as cli

    run = _run(tmp_path)
    monkeypatch.setattr(cli, "_cmd_replay", lambda args: 2)
    result = cli.main(["replay", str(tmp_path / "unused"), "--run-dir", str(run)])
    assert result == 2
    assert not (run / r.RECEIPT_FILE).exists()


def test_refuses_folder_with_another_finalizers_report(tmp_path):
    from openadapt_flow.ir import RunReport

    other = RunReport(workflow_name="other", started_at="2026-11-21T00:00:00Z")
    run = _run(tmp_path)
    assert "report_changed" in receipt_after_run(run, expected_report=other)
    assert not (run / r.RECEIPT_FILE).exists()


def test_legacy_summary_recognized_by_schema_under_any_filename(tmp_path):
    from openadapt_flow.ir import RunReport
    from openadapt_flow.receipt import build_receipt, read_run_summary

    run = _run(tmp_path)
    report = RunReport.model_validate_json((run / "report.json").read_bytes())
    legacy = build_receipt(report).model_dump_json().encode()
    old = run / "receipt.json"
    old.write_bytes(legacy)
    assert read_run_summary(run)["schema_version"] == "openadapt.run-receipt/v2"
    write_receipt_record(run)
    assert (run / "run-summary.json").read_bytes() == legacy
    assert read_run_summary(run)["schema_version"] == "openadapt.run-receipt/v2"
    alternative = run / "audit-export.json"
    alternative.write_bytes(legacy)
    result = _verify(str(alternative))
    assert "run-receipt/v2" in result.output


def test_declared_but_unbound_effect_stays_visible():
    report = {
        "execution_outcome": "HALTED",
        "results": [
            {
                "step_id": "save",
                "intent": "save",
                "ok": False,
                "delivery_attempted": True,
            }
        ],
        "change_ledger": [
            {
                "step_id": "save",
                "position": 1,
                "status": "unknown",
                "effects": [{"contract_hash": None, "status": "unknown"}],
            }
        ],
    }
    statement, _ = build_statement(report, "a" * 64)
    (line,) = statement["predicate"]["lines"]
    assert (statement["predicate"]["outcome"], line["check"], line["strength"]) == (
        "STOPPED",
        "not_checked",
        "none",
    )
    assert "effect_not_declared" not in line["not_checked"]


def _approved_run(tmp_path: Path, approvals: list, mode: str = "each_item") -> Path:
    run = _run(tmp_path, "VERIFIED", f"approved-{mode}-{len(approvals)}")
    report = json.loads((run / "report.json").read_text())
    report["approval"] = {
        "schema_version": "openadapt.approval-mode-item/v1",
        "mode": mode,
        "decision": "approved" if approvals else "no_person",
        "needs_person": False,
        "approvals": approvals,
    }
    (run / "report.json").write_text(json.dumps(report))
    return run


PERSON_APPROVAL = {
    "step_id": "save",
    "request_sha256": "sha256:" + "3" * 64,
    "answer_sha256": "sha256:" + "4" * 64,
    "decision": "approved",
    "decided_by": "human",
    "route": "desktop_run_it",
    "approver_kind": "cloud_user",
    "approver_id": "usr_synthetic01",
    "auth_method": "cloud_aal2",
}


def test_an_approved_runs_receipt_records_the_mode_and_approver_kind(tmp_path):
    run = _approved_run(tmp_path, [PERSON_APPROVAL])
    write_receipt_record(run)
    payload = base64.b64decode(
        json.loads((run / "receipt.json").read_text())["payload"]
    )
    assert b"usr_synthetic01" not in payload
    result = verify_run_receipt(run, local_registry())
    assert result.ok, result.detail
    approval = result.statement["predicate"]["approval"]
    assert (approval["mode"], approval["approver_kind"]) == ("each_item", "person")
    assert set(approval["commitments"]) == {"principal", "scope"}
    assert result.approval_line == "Approved by a person: each item"
    assert result.disclosed["approval.principal"].startswith("person:pr-")
    out = _verify(str(run))
    assert "Approved by a person: each item" in out.output


def test_an_unapproved_run_says_no_approval_recorded(tmp_path):
    run = _run(tmp_path, "VERIFIED")
    write_receipt_record(run)
    result = verify_run_receipt(run, local_registry())
    assert result.ok
    assert "approval" not in result.statement["predicate"]
    assert result.approval_line == "No approval recorded"
    assert "No approval recorded" in _verify(str(run)).output


def test_a_not_gated_run_says_so(tmp_path):
    run = _approved_run(tmp_path, [], mode="not_gated")
    write_receipt_record(run)
    result = verify_run_receipt(run, local_registry())
    assert (
        result.approval_line == "Not gated: no approval was required for these writes"
    )
