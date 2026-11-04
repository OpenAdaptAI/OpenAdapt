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
CREATE TABLE recent_patients (id INTEGER PRIMARY KEY, user_id INTEGER, pid INTEGER,
  patients TEXT);
CREATE TABLE user_prefs (owner_name TEXT, pref TEXT, value TEXT,
  PRIMARY KEY (owner_name, pref));
CREATE TABLE keys (id INTEGER PRIMARY KEY, name TEXT, value TEXT);
CREATE TABLE uuid_registry (id INTEGER PRIMARY KEY, uuid TEXT, table_name TEXT);
CREATE TABLE extended_log (id INTEGER PRIMARY KEY, event TEXT, user TEXT,
  recipient TEXT, description TEXT, patient_id INTEGER);
CREATE TABLE audit_master (id INTEGER PRIMARY KEY, pid INTEGER,
  approval_status INTEGER, type INTEGER);
CREATE TABLE audit_details (id INTEGER PRIMARY KEY, table_name TEXT,
  field_name TEXT, field_value TEXT, audit_master_id INTEGER);
CREATE TABLE ip_tracking (id INTEGER PRIMARY KEY, ip_string TEXT,
  total_ip_login_fail_counter INTEGER, ip_login_fail_counter INTEGER,
  ip_last_login_fail TEXT, ip_force_block INTEGER);
CREATE TABLE log (id INTEGER PRIMARY KEY, user TEXT, patient_id INTEGER,
  event TEXT, tbl TEXT);
INSERT INTO patient_data VALUES (1, 1, 'Wendell', 'Prior', 'w@example.test', 'u-1');
INSERT INTO users VALUES (1, 'clinic2', 'uu-1', '2026-01-01 00:00:00');
INSERT INTO users VALUES (2, 'bot', 'uu-2', '2026-01-01 00:00:00');
INSERT INTO users VALUES (3, 'clinic3', 'uu-3', '2026-01-01 00:00:00');
INSERT INTO users_secure VALUES (1, 'clinic2', 'hash1');
INSERT INTO user_settings VALUES (1, 'gacl_protect', '1');
INSERT INTO user_settings VALUES (3, 'gacl_protect', '1');
INSERT INTO user_settings VALUES (0, 'gacl_protect', '1');
INSERT INTO recent_patients VALUES (1, 1, NULL, '1');
INSERT INTO recent_patients VALUES (2, 1, 9, '9');
INSERT INTO user_prefs VALUES ('clinic2', 'theme', 'light');
INSERT INTO keys VALUES (1, 'oauth2key', 'k1');
INSERT INTO uuid_registry VALUES (1, 'u-1', 'patient_data');
INSERT INTO ip_tracking VALUES (1, '203.0.113.9', 40, 40, '2026-10-28', 0);
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


def _why(result: dict) -> tuple[str, int, int]:
    """(status, rows refuting, of them unexplained). A refuting row that is
    explained is the bot's: its own entry for the table, or its audit entry
    for nothing the save declares."""

    frame = result["evidence"]["frame"]
    return (frame["status"], frame["collateral_count"], frame["unattributed_count"])


def _entry(
    user: str, event: str, table: str = "user_settings", pid: str = "NULL"
) -> str:
    return (
        "INSERT INTO log (user, patient_id, event, tbl) "
        f"VALUES ('{user}', {pid}, '{event}', '{table}')"
    )


CHANGE_OWN = "UPDATE user_settings SET setting_value = '0' WHERE setting_user = 1"
#: The bot's entry for user_settings, filed under the chart it saved: the
#: audit-only check passes it, so only the owner path's own rule can refute.
BOT_UNDER_ITS_CHART = _entry("bot", "update", pid="2")


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
        # Another profile own_rows table, a row that names no chart.
        (
            _entry("clinic2", "update", "recent_patients"),
            "UPDATE recent_patients SET patients = '1,3' WHERE id = 1",
        ),
        # The table named in another case: the same table.
        (_entry("clinic2", "update", "User_Settings"), CHANGE_OWN),
    ],
)
def test_a_users_change_to_their_own_row_is_theirs(
    tmp_path: Path, sql: tuple[str, ...]
) -> None:
    assert _frame(_run(tmp_path, *sql)) == ("held", 1)


