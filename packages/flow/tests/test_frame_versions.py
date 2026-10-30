"""``frame.versions``: a lost update stops the run.

The agent reads a record, another user saves a change, and the agent saves
its stale copy over it. Before and after look like a correct update; only the
record's versions show the second save. Each test runs ``verify_run`` on a
real SQLite database whose app keeps one version row per save (HAPI's
``hfj_res_ver`` shape: record, version number and, in some apps, the author).
Records are made up.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Callable, Optional

import pytest

from openadapt_flow.runtime.effects.frame import FrameSpec
from openadapt_flow.verifier import _reasons
from openadapt_flow.verify_run import capture_baseline, parse_verify_config, verify_run

SCHEMA = """
CREATE TABLE person (id INTEGER PRIMARY KEY, name TEXT, phone TEXT, addr TEXT,
  ver INTEGER);
CREATE TABLE person_version (id INTEGER PRIMARY KEY AUTOINCREMENT,
  person_id INTEGER, ver TEXT, author TEXT);
INSERT INTO person VALUES (1, 'Maria Alvarez', '555-0100', '1 Elm St', 1),
  (2, 'Yuki Tanaka', '555-0200', '2 Oak St', 1);
INSERT INTO person_version (person_id, ver, author) VALUES (1, '1', 'admin'),
  (2, '1', 'admin');
"""
PARAMS = {"phone": "555-0199", "bot": "bot"}


def _save(
    con: sqlite3.Connection, pid: int, author: Optional[str], **values: str
) -> None:
    """The app's save: the row, its version number, one version row."""

    sets = ", ".join(f"{k} = ?" for k in values)
    con.execute(
        f"UPDATE person SET {sets}, ver = ver + 1 WHERE id = ?", [*values.values(), pid]
    )
    (ver,) = con.execute("SELECT ver FROM person WHERE id = ?", [pid]).fetchone()
    con.execute(
        "INSERT INTO person_version (person_id, ver, author) VALUES (?, ?, ?)",
        [pid, str(ver), author],
    )


def _doc(db: Path, versions: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "openadapt.verify/v1",
        "verifier": {
            "kind": "sql",
            "sqlite_database": str(db),
            "sql_query": "SELECT * FROM person",
        },
        "effects": [
            {
                "kind": "record_written",
                "match": {"id": "1", "phone": {"param": "phone"}},
                "expected_count": 1,
            }
        ],
        "frame": {
            "identity_field": "id",
            "target_fields": ["phone", "ver"],
            "versions": {
                "read": {
                    "kind": "sql",
                    "sqlite_database": str(db),
                    "sql_query": "SELECT person_id, ver, author FROM person_version",
                },
                "record_field": "person_id",
                "version_field": "ver",
                **versions,
            },
        },
    }


AUTHORS = {"author_field": "author", "self": [{"param": "bot"}]}


def _run(
    tmp_path: Path,
    window: Callable[[sqlite3.Connection], None],
    versions: Optional[dict[str, Any]] = None,
    setup: Callable[[sqlite3.Connection], None] = lambda con: None,
) -> dict[str, Any]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = tmp_path / "app.sqlite"
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    setup(con)
    con.commit()
    cfg = parse_verify_config(_doc(db, {} if versions is None else versions))
    capture_baseline(cfg, PARAMS, tmp_path / "baseline.json")
    window(con)
    con.commit()
    con.close()
    return verify_run(
        cfg,
        params=PARAMS,
        run_record={"executor_kind": "agent", "executor_id": "bot"},
        authorization_ref="test",
        baseline_path=tmp_path / "baseline.json",
    )


def _versions(result: dict[str, Any]) -> dict[str, Any]:
    return result["evidence"]["frame"]["versions"]


def _correct(con: sqlite3.Connection) -> None:
    _save(con, 1, "bot", phone="555-0199")


def _lost_update(con: sqlite3.Connection) -> None:
    # The front desk saves a new address; the bot saves its stale copy
    # (the old address) with the new phone, reverting the address.
    _save(con, 1, "frontdesk", addr="9 New Rd")
    _save(con, 1, "bot", phone="555-0199", addr="1 Elm St")


@pytest.mark.parametrize("versions", [None, AUTHORS], ids=["steps", "authors"])
def test_a_correct_update_is_verified(tmp_path: Path, versions: Any) -> None:
    result = _run(tmp_path, _correct, versions)
    assert result["receipt"]["outcome"] == "verified"
    assert _versions(result)["status"] == "held"
    assert _versions(result)["new_versions"] == 1


