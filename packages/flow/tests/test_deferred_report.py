"""Checked later: a save confirmed by a report that arrives after the run.

A run whose only check is a declared later report ends SAVED_AWAITING_CHECK:
never VERIFIED, never billable. A matching accepting row in the report settles
it VERIFIED; a rejecting row, or no row by the deadline, settles it
RECONCILIATION_REQUIRED. Synthetic X12 and CSV reports only.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from openadapt_flow.deployment import EffectsConfig, build_effect_verifier
from openadapt_flow.ir import (
    ActionKind,
    EffectVerificationEvidence,
    Postcondition,
    PostconditionKind,
    RunReport,
    Step,
    StepResult,
    Workflow,
)
from openadapt_flow.plain_outcome import plain_result
from openadapt_flow.reports.parse import (
    ReportError,
    ReportFormat,
    parse_277ca,
    parse_999,
    parse_report,
)
from openadapt_flow.runtime.effects import Effect, EffectKind, Verdict
from openadapt_flow.runtime.effects.deferred_report import (
    DeferredReportSpec,
    DeferredReportVerifier,
    PendingLedger,
    settled_outcome,
)
from openadapt_flow.runtime.replayer import Replayer
from openadapt_flow.transaction import TransactionOutcome, classify_transaction_outcome
from tests.test_replayer import FakeBackend, FakeVision, Match

HASH = "sha256:" + "a" * 64


def _isa(body: str, *, segment: str = "~", element: str = "*") -> str:
    isa = element.join(
        [
            "ISA",
            "00",
            " " * 10,
            "00",
            " " * 10,
            "ZZ",
            "SUBMITTER".ljust(15),
            "ZZ",
            "RECEIVER".ljust(15),
            "261018",
            "1200",
            "^",
            "00501",
            "000000001",
            "0",
            "T",
            ":",
        ]
    )
    assert len(isa) == 105, len(isa)
    return isa + segment + body


ACK_999 = _isa(
    "GS*FA*RECEIVER*SUBMITTER*20261018*1200*1*X*005010X231A1~"
    "ST*999*0001*005010X231A1~"
    "AK1*HC*17*005010X222A1~"
    "AK2*837*0001~IK5*A~"
    "AK2*837*0002~IK5*R*5~"
    "AK9*P*2*2*1~SE*8*0001~GE*1*1~IEA*1*000000001~"
)


def ack_277ca(when: datetime) -> str:
    stamp = when.strftime("%Y%m%d*%H%M")
    return _isa(
        "GS*HN*PAYER*SUBMITTER*20261018*1200*1*X*005010X214~"
        f"ST*277*0001*005010X214~BHT*0085*08*1*{stamp}*TH~"
        "HL*1**20*1~NM1*PR*2*PAYER~"
        "HL*2*1*21*1~NM1*41*2*CLINIC~"
        "HL*3*2*19*1~NM1*85*2*CLINIC~TRN*1*CLM-9~STC*A2:20*20261018*WQ*1~"
        "HL*4*3*PT~NM1*QC*1*DOE*JANE~TRN*2*CLM-1001~STC*A2:20*20261018*WQ*120~"
        "HL*5*3*PT~NM1*QC*1*ROE*RICH~TRN*2*CLM-1002~STC*A7:562*20261018*U*80~"
        "HL*6*3*PT~NM1*QC*1*POE*PAT~TRN*2*CLM-1003~STC*A1:19*20261018*WQ*60~"
        "SE*20*0001~GE*1*1~IEA*1*000000001~"
    )


#: The save happened at T0; the payer's acknowledgment came out at T1.
T0 = datetime(2026, 10, 18, 6, tzinfo=timezone.utc)
T1 = datetime(2026, 10, 18, 12)
ACK_277CA = ack_277ca(T1)
SETTLE_AT = datetime(2026, 10, 18, 13, tzinfo=timezone.utc)

FMT_277 = ReportFormat(format="x12-277ca")


# -- reading reports ------------------------------------------------------------


def test_a_999_can_only_reject() -> None:
    # A 999 says the file passed syntax checks, not that a claim was accepted.
    rows = {row.key: row for row in parse_999(ACK_999)}
    assert rows["17:0001"].status == "pending"
    assert rows["17:0002"].status == "rejected"


def test_277ca_reads_claim_status_categories() -> None:
    # The provider-level TRN*1*CLM-9 is not a claim and is ignored.
    rows = {row.key: (row.status, row.code) for row in parse_277ca(ACK_277CA)}
    assert rows == {
        "CLM-1001": ("accepted", "A2"),
        "CLM-1002": ("rejected", "A7"),
        "CLM-1003": ("pending", "A1"),  # received, not yet accepted
    }


def test_csv_and_text_reports_and_conflicting_rows() -> None:
    fmt = ReportFormat(
        format="csv",
        key_column="order_id",
        status_column="status",
        accept=["Posted"],
        reject=["Failed"],
    )
    rows = parse_report(
        "order_id,status\nORD-1,posted\nORD-2,Failed\nORD-3,Queued\nORD-4,Posted\n"
        "ORD-4,Failed\n",
        fmt,
    )
    assert rows["ORD-1"].status == "accepted"
    assert rows["ORD-2"].status == "rejected"
    assert rows["ORD-3"].status == "pending"
    assert rows["ORD-4"].status == "conflicting"
    text = ReportFormat(
        format="text",
        pattern=r"Claim (?P<key>\S+): (?P<status>\w+)",
        accept=["Accepted"],
    )
    assert parse_report("Claim C9: Accepted\n", text)["C9"].status == "accepted"


def test_unreadable_reports_are_refused() -> None:
    with pytest.raises(ReportError):
        parse_999("not x12")
    with pytest.raises(ReportError):
        parse_277ca(ACK_999)
    with pytest.raises(ValueError, match="accept"):
        ReportFormat(format="csv", key_column="k", status_column="s")


# -- the ledger -------------------------------------------------------------------


def _ledger(tmp_path: Path) -> PendingLedger:
    return PendingLedger(tmp_path / "ledger")


def _add(ledger: PendingLedger, key: str, *, hours: float = 48.0, now=T0):
    return ledger.add(
        key=key,
        effect_contract_hash=HASH,
        fmt=FMT_277,
        describe="payer 277CA",
        deadline_hours=hours,
        now=now,
    )


def test_the_ledger_keeps_no_key_and_is_private(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    entry = _add(ledger, "CLM-1001")
    path = ledger.directory / f"{entry.id.split(':')[1]}.json"
    assert "CLM-1001" not in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600
    assert ledger.directory.stat().st_mode & 0o777 == 0o700
    assert entry.state == "waiting"


def test_a_report_settles_accepted_rejected_and_pending_rows(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    accepted = _add(ledger, "CLM-1001")
    rejected = _add(ledger, "CLM-1002")
    pending = _add(ledger, "CLM-1003")
    unmentioned = _add(ledger, "CLM-9999")
    settled = {s.id: s.state for s in ledger.settle(ACK_277CA, FMT_277, now=SETTLE_AT)}
    assert settled == {
        accepted.id: "verified",
        rejected.id: "reconciliation_required",
    }
    assert ledger.get(pending.id).state == "waiting"
    assert ledger.get(unmentioned.id).state == "waiting"
    # A report in another format never settles a 277CA check.
    other = ReportFormat(format="csv", key_column="k", status_column="s", accept=["ok"])
    assert (
        ledger.settle(
            "k,s\nCLM-1003,ok\n",
            other,
            issued_at=SETTLE_AT,
            now=SETTLE_AT,
        )
        == []
    )


def test_unmatched_at_the_deadline_needs_reconciliation(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    start = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
    entry = _add(ledger, "CLM-4242", hours=24, now=start)
    assert ledger.settle(None, None, now=start + timedelta(hours=23)) == []
    settled = ledger.settle(None, None, now=start + timedelta(hours=25))
    assert [(s.id, s.state, s.code) for s in settled] == [
        (entry.id, "reconciliation_required", "deadline")
    ]


def test_two_saves_on_one_key_are_never_settled_verified(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    first = _add(ledger, "CLM-1001")
    second = _add(ledger, "CLM-1001")
    states = {s.id: s.state for s in ledger.settle(ACK_277CA, FMT_277, now=SETTLE_AT)}
    assert states == {
        first.id: "reconciliation_required",
        second.id: "reconciliation_required",
    }


# -- the run --------------------------------------------------------------------------


def _spec(tmp_path: Path) -> DeferredReportSpec:
    return DeferredReportSpec.model_validate(
        {
            "report": {"format": "x12-277ca"},
            "key": {"param": "claim_id"},
            "deadline_hours": 48,
            "ledger": str(tmp_path / "ledger"),
            "describe": "payer 277CA",
        }
    )


def _claim_effect() -> Effect:
    return Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"claim_id": "CLM-1001"},
        expected_count=1,
        timeout_s=0.0,
    )


def test_the_verifier_records_the_save_as_waiting(tmp_path: Path) -> None:
    verifier = build_effect_verifier(
        EffectsConfig(
            kind="deferred_report", deferred_report=_spec(tmp_path).model_dump()
        ),
        {"claim_id": "CLM-1001"},
    )
    inner = getattr(verifier, "_inner", verifier)
    assert isinstance(inner, DeferredReportVerifier) or verifier is not None
    verdict = verifier.verify(_claim_effect(), verifier.capture_pre_state())
    assert verdict.verdict is Verdict.INDETERMINATE
    assert verdict.deferred_check and verdict.deferred_check.startswith("deferred:")
    assert not verdict.confirmed
    assert "deferred_report" not in EffectsConfig().model_dump()


def test_an_unwritable_ledger_is_an_ordinary_halt(tmp_path: Path) -> None:
    blocker = tmp_path / "ledger"
    blocker.write_text("a file, not a directory")
    verifier = DeferredReportVerifier(_spec(tmp_path), "CLM-1001")
    verdict = verifier.verify(_claim_effect(), verifier.capture_pre_state())
    assert verdict.verdict is Verdict.INDETERMINATE
    assert verdict.deferred_check is None


def _evidence(**kw) -> EffectVerificationEvidence:
    return EffectVerificationEvidence(
        effect_contract_hash=HASH,
        substrate="deferred-report",
        verification_tier=1,
        initial_verdict="indeterminate",
        final_verdict="indeterminate",
        **kw,
    )


def _report(*results: StepResult) -> RunReport:
    return RunReport(
        workflow_name="claim",
        started_at="2026-10-18T12:00:00+00:00",
        success=False,
        execution_outcome="HALTED",
        results=list(results),
    )


def _save(evidence: list[EffectVerificationEvidence]) -> StepResult:
    return StepResult(
        step_id="submit",
        intent="submit claim",
        ok=True,
        risk="irreversible",
        delivery_attempted=True,
        effect_contract_hashes=[HASH],
        effect_evidence=evidence,
        effect_verified=False,
    )


def test_a_run_waiting_for_its_report_is_saved_awaiting_check() -> None:
    report = _report(_save([_evidence(deferred_check="deferred:" + "1" * 32)]))
    outcome = classify_transaction_outcome(report)
    assert outcome is TransactionOutcome.SAVED_AWAITING_CHECK
    assert not outcome.is_billable and not outcome.is_production_success
    plain = plain_result(outcome.value)
    assert plain.label == "Saved, waiting for its check"
    assert not plain.retry_allowed_without_check


def test_anything_else_uncertain_falls_back_to_reconciliation() -> None:
    # The same indeterminate save without a deferred check.
    assert (
        classify_transaction_outcome(_report(_save([_evidence()])))
        is TransactionOutcome.RECONCILIATION_REQUIRED
    )
    # A deferred save plus another step whose write is unsettled.
    other = StepResult(
        step_id="note",
        intent="add note",
        ok=True,
        risk="irreversible",
        delivery_attempted=True,
    )
    report = _report(other, _save([_evidence(deferred_check="deferred:" + "1" * 32)]))
    assert classify_transaction_outcome(report) is (
        TransactionOutcome.RECONCILIATION_REQUIRED
    )


def test_a_matching_report_row_settles_the_run(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    entry = _add(ledger, "CLM-1001")
    report = _report(_save([_evidence(deferred_check=entry.id)]))
    report.transaction_outcome = classify_transaction_outcome(report).value
    assert settled_outcome(report, ledger, now=T0) == "SAVED_AWAITING_CHECK"
    ledger.settle(ACK_277CA, FMT_277, now=SETTLE_AT)
    assert settled_outcome(report, ledger, now=SETTLE_AT) == "VERIFIED"


def test_a_rejected_or_overdue_report_needs_reconciliation(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    rejected = _add(ledger, "CLM-1002")
    report = _report(_save([_evidence(deferred_check=rejected.id)]))
    report.transaction_outcome = "SAVED_AWAITING_CHECK"
    ledger.settle(ACK_277CA, FMT_277, now=SETTLE_AT)
    assert settled_outcome(report, ledger, now=SETTLE_AT) == "RECONCILIATION_REQUIRED"
    late = _add(ledger, "CLM-7777", hours=1)
    report = _report(_save([_evidence(deferred_check=late.id)]))
    report.transaction_outcome = "SAVED_AWAITING_CHECK"
    later = T0 + timedelta(hours=2)
    assert settled_outcome(report, ledger, now=later) == "RECONCILIATION_REQUIRED"


def test_through_the_replayer_and_the_cli(tmp_path: Path, capsys) -> None:
    from openadapt_flow.__main__ import build_parser

    workflow = Workflow(
        name="claim",
        steps=[
            Step(
                id="submit",
                intent="submit claim",
                action=ActionKind.KEY,
                key="Enter",
                risk="irreversible",
                expect=[
                    Postcondition(
                        kind=PostconditionKind.TEXT_PRESENT,
                        text="Submitted",
                        timeout_s=0.2,
                    )
                ],
                effects=[_claim_effect()],
            )
        ],
    )
    vision = FakeVision()
    vision.text_results = {
        "Submitted": Match(point=(50, 10), region=(30, 5, 40, 10), confidence=0.9)
    }
    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True)
    run_dir = tmp_path / "run"
    verifier = DeferredReportVerifier(_spec(tmp_path), "CLM-1001")
    report = Replayer(
        FakeBackend(), vision=vision, effect_verifier=verifier, poll_interval_s=0.01
    ).run(workflow, bundle_dir=bundle, run_dir=run_dir)
    assert report.transaction_outcome == "SAVED_AWAITING_CHECK"
    assert report.transaction_billable is False
    saved = json.loads((run_dir / "report.json").read_text())
    assert saved["transaction_outcome"] == "SAVED_AWAITING_CHECK"

    ack = tmp_path / "ack.277"
    ack.write_text(ack_277ca(datetime.now(timezone.utc).replace(tzinfo=None)))
    ledger_dir = str(tmp_path / "ledger")
    parser = build_parser()
    status = parser.parse_args(
        ["deferred", "status", str(run_dir), "--ledger", ledger_dir]
    )
    assert status.func(status) == 1
    settle = parser.parse_args(
        [
            "deferred",
            "settle",
            "--ledger",
            ledger_dir,
            "--report",
            str(ack),
            "--format",
            "x12-277ca",
        ]
    )
    assert settle.func(settle) == 0
    assert status.func(status) == 0
    out = capsys.readouterr().out
    assert "now VERIFIED (billable)" in out
    # The run's own report is never rewritten.
    assert json.loads((run_dir / "report.json").read_text()) == saved


# -- review findings, pinned -------------------------------------------------------


def test_a_report_after_the_deadline_never_promotes(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    entry = _add(ledger, "CLM-1001", hours=1)  # due at T0 + 1h
    # The acknowledgment arrives after the deadline: the entry expires first.
    settled = ledger.settle(ACK_277CA, FMT_277, now=SETTLE_AT)
    assert [(s.id, s.state, s.code) for s in settled] == [
        (entry.id, "reconciliation_required", "deadline")
    ]
    assert ledger.get(entry.id).state == "reconciliation_required"


def test_a_report_older_than_the_save_settles_nothing(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    entry = _add(ledger, "CLM-1001", now=datetime(2026, 10, 20, tzinfo=timezone.utc))
    assert (
        ledger.settle(
            ACK_277CA, FMT_277, now=datetime(2026, 10, 20, 1, tzinfo=timezone.utc)
        )
        == []
    )
    assert ledger.get(entry.id).state == "waiting"


def test_csv_and_text_reports_need_their_issue_time(tmp_path: Path) -> None:
    fmt = ReportFormat(
        format="csv", key_column="k", status_column="s", accept=["Posted"]
    )
    ledger = _ledger(tmp_path)
    entry = ledger.add(
        key="ORD-1",
        effect_contract_hash=HASH,
        fmt=fmt,
        describe="nightly export",
        deadline_hours=48,
        now=T0,
    )
    with pytest.raises(ReportError, match="issue time"):
        ledger.settle("k,s\nORD-1,Posted\n", fmt, now=SETTLE_AT)
    # Another CSV read differently (other accept values) settles nothing.
    other = ReportFormat(
        format="csv", key_column="k", status_column="s", accept=["Posted", "Queued"]
    )
    assert (
        ledger.settle("k,s\nORD-1,Posted\n", other, issued_at=SETTLE_AT, now=SETTLE_AT)
        == []
    )
    [done] = ledger.settle(
        "k,s\nORD-1,Posted\n", fmt, issued_at=SETTLE_AT, now=SETTLE_AT
    )
    assert (done.id, done.state) == (entry.id, "verified")


def test_an_unchecked_second_effect_is_never_awaiting() -> None:
    # The step declared two effects; only the deferred one has evidence.
    step = _save([_evidence(deferred_check="deferred:" + "1" * 32)])
    step.effect_contract_hashes = [HASH, "sha256:" + "b" * 64]
    assert classify_transaction_outcome(_report(step)) is (
        TransactionOutcome.RECONCILIATION_REQUIRED
    )


def test_a_failed_run_is_never_awaiting() -> None:
    report = _report(_save([_evidence(deferred_check="deferred:" + "1" * 32)]))
    report.execution_outcome = "FAILED"
    assert classify_transaction_outcome(report) is (
        TransactionOutcome.RECONCILIATION_REQUIRED
    )


def test_an_entry_for_another_effect_settles_nothing(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    entry = _add(ledger, "CLM-1001")
    ledger.settle(ACK_277CA, FMT_277, now=SETTLE_AT)
    other = "sha256:" + "c" * 64
    report = _report(
        _save(
            [
                _evidence(deferred_check=entry.id).model_copy(
                    update={"effect_contract_hash": other}
                )
            ]
        )
    )
    report.results[0].effect_contract_hashes = [other]
    report.transaction_outcome = "SAVED_AWAITING_CHECK"
    assert settled_outcome(report, ledger, now=SETTLE_AT) == "RECONCILIATION_REQUIRED"


def test_verify_run_refuses_a_deferred_check(tmp_path: Path) -> None:
    from openadapt_flow.verify_run import VerifyRunError, load_verify_config

    config = tmp_path / "verify.yaml"
    config.write_text(
        json.dumps(
            {
                "verifier": {
                    "kind": "deferred_report",
                    "deferred_report": _spec(tmp_path).model_dump(mode="json"),
                },
                "effects": [_claim_effect().model_dump(mode="json")],
            }
        )
    )
    with pytest.raises(VerifyRunError, match="settled later"):
        load_verify_config(config)
