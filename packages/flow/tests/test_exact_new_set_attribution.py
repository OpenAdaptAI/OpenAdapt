"""``exact_new_set`` sets aside a record the frame attributes to another user.

A colleague registering a different patient in the same window is not this
save's addition. Only an addition the audit log names another actor for is
set aside; an unattributed one still refutes. Records are made up.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from openadapt_flow.verify_run import capture_baseline, parse_verify_config, verify_run


def _db(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE patient (id INTEGER PRIMARY KEY, pid INTEGER, lname TEXT, email TEXT)"
    )
    con.execute(
        "CREATE TABLE log (id INTEGER PRIMARY KEY, user TEXT, patient_id INTEGER, "
        "event TEXT, tbl TEXT)"
    )
    con.execute("CREATE TABLE history (id INTEGER PRIMARY KEY, pid INTEGER)")
    con.execute("INSERT INTO patient VALUES (1, 1, 'Old', 'old@example.test')")
    con.commit()
    return con


def _run(
    tmp_path: Path,
    colleague_logged: bool,
    *,
    colleague_event: str = "insert",
    bot_extra: bool = False,
    insert_events: bool = True,
    colleague_table: str = "patient",
    bot_name: str = "bot",
    views: dict[str, str] | None = None,
    extra: tuple[tuple[str, tuple], ...] = (),
    surface_query: str = "SELECT * FROM patient",
    separate_surface: bool = False,
    separate_audit: bool = False,
    routed: bool = False,
    frame_identity: str = "pid",
    max_rows: int = 2000,
    clone_surface: bool = False,
    view_surface: bool = False,
) -> str:
    tmp_path.mkdir(exist_ok=True)
    db = tmp_path / "s.db"
    con = _db(db)
    if clone_surface:
        con.execute("CREATE TABLE mirror AS SELECT * FROM patient")
        con.commit()
        surface_query = "SELECT * FROM mirror"
    if view_surface:
        con.execute("CREATE VIEW patient_view AS SELECT * FROM patient")
        con.commit()
        surface_query = "SELECT * FROM patient_view"
    surface_db = tmp_path / "surface.db" if separate_surface else db
    audit_db = tmp_path / "audit.db" if separate_audit else db
    for other in {surface_db, audit_db} - {db}:
        _db(other).close()
    cfg = parse_verify_config(
        {
            "schema_version": "openadapt.verify/v1",
            "verifier": {
                "kind": "sql",
                "sqlite_database": str(surface_db),
                "sql_query": surface_query,
            },
            "record": {
                "name": "patient",
                "match": ["email"],
                "exact_new_set": True,
                "fields": {"email": "email", "lname": "lname"},
            },
            "frame": {
                "identity_field": frame_identity,
                "attribution": {
                    "kind": "audit_read",
                    "actor_field": "user",
                    "record_field": "patient_id",
                    "self": ["bot"],
                    "event_field": "event",
                    "write_events": ["insert", "update"],
                    "insert_events": ["insert"] if insert_events else [],
                    "table_field": "tbl",
                    "record_key": "pid",
                    "read": {
                        "kind": "sql",
                        "sqlite_database": str(audit_db),
                        "sql_query": "SELECT * FROM log",
                    },
                },
                # The whole database, so an insert entry names its table.
                "database": {
                    "dialect": "sqlite",
                    "read": {"sqlite_database": str(db)},
                    "allow_tables": ["log", "mirror"] if clone_surface else ["log"],
                    "views": views if views is not None else {"patient": ""},
                    "new_row_fields": {"patient": ["pid"]},
                    "max_rows": max_rows,
                },
            },
        }
    )
    if routed:
        doc = cfg.model_dump(mode="json")
        doc["verifier"] = {"kind": "surfaces", "surfaces": {"charts": doc["verifier"]}}
        for effect in doc["effects"]:
            effect["probe"] = "surface=charts"
        doc["frame"]["database"]["views"] = {
            table: "charts" for table in doc["frame"]["database"]["views"]
        }
        cfg = parse_verify_config(doc)
    params = {"email": "new@example.test", "lname": "New"}
    capture_baseline(cfg, params, tmp_path / "b.json")
    con.execute("INSERT INTO patient VALUES (2, 2, 'New', 'new@example.test')")
    con.execute(
        "INSERT INTO log (user, patient_id, event, tbl) "
        "VALUES ('bot', 2, 'insert', 'patient')"
    )
    # Patient 3: a colleague's new chart, or the bot's own extra record.
    con.execute("INSERT INTO patient VALUES (3, 3, 'Colleague', 'c@example.test')")
    if bot_extra:
        con.execute(
            "INSERT INTO log (user, patient_id, event, tbl) "
            "VALUES (?, 3, 'insert', 'patient')",
            (bot_name,),
        )
    if colleague_logged:
        con.execute(
            "INSERT INTO log (user, patient_id, event, tbl) VALUES ('clinic2', 3, ?, ?)",
            (colleague_event, colleague_table),
        )
    for sql, args in extra:
        con.execute(sql, args)
    if clone_surface:
        con.execute("INSERT INTO mirror SELECT * FROM patient WHERE id != 1")
    con.commit()
    for other in {surface_db, audit_db} - {db}:
        with sqlite3.connect(other) as copy:
            con.backup(copy)
            if other == surface_db:
                copy.execute("UPDATE log SET user = 'bot' WHERE user = 'clinic2'")
    con.close()
    receipt = verify_run(
        cfg,
        params=params,
        run_record={"executor_kind": "agent", "executor_id": "bot"},
        authorization_ref="t",
        baseline_path=tmp_path / "b.json",
    )["receipt"]
    return receipt["outcome"]


def test_a_colleagues_new_patient_does_not_refute_the_save(tmp_path: Path) -> None:
    assert _run(tmp_path, colleague_logged=True) == "verified"


def test_an_unattributed_new_record_still_refutes(tmp_path: Path) -> None:
    assert _run(tmp_path, colleague_logged=False) == "reconciliation_required"


def test_only_an_insert_event_says_who_added_a_record(tmp_path: Path) -> None:
    """Adversarial review of #1571: a colleague's later edit of the bot's
    extra record, or a log without insert events, never sets it aside."""

    assert (
        _run(tmp_path / "a", colleague_logged=True, colleague_event="update")
        == "reconciliation_required"
    )
    assert (
        _run(tmp_path / "b", colleague_logged=True, insert_events=False)
        == "reconciliation_required"
    )
    assert (
        _run(tmp_path / "c", colleague_logged=True, bot_extra=True)
        == "reconciliation_required"
    )


def test_an_insert_into_another_table_or_by_the_bot_under_another_case_still_refutes(
    tmp_path: Path,
) -> None:
    """Re-review of #1571: a colleague's insert into another table, or the
    bot's own insert logged as "Bot", never sets the bot's record aside."""

    assert (
        _run(tmp_path / "a", colleague_logged=True, colleague_table="rx")
        == "reconciliation_required"
    )
    for i, name in enumerate(("Bot", "bot ")):
        assert (
            _run(
                tmp_path / f"case{i}",
                colleague_logged=True,
                bot_extra=True,
                bot_name=name,
            )
            == "reconciliation_required"
        )


def test_a_blank_actor_is_nobody(tmp_path: Path) -> None:
    """Round 3 of #1571: an entry whose user is only spaces names no one, so
    the bot's extra record it logs is unattributed and still refutes."""

    assert (
        _run(tmp_path, colleague_logged=False, bot_extra=True, bot_name="  ")
        == "reconciliation_required"
    )


