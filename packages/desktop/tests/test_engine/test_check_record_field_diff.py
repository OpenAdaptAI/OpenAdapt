"""A "Check the record" stop shows the fields the record check found different.

The values can identify a patient. They are sealed at rest, shown on the local
screen, and never reach a notification, an emitted event or the hosted lanes.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from openadapt_desktop.outcomes import field_diffs
from openadapt_desktop.portal.notifications import (
    assert_generic_notification,
    build_notification,
)

from .test_work_items import _Bridge, _run, _verified_report, task  # noqa: F401

TYPED = "1949-12-11"
ON_FILE = "1960-01-01"


def _refuted_line(field: str, observed: str, expected: str) -> str:
    return (
        f"[rest] field_equals: REFUTED — field {field!r} is {observed!r}, "
        f"expected {expected!r} (the system of record contradicts the declared outcome)"
    )


def _check_record_report() -> dict:
    return _verified_report(
        transaction="RECONCILIATION_REQUIRED",
        execution="HALTED",
        halt={"state_id": "step_1", "intent": "Click Save", "reason": "Record differs"},
        results=[
            {
                "step_id": "step-1",
                "ok": False,
                "effect_verified": False,
                "effect_results": [_refuted_line("dob", ON_FILE, TYPED)],
            }
        ],
    )


def test_field_diffs_read_flow_verdict_lines() -> None:
    assert field_diffs(_check_record_report()) == [
        {"field": "dob", "typed": TYPED, "on_file": ON_FILE}
    ]


def test_field_diffs_handle_either_quote_style_and_skip_matches() -> None:
    report = {
        "results": [
            {
                "effect_results": [
                    _refuted_line("last_name", "O'Brien", "OBrien"),
                    "[rest] field_equals: CONFIRMED — field 'dob' equals the expected value",
                    "not a verdict line",
                ]
            }
        ]
    }
    assert field_diffs(report) == [
        {"field": "last_name", "typed": "OBrien", "on_file": "O'Brien"}
    ]


def test_structured_field_checks_win_over_lines() -> None:
    report = {
        "results": [
            {
                "effect_field_checks": [
                    {"field": "dob", "expected": TYPED, "observed": ON_FILE, "matched": False},
                    {"field": "mrn", "expected": "M-1", "observed": "M-1", "matched": True},
                ],
                "effect_results": [_refuted_line("zip", "1", "2")],
            }
        ]
    }
    assert field_diffs(report) == [{"field": "dob", "typed": TYPED, "on_file": ON_FILE}]


@pytest.mark.parametrize("report", [{}, {"results": "x"}, {"results": [None, 3]}])
def test_malformed_reports_have_no_diffs(report: dict) -> None:
    assert field_diffs(report) == []


def test_check_record_card_gets_the_diff_locally_and_nowhere_else(task, tmp_path) -> None:  # noqa: F811
    disp, db, events = task
    disp.services._flow_bridge = _Bridge([_check_record_report()])

    result = _run(disp, "M-7001")

    item = result["item"]
    assert item["result"] == "check_record"
    assert item["halt_kind"] == "save_not_confirmed"
    assert item["field_diffs"] == [{"field": "dob", "typed": TYPED, "on_file": ON_FILE}]

    listed = disp.dispatch("list_attention_items", {})
    [card] = [i for i in listed["items"] if i["item_id"] == item["item_id"]]
    assert card["field_diffs"] == item["field_diffs"]

    # Sealed at rest: the database file never holds the values in plain text.
    raw = sqlite3.connect(str(tmp_path / "index.db"))
    try:
        stored = raw.execute(
            "SELECT field_diffs_json FROM work_items WHERE item_id = ?",
            (item["item_id"],),
        ).fetchone()[0]
    finally:
        raw.close()
    assert stored.startswith("enc:v1:")
    for path in tmp_path.glob("index.db*"):
        data = path.read_bytes()
        assert TYPED.encode() not in data and ON_FILE.encode() not in data

    # Never in an emitted event (the tray, overlay and hosted lanes read these).
    assert TYPED not in json.dumps(events) and ON_FILE not in json.dumps(events)

    # Never in a phone or lock-screen notification.
    count = disp.dispatch("get_needs_attention", {})["count"]
    notification = build_notification(count)
    assert_generic_notification(notification)
    assert TYPED not in json.dumps(notification) and ON_FILE not in json.dumps(notification)


def test_wrong_record_stop_keeps_its_side_by_side_and_no_field_diff(task) -> None:  # noqa: F811
    disp, _db, _events = task
    disp.services._flow_bridge = _Bridge(
        [
            _verified_report(
                transaction="HALTED_BEFORE_EFFECT",
                execution="HALTED",
                halt={"state_id": "step_1", "intent": "Click Save", "reason": "Wrong record"},
                results=[
                    {
                        "ok": False,
                        "identity": {"status": "mismatch", "expected": "M-1", "observed": "M-9"},
                    }
                ],
            )
        ]
    )
    item = _run(disp, "M-1")["item"]
    assert item["halt_kind"] == "wrong_record"
    assert item["field_diffs"] == []


def test_older_databases_gain_the_column(tmp_path: Path) -> None:
    from openadapt_desktop.db import IndexDB

    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE work_items (item_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL,"
        " workflow_id TEXT NOT NULL, position INTEGER NOT NULL DEFAULT 0,"
        " label TEXT DEFAULT '', inputs_json TEXT NOT NULL DEFAULT '{}',"
        " inputs_digest TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',"
        " expected TEXT DEFAULT '', observed TEXT DEFAULT '', created_at TEXT NOT NULL)"
    )
    conn.commit()
    conn.close()
    db = IndexDB(path)
    db.initialize()
    try:
        columns = {r["name"] for r in db.conn.execute("PRAGMA table_info(work_items)")}
        assert "field_diffs_json" in columns
    finally:
        db.close()