UNEXPLAINED = ("refuted", 1, 1)
BOTS = ("refuted", 1, 0)


@pytest.mark.parametrize(
    ("sql", "why"),
    [
        # Their entry, another user's row.
        (
            (
                _entry("clinic2", "update"),
                "UPDATE user_settings SET setting_value = '0' WHERE setting_user = 3",
            ),
            UNEXPLAINED,
        ),
        # The bot wrote the table in the window: its entry names no row, so
        # their row may be its change. The bot's entry is filed under the
        # chart it saved, so only this rule refutes.
        ((_entry("clinic2", "update"), BOT_UNDER_ITS_CHART, CHANGE_OWN), BOTS),
        # ... whatever case or schema the entry names the table in.
        (
            (
                _entry("clinic2", "update"),
                _entry("bot", "update", "USER_SETTINGS", pid="2"),
                CHANGE_OWN,
            ),
            BOTS,
        ),
        (
            (
                _entry("clinic2", "update"),
                _entry("bot", "update", "openemr.user_settings", pid="2"),
                CHANGE_OWN,
            ),
            BOTS,
        ),
        # A bot entry naming no chart is also audit-only collateral.
        ((_entry("bot", "update"), CHANGE_OWN), ("refuted", 2, 0)),
        # A table with no declared owner, though they wrote it.
        (
            (_entry("clinic2", "update", "keys"), "UPDATE keys SET value = 'k2'"),
            UNEXPLAINED,
        ),
        # Credentials never widen: users_secure's owner is a column rule's,
        # for the acting user's sign-in counters only.
        (
            (
                _entry("clinic2", "update", "users_secure"),
                "UPDATE users_secure SET password = 'x' WHERE username = 'clinic2'",
            ),
            UNEXPLAINED,
        ),
        # No entry.
        ((CHANGE_OWN,), UNEXPLAINED),
        # Their entry for this table in another schema.
        (
            (_entry("clinic2", "update", "other.user_settings"), CHANGE_OWN),
            UNEXPLAINED,
        ),
        # An entry for another table, or a view of this one.
        ((_entry("clinic2", "update", "patient_data"), CHANGE_OWN), UNEXPLAINED),
        ((_entry("clinic2", "view"), CHANGE_OWN), UNEXPLAINED),
        # The wrong kind of write: an update never explains an added row.
        (
            (
                _entry("clinic2", "update"),
                "INSERT INTO user_settings VALUES (1, 'dem_expand', '1')",
            ),
            UNEXPLAINED,
        ),
        # One entry explains one row: the other stays unexplained.
        (
            (
                _entry("clinic2", "insert"),
                "INSERT INTO user_settings VALUES (1, 'dem_expand', '1')",
                "INSERT INTO user_settings VALUES (1, 'fax_expand', '1')",
            ),
            UNEXPLAINED,
        ),
        # Unresolvable owner: an actor the users table doesn't know, and a
        # row with no owner.
        ((_entry("ghost", "update"), CHANGE_OWN), UNEXPLAINED),
        (
            (
                _entry("clinic2", "insert"),
                "INSERT INTO user_settings VALUES (NULL, 'dem_expand', '1')",
            ),
            UNEXPLAINED,
        ),
        # A row that names a chart is judged by its chart: their entry for
        # chart 5 doesn't explain a change to their row for chart 9.
        (
            (
                _entry("clinic2", "update", "recent_patients", pid="5"),
                "UPDATE recent_patients SET patients = '9,3' WHERE id = 2",
            ),
            UNEXPLAINED,
        ),
    ],
)
def test_nothing_else_is_excused(
    tmp_path: Path, sql: tuple[str, ...], why: tuple[str, int, int]
) -> None:
    assert _why(_run(tmp_path, *sql)) == why, sql


