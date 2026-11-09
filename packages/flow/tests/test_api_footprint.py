"""The side-effect comparison that qualifies an API step (api_footprint)."""

from __future__ import annotations

import sqlite3

import pytest

from openadapt_flow.api_footprint import diff_dumps, footprint_verdict
from openadapt_flow.compiler.api_proposal import ApiProposalError, admit_api_step
from openadapt_flow.ir import Workflow


def _save(n: int, tag: str, *, audit: bool = True, agent: str = "Chrome") -> dict:
    """One save's changes: a ticket row, and the audit entry the page posts."""

    before = {"ticket": [{"id": 1, "summary": "old", "agent": agent}], "audit": []}
    after = {
        "ticket": before["ticket"]
        + [
            {
                "id": 1 + n,
                "summary": f"Printer jams {tag}",
                "agent": agent,
                "created": f"10:0{n}",
            }
        ],
        "audit": [{"event": "create", "summary": f"Printer jams {tag}"}]
        if audit
        else [],
    }
    return diff_dumps(before, after)


def test_varying_insert_ids_and_times_do_not_prove_server_ownership():
    gui = [_save(1, "g1"), _save(2, "g2")]
    api = [_save(3, "a1"), _save(4, "a2")]
    v = footprint_verdict(gui, api, ["g1", "g2"], ["a1", "a2"])
    assert not v["equivalent"]
    assert v["server_owned"] == []
    assert "ticket.summary" not in v["server_owned"]


def test_a_skipped_audit_entry_is_a_difference():
    gui = [_save(1, "g1"), _save(2, "g2")]
    api = [_save(3, "a1", audit=False), _save(4, "a2", audit=False)]
    v = footprint_verdict(gui, api, ["g1", "g2"], ["a1", "a2"])
    assert not v["equivalent"]
    assert any(
        "audit added 1 row(s) through the GUI and 0" in d for d in v["differences"]
    )


def test_a_field_the_page_would_have_set_otherwise_is_a_difference():
    gui = [_save(1, "g1"), _save(2, "g2")]
    api = [_save(3, "a1", agent="Playwright"), _save(4, "a2", agent="Playwright")]
    v = footprint_verdict(gui, api, ["g1", "g2"], ["a1", "a2"])
    assert not v["equivalent"]
    assert any("differ in agent" in d for d in v["differences"])


def test_one_gui_save_cant_tell_server_owned_columns_apart():
    v = footprint_verdict([_save(1, "g1")], [_save(2, "a1")], ["g1"], ["a1"])
    assert not v["equivalent"]


def test_admission_refuses_an_api_step_with_another_footprint():
    with pytest.raises(ApiProposalError, match="side effects"):
        admit_api_step(
            Workflow(name="w", steps=[]),
            admitted_by="a person",
            footprint={"equivalent": False, "differences": ["audit added 1 row"]},
        )


def _change(row_id, before, after):
    return diff_dumps(
        {"collateral": [{"id": row_id, "counter": before, "status": "old"}]},
        {"collateral": [{"id": row_id, "counter": after, "status": "saved"}]},
    )


def test_unchanged_identity_of_a_changed_row_is_required():
    gui = [_change(17, n, n) for n in (100, 101, 102)]
    api = [_change(29, n, n) for n in (100, 101, 102)]
    verdict = footprint_verdict(
        gui, api, ["gui-unique-tag"] * 3, ["api-unique-tag"] * 3
    )
    assert not verdict["equivalent"]
    assert any("before.id" in d and "after.id" in d for d in verdict["differences"])


def test_varying_business_increment_cannot_be_ignored():
    gui = [_change(17, n, n + 1) for n in (100, 101, 102)]
    api = [_change(17, n, n) for n in (100, 101, 102)]
    verdict = footprint_verdict(
        gui, api, ["gui-unique-tag"] * 3, ["api-unique-tag"] * 3
    )
    assert not verdict["equivalent"]
    assert verdict["server_owned"] == []
    assert any("after.counter" in d for d in verdict["differences"])


