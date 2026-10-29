"""Owner-scoped attribution: another user's change to their own row.

A row of a per-user table (OpenEMR's ``user_settings``, owned by
``setting_user``) names no patient, so no audit entry can name it by record.
Before this, another signed-in user changing their own UI setting while the
bot saved a chart was "no actor explains" and the save was stopped. Now the
frame attributes such a change to that user when their own write entry in the
window names the table and the row is theirs. Nothing else is excused: a row
of someone else, a row the bot may have written, a table with no declared
owner, a change with no entry, and an owner that can't be resolved all still
refute. Records are made up; the database is SQLite with OpenEMR-like tables.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Optional

import pytest

from openadapt_flow.app_profiles import load_profile
from openadapt_flow.verify_run import capture_baseline, parse_verify_config, verify_run

SCHEMA = """
CREATE TABLE patient_data (id INTEGER PRIMARY KEY, pid INTEGER, fname TEXT,
  lname TEXT, email TEXT, uuid TEXT);
CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, uuid TEXT,
  last_updated TEXT);
CREATE TABLE users_secure (id INTEGER PRIMARY KEY, username TEXT, password TEXT);
CREATE TABLE user_settings (setting_user INTEGER, setting_label TEXT,
  setting_value TEXT, PRIMARY KEY (setting_user, setting_label));
CREATE TABLE user_prefs (owner_name TEXT, pref TEXT, value TEXT,
  PRIMARY KEY (owner_name, pref));
CREATE TABLE user_notes (id INTEGER PRIMARY KEY, owner_name TEXT, body TEXT);
CREATE TABLE keys (id INTEGER PRIMARY KEY, name TEXT, value TEXT);
CREATE TABLE log (id INTEGER PRIMARY KEY, user TEXT, patient_id INTEGER,
  event TEXT, tbl TEXT);