def test_a_row_moved_to_another_user_is_not_excused(tmp_path: Path) -> None:
    # setting_user is part of the key: moving the row removes one and adds
    # another, and an update explains neither.
    moved = (
        "UPDATE user_settings SET setting_label = 'moved', setting_user = 3 "
        "WHERE setting_user = 1"
    )
    result = _run(tmp_path / "a", _entry("clinic2", "update"), moved)
    assert _why(result) == ("refuted", 2, 2)
    # An owner column outside the key: the row keeps its key and changes
    # hands. Neither user's entry explains that, whoever wrote it.
    handed = "UPDATE recent_patients SET user_id = 3 WHERE id = 1"
    for i, actor in enumerate(("clinic2", "clinic3")):
        entry = _entry(actor, "update", "recent_patients")
        assert _why(_run(tmp_path / f"b{i}", entry, handed)) == UNEXPLAINED, actor


def test_a_contract_may_restate_a_profile_owner(tmp_path: Path) -> None:
    owners = {
        "user_settings": {
            "column": "setting_user",
            "by": "user_id",
            "default": {"value": "0", "same": ["setting_label", "setting_value"]},
        }
    }
    result = _run(tmp_path, _entry("clinic2", "update"), CHANGE_OWN, owners=owners)
    assert _frame(result) == ("held", 1)


@pytest.mark.parametrize(
    ("owners", "match"),
    [
        # Credentials: the probe of the review of #1637. Declared, this
        # excused another user's password change.
        ({"users_secure": {"column": "username", "by": "username"}}, "credentials"),
        # A column rule's table, an append-only table, and a table the
        # profile never reviewed.
        ({"users": {"column": "username", "by": "username"}}, "reviewed"),
        ({"contact": {"column": "foreign_id", "by": "user_id"}}, "reviewed"),
        ({"user_prefs": {"column": "owner_name", "by": "username"}}, "reviewed"),
        ({"patient_data": {"column": "email", "by": "username"}}, "reviewed"),
        # A profile table, another way, or without its default rows.
        (
            {"user_settings": {"column": "setting_value", "by": "username"}},
            "differs from the app profile",
        ),
        (
            {"user_settings": {"column": "setting_user", "by": "user_id"}},
            "differs from the app profile",
        ),
    ],
)
def test_a_contract_may_not_give_another_table_an_owner(
    owners: dict, match: str
) -> None:
    with pytest.raises(Exception, match=match):
        parse_verify_config(_doc(Path("unused.sqlite"), owners=owners))


def test_an_unreadable_audit_log_excuses_nothing(tmp_path: Path) -> None:
    # The audit read points at a file with no log table: unreadable.
    missing = tmp_path / "no-log.sqlite"
    result = _run(
        tmp_path / "run", _entry("clinic2", "update"), CHANGE_OWN, audit_db=missing
    )
    assert _why(result) == UNEXPLAINED
    assert result["evidence"]["frame"]["attribution"] == "audit_read_unavailable"


def test_owners_need_a_table_field_a_database_and_a_profile() -> None:
    db = Path("unused.sqlite")
    owners = {"user_settings": {"column": "setting_user", "by": "user_id"}}
    doc = _doc(db, owners=owners)
    del doc["frame"]["attribution"]["table_field"]
    del doc["frame"]["database"]
    with pytest.raises(Exception, match="table_field"):
        parse_verify_config(doc)
    doc = _doc(db, owners=owners)
    del doc["frame"]["database"]
    with pytest.raises(Exception, match="frame.database"):
        parse_verify_config(doc)
    doc = _doc(db, owners=owners)
    del doc["frame"]["database"]["profile"]
    del doc["frame"]["database"]["profile_sha256"]
    with pytest.raises(Exception, match="profile"):
        parse_verify_config(doc)


