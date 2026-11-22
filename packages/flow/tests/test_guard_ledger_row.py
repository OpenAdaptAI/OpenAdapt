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
    assert row["guard_verdict"] == "VERIFIED"
    assert row["verdict_reason"] == "readback_verified"
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
    assert row["guard_verdict"] == "REFUTED"
    assert row["verdict_reason"] == "readback_refuted"
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
        # The guard says VERIFIED and the diff says a field differs: neither
        # is trusted (it was CAUGHT_ERROR, against summary.json's VERIFIED).
        (GuardVerdict.VERIFIED, {"differing": True}, "UNKNOWN"),
        (GuardVerdict.VERIFIED, {"tier": "screen_readback"}, "UNKNOWN"),
        (GuardVerdict.VERIFIED, {"tier": None}, "UNKNOWN"),
        (GuardVerdict.REFUTED, {}, "CAUGHT_ERROR"),
        # Sent, and not confirmed either way: not a person's escalation.
        (GuardVerdict.RECONCILIATION_REQUIRED, {}, "UNKNOWN"),
        (GuardVerdict.RECONCILIATION_REQUIRED, {"differing": True}, "UNKNOWN"),
        (None, {"refusal": "identity_mismatch"}, "CAUGHT_ERROR"),
        # A person said no: escalated, not a caught error (skeptic #16).
        (None, {"refusal": "declined"}, "ESCALATED"),
        (None, {"refusal": "baseline_unreadable"}, "FAILED"),
        (None, {"refusal": "request_expired"}, "ESCALATED"),
        (None, {"refusal": "answer_malformed"}, "FAILED"),
        (None, {"refusal": "approval_already_used"}, "CAUGHT_ERROR"),
        (None, {"refusal": "approval_not_authenticated_person"}, "FAILED"),
        (None, {"refusal": "save_waiting_for_person"}, "CAUGHT_ERROR"),
        (None, {"refusal": "already_saved"}, "CAUGHT_ERROR"),
        (None, {"refusal": "other_origin"}, "CAUGHT_ERROR"),
        (None, {"refusal": "value_not_declared", "scope": True}, "CAUGHT_ERROR"),
        # A code the row can't name is not counted as a catch.
        (None, {"refusal": "something_new"}, "UNKNOWN"),
    ],
)
def test_ledger_verdicts(verdict: Any, kw: dict, expected: str) -> None:
    assert ledger_verdict(verdict, **kw) == expected


def test_the_mapping_matches_the_run_ledger_reasons() -> None:
    """Every reason the row names is the run ledger's word for that bucket,
    except the one contradiction only the row can see."""

    from openadapt_flow.agent_guard.ledger_row import readback_outcome, refusal_outcome

    expected = {
        "readback_verified": "VERIFIED",
        "readback_refuted": "CAUGHT_ERROR",
        "reconciliation_required": "UNKNOWN",
        "screen_readback_only": "UNKNOWN",
        "completed_unverified": "UNKNOWN",
        "save_blocked_wrong_record": "CAUGHT_ERROR",
        "save_blocked_other_origin": "CAUGHT_ERROR",
        "save_blocked_double_save": "CAUGHT_ERROR",
        "save_blocked_out_of_scope": "CAUGHT_ERROR",
        "person_declined": "ESCALATED",
        "approval_declined": "ESCALATED",
        "policy_rejected_no_write": "FAILED",
        "unmapped_outcome": "UNKNOWN",
        "readback_fields_disagree": "UNKNOWN",
    }
    seen = [
        readback_outcome(v, tier=t, differing=d)
        for v in GuardVerdict
        for t in ("independent_readback", "screen_readback", None)
        for d in (False, True)
    ] + [
        refusal_outcome(c, scope=sc)
        for c in (
            "identity_mismatch",
            "other_origin",
            "already_saved",
            "declined",
            "request_expired",
            "baseline_unreadable",
            "x",
        )
        for sc in (False, True)
    ]
    for bucket, reason in seen:
        assert expected[reason] == bucket, reason


def test_a_save_that_did_not_land_is_unknown_and_says_why(sor, tmp_path: Path) -> None:
    """The not-saved run: the guard says RECONCILIATION_REQUIRED and the row
    says the same thing in the ledger's words, never a bare ESCALATED."""

    page = _Page(FakeAgent(sor[0]), "p1", fault="no_persist")
    guard = _guard(sor, tmp_path, page)
    page.save()
    guard.poll()
    assert guard.summary.verdict == "RECONCILIATION_REQUIRED"
    row = json.loads((tmp_path / ROW_FILENAME).read_text())
    assert row["verdict"] == "UNKNOWN"
    assert row["guard_verdict"] == "RECONCILIATION_REQUIRED"
    assert row["verdict_reason"] == "reconciliation_required"


def test_a_guard_with_no_store_and_no_events_file_builds(sor, tmp_path: Path) -> None:
    """J3: a guard stand-in with no ``state_dir`` (P1's shadow hold) builds
    and writes no row; it crashed with AttributeError before."""

    class _NoStore:
        approval = "not_gated"
        approval_fields = False

    guard = CommitGuard(
        _NoStore(),  # type: ignore[arg-type]
        _task(),
        _Page(FakeAgent(sor[0]), "p1"),
        app_origin=sor[0],
    )
    assert guard.ledger_dir is None