INSERT INTO patient_data VALUES (1, 1, 'Wendell', 'Prior', 'w@example.test', 'u-1');
INSERT INTO users VALUES (1, 'clinic2', 'uu-1', '2026-01-01 00:00:00');
INSERT INTO users VALUES (2, 'bot', 'uu-2', '2026-01-01 00:00:00');
INSERT INTO users VALUES (3, 'clinic3', 'uu-3', '2026-01-01 00:00:00');
INSERT INTO users_secure VALUES (1, 'clinic2', 'hash1');
INSERT INTO user_settings VALUES (1, 'gacl_protect', '1');
INSERT INTO user_settings VALUES (3, 'gacl_protect', '1');
INSERT INTO user_prefs VALUES ('clinic2', 'theme', 'light');
INSERT INTO user_notes VALUES (1, 'clinic2', 'call back');
INSERT INTO keys VALUES (1, 'oauth2key', 'k1');
"""
NEW = {"fname": "Ada", "lname": "Quill", "email": "ada@example.test"}


def _doc(db: Path, audit_db: Optional[Path] = None, **attribution: Any) -> dict:
    profile = load_profile("openemr")
    return {
        "schema_version": "openadapt.verify/v1",
        "verifier": {
            "kind": "sql",
            "sqlite_database": str(db),
            "sql_query": "SELECT * FROM patient_data",
        },
        "record": {
            "name": "patient",
            "match": ["email", "lname"],
            "fields": {"fname": "fname", "lname": "lname", "email": "email"},
        },
        "frame": {
            "identity_field": "id",
            "attribution": {
                "kind": "audit_read",
                "actor_field": "user",
                "self": ["bot"],
                "record_field": "patient_id",
                "record_key": "pid",
                "event_field": "event",
                "write_events": ["insert", "update", "delete"],
                "insert_events": ["insert"],
                "delete_events": ["delete"],
                "table_field": "tbl",
                "read": {
                    "kind": "sql",
                    "sqlite_database": str(audit_db or db),
                    "sql_query": "SELECT * FROM log",
                },
                **attribution,
            },
            "database": {
                "dialect": "sqlite",
                "read": {"sqlite_database": str(db)},
                "profile": "openemr",
                "profile_sha256": profile.sha256(),
                "views": {"patient_data": ""},
                "new_row_fields": {"patient_data": ["pid", "uuid"]},
                "settle_s": 0,
            },
        },
    }


def _run(
    tmp_path: Path,
    *sql: str,
    audit_db: Optional[Path] = None,
    **attribution: Any,
) -> dict:
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = tmp_path / "app.sqlite"
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    con.commit()
    con.close()
    cfg = parse_verify_config(_doc(db, audit_db, **attribution))
    capture_baseline(cfg, NEW, tmp_path / "baseline.json")
    con = sqlite3.connect(db)
    # The bot's save: a new chart, logged under it.
    con.execute(
        "INSERT INTO patient_data (pid, fname, lname, email, uuid) "
        "VALUES (2, 'Ada', 'Quill', 'ada@example.test', 'u-2')"
    )
    con.execute(
        "INSERT INTO log (user, patient_id, event, tbl) "
        "VALUES ('bot', 2, 'insert', 'patient_data')"
    )
    for statement in sql:
        con.execute(statement)
    con.commit()
    con.close()
    return verify_run(
        cfg,
        params=NEW,
        run_record={"executor_kind": "agent", "executor_id": "bot"},
        authorization_ref="test",
        baseline_path=tmp_path / "baseline.json",
    )


def _frame(result: dict) -> tuple[str, int]:
    return (
        result["receipt"]["checked"]["frame"],
        result["evidence"]["frame"]["concurrent_count"],
    )


def _entry(user: str, event: str, table: str = "user_settings") -> str:
    return (
        "INSERT INTO log (user, patient_id, event, tbl) "
        f"VALUES ('{user}', NULL, '{event}', '{table}')"
    )


CHANGE_OWN = "UPDATE user_settings SET setting_value = '0' WHERE setting_user = 1"


@pytest.mark.parametrize(
    "sql",
    [
        # Changed, added and removed: each by the matching kind of entry.
        (_entry("clinic2", "update"), CHANGE_OWN),
        (
            _entry("clinic2", "insert"),
            "INSERT INTO user_settings VALUES (1, 'dem_expand', '1')",
        ),
        (
            _entry("clinic2", "delete"),
            "DELETE FROM user_settings WHERE setting_user = 1",
        ),
    ],
)
def test_a_users_change_to_their_own_row_is_theirs(
    tmp_path: Path, sql: tuple[str, ...]
) -> None:
    assert _frame(_run(tmp_path, *sql)) == ("held", 1)


@pytest.mark.parametrize(
    "sql",
    [
        # Their entry, another user's row.
        (
            _entry("clinic2", "update"),
            "UPDATE user_settings SET setting_value = '0' WHERE setting_user = 3",
        ),
        # The bot changed their row: its own entry for the table is in the
        # window, so any row of the table may be its change, theirs too.
        (_entry("clinic2", "update"), _entry("bot", "update"), CHANGE_OWN),
        (_entry("bot", "update"), CHANGE_OWN),
        # Even when the app files the bot's entry under the chart it saved.
        (
            _entry("clinic2", "update"),
            "INSERT INTO log (user, patient_id, event, tbl) "
            "VALUES ('bot', 2, 'update', 'user_settings')",
            CHANGE_OWN,
        ),
        # A table with no declared owner, though they wrote it.
        (_entry("clinic2", "update", "keys"), "UPDATE keys SET value = 'k2'"),
        # Credentials never widen: users_secure's owner is a column rule's,
        # for the acting user's sign-in counters only.
        (
            _entry("clinic2", "update", "users_secure"),
            "UPDATE users_secure SET password = 'x' WHERE username = 'clinic2'",
        ),
        # No entry.
        (CHANGE_OWN,),
        # An entry for another table, or a view of this one.
        (_entry("clinic2", "update", "patient_data"), CHANGE_OWN),
        (_entry("clinic2", "view"), CHANGE_OWN),
        # The wrong kind of write: an update never explains an added row.
        (
            _entry("clinic2", "update"),
            "INSERT INTO user_settings VALUES (1, 'dem_expand', '1')",
        ),
        # One entry explains one row.
        (
            _entry("clinic2", "insert"),
            "INSERT INTO user_settings VALUES (1, 'dem_expand', '1')",
            "INSERT INTO user_settings VALUES (1, 'fax_expand', '1')",
        ),
        # Unresolvable owner: an actor the users table doesn't know, and a
        # row with no owner.
        (_entry("ghost", "update"), CHANGE_OWN),
        (
            _entry("clinic2", "insert"),
            "INSERT INTO user_settings VALUES (NULL, 'dem_expand', '1')",
        ),
    ],
)
def test_nothing_else_is_excused(tmp_path: Path, sql: tuple[str, ...]) -> None:
    assert _frame(_run(tmp_path, *sql))[0] == "refuted", sql


def test_a_row_moved_to_another_user_is_not_excused(tmp_path: Path) -> None:
    # setting_user is part of the key: moving the row removes one and adds
    # another, and an update explains neither.
    moved = (
        "UPDATE user_settings SET setting_label = 'moved', setting_user = 3 "
        "WHERE setting_user = 1"
    )
    assert _frame(_run(tmp_path / "a", _entry("clinic2", "update"), moved))[0] == (
        "refuted"
    )
    # An owner column outside the key: the row keeps its key and changes
    # hands. Neither user's entry explains that, whoever wrote it.
    owners = {"user_notes": {"column": "owner_name", "by": "username"}}
    handed = "UPDATE user_notes SET owner_name = 'clinic3' WHERE id = 1"
    for i, actor in enumerate(("clinic2", "clinic3")):
        entry = _entry(actor, "update", "user_notes")
        result = _run(tmp_path / f"b{i}", entry, handed, owners=owners)
        assert _frame(result)[0] == "refuted", actor


def test_a_contract_can_declare_a_tables_owner(tmp_path: Path) -> None:
    change = "UPDATE user_prefs SET value = 'dark' WHERE owner_name = 'clinic2'"
    owners = {"user_prefs": {"column": "owner_name", "by": "username"}}
    entry = _entry("clinic2", "update", "user_prefs")
    assert _frame(_run(tmp_path / "a", entry, change, owners=owners)) == ("held", 1)
    # Without the declaration the table has no owner.
    assert _frame(_run(tmp_path / "b", entry, change))[0] == "refuted"
    # Another user's entry still doesn't make clinic2's row theirs.
    other = _entry("clinic3", "update", "user_prefs")
    assert _frame(_run(tmp_path / "c", other, change, owners=owners))[0] == "refuted"


def test_an_unreadable_audit_log_excuses_nothing(tmp_path: Path) -> None:
    # The audit read points at a file with no log table: unreadable.
    missing = tmp_path / "no-log.sqlite"
    result = _run(
        tmp_path / "run", _entry("clinic2", "update"), CHANGE_OWN, audit_db=missing
    )
    assert _frame(result)[0] == "refuted"
    assert result["evidence"]["frame"]["attribution"] == "audit_read_unavailable"


def test_owners_need_a_write_entry_naming_the_table_and_the_database() -> None:
    db = Path("unused.sqlite")
    owners = {"user_prefs": {"column": "owner_name", "by": "username"}}
    doc = _doc(db, owners=owners)
    del doc["frame"]["attribution"]["table_field"]
    del doc["frame"]["database"]
    with pytest.raises(Exception, match="table_field"):
        parse_verify_config(doc)
    doc = _doc(db, owners=owners)
    del doc["frame"]["database"]
    with pytest.raises(Exception, match="frame.database"):
        parse_verify_config(doc)
    # A contract may not redeclare a profile table's owner another way.
    other = {"user_settings": {"column": "setting_value", "by": "username"}}
    doc = _doc(db, owners=other)
    with pytest.raises(Exception, match="differs from the app profile"):
        parse_verify_config(doc)