def test_an_addition_in_another_table_of_the_surface_excuses_nothing(
    tmp_path: Path,
) -> None:
    """Round 3 of #1571: with two tables behind one surface, a colleague's
    new history row for pid 3 never sets aside the bot's patient 3."""

    assert (
        _run(
            tmp_path,
            colleague_logged=True,
            colleague_table="history",
            views={"patient": "", "history": ""},
            extra=(
                ("INSERT INTO history VALUES (1, 3)", ()),
                (
                    "INSERT INTO log (user, patient_id, event, tbl) "
                    "VALUES ('clinic2', 3, 'insert', 'history')",
                    (),
                ),
            ),
        )
        == "reconciliation_required"
    )


def test_two_colleague_entries_never_explain_the_bots_record_too(
    tmp_path: Path,
) -> None:
    """Round 3 of #1571: the bot's extra record under the colleague's pid,
    with two colleague insert entries for that pid, still refutes."""

    assert (
        _run(
            tmp_path,
            colleague_logged=True,
            extra=(
                ("INSERT INTO patient VALUES (4, 3, 'Extra', 'x@example.test')", ()),
                (
                    "INSERT INTO log (user, patient_id, event, tbl) "
                    "VALUES ('clinic2', 3, 'insert', 'patient')",
                    (),
                ),
            ),
        )
        == "reconciliation_required"
    )


