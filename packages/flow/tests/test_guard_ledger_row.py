"""The guard's per-save ledger row holds no values; its private diff does.

Synthetic MockMed fault server on localhost; no browser, no model calls.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

from benchmark.harness.guard_demo import NOTE, FakeAgent, person_approves, verify_yaml
from openadapt_flow.agent_guard.commit_guard import CommitGuard
from openadapt_flow.agent_guard.ledger_row import (
    DIFF_FILENAME,
    ROW_FILENAME,
    ROW_KEYS,
    ROW_SCHEMA,
    SCRUBBED_FILENAME,
    export_scrubbed,
    ledger_verdict,
)
from openadapt_flow.guard import GuardedSave, GuardTask, GuardVerdict
from openadapt_flow.mockmed.fault_server import serve as fault_serve
from openadapt_flow.verify_run import parse_verify_config
from tests.test_agent_guard import FakePage

OTHER = "Referral to cardiology for chest pain"
#: Every value the fixture puts in a record or a task.
FIXTURE_VALUES = (NOTE, OTHER, "Referral to orthopedics", "cardiology")


@pytest.fixture
def sor():
    url, db, stop = fault_serve()
    try:
        yield url.rstrip("/"), db
    finally:
        stop()


def _task() -> GuardTask:
    return GuardTask(
        task_id="task-ledger-0001",
        expected_record={"patient_id": "p1"},
        intended_fields={"note": NOTE},
    )


class _Page(FakePage):
    """Saves ``note`` instead of the task's when it is set."""

    note: str = NOTE

    def continue_request(self, request_id: str) -> None:
        self.sent.append(request_id)
        held = self.pending.pop(request_id, None)
        if held is not None and held.method == "POST":
            self.agent.click_save(self.patient, self.note, fault=self.fault)
            assert self.guard is not None
            self.guard.on_request_done(held.network_id)


def _guard(sor, tmp_path: Path, page: FakePage, **kw: Any) -> CommitGuard:
    approval = kw.pop("approval", "not_gated")
    guarded = GuardedSave(
        parse_verify_config(verify_yaml(sor[0])), tmp_path / "state", approval=approval
    )
    guard = CommitGuard(
        guarded,
        _task(),
        page,
        app_origin=sor[0],
        events_path=tmp_path / "events.jsonl",
        **kw,
    )
    page.guard = guard
    return guard


def _no_values(path: Path) -> None:
    data = path.read_bytes()
    for value in FIXTURE_VALUES:
        assert value.encode("utf-8") not in data, value
    assert b"p1" not in data


def test_a_verified_save_writes_a_row_without_values(sor, tmp_path: Path) -> None:
    page = _Page(FakeAgent(sor[0]), "p1")
    guard = _guard(sor, tmp_path, page)
    page.save()
    guard.poll()
    row = json.loads((tmp_path / ROW_FILENAME).read_text())
    assert set(row) == set(ROW_KEYS) and row["schema"] == ROW_SCHEMA
    assert row["verdict"] == "VERIFIED"
    assert row["fields_checked"] == ["note"] and row["fields_differing"] == []
    assert row["readback_tier"] == "independent_readback"
    assert row["approver_kind"] == "none" and row["approval_opened_at"] is None
    assert row["diff_path"] == DIFF_FILENAME and row["hold_ms"] is not None
    _no_values(tmp_path / ROW_FILENAME)
    diff = tmp_path / DIFF_FILENAME
    assert diff.stat().st_mode & 0o777 == 0o600
    [field] = json.loads(diff.read_text())["fields"]
    assert field == {
        "field": "note",
        "intended": NOTE,
        "before": None,
        "saved": NOTE,
        "status": "same",
    }


def test_a_planted_wrong_value_differs_and_is_caught(sor, tmp_path: Path) -> None:
    page = _Page(FakeAgent(sor[0]), "p1")
    page.note = OTHER
    guard = _guard(sor, tmp_path, page)
    page.save()
    guard.poll()
    assert guard.summary.verdict == "REFUTED"
    row = json.loads((tmp_path / ROW_FILENAME).read_text())
    assert row["verdict"] == "CAUGHT_ERROR"
    assert row["fields_differing"] == ["note"]
    _no_values(tmp_path / ROW_FILENAME)
    [field] = json.loads((tmp_path / DIFF_FILENAME).read_text())["fields"]
    assert (field["intended"], field["saved"], field["status"]) == (
        NOTE,
        OTHER,
        "differs",
    )
    assert (tmp_path / DIFF_FILENAME).stat().st_mode & 0o777 == 0o600


def test_a_refusal_writes_a_row_with_no_diff(sor, tmp_path: Path) -> None:
    page = _Page(FakeAgent(sor[0]), "p2")
    _guard(sor, tmp_path, page)
    page.save()
    row = json.loads((tmp_path / ROW_FILENAME).read_text())
    assert row["verdict"] == "CAUGHT_ERROR"
    assert row["readback_tier"] is None and row["diff_path"] is None
    assert row["fields_checked"] == []
    assert not (tmp_path / DIFF_FILENAME).exists()
    _no_values(tmp_path / ROW_FILENAME)


def test_an_approved_save_records_who_and_when(sor, tmp_path: Path, approver) -> None:
    page = _Page(FakeAgent(sor[0]), "p1")
    guard = _guard(sor, tmp_path, page, approval="each_item")
    page.save()
    waiting = guard._waiting
    assert waiting is not None
    g = waiting[0]
    person_approves(
        str(g.dir), g._request.request_id, g._request.digest(), signer=approver[1]
    )
    guard.poll()
    guard.poll()
    assert guard.summary.verdict == "VERIFIED"
    row = json.loads((tmp_path / ROW_FILENAME).read_text())
    assert row["approver_kind"] == "person"
    assert row["approval_opened_at"] == g._request.created_at
    assert row["approval_decided_at"] is not None
    _no_values(tmp_path / ROW_FILENAME)


