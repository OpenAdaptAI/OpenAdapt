"""One per-app profile of the app's own bookkeeping, shared by the frame,
rollback and the RL grader.

Every table a profile names is still compared: credentials only excuse the
acting user's own sign-in counters, a uuid only a backfill from NULL, and a
rule may name only a reviewed bookkeeping table. Records are made up; the
database is SQLite with OpenEMR-like tables (from the review of #1604).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Callable

import pytest

from openadapt_flow.app_profiles import AppProfile, load_profile
from openadapt_flow.verify_run import capture_baseline, parse_verify_config, verify_run

SCHEMA = """
CREATE TABLE patient_data (id INTEGER PRIMARY KEY, pid INTEGER, fname TEXT,
  lname TEXT, email TEXT, uuid TEXT);
CREATE TABLE history_data (id INTEGER PRIMARY KEY, pid INTEGER, tobacco TEXT DEFAULT '',
  uuid TEXT);
CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, authorized INTEGER,
  uuid TEXT, last_updated TEXT);
CREATE TABLE users_secure (id INTEGER PRIMARY KEY, username TEXT, password TEXT,
  last_update_password TEXT, login_fail_counter INTEGER DEFAULT 0, last_update TEXT);
CREATE TABLE login_mfa_registrations (user_id INTEGER, name TEXT, method TEXT,
  var1 TEXT, last_challenge TEXT, PRIMARY KEY (user_id, name));
CREATE TABLE keys (id INTEGER PRIMARY KEY, name TEXT, value TEXT);
CREATE TABLE session_tracker (uuid TEXT PRIMARY KEY, number_scripts INTEGER);
CREATE TABLE background_services (name TEXT PRIMARY KEY, active INTEGER,
  running INTEGER, next_run TEXT);
CREATE TABLE user_settings (setting_user INTEGER, setting_label TEXT,
  setting_value TEXT, PRIMARY KEY (setting_user, setting_label));
CREATE TABLE sequences (id INTEGER);
CREATE TABLE log (id INTEGER PRIMARY KEY, user TEXT, patient_id INTEGER,
  event TEXT, tbl TEXT);