def test_a_filtered_duplicate_identity_cannot_borrow_an_insert(tmp_path: Path) -> None:
    assert (
        _run(
            tmp_path,
            colleague_logged=True,
            surface_query="SELECT * FROM patient WHERE lname != 'Colleague'",
            extra=(
                ("INSERT INTO patient VALUES (4, 3, 'Extra', 'x@example.test')", ()),
                (
                    "INSERT INTO log (user, patient_id, event, tbl) "
                    "VALUES ('clinic2', 3, 'insert', 'patient')",
                    (),
                ),
            ),
        )
        == "reconciliation_required"
    )


def test_an_existing_hidden_row_also_makes_the_audit_identity_ambiguous(
    tmp_path: Path,
) -> None:
    assert (
        _run(
            tmp_path,
            colleague_logged=True,
            surface_query="SELECT * FROM patient WHERE lname != 'Old'",
            extra=(
                ("UPDATE patient SET pid = 1 WHERE id = 3", ()),
                ("UPDATE log SET patient_id = 1 WHERE user = 'clinic2'", ()),
            ),
        )
        == "reconciliation_required"
    )


def test_identical_rows_in_a_different_source_are_not_a_witness(tmp_path: Path) -> None:
    for name in ("separate_surface", "separate_audit"):
        assert (
            _run(tmp_path / name, colleague_logged=True, **{name: True})
            == "reconciliation_required"
        )


def test_a_surface_without_the_physical_key_cannot_set_a_row_aside(
    tmp_path: Path,
) -> None:
    assert (
        _run(
            tmp_path,
            colleague_logged=True,
            surface_query="SELECT pid, lname, email FROM patient",
        )
        == "reconciliation_required"
    )


def test_a_filtered_unique_physical_row_still_has_a_witness(tmp_path: Path) -> None:
    assert (
        _run(
            tmp_path,
            colleague_logged=True,
            surface_query="SELECT * FROM patient WHERE lname != 'Old'",
        )
        == "verified"
    )


def test_a_named_surface_uses_its_own_physical_row_witness(tmp_path: Path) -> None:
    assert _run(tmp_path / "same", colleague_logged=True, routed=True) == "verified"
    assert (
        _run(
            tmp_path / "different",
            colleague_logged=True,
            routed=True,
            separate_surface=True,
        )
        == "reconciliation_required"
    )


def test_a_unique_frame_key_does_not_make_the_audit_key_unique(tmp_path: Path) -> None:
    assert (
        _run(
            tmp_path,
            colleague_logged=True,
            frame_identity="id",
            max_rows=1,
            surface_query="SELECT * FROM patient WHERE lname != 'Colleague'",
            extra=(
                ("INSERT INTO patient VALUES (4, 3, 'Extra', 'x@example.test')", ()),
                (
                    "INSERT INTO log (user, patient_id, event, tbl) "
                    "VALUES ('clinic2', 3, 'insert', 'patient')",
                    (),
                ),
            ),
        )
        == "reconciliation_required"
    )


def test_a_surface_row_must_match_the_witnessed_contents(tmp_path: Path) -> None:
    assert (
        _run(
            tmp_path,
            colleague_logged=True,
            surface_query=(
                "SELECT id, pid, CASE WHEN id = 3 THEN 'Extra' ELSE lname END AS lname, "
                "email FROM patient"
            ),
        )
        == "reconciliation_required"
    )


def test_an_identical_clone_table_is_not_the_witnessed_physical_source(
    tmp_path: Path,
) -> None:
    """The audit names patient; the surface reads an append-only clone."""

    assert (
        _run(tmp_path, colleague_logged=True, clone_surface=True)
        == "reconciliation_required"
    )


def test_unknown_query_provenance_never_sets_a_record_aside(tmp_path: Path) -> None:
    queries = (
        "SELECT * FROM patient AS p",
        "SELECT patient.* FROM patient JOIN (SELECT 1) AS marker ON 1 = 1",
        "WITH p AS (SELECT * FROM patient) SELECT * FROM p",
        "SELECT id, pid, lname, email FROM patient",
    )
    for i, query in enumerate(queries):
        assert (
            _run(tmp_path / f"query{i}", colleague_logged=True, surface_query=query)
            == "reconciliation_required"
        )
    assert (
        _run(tmp_path / "view", colleague_logged=True, view_surface=True)
        == "reconciliation_required"
    )