def test_a_lost_update_without_authors_is_refuted_by_its_steps(tmp_path: Path) -> None:
    result = _run(tmp_path, _lost_update)
    assert result["receipt"]["checked"]["frame"] == "refuted"
    assert result["receipt"]["outcome"] == "reconciliation_required"
    assert _versions(result)["problem"] == "extra_steps"
    assert any("lost update" in r for r in _reasons(result["evidence"], "x"))


def test_a_lost_update_with_authors_is_refuted_by_the_author(tmp_path: Path) -> None:
    result = _run(tmp_path, _lost_update, AUTHORS)
    assert result["receipt"]["checked"]["frame"] == "refuted"
    assert _versions(result)["problem"] == "another_author"
    assert _versions(result)["own_versions"] == 1


def test_a_current_version_only_read_still_counts_the_steps(tmp_path: Path) -> None:
    # The read shows only the record's current version (a version column).
    result = _run(
        tmp_path,
        _lost_update,
        {"read": {"kind": "sql", "sqlite_database": str(tmp_path / "app.sqlite"),
                  "sql_query": "SELECT id AS person_id, ver FROM person"}},
    )  # fmt: skip
    assert _versions(result)["problem"] == "extra_steps"


def test_another_users_edit_of_an_unrelated_record_is_not_a_lost_update(
    tmp_path: Path,
) -> None:
    def window(con: sqlite3.Connection) -> None:
        _save(con, 2, "frontdesk", phone="555-0222")
        _correct(con)

    for versions in (None, AUTHORS):
        result = _run(tmp_path / str(bool(versions)), window, versions)
        # Only the target's versions are judged. (The frame has no
        # attribution here, so record 2's change refutes it as collateral,
        # as before: the version check adds nothing to that.)
        assert _versions(result)["status"] == "held"
        assert _versions(result)["problem"] is None


def test_a_version_with_no_author_is_not_checked(tmp_path: Path) -> None:
    result = _run(tmp_path, lambda con: _save(con, 1, None, phone="555-0199"), AUTHORS)
    assert result["receipt"]["checked"]["frame"] == "not_checked"
    assert result["receipt"]["outcome"] != "verified"
    assert _versions(result)["problem"] == "no_author"


def test_a_version_that_isnt_a_number_is_not_checked(tmp_path: Path) -> None:
    def window(con: sqlite3.Connection) -> None:
        _correct(con)
        con.execute("UPDATE person_version SET ver = 'W/\"2\"' WHERE ver = '2'")

    result = _run(tmp_path, window)
    assert _versions(result)["problem"] == "not_a_number"
    assert result["receipt"]["outcome"] != "verified"


def test_an_unreadable_versions_read_is_not_checked(tmp_path: Path) -> None:
    def window(con: sqlite3.Connection) -> None:
        _correct(con)
        con.execute("ALTER TABLE person_version RENAME TO gone")

    result = _run(tmp_path, window)
    assert _versions(result)["problem"] == "unreadable"
    assert result["receipt"]["outcome"] != "verified"


def test_a_target_with_no_version_is_not_checked(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        _correct,
        setup=lambda con: con.execute("DELETE FROM person_version WHERE person_id = 1"),
    )
    # The bot's save writes version 2, but the baseline had none for it.
    assert _versions(result)["problem"] == "missing"
    assert result["receipt"]["outcome"] != "verified"


def test_versions_that_go_back_are_refuted(tmp_path: Path) -> None:
    def window(con: sqlite3.Connection) -> None:
        con.execute("UPDATE person SET phone = '555-0199' WHERE id = 1")
        con.execute("UPDATE person_version SET ver = '0' WHERE person_id = 1")

    result = _run(tmp_path, window)
    assert _versions(result)["problem"] == "went_back"


def test_author_and_self_go_together() -> None:
    with pytest.raises(ValueError, match="author_field and self go together"):
        FrameSpec.model_validate(
            {"versions": {"read": {}, "record_field": "a", "version_field": "b",
                          "author_field": "c"}}
        )  # fmt: skip


def test_a_frame_without_versions_keeps_its_digest() -> None:
    spec = FrameSpec.model_validate({"identity_field": "id"})
    assert "versions" not in spec.model_dump(mode="json", by_alias=True)