def test_matching_varying_business_increment_is_allowed():
    gui = [_change(17, n, n + 1) for n in (100, 101, 102)]
    verdict = footprint_verdict(
        gui, gui, ["gui-unique-tag"] * 3, ["api-unique-tag"] * 3
    )
    assert verdict["equivalent"], verdict["differences"]
    assert verdict["server_owned"] == []


def test_existing_row_identity_and_business_changes_are_both_compared():
    gui = [_change(17, n, n + 1) for n in (100, 101, 102)]
    api = [_change(29, n, n) for n in (100, 101, 102)]
    verdict = footprint_verdict(
        gui,
        api,
        ["gui-unique-tag"] * 3,
        ["api-unique-tag"] * 3,
    )
    assert not verdict["equivalent"]
    assert any(
        "after.counter" in d and "before.id" in d for d in verdict["differences"]
    )


def test_changed_rows_with_equal_new_values_still_compare_prior_states():
    gui = [_change(17, n, 200) for n in (100, 101)]
    api = [_change(17, n + 10, 200) for n in (100, 101)]
    verdict = footprint_verdict(
        gui, api, ["gui-unique-tag"] * 2, ["api-unique-tag"] * 2
    )
    assert not verdict["equivalent"]
    assert any("before.counter" in d for d in verdict["differences"])


# An ``id`` column is not proof of a row key: it can be one part of a
# composite key or be null. Dumps come from real SQLite tables here.


def _sqlite_dump(conn) -> dict:
    conn.row_factory = sqlite3.Row
    return {
        "item": [dict(r) for r in conn.execute("SELECT * FROM item ORDER BY rowid")]
    }


def _composite_save(update: str | None, *, nullable: bool = False) -> dict:
    """Changes of one save on a table whose ``id`` does not identify a row."""

    conn = sqlite3.connect(":memory:")
    if nullable:
        conn.execute("CREATE TABLE item (tenant TEXT, id INTEGER, status TEXT)")
        rows = [("A", None, "new"), ("B", None, "new"), ("C", None, "new")]
    else:
        conn.execute(
            "CREATE TABLE item (tenant TEXT, id INTEGER, status TEXT,"
            " PRIMARY KEY (tenant, id))"
        )
        rows = [("A", 7, "new"), ("B", 7, "new"), ("C", 7, "new")]
    conn.executemany("INSERT INTO item VALUES (?, ?, ?)", rows)
    before = _sqlite_dump(conn)
    if update is not None:
        conn.execute("UPDATE item SET status = 'saved' WHERE tenant = ?", (update,))
    after = _sqlite_dump(conn)
    return diff_dumps(before, after)


@pytest.mark.parametrize("nullable", [False, True], ids=["composite", "null_id"])
@pytest.mark.parametrize("api_update", [None, "B"], ids=["omitted", "wrong_row"])
def test_a_non_unique_id_column_does_not_hide_writes(nullable, api_update):
    gui = [_composite_save("A", nullable=nullable) for _ in range(2)]
    api = [_composite_save(api_update, nullable=nullable) for _ in range(2)]
    assert gui[0], "the GUI save's write must be visible in the diff"
    verdict = footprint_verdict(gui, api, ["g1", "g2"], ["a1", "a2"])
    assert not verdict["equivalent"]


@pytest.mark.parametrize("nullable", [False, True], ids=["composite", "null_id"])
def test_a_non_unique_id_column_still_admits_the_same_write(nullable):
    gui = [_composite_save("A", nullable=nullable) for _ in range(2)]
    api = [_composite_save("A", nullable=nullable) for _ in range(2)]
    verdict = footprint_verdict(gui, api, ["g1", "g2"], ["a1", "a2"])
    assert verdict["equivalent"], verdict["differences"]


def test_duplicate_whole_rows_keep_their_multiplicity():
    row = {"id": 7, "status": "new"}
    before = {"item": [row, row]}
    gui = diff_dumps(before, {"item": [row, row, row]})
    api = diff_dumps(before, {"item": [row, row]})
    assert gui == {"item": {"added": [row], "removed": [], "changed": []}}
    assert api == {}
