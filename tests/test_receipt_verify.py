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
    canonical = r.report_canonical_sha256(json.loads(raw))
    exact = hashlib.sha256(raw).hexdigest()
    assert canonical != exact
    legacy = hashlib.sha256(r.canonical_json(json.loads(raw)).encode()).hexdigest()
    checked = r.verify_receipt(envelope, local_registry())
    assert checked.ok
    subjects = {s["name"]: s["digest"]["sha256"] for s in checked.statement["subject"]}
    assert (
        subjects["report.json"] == exact
        and subjects["report.canonical"] == canonical
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
        wire, pinned_signer=pin, report_jcs_sha256=canonical, outcome=checked.outcome
    ) == {
        "receipt_record": envelope,
        "receipt_record_signer": pin,
    }
    with pytest.raises(ValueError, match="canonical report"):
        verified_receipt_metrics(
            wire, pinned_signer=pin, report_jcs_sha256=exact, outcome=checked.outcome
        )
    if legacy != canonical:
        with pytest.raises(ValueError, match="canonical report"):
            verified_receipt_metrics(
                wire, pinned_signer=pin, report_jcs_sha256=legacy, outcome=checked.outcome
            )
    with pytest.raises(ValueError, match="outcome"):
        verified_receipt_metrics(
            wire, pinned_signer=pin, report_jcs_sha256=canonical, outcome="FAILED"
        )
    if fixture == "run-broken":
        # A stopped execution can still need reconciliation for a possible write.
        precise_outcome = r.receipt_outcome(json.loads(raw)["transaction_outcome"])[0]
        assert (checked.outcome, precise_outcome) == ("STOPPED", "UNKNOWN")
        with pytest.raises(ValueError, match="outcome"):
            verified_receipt_metrics(
                wire,
                pinned_signer=pin,
                report_jcs_sha256=canonical,
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


@pytest.mark.parametrize("finalizer", ["cli", "pool"])
def test_completed_unverified_no_write_uses_actual_report_serialization(
    tmp_path, monkeypatch, finalizer
):
    from unittest.mock import patch

    import openadapt_flow.__main__ as cli
    from openadapt_flow import receipt_record
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
    if finalizer == "cli":
        monkeypatch.setattr(
            cli, "_cmd_replay", lambda args: cli._finish_replay(tmp_path, report, args)
        )
        with patch.object(
            receipt_record, "receipt_after_run", wraps=receipt_record.receipt_after_run
        ) as sign:
            cli.main(["replay", str(tmp_path / "unused"), "--run-dir", str(tmp_path)])
        sign.assert_called_once_with(tmp_path, require_opt_in=False, expected_report=report)
    else:
        from openadapt_flow.cli_worklist_pool import _item_run

        monkeypatch.setenv("OPENADAPT_RECEIPT", "on")
        _item_run(report, None, tmp_path)
    envelope = json.loads((tmp_path / r.RECEIPT_FILE).read_bytes())
    entry = public_key_entry()
    digest = r.report_canonical_sha256(json.loads(path.read_bytes()))
    keys = {name: entry[name] for name in ("key_id", "public_key")}
    checked = r.verify_receipt(envelope, local_registry())
    assert checked.ok and checked.outcome == "UNKNOWN"
    assert checked.statement["predicate"]["lines"] == []
    assert (
        verified_receipt_metrics(
            (tmp_path / r.RECEIPT_FILE).read_bytes(),
            pinned_signer=keys,
            report_jcs_sha256=digest,
            outcome=r.receipt_outcome(report.transaction_outcome)[0],
        )["receipt_record"]
        == envelope
    )


def test_cli_refusal_cannot_sign_a_prior_report(tmp_path, monkeypatch):
    from unittest.mock import Mock

    import openadapt_flow.__main__ as cli
    from openadapt_flow import offline_seal

    run = _run(tmp_path)
    monkeypatch.setattr(cli, "_cmd_replay", lambda args: 2)
    legacy = Mock(return_value=None)
    monkeypatch.setattr(offline_seal, "seal_after_run", legacy)
    result = cli.main(["replay", str(tmp_path / "unused"), "--run-dir", str(run)])
    assert result == 2
    legacy.assert_called_once_with(run)
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

    run = tmp_path / "run"
    run.mkdir()
    fixture = Path(__file__).parent / "fixtures" / "seal_view" / "run-1" / "report.json"
    raw = fixture.read_bytes()
    (run / "report.json").write_bytes(raw)
    report = RunReport.model_validate_json(raw)
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
    """A run whose report carries Flow's own approval block, built by its model."""
    from openadapt_flow.approval_mode.model import (
        ApprovalModeItemEvidence,
        ItemApprovalRef,
    )

    run = _run(tmp_path, "VERIFIED", f"approved-{mode}-{len(approvals)}")
    report = json.loads((run / "report.json").read_text())
    if mode == "not_gated":
        decision = "not_gated"
    else:
        decision = "approved" if approvals else "no_person"
    evidence = ApprovalModeItemEvidence(
        mode=mode,
        decision=decision,
        needs_person=False,
        approvals=[ItemApprovalRef.model_validate(a) for a in approvals],
    )
    report["approval"] = evidence.model_dump(mode="json")
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
    envelope = write_receipt_record(run)
    assert write_receipt_record(run) == envelope
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


# -- routing, broken receipts and older formats ---------------------------------

FIXTURES = Path(__file__).parent / "fixtures" / "receipts"
PUBLIC_SEAL_VECTORS = (
    Path(__file__).resolve().parents[1]
    / "packages/flow/tests/fixtures/public_seal/golden_vectors.json"
)


def test_a_folder_named_like_an_operation_stays_an_operation(tmp_path, monkeypatch):
    (tmp_path / "ledger").mkdir()
    monkeypatch.chdir(tmp_path)
    out = CliRunner().invoke(main, ["verify", "ledger", "--help"])
    assert "receipt" not in out.output.lower().split("usage")[0]
    assert "Usage: openadapt verify <file|folder|link>" not in out.output


def test_a_broken_receipt_fails_even_beside_a_seal(tmp_path):
    from openadapt_flow.offline_seal import seal_run

    run = _run(tmp_path, "VERIFIED")
    seal_run(run, enqueue=False)
    (run / "receipt.json").write_text('{"not": "a receipt"}')
    out = _verify(str(run))
    assert out.exit_code == 1
    assert "malformed receipt" in out.output


def test_a_lone_seal_file_checks_its_run(tmp_path):
    from openadapt_flow.offline_seal import seal_run

    run = _run(tmp_path, "HALTED", "lone")
    seal_run(run, enqueue=False)
    out = _verify(str(run / "seal.json"))
    assert out.exit_code == 0, out.output
    assert "older format" in out.output
    report = json.loads((run / "report.json").read_text())
    report["model_calls"] = 9
    (run / "report.json").write_text(json.dumps(report))
    assert _verify(str(run / "seal.json")).exit_code != 0


def test_an_older_run_summary_verifies_and_a_tampered_one_does_not(tmp_path):
    good = tmp_path / "run-summary.json"
    good.write_bytes((FIXTURES / "run-summary.json").read_bytes())
    out = _verify(str(good))
    assert out.exit_code == 0, out.output
    assert "older format" in out.output and "Not signed" in out.output
    data = json.loads(good.read_text())
    data["effects_confirmed"] = data["effects_required"] + 1
    bad = tmp_path / "tampered.json"
    bad.write_text(json.dumps(data))
    assert _verify(str(bad)).exit_code == 1


def test_public_seal_golden_vectors_verify_only_with_their_key_pinned(tmp_path):
    vectors = json.loads(PUBLIC_SEAL_VECTORS.read_text())
    registry = tmp_path / "keys.json"
    registry.write_text(
        json.dumps(
            {
                "schema": r.REGISTRY_SCHEMA,
                "keys": [
                    {
                        "key_id": vectors["key_id"],
                        "public_key": vectors["public_key"],
                        "role": "this_computer",
                    }
                ],
            }
        )
    )
    checked = 0
    for case in vectors["cases"]:
        path = tmp_path / f"{case['name']}.json"
        path.write_text(case["body"])
        pinned = _verify(str(path), "--keys", str(registry))
        assert (pinned.exit_code == 0) == (case["expect"] == "valid"), case["name"]
        if case["expect"] == "valid":
            unpinned = _verify(str(path))
            assert unpinned.exit_code == 1
            assert "unknown signer" in unpinned.output
            checked += 1
    assert checked >= 1


def test_receipts_trust_cloud_keys_pinned_where_seal_keys_pin_writes(
    tmp_path, monkeypatch
):
    vectors = (
        Path(r.__file__).resolve().parents[1] / "tests" / "vectors" / "record-run-v1"
    )
    if not vectors.is_dir():
        pytest.skip("installed openadapt-types has no vectors")
    registry = json.loads((vectors / "registry.json").read_text())
    by_role = {k["role"]: k for k in registry["keys"]}
    pinned = tmp_path / "seal-keys.json"
    pinned.write_text(
        json.dumps(
            {
                "keys": [
                    {
                        "key_id": by_role["openadapt_cloud"]["key_id"],
                        "public_key": by_role["openadapt_cloud"]["public_key"],
                        "status": "active",
                    }
                ]
            }
        )
    )
    monkeypatch.setenv("OPENADAPT_SEAL_KEYS_PATH", str(pinned))
    keys = {k.key_id: k.role for k in local_registry()}
    assert keys[by_role["openadapt_cloud"]["key_id"]] == "openadapt_cloud"


def test_one_delegate_answer_never_reads_as_approved_by_a_person(tmp_path):
    delegate = {
        **PERSON_APPROVAL,
        "step_id": "save-2",
        "decided_by": "automation",
        "route": "mcp",
        "approver_kind": "unauthenticated",
        "approver_id": None,
        "auth_method": "none",
        "approver": {
            "kind": "agent",
            "auth": "mcp_session",
            "id": "agent-approver-bot",
        },
    }
    run = _approved_run(tmp_path, [PERSON_APPROVAL, delegate])
    write_receipt_record(run)
    result = verify_run_receipt(run, local_registry())
    assert result.ok, result.detail
    assert result.statement["predicate"]["approval"]["approver_kind"] == "agent"
    assert result.approval_line == "Approved by an agent delegate: each item"


def test_a_receipt_names_both_report_digests(tmp_path):
    run = _run(tmp_path, "VERIFIED", "canonical")
    write_receipt_record(run)
    result = verify_run_receipt(run, local_registry())
    names = {s["name"]: s["digest"]["sha256"] for s in result.statement["subject"]}
    report = json.loads((run / "report.json").read_text())
    assert names["report.canonical"] == r.report_canonical_sha256(report)
    # The same report written with other whitespace matches report.canonical.
    (run / "report.json").write_text(json.dumps(report, indent=4))
    assert verify_run_receipt(run, local_registry()).ok
    report["model_calls"] = 3
    (run / "report.json").write_text(json.dumps(report, indent=4))
    assert not verify_run_receipt(run, local_registry()).ok


@pytest.mark.parametrize("status", ["pending", "approved", "continuing"])
def test_durable_pause_cannot_be_signed(tmp_path, status):
    from openadapt_flow.runtime.durable.checkpoint import (
        CheckpointStore,
        PendingEscalation,
    )

    run = _run(tmp_path)
    store = CheckpointStore(run)
    store.write_pending(
        PendingEscalation(
            workflow_name="read",
            step_index=0,
            step_id="save",
            category="effect_escalated",
            status=status,
        )
    )
    with pytest.raises(ReceiptRecordError, match="restartable_run"):
        write_receipt_record(run)
    assert not (run / r.RECEIPT_FILE).exists()
    assert not (run / r.PRIVATE_FILE).exists()
