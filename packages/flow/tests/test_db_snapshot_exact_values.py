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
import math
import os
import secrets
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from openadapt_flow.runtime.effects import db_snapshot
from openadapt_flow.runtime.effects.db_snapshot import (
    SnapshotError,
    canonical,
    diff,
    take_snapshot,
)
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


@pytest.mark.parametrize("column", ["", "BLOB", "NUMERIC"])
@pytest.mark.parametrize(
    "before, after",
    [("1", "'1'"), ("x'AB'", "'AB'"), ("1", "1.0"), ("x'31'", "'31'")],
    ids=["integer to text", "blob to text", "integer to real", "blob to its hex"],
)
def test_sqlite_a_storage_class_change_shows(
    tmp_path: Path, column: str, before: str, after: str
) -> None:
    path = tmp_path / "t.sqlite"
    con = sqlite3.connect(path)
    con.execute(f"CREATE TABLE t (id INTEGER PRIMARY KEY, v {column})")
    con.execute(f"INSERT INTO t VALUES (1, {before}), (2, {after})")
    stored = con.execute("SELECT typeof(v) FROM t ORDER BY id").fetchall()
    con.close()
    (tmp_path / "case").mkdir()
    moved, _ = _sqlite_case(tmp_path / "case", column, before, after)
    if stored[0] == stored[1]:
        # NUMERIC stores '1' and 1.0 as the integer 1: nothing moved.
        assert column == "NUMERIC" and not moved
    else:
        assert (moved["t"].changed, moved["t"].changed_columns) == (
            1,
            ["(columns not compared)"],
        )


@pytest.mark.parametrize(
    "after, text",
    [("1", "1"), ("'1'", "1"), ("x'AB'", "AB"), ("1.5", "1.5")],
    ids=["integer", "text", "blob", "real"],
)
def test_sqlite_a_storage_class_keeps_selectors_and_cell_macs(
    tmp_path: Path, after: str, text: str
) -> None:
    # The class is in the row MAC only: a selector and a cell MAC compare by
    # the value's text, as before.
    _, b = _sqlite_case(tmp_path, "", "NULL", after)
    (row,) = b.tables["t"].values.values()
    assert str(row["v"]) == text
    assert record_matches(row, {"v": text})
    assert canonical(row["v"]) == text


@pytest.mark.parametrize(
    "literal, text",
    [("10.0", "10"), ("1e16", "10000000000000000"), ("1500.0", "1500")],
)
def test_sqlite_a_round_real_reads_in_plain_notation(
    tmp_path: Path, literal: str, text: str
) -> None:
    _, b = _sqlite_case(tmp_path, "REAL", "NULL", literal)
    (row,) = b.tables["t"].values.values()
    assert str(row["v"]) == text
    assert record_matches(row, {"v": text})


@pytest.mark.parametrize(
    "value", [1.0000000000000002e300, 3e-300, 1.7976931348623157e308]
)
def test_sqlite_adjacent_doubles_at_the_ends_of_the_range_differ(
    tmp_path: Path, value: float
) -> None:
    # json_object's 17-digit text was not exact here before SQLite 3.53; the
    # snapshot reads the double itself.
    path = tmp_path / "t.sqlite"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v REAL)")
    con.execute("INSERT INTO t VALUES (1, ?)", (value,))
    con.commit()

    def connect() -> Any:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True)

    a = take_snapshot(connect, "sqlite", key=KEY)
    con.execute("UPDATE t SET v = ?", (math.nextafter(value, 0.0),))
    con.commit()
    con.close()
    b = take_snapshot(connect, "sqlite", key=KEY, before=a)
    moved = diff(a, b, key=KEY)
    assert (moved["t"].changed, moved["t"].changed_columns) == (1, ["v"])
    (row,) = b.tables["t"].values.values()
    assert float(row["v"]) == math.nextafter(value, 0.0)


def test_sqlite_a_connection_with_converters_is_unreadable(tmp_path: Path) -> None:
    path = tmp_path / "t.sqlite"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v DATE)")
    con.execute("INSERT INTO t VALUES (1, '2026-01-02')")
    con.commit()
    con.close()

    def connect() -> Any:
        return sqlite3.connect(
            f"file:{path}?mode=ro", uri=True, detect_types=sqlite3.PARSE_DECLTYPES
        )

    with pytest.raises(SnapshotError, match="converters"):
        take_snapshot(connect, "sqlite", key=KEY)


def test_float32_text_is_plain() -> None:
    # MySQL 8 FLOAT, read widened to DOUBLE: %g printed 10 as 1e+01.
    assert str(db_snapshot._float32_text(Decimal("10"))) == "10"
    assert str(db_snapshot._float32_text(Decimal("1500"))) == "1500"
    assert str(db_snapshot._float32_text(Decimal("1.100000023841858"))) == "1.1"
    assert str(db_snapshot._float32_text(Decimal("1E+20"))) == "100000000000000000000"
    assert str(db_snapshot._pg_value("float8", "1e+16")) == "10000000000000000"
    assert str(db_snapshot._pg_value("float4", "10")) == "10"


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
