"""Follow-up to #1661's review: values the snapshot read wrongly or in a form
selectors don't match.

The cases are the reviewer's attack cases
(``first-contact-retest-2026-10/fix-evidence/reviews/1661-r1``): SQLite
``REAL`` values that printed the same, the text ``"\\x00null"`` that read as
NULL, and MySQL ``FLOAT`` and ``DECIMAL`` values whose plain form no longer
matched a selector after #1661 (a false stop on a correct write).

The SQLite tests run on a real SQLite file. The MySQL/MariaDB tests need
``OPENADAPT_SNAPSHOT_MYSQL`` (see ``test_db_snapshot_column_types.py``).
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from openadapt_flow.runtime.effects.db_snapshot import canonical, diff, take_snapshot
from openadapt_flow.runtime.effects.effect import record_matches

KEY = b"k" * 32


def _sqlite_case(tmp_path: Path, column: str, before: str, after: str) -> Any:
    path = tmp_path / "t.sqlite"
    con = sqlite3.connect(path)
    con.execute(f"CREATE TABLE t (id INTEGER PRIMARY KEY, v {column})")
    con.execute(f"INSERT INTO t VALUES (1, {before})")
    con.commit()

    def connect() -> Any:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True)

    a = take_snapshot(connect, "sqlite", key=KEY)
    con.execute(f"UPDATE t SET v = {after}")
    con.commit()
    con.close()
    b = take_snapshot(connect, "sqlite", key=KEY, before=a)
    return diff(a, b, key=KEY), b


@pytest.mark.parametrize(
    "column, before, after",
    [
        ("REAL", "0.3", "0.1+0.2"),
        ("REAL", "1e16", "1e16+2"),
        ("", "0.3", "0.1+0.2"),
        ("TEXT", "NULL", "char(0)||'null'"),
        ("", "NULL", "char(0)||'null'"),
    ],
)
def test_sqlite_a_change_shows(
    tmp_path: Path, column: str, before: str, after: str
) -> None:
    moved, _ = _sqlite_case(tmp_path, column, before, after)
    assert (moved["t"].changed, moved["t"].changed_columns) == (1, ["v"])


def test_sqlite_a_real_reads_as_people_write_it(tmp_path: Path) -> None:
    _, b = _sqlite_case(tmp_path, "REAL", "1.5", "0.3")
    (row,) = b.tables["t"].values.values()
    assert str(row["v"]) == "0.3"
    assert record_matches(row, {"v": "0.3"})


@pytest.mark.parametrize(
    "before, after",
    [("1", "'1'"), ("x'AB'", "'AB'")],
    ids=["integer to text", "blob to text"],
)
@pytest.mark.xfail(
    strict=True,
    reason="limit: in a SQLite column with no type, a value that changes type "
    "but not its text (1 to '1', x'AB' to 'AB') reads the same",
)
def test_sqlite_a_type_change_in_an_untyped_column(
    tmp_path: Path, before: str, after: str
) -> None:
    moved, _ = _sqlite_case(tmp_path, "", before, after)
    assert moved


def test_the_text_nul_null_is_not_null() -> None:
    assert canonical("\x00null") != canonical(None)
    assert canonical("\x00bytes:00") != canonical(b"\x00")
    assert canonical("plain") == "plain"


# -- MySQL / MariaDB -----------------------------------------------------------

_ARGS = os.environ.get("OPENADAPT_SNAPSHOT_MYSQL")
live = pytest.mark.skipif(
    not _ARGS, reason="set OPENADAPT_SNAPSHOT_MYSQL to pymysql connect args (JSON)"
)


def _mysql_case(coldef: str, before: str, after: str) -> Any:
    import pymysql

    args = json.loads(_ARGS or "{}")
    name = "openadapt_exact_" + secrets.token_hex(4)
    admin = pymysql.connect(**args, autocommit=True)
    try:
        with admin.cursor() as cur:
            cur.execute(f"CREATE DATABASE `{name}`")
            cur.execute(f"CREATE TABLE `{name}`.t (id INT PRIMARY KEY, v {coldef})")
            cur.execute(f"INSERT INTO `{name}`.t VALUES (1, {before})")

        def connect() -> Any:
            return pymysql.connect(**args, database=name)

        a = take_snapshot(connect, "mysql", key=KEY)
        with admin.cursor() as cur:
            cur.execute(f"UPDATE `{name}`.t SET v = {after} WHERE id = 1")
        b = take_snapshot(connect, "mysql", key=KEY, before=a)
        return diff(a, b, key=KEY), b
    finally:
        with admin.cursor() as cur:
            cur.execute(f"DROP DATABASE `{name}`")
        admin.close()


@live
@pytest.mark.parametrize(
    "coldef, before, after, selector",
    [
        ("FLOAT", "1.0", "1.1", "1.1"),
        ("FLOAT", "0.1", "0.1000001", "0.1000001"),
        ("DECIMAL(24,8)", "1", "1.5", "1.5"),
        ("DECIMAL(24,8)", "1", "100", "100"),
        ("DECIMAL(65,30)", "1", "1.000000000000000000000000000002",
         "1.000000000000000000000000000002"),
        ("DOUBLE", "0.3", "0.1e0+0.2e0", "0.30000000000000004"),
    ],
)  # fmt: skip
def test_mysql_a_number_changes_and_a_selector_matches_it(
    coldef: str, before: str, after: str, selector: str
) -> None:
    moved, b = _mysql_case(coldef, before, after)
    assert (moved["t"].changed, moved["t"].changed_columns) == (1, ["v"])
    (row,) = b.tables["t"].values.values()
    assert record_matches(row, {"v": selector}), row["v"]


@live
def test_mysql_the_text_nul_null_is_not_null() -> None:
    moved, _ = _mysql_case("VARCHAR(10) NULL", "NULL", "CONCAT(CHAR(0), 'null')")
    assert (moved["t"].changed, moved["t"].changed_columns) == (1, ["v"])