# OpenEMR's default rows. The first time anyone saves a label,
# setUserSetting(..., createDefault=true) also inserts the global default for
# it: a row with setting_user = 0 and the same setting_label. Seen on the
# 8.0.0.3 fixture: a second user's one setting added rows for users 0 and 8.

ADD_OWN = "INSERT INTO user_settings VALUES (1, 'dem_expand', '1')"
ADD_DEFAULT = "INSERT INTO user_settings VALUES (0, 'dem_expand', '1')"


@pytest.mark.parametrize(
    "entries",
    [
        # OpenEMR logs both inserts under the user, or (other configurations)
        # only theirs: either way their row explains the default beside it.
        (_entry("clinic2", "insert"), _entry("clinic2", "insert")),
        (_entry("clinic2", "insert"),),
    ],
)
def test_a_default_row_added_beside_a_users_first_save_is_theirs(
    tmp_path: Path, entries: tuple[str, ...]
) -> None:
    result = _run(tmp_path, *entries, ADD_OWN, ADD_DEFAULT)
    assert _frame(result) == ("held", 2), result["evidence"]["frame"]


@pytest.mark.parametrize("pid", ["2", "NULL", "0"])
def test_the_bots_first_use_of_a_setting_is_its_own_bookkeeping(
    tmp_path: Path, pid: str
) -> None:
    # The bot's own row is the profile's own_rows bookkeeping, and so is the
    # default OpenEMR adds beside it. OpenEMR files the bot's entries under
    # the chart it has open, or under no chart (NULL or 0).
    result = _run(
        tmp_path,
        _entry("bot", "insert", pid=pid),
        _entry("bot", "insert", pid=pid),
        "INSERT INTO user_settings VALUES (2, 'dem_expand', '1')",
        ADD_DEFAULT,
    )
    assert _frame(result) == ("held", 0), result["evidence"]["frame"]
    used = result["evidence"]["frame"]["database"]["profile"]["rules_used"]
    assert "user-settings" in used


@pytest.mark.parametrize("pid", ["NULL", "0"])
@pytest.mark.parametrize(
    "sql",
    [
        (
            _entry("bot", "update", "recent_patients", pid="{pid}"),
            "INSERT INTO recent_patients VALUES (3, 2, NULL, '2')",
        ),
        (
            _entry("bot", "update", "user_settings", pid="{pid}"),
            "INSERT INTO user_settings VALUES (2, 'gacl_protect', '0')",
        ),
    ],
)
def test_the_bots_entry_for_its_own_per_user_row_is_its_bookkeeping(
    tmp_path: Path, sql: tuple[str, str], pid: str
) -> None:
    # Calibration on OpenEMR 8.0.0.3 (#1637): a clean save refuted when the
    # audit read also returned the bot's entries that name no chart, for
    # its own recent_patients and user_settings rows. The row is excused as
    # the bot's own; its entry is not "written by this run" collateral.
    statements = tuple(x.replace("{pid}", pid) for x in sql)
    result = _run(tmp_path, *statements)
    assert _why(result) == ("held", 0, 0), result["evidence"]["frame"]
    rules = set(result["evidence"]["frame"]["database"]["profile"]["rules_used"])
    assert rules & {"recent-patients", "user-settings"}