# -- review round 1 (#1662): each attack ends in a stop ------------------------


def test_r1_a1_authors_with_a_current_version_only_read_is_not_checked(
    tmp_path: Path,
) -> None:
    # The read shows only the record's current version and its last author:
    # the front desk's version is gone after the write.
    db = tmp_path / "app.sqlite"
    read = {
        "kind": "sql",
        "sqlite_database": str(db),
        "sql_query": "SELECT p.id AS person_id, p.ver AS ver, (SELECT author FROM "
        "person_version v WHERE v.person_id = p.id ORDER BY v.id DESC LIMIT 1) "
        "AS author FROM person p",
    }
    result = _run(tmp_path, _lost_update, {**AUTHORS, "read": read})
    assert result["receipt"]["outcome"] != "verified"
    assert _versions(result)["problem"] == "history_changed"


@pytest.mark.parametrize("versions", [AUTHORS, {}], ids=["authors", "steps"])
def test_r1_a2_a_null_version_is_not_checked(tmp_path: Path, versions: Any) -> None:
    result = _run(tmp_path, _lost_update, {**versions, "version_field": "version"})
    assert result["receipt"]["outcome"] != "verified"
    assert _versions(result)["problem"] == "null_version"


def test_r1_a3_a_duplicate_version_is_not_checked(tmp_path: Path) -> None:
    def window(con: sqlite3.Connection) -> None:
        _lost_update(con)
        con.execute("UPDATE person_version SET ver = '1' WHERE author = 'frontdesk'")

    result = _run(tmp_path, window, AUTHORS)
    assert result["receipt"]["outcome"] != "verified"
    assert _versions(result)["problem"] == "duplicate_version"


def test_r1_a4_versions_are_read_before_the_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import openadapt_flow.verify_run as vr

    real = vr._read_versions
    calls = {"n": 0}

    def wrapped(cfg: Any, params: Any) -> Any:
        calls["n"] += 1
        rows = real(cfg, params)
        if calls["n"] == 1:  # another user saves between the two baseline reads
            con = sqlite3.connect(tmp_path / "app.sqlite")
            _save(con, 1, "frontdesk", addr="9 New Rd")
            con.commit()
            con.close()
        return rows

    monkeypatch.setattr(vr, "_read_versions", wrapped)
    result = _run(
        tmp_path, lambda con: _save(con, 1, "bot", phone="555-0199", addr="1 Elm St")
    )
    assert result["receipt"]["outcome"] != "verified"


@pytest.mark.parametrize("versions", [AUTHORS, {}], ids=["authors", "steps"])
def test_r1_a_changed_target_with_no_new_version_is_not_checked(
    tmp_path: Path, versions: Any
) -> None:
    # The app saved without writing a version row.
    result = _run(
        tmp_path,
        lambda con: con.execute("UPDATE person SET phone = '555-0199' WHERE id = 1"),
        versions,
    )
    assert result["receipt"]["outcome"] != "verified"
    assert _versions(result)["problem"] == "no_new_version"


def test_r1_an_unchanged_target_with_no_new_version_holds(tmp_path: Path) -> None:
    # Already right, nothing written: no save could overwrite anything.
    def setup(con: sqlite3.Connection) -> None:
        con.execute("UPDATE person SET phone = '555-0199' WHERE id = 1")

    result = _run(tmp_path, lambda con: None, AUTHORS, setup=setup)
    assert result["receipt"]["outcome"] == "verified"
    assert _versions(result)["new_versions"] == 0


# -- review round 2 (#1662) ------------------------------------------------------


def test_r2_i2_a_target_field_the_write_changes_is_not_a_new_record(
    tmp_path: Path,
) -> None:
    # The versions are keyed by the phone, which the agent's write changes:
    # the record existed (by its identity), so no baseline under the new
    # phone is a missing version, not a record created in the window.
    db = tmp_path / "app.sqlite"
    read = {
        "kind": "sql",
        "sqlite_database": str(db),
        "sql_query": "SELECT p.phone AS phone, v.ver FROM person_version v "
        "JOIN person p ON p.id = v.person_id",
    }
    result = _run(
        tmp_path,
        _lost_update,
        {"target_field": "phone", "record_field": "phone", "read": read},
    )
    assert result["receipt"]["outcome"] != "verified"
    assert _versions(result)["problem"] == "missing"