def test_a_declined_approval_is_escalated(sor, tmp_path: Path, approver) -> None:
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
    assert row["verdict"] == "ESCALATED"
    assert row["guard_verdict"] is None
    assert row["verdict_reason"] == "person_declined"
    # A person signed the decline: the row says so (it said "none" before).
    assert row["approver_kind"] == "person"
    assert g._approver is not None and g._approver["id"] == "front-desk"
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


def test_a_decline_from_the_phone_card_carries_a_reason(
    sor, tmp_path: Path, approver
) -> None:
    """Item 4 of the e2e findings: ``make_answer`` refuses a decline with no
    ``decline_reason``. The phone card always sends one: it asks why before a
    decline (``openReasons`` in Desktop's ``portal/shell/app.js``) and offers
    only the reasons Flow lists for the save. This drives that path through
    Flow's console with a real guard on MockMed."""

    from openadapt_flow.console import save_approvals

    page = _Page(FakeAgent(sor[0]), "p1")
    guard = _guard(sor, tmp_path, page, approval="each_item")
    page.save()
    assert guard._waiting is not None
    g = guard._waiting[0]
    rid = g._request.request_id
    detail = save_approvals.save_approval_detail(g.dir.parent, rid)
    reasons = detail["presentation"]["decline_reasons"]
    # The card has a reason to send for Don't save, from the closed list.
    assert reasons and set(reasons) <= {
        "wrong_values",
        "wrong_record",
        "screen_problem",
    }

    def payload(**kw: Any) -> Any:
        return save_approvals.SaveApprovalActionRequest(
            action="reject",
            task_digest=detail["task_digest"],
            task_signature=detail["task"]["signature"],
            **kw,
        )

    # A reject with no reason (which the card never sends) changes nothing.
    with pytest.raises(save_approvals.SaveApprovalRefused) as refused:
        save_approvals.decide_save_approval(g.dir.parent, rid, payload())
    assert refused.value.code == "decline_reason_required"
    guard.poll()
    assert guard._waiting is not None

    # What the card sends: reject plus the reason the person tapped.
    save_approvals.decide_save_approval(
        g.dir.parent, rid, payload(decline_reason=reasons[0])
    )
    guard.poll()
    assert guard._waiting is None
    assert [b["code"] for b in guard.summary.blocked] == ["declined"]
    row = json.loads((tmp_path / ROW_FILENAME).read_text())
    assert (row["verdict"], row["verdict_reason"]) == ("ESCALATED", "person_declined")
    assert row["approver_kind"] == "person"


def test_an_observe_only_verified_save_is_verified_in_the_row(
    sor, tmp_path: Path
) -> None:
    """Observe-only takes the tier from the verify-run receipt; its words are
    the same, so a verified observed save is VERIFIED in the row too."""

    page = _Page(FakeAgent(sor[0]), "p1")
    guard = _guard(sor, tmp_path, page, observe_only=True)
    page.save()
    guard.poll()
    assert guard.summary.verdict == "VERIFIED"
    row = json.loads((tmp_path / ROW_FILENAME).read_text())
    assert row["readback_tier"] == "independent_readback"
    assert (row["verdict"], row["guard_verdict"], row["verdict_reason"]) == (
        "VERIFIED",
        "VERIFIED",
        "readback_verified",
    )


def test_no_values_and_no_screenshot_offers_no_card(
    sor, tmp_path: Path, approver
) -> None:
    """Nobody could see what would be approved: the guard asks no one, blocks
    the save, and the row is ESCALATED with values_unread. Nothing is written."""

    class _Blind(_Page):
        def screenshot(self) -> None:
            return None

    page = _Blind(FakeAgent(sor[0]), "p1")
    guard = _guard(sor, tmp_path, page, approval="each_item")
    page.save()
    assert guard._waiting is None
    assert [b["code"] for b in guard.summary.blocked] == ["values_unread"]
    assert page.sent == [] and len(page.blocked) == 1
    assert not list((tmp_path / "state").rglob("item-approval/request-*.json"))
    row = json.loads((tmp_path / ROW_FILENAME).read_text())
    assert (row["verdict"], row["verdict_reason"]) == ("ESCALATED", "values_unread")


def test_a_refused_answer_gives_no_decision_time(sor, tmp_path: Path, approver) -> None:
    """An answer the guard refuses (bound to another request) is not a
    decision: the row has no approval_decided_at and no approver."""

    from openadapt_flow.approval_mode.item_approval import (
        ItemApprovalStore,
        make_answer,
    )
    from openadapt_flow.approval_mode.model import ITEM_APPROVAL_ROUTE

    page = _Page(FakeAgent(sor[0]), "p1")
    guard = _guard(sor, tmp_path, page, approval="each_item")
    page.save()
    g = guard._waiting[0]
    store = ItemApprovalStore(g.dir)
    answer = make_answer(
        store.read_request(g._request.request_id),
        decision="approve",
        decided_by="human",
        route=ITEM_APPROVAL_ROUTE,
        approver_id=None,
        auth_method="none",
        policy=store.context(),
        signer=approver[1],
    )
    store.write_answer(
        answer.model_copy(update={"request_sha256": "sha256:" + "0" * 64})
    )
    guard.poll()
    assert guard.summary.blocked, guard.summary
    row = json.loads((tmp_path / ROW_FILENAME).read_text())
    assert row["approval_opened_at"] == g._request.created_at
    assert row["approval_decided_at"] is None
    assert row["approver_kind"] == "none"
    assert page.sent == []