@pytest.mark.parametrize(
    ("sql", "why"),
    [
        # The bot's entry for a per-user table where nothing of its own
        # moved: still collateral, written by this run per the log.
        ((_entry("bot", "update", "user_settings"),), ("refuted", 1, 0)),
        # Its own row and another user's row: the other row is the bot's
        # (its entry for the table names no row), its own row excused.
        (
            (
                _entry("bot", "update", "user_settings"),
                _entry("clinic2", "update"),
                "INSERT INTO user_settings VALUES (2, 'gacl_protect', '0')",
                CHANGE_OWN,
            ),
            BOTS,
        ),
        # Its entry for the same table in a schema the frame doesn't read
        # (review of #1637, probe S9): a write there escapes the diff, so the
        # entry is still the bot's write the contract doesn't cover.
        (
            (
                "INSERT INTO user_settings VALUES (2, 'gacl_protect', '0')",
                _entry("bot", "update", "other.USER_SETTINGS", pid="1"),
            ),
            ("refuted", 1, 0),
        ),
        # Its entry for one per-user table doesn't cover another.
        (
            (
                _entry("bot", "update", "recent_patients"),
                _entry("bot", "update", "user_settings"),
                "INSERT INTO recent_patients VALUES (3, 2, NULL, '2')",
            ),
            ("refuted", 1, 0),
        ),
    ],
)
def test_the_bots_entry_covers_only_its_own_rows_in_that_table(
    tmp_path: Path, sql: tuple[str, ...], why: tuple[str, int, int]
) -> None:
    assert _why(_run(tmp_path, *sql)) == why, sql


@pytest.mark.parametrize(
    ("sql", "why"),
    [
        # A default row added alone, with or without an entry.
        ((ADD_DEFAULT,), UNEXPLAINED),
        ((_entry("clinic2", "insert"), ADD_DEFAULT), UNEXPLAINED),
        # A default row changed or removed, even beside their own change to
        # that label.
        (
            (
                _entry("clinic2", "update"),
                _entry("clinic2", "update"),
                CHANGE_OWN,
                "UPDATE user_settings SET setting_value = '0' WHERE setting_user = 0",
            ),
            UNEXPLAINED,
        ),
        (
            (
                _entry("clinic2", "delete"),
                "DELETE FROM user_settings WHERE setting_user = 0",
            ),
            UNEXPLAINED,
        ),
        # Beside an explained row of that label with another value: OpenEMR
        # copies the value, so a different one is planted, not its default.
        (
            (
                _entry("clinic2", "insert"),
                ADD_OWN,
                "INSERT INTO user_settings VALUES (0, 'dem_expand', 'planted')",
            ),
            UNEXPLAINED,
        ),
        # Beside an explained row of another label.
        (
            (
                _entry("clinic2", "insert"),
                "INSERT INTO user_settings VALUES (1, 'fax_expand', '1')",
                ADD_DEFAULT,
            ),
            UNEXPLAINED,
        ),
        # Beside a row nothing explains: both stay unexplained.
        (
            ("INSERT INTO user_settings VALUES (3, 'dem_expand', '1')", ADD_DEFAULT),
            ("refuted", 2, 2),
        ),
        # Beside a row that is the bot's (its entry for the table): the
        # partner refutes as the bot's, the default stays unexplained.
        (
            (
                _entry("clinic2", "insert"),
                _entry("bot", "insert", pid="2"),
                ADD_OWN,
                ADD_DEFAULT,
            ),
            ("refuted", 2, 1),
        ),
        # One explained row explains one default row: a second user's own
        # row and its default, plus a lone default of another label.
        (
            (
                _entry("clinic2", "insert"),
                ADD_OWN,
                ADD_DEFAULT,
                "INSERT INTO user_settings VALUES (0, 'fax_expand', '1')",
            ),
            UNEXPLAINED,
        ),
    ],
)
def test_a_default_row_is_explained_only_beside_an_explained_row(
    tmp_path: Path, sql: tuple[str, ...], why: tuple[str, int, int]
) -> None:
    assert _why(_run(tmp_path, *sql)) == why, sql


@pytest.mark.parametrize(
    "owner",
    [
        # A default row is for per-user state only, never a column rule's.
        {"rule": "users-secure-sign-in", "default": {"value": "0", "same": ["id"]}},
        # Its partner can't be matched by the owner column itself.
        {"rule": "user-settings", "default": {"value": "0", "same": ["setting_user"]}},
    ],
)
def test_a_profile_default_owner_is_for_own_rows_and_another_column(
    owner: dict,
) -> None:
    from openadapt_flow.app_profiles import AppProfile

    base = load_profile("openemr").model_dump(mode="json")
    for rule in base["rules"]:
        if rule["id"] == owner["rule"]:
            rule["owner"]["default"] = owner["default"]
    with pytest.raises(ValueError, match="default owner|same"):
        AppProfile.model_validate(base)