def test_no_ledger_row_when_turned_off(sor, tmp_path: Path) -> None:
    page = _Page(FakeAgent(sor[0]), "p1")
    guard = _guard(sor, tmp_path, page, ledger_row=False)
    page.save()
    guard.poll()
    assert guard.summary.verdict == "VERIFIED"
    assert not (tmp_path / ROW_FILENAME).exists()
    assert not (tmp_path / DIFF_FILENAME).exists()


def test_redact_report_exports_a_scrubbed_diff(sor, tmp_path: Path) -> None:
    from openadapt_flow.cli_redact_report import _cmd_redact_report

    page = _Page(FakeAgent(sor[0]), "p1")
    page.note = OTHER
    guard = _guard(sor, tmp_path, page)
    page.save()
    guard.poll()
    code = _cmd_redact_report(
        argparse.Namespace(run_dir=str(tmp_path), policy=None, json=False)
    )
    assert code == 0
    scrubbed = tmp_path / SCRUBBED_FILENAME
    _no_values(scrubbed)
    [field] = json.loads(scrubbed.read_text())["fields"]
    assert field["status"] == "differs"
    assert field["intended"] != field["saved"]
    assert field["intended"].startswith("salted:")
    assert export_scrubbed(tmp_path / "nowhere") is None


@pytest.mark.parametrize(
    ("verdict", "kw", "expected"),
    [
        (GuardVerdict.VERIFIED, {}, "VERIFIED"),
        (GuardVerdict.VERIFIED, {"differing": True}, "CAUGHT_ERROR"),
        (GuardVerdict.REFUTED, {}, "CAUGHT_ERROR"),
        (GuardVerdict.RECONCILIATION_REQUIRED, {}, "ESCALATED"),
        (None, {"refusal": "identity_mismatch"}, "CAUGHT_ERROR"),
        (None, {"refusal": "declined"}, "CAUGHT_ERROR"),
        (None, {"refusal": "baseline_unreadable"}, "FAILED"),
        (None, {"refusal": "request_expired"}, "ESCALATED"),
        (None, {"refusal": "answer_malformed"}, "FAILED"),
        (None, {"refusal": "approval_already_used"}, "FAILED"),
        (None, {"refusal": "save_waiting_for_person"}, "CAUGHT_ERROR"),
        (None, {"refusal": "already_saved"}, "CAUGHT_ERROR"),
        (None, {"refusal": "other_origin"}, "CAUGHT_ERROR"),
    ],
)
def test_ledger_verdicts(verdict: Any, kw: dict, expected: str) -> None:
    assert ledger_verdict(verdict, **kw) == expected


def test_a_declined_approval_is_caught(sor, tmp_path: Path, approver) -> None:
    from openadapt_flow.approval_mode.item_approval import (
        ItemApprovalStore,
        make_answer,
    )
    from openadapt_flow.approval_mode.model import ITEM_APPROVAL_ROUTE

    page = _Page(FakeAgent(sor[0]), "p1")
    guard = _guard(sor, tmp_path, page, approval="each_item")
    page.save()
    assert guard._waiting is not None
    g = guard._waiting[0]
    store = ItemApprovalStore(g.dir)
    store.write_answer(
        make_answer(
            store.read_request(g._request.request_id),
            decision="decline",
            decline_reason="wrong_values",
            decided_by="human",
            route=ITEM_APPROVAL_ROUTE,
            approver_id=None,
            auth_method="none",
            policy=store.context(),
            signer=approver[1],
        )
    )
    guard.poll()
    row = json.loads((tmp_path / ROW_FILENAME).read_text())
    assert row["verdict"] == "CAUGHT_ERROR"
    assert row["approver_kind"] == "none"
    assert row["approval_opened_at"] == g._request.created_at
    assert row["approval_decided_at"] is not None
    assert not (tmp_path / DIFF_FILENAME).exists()
    _no_values(tmp_path / ROW_FILENAME)


def test_blocked_then_saved_right_keeps_the_read_back(sor, tmp_path: Path) -> None:
    page = _Page(FakeAgent(sor[0]), "p2")
    guard = _guard(sor, tmp_path, page)
    page.save()  # the wrong record on screen: blocked
    assert json.loads((tmp_path / ROW_FILENAME).read_text())["verdict"] == (
        "CAUGHT_ERROR"
    )
    page.patient = "p1"
    page.save()
    guard.poll()
    row = json.loads((tmp_path / ROW_FILENAME).read_text())
    assert row["verdict"] == "VERIFIED" and row["diff_path"] == DIFF_FILENAME


def test_a_refused_second_save_keeps_the_verified_row(sor, tmp_path: Path) -> None:
    page = _Page(FakeAgent(sor[0]), "p1")
    guard = _guard(sor, tmp_path, page)
    page.save()
    guard.poll()
    assert guard.summary.verdict == "VERIFIED"
    page.save()  # a second save of a verified task: blocked
    assert guard.summary.blocked
    row = json.loads((tmp_path / ROW_FILENAME).read_text())
    assert row["verdict"] == "VERIFIED" and row["diff_path"] == DIFF_FILENAME
    assert (tmp_path / DIFF_FILENAME).exists()