INSERT INTO patient_data VALUES (1, 1, 'Wendell', 'Prior', 'w@example.test', 'u-1');
INSERT INTO patient_data VALUES (3, 3, 'Mira', 'Stone', 'm@example.test', 'u-3');
INSERT INTO history_data VALUES (1, 1, '', NULL);
INSERT INTO users VALUES (1, 'clinic2', 1, 'uu-1', '2026-01-01 00:00:00');
INSERT INTO users VALUES (2, 'bot', 1, NULL, '2026-01-01 00:00:00');
INSERT INTO users_secure VALUES (1, 'clinic2', 'hash1', '2026-01-01', 0, '2026-01-01');
INSERT INTO users_secure VALUES (2, 'bot', 'hash2', '2026-01-01', 0, '2026-01-01');
INSERT INTO login_mfa_registrations VALUES (2, 'phone', 'TOTP', 'seed', NULL);
INSERT INTO keys VALUES (1, 'oauth2key', 'k1');
INSERT INTO session_tracker VALUES ('s1', 1);
INSERT INTO background_services VALUES ('phimail', 1, 0, '2026-10-28 00:00:00');
INSERT INTO user_settings VALUES (1, 'gacl_protect', '1');
INSERT INTO sequences VALUES (5);
"""
NEW = {"fname": "Ada", "lname": "Quill", "email": "ada@example.test"}


def _doc(db: Path, **database: Any) -> dict[str, Any]:
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
                    "sqlite_database": str(db),
                    "sql_query": "SELECT * FROM log",
                },
            },
            "database": {
                "dialect": "sqlite",
                "read": {"sqlite_database": str(db)},
                "profile": "openemr",
                "profile_sha256": profile.sha256(),
                "views": {"patient_data": ""},
                "new_row_fields": {"patient_data": ["pid", "uuid"]},
                "settle_s": 0,
                **database,
            },
        },
    }


def _run(
    tmp_path: Path, after: Callable[[sqlite3.Connection], Any], **database: Any
) -> dict:
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = tmp_path / "app.sqlite"
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    con.commit()
    con.close()
    cfg = parse_verify_config(_doc(db, **database))
    capture_baseline(cfg, NEW, tmp_path / "baseline.json")
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO patient_data (pid, fname, lname, email, uuid) "
        "VALUES (2, 'Ada', 'Quill', 'ada@example.test', 'u-2')"
    )
    con.execute(
        "INSERT INTO log (user, patient_id, event, tbl) "
        "VALUES ('bot', 2, 'insert', 'patient_data')"
    )
    after(con)
    con.commit()
    con.close()
    return verify_run(
        cfg,
        params=NEW,
        run_record={"executor_kind": "agent", "executor_id": "bot"},
        authorization_ref="test",
        baseline_path=tmp_path / "baseline.json",
    )


def _frame(result: dict) -> str:
    return result["receipt"]["checked"]["frame"]


def _x(*sql: str) -> Callable[[sqlite3.Connection], None]:
    return lambda con: [con.execute(s) for s in sql]


def test_the_apps_own_bookkeeping_holds(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        _x(
            # The acting user's own sign-in bookkeeping and UI state.
            "UPDATE users_secure SET login_fail_counter = 0, last_update = '2026-10-29' "
            "WHERE username = 'bot'",
            "INSERT INTO user_settings VALUES (2, 'dem_expand', '1')",
            # Session and scheduler state, counters, a key added lazily.
            "INSERT INTO session_tracker VALUES ('s2', 1)",
            "UPDATE session_tracker SET number_scripts = 9 WHERE uuid = 's1'",
            "UPDATE background_services SET next_run = '2026-10-29', running = 1",
            "UPDATE sequences SET id = 6",
            # Backfills: a uuid from NULL, and the stamp it bumps.
            "UPDATE history_data SET uuid = 'h-1' WHERE id = 1",
            "UPDATE users SET uuid = 'uu-2', last_updated = '2026-10-29' WHERE id = 2",
        ),
    )
    assert _frame(result) == "held", result["evidence"]["frame"]
    used = set(result["evidence"]["frame"]["database"]["profile"]["rules_used"])
    assert {
        "users-secure-sign-in",
        "user-settings",
        "session-tracker",
        "background-services",
        "sequences",
        "uuid-backfill",
        "users-uuid-backfill",
        "users-last-updated",
    } <= used


@pytest.mark.parametrize(
    "write",
    [
        # Credentials and access (the review's silent passes).
        "UPDATE users_secure SET password = 'attacker' WHERE username = 'clinic2'",
        "UPDATE users_secure SET password = 'x' WHERE username = 'bot'",
        "UPDATE users_secure SET login_fail_counter = 5 WHERE username = 'clinic2'",
        "INSERT INTO login_mfa_registrations VALUES (1, 'evil', 'TOTP', 'seed', NULL)",
        "UPDATE keys SET value = 'k2' WHERE id = 1",
        "UPDATE background_services SET active = 0",
        "INSERT INTO user_settings VALUES (1, 'gacl_protect2', '0')",
        "UPDATE user_settings SET setting_value = '0' WHERE setting_user = 1",
        # uuid: a backfill only, never a swap, an overwrite or a clear.
        "UPDATE patient_data SET uuid = 'u-3' WHERE id = 1",
        "UPDATE patient_data SET uuid = NULL WHERE id = 3",
        "UPDATE users SET uuid = 'other' WHERE id = 1",
        "UPDATE users SET last_updated = '2026-10-29' WHERE id = 1",
        # Removed bookkeeping rows.
        "DELETE FROM session_tracker",
        "DELETE FROM keys",
        "INSERT INTO keys (name, value) VALUES ('oauth2passphrase', 'p')",
        "UPDATE login_mfa_registrations SET last_challenge = '2026-10-29' WHERE user_id = 2",
    ],
)
def test_anything_else_in_a_bookkeeping_table_refutes(
    tmp_path: Path, write: str
) -> None:
    assert _frame(_run(tmp_path, _x(write))) == "refuted", write


@pytest.mark.parametrize(
    "table", ["transactions", "lbf_data", "Patient_data", "site2.patient_data"]
)
def test_a_rule_may_name_only_a_reviewed_bookkeeping_table(table: str) -> None:
    base = load_profile("openemr").model_dump(mode="json")
    rule = {"id": "x", "table": table, "mode": "append_only", "reason": "no"}
    with pytest.raises(ValueError, match="bookkeeping"):
        AppProfile.model_validate({**base, "rules": [*base["rules"], rule]})


def test_every_table_only_for_a_system_column_backfill() -> None:
    base = load_profile("openemr").model_dump(mode="json")
    for rule in (
        {
            "id": "x",
            "table": "*",
            "mode": "columns",
            "columns": ["email"],
            "fill_only": True,
            "reason": "no",
        },
        {
            "id": "x",
            "table": "*",
            "mode": "columns",
            "columns": ["uuid"],
            "reason": "no",
        },
    ):
        with pytest.raises(ValueError):
            AppProfile.model_validate({**base, "rules": [*base["rules"], rule]})


def test_profiles_load_only_from_the_package_by_name() -> None:
    with pytest.raises(ValueError):
        load_profile("../../../../tmp/evil")


def test_the_contract_pins_the_profile(tmp_path: Path) -> None:
    db = tmp_path / "app.sqlite"
    with pytest.raises(Exception, match="profile_sha256"):
        parse_verify_config(_doc(db, profile_sha256="sha256:" + "0" * 64))


def test_a_clean_run_lists_only_the_rules_it_used(tmp_path: Path) -> None:
    result = _run(tmp_path, _x())
    assert _frame(result) == "held"
    used = result["evidence"]["frame"]["database"]["profile"]["rules_used"]
    assert used == ["log"]


def test_a_colleagues_own_ui_state_is_theirs_only_by_their_entry_for_it(
    tmp_path: Path,
) -> None:
    # Owner-scoped attribution (runtime/effects/frame.py): their write entry
    # for user_settings, and the row is theirs.
    own = _x(
        "INSERT INTO log (user, patient_id, event, tbl) "
        "VALUES ('clinic2', NULL, 'insert', 'user_settings')",
        "INSERT INTO user_settings VALUES (1, 'dem_expand', '1')",
    )
    result = _run(tmp_path / "a", own)
    assert _frame(result) == "held", result["evidence"]["frame"]
    assert result["evidence"]["frame"]["concurrent_count"] == 1
    # A write to another table no longer makes their UI state theirs.
    elsewhere = _x(
        "INSERT INTO log (user, patient_id, event, tbl) "
        "VALUES ('clinic2', 3, 'update', 'patient_data')",
        "INSERT INTO user_settings VALUES (1, 'dem_expand', '1')",
    )
    assert _frame(_run(tmp_path / "b", elsewhere)) == "refuted"
    idle = _x("INSERT INTO user_settings VALUES (1, 'dem_expand', '1')")
    assert _frame(_run(tmp_path / "c", idle)) == "refuted"
    # Credentials never widen: an active colleague's password still refutes.
    password = _x(
        "INSERT INTO log (user, patient_id, event, tbl) "
        "VALUES ('clinic2', 3, 'update', 'patient_data')",
        "UPDATE users_secure SET login_fail_counter = 3 WHERE username = 'clinic2'",
    )
    assert _frame(_run(tmp_path / "d", password)) == "refuted"


def test_the_openemr_profile_names_its_no_record_value() -> None:
    """Record 0 names no chart in OpenEMR's data, not in the engine."""
    profile = load_profile("openemr")
    assert profile.no_record_values == ["0"]