ADD_UUID = "INSERT INTO uuid_registry (uuid, table_name) VALUES ('u-2', 'patient_data')"


@pytest.mark.parametrize("pid", ["NULL", "0"])
def test_the_bots_entry_for_an_append_only_table_is_its_bookkeeping(
    tmp_path: Path, pid: str
) -> None:
    # Calibration of run 2 on OpenEMR 8.0.0.3: with the audit read covering
    # entries that name no chart, every clean UI save refuted on the bot's
    # insert into uuid_registry, an id map the profile reads as append-only.
    result = _run(tmp_path, _entry("bot", "insert", "uuid_registry", pid=pid), ADD_UUID)
    assert _why(result) == ("held", 0, 0), result["evidence"]["frame"]
    assert (
        "uuid-registry"
        in result["evidence"]["frame"]["database"]["profile"]["rules_used"]
    )


@pytest.mark.parametrize(
    "sql",
    [
        # An existing row of the append-only table changed: the snapshot
        # refutes it, whoever's entry names the table.
        (
            _entry("bot", "update", "uuid_registry"),
            "UPDATE uuid_registry SET table_name = 'users' WHERE id = 1",
        ),
        # Removed.
        (
            _entry("bot", "delete", "uuid_registry"),
            "DELETE FROM uuid_registry WHERE id = 1",
        ),
        # The same name in a schema the frame doesn't read: not this table.
        (_entry("bot", "insert", "other.uuid_registry"), ADD_UUID),
        # A table the profile compares row by row is not append-only.
        (_entry("bot", "update", "keys"),),
        # Review of #1649: append-only tables that hold a disclosure, a
        # pending change to a chart, or a sign-in lockout are not marked, so
        # the bot's entry there still refutes.
        (
            _entry("bot", "insert", "extended_log", pid="1"),
            "INSERT INTO extended_log (event, user, recipient, description, "
            "patient_id) VALUES ('disclosure-healthcare', 'bot', 'acme-insurer', "
            "'full chart', 1)",
        ),
        (
            _entry("bot", "insert", "audit_master", pid="1"),
            "INSERT INTO audit_master (pid, approval_status, type) VALUES (1, 1, 1)",
            _entry("bot", "insert", "audit_details", pid="1"),
            "INSERT INTO audit_details (table_name, field_name, field_value, "
            "audit_master_id) VALUES ('patient_data', 'DOB', '1901-01-01', 1)",
        ),
        (
            _entry("bot", "update", "ip_tracking", pid="1"),
            "UPDATE ip_tracking SET total_ip_login_fail_counter = 0, "
            "ip_login_fail_counter = 0, ip_last_login_fail = NULL WHERE id = 1",
        ),
        # A marked table, but the entry names a chart the save doesn't declare.
        (_entry("bot", "insert", "uuid_registry", pid="1"), ADD_UUID),
    ],
)
def test_only_a_marked_tables_insert_naming_no_record_is_bookkeeping(
    tmp_path: Path, sql: tuple[str, ...]
) -> None:
    status, refuting, _ = _why(_run(tmp_path, *sql))
    assert (status, refuting > 0) == ("refuted", True), sql


def test_a_rule_that_excuses_insert_entries_has_rows_that_never_change() -> None:
    from openadapt_flow.app_profiles import ProfileRule

    for bad in (
        {"mode": "columns", "columns": ["id"]},
        {"mode": "append_only", "columns": ["ip_login_fail_counter"]},
    ):
        with pytest.raises(ValueError):
            ProfileRule(
                id="x",
                table="ip_tracking",
                reason="r",
                insert_entry_names_no_record=True,
                **bad,
            )
    marked = load_profile("openemr").no_record_insert_tables()
    assert marked == {"uuid_registry", "uuid_mapping", "contact", "clinical_rules_log"}