def test_a_rule_never_judges_a_row_whose_columns_have_no_cell_macs(
    tmp_path: Path,
) -> None:
    # #1793 item 1: over max_rows a table keeps cell MACs only for its
    # reference columns. A uuid backfill beside a clinical edit in the same
    # row used to show only the uuid, and the backfill rule excused the row.
    # users has two rows: over max_rows=1, its cells are only its references.
    backfill = (
        "UPDATE users SET uuid = 'uu-2', last_updated = '2026-10-29' WHERE id = 2"
    )
    hidden = (
        "UPDATE users SET uuid = 'uu-2', last_updated = '2026-10-29', "
        "authorized = 0 WHERE id = 2"
    )
    assert _frame(_run(tmp_path / "small-backfill", _x(backfill))) == "held"
    assert _frame(_run(tmp_path / "small-hidden", _x(hidden))) == "refuted"
    assert _frame(_run(tmp_path / "over-hidden", _x(hidden), max_rows=1)) == "refuted"


def test_the_rollback_diff_keeps_a_row_whose_columns_have_no_cell_macs(
    tmp_path: Path,
) -> None:
    from openadapt_flow.rollback import _excuser
    from openadapt_flow.rollback_state import DatabaseReader, changes, diff_states

    db = tmp_path / "app.sqlite"
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    con.commit()
    con.close()

    def moved(max_rows: int, sql: str) -> list[str]:
        con = sqlite3.connect(db)
        con.execute("UPDATE history_data SET uuid = NULL, tobacco = '' WHERE id = 1")
        con.commit()
        reader = DatabaseReader(
            dialect="sqlite",
            read_config={"sqlite_database": str(db)},
            profile="openemr",
            actor_names=("bot",),
            max_rows=max_rows,
        )
        before = reader.read()
        con.execute(sql)
        con.commit()
        con.close()
        after = reader.read(before=before)
        diff = diff_states(before, after, excuse=_excuser(reader, after))
        return [c[0] for c in changes(diff)]

    backfill = "UPDATE history_data SET uuid = 'h-1' WHERE id = 1"
    hidden = "UPDATE history_data SET uuid = 'h-1', tobacco = 'daily' WHERE id = 1"
    # Every column hashed: the backfill alone is the app's, the edit isn't.
    assert moved(2000, backfill) == []
    assert moved(2000, hidden) == ["history_data"]
    # Over max_rows: no rule judges the row, backfill or not.
    assert moved(0, hidden) == ["history_data"]
    assert moved(0, backfill) == ["history_data"]
