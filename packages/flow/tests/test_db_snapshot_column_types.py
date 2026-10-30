"""Every column type is read exactly by the whole-database snapshot.

The first-contact study found the whole-database frame unreadable on
Dolibarr 21 (MariaDB 11): a ``POINT`` column turned ``JSON_OBJECT``'s result
into a binary string, and the read raised ``TypeError``. These tests hold the
fix: no column type is dropped, and a one-step change to any column shows in
the diff as that column and nothing else.

The ``canonical`` tests need no database. The MySQL/MariaDB tests run only
with ``OPENADAPT_SNAPSHOT_MYSQL`` set to a JSON object of ``pymysql`` connect
arguments for an account that may create and drop a scratch database, for
example::

    OPENADAPT_SNAPSHOT_MYSQL='{"host": "127.0.0.1", "port": 3306,
        "user": "root", "password": "..."}' \\
        python -m pytest tests/test_db_snapshot_column_types.py -v
"""

from __future__ import annotations

import datetime
import json
import os
import secrets
import uuid
from decimal import Decimal
from typing import Any, Iterator

import pytest

from openadapt_flow.runtime.effects import db_snapshot
from openadapt_flow.runtime.effects.db_snapshot import (
    SnapshotError,
    canonical,
    diff,
    take_snapshot,
)

# -- canonical: one exact text per value --------------------------------------


@pytest.mark.parametrize(
    "a, b",
    [
        (
            Decimal("12345678901234567890.0123456789"),
            Decimal("12345678901234567890.0123456788"),
        ),
        (0.30000000000000004, 0.3),
        (b"\x00\xff", b"\x00\xfe"),
        (bytearray(b"\x01"), b"\x02"),
        (memoryview(b"ab"), memoryview(b"ac")),
        (
            datetime.timedelta(hours=-838, minutes=-59),
            datetime.timedelta(hours=838, minutes=59),
        ),
        (datetime.timedelta(microseconds=1), datetime.timedelta(0)),
        (
            datetime.datetime(2026, 1, 2, 3, 4, 5, 600000),
            datetime.datetime(2026, 1, 2, 3, 4, 5),
        ),
        (datetime.time(1, 2, 3), datetime.time(1, 2, 4)),
        ({"a", "b"}, {"a"}),
        (
            {"b": Decimal("1.10"), "a": [1, 2]},
            {"b": Decimal("1.1000000000000001"), "a": [1, 2]},
        ),
        ([1, 2], [2, 1]),
        (uuid.UUID(int=1), uuid.UUID(int=2)),
        ("a ", "a"),
        (None, ""),
    ],
)
def test_different_values_have_different_text(a: Any, b: Any) -> None:
    assert canonical(a) != canonical(b)


@pytest.mark.parametrize(
    "a, b",
    [
        (Decimal("12.50"), Decimal("12.5")),
        (1, Decimal("1.000")),
        (bytearray(b"\x00\xff"), memoryview(b"\x00\xff")),
        ({"b", "a"}, frozenset({"a", "b"})),
        ({"b": 1, "a": [Decimal("1.0")]}, {"a": [1], "b": 1}),
        (datetime.timedelta(hours=-838, minutes=-59, seconds=-59), "-838:59:59"),
        (datetime.timedelta(seconds=1, microseconds=500000), "00:00:01.500000"),
        (datetime.datetime(2026, 1, 2, 3, 4, 5, 600000), "2026-01-02 03:04:05.600000"),
    ],
)
def test_equal_values_have_one_text(a: Any, b: Any) -> None:
    assert canonical(a) == canonical(b)


def test_a_value_without_an_exact_text_is_refused() -> None:
    with pytest.raises(TypeError, match="no exact text form"):
        canonical(object())


def test_a_binary_json_row_is_read_and_a_non_json_one_refused() -> None:
    assert db_snapshot._row_part("t", b'{"x": null, "n": 1.10}') == {
        "x": None,
        "n": Decimal("1.10"),
    }
    with pytest.raises(SnapshotError, match="isn't valid JSON"):
        db_snapshot._row_part("t", b'{"x": \x15}')
    with pytest.raises(SnapshotError, match="isn't UTF-8"):
        db_snapshot._row_part("t", b'{"x": "\xff"}')


@pytest.mark.parametrize(
    "data_type, how",
    [
        ("point", "hex"),
        ("geometry", "hex"),
        ("bit", "hex"),
        ("varbinary", "hex"),
        ("longblob", "hex"),
        ("vector", "hex"),
        ("float", "double"),
        ("double", ""),
        ("json", "text"),
        ("longtext", "text"),
        ("decimal", ""),
        ("set", ""),
        ("varchar", ""),
    ],
)
def test_mysql_types_are_read_by_a_declared_rule(data_type: str, how: str) -> None:
    assert db_snapshot._mysql_encoding(data_type) == how


# -- MySQL / MariaDB: a real engine --------------------------------------------

_ARGS = os.environ.get("OPENADAPT_SNAPSHOT_MYSQL")
live = pytest.mark.skipif(
    not _ARGS, reason="set OPENADAPT_SNAPSHOT_MYSQL to pymysql connect args (JSON)"
)

#: One column per type the server can store, each with a value, and a second
#: value one smallest step away (or differing only where a lossy read would
#: hide it).
COLUMNS: list[tuple[str, str, str, str]] = [
    ("c_tinyint", "TINYINT", "-128", "-127"),
    ("c_bigint_u", "BIGINT UNSIGNED", "18446744073709551615", "18446744073709551614"),
    (
        "c_decimal",
        "DECIMAL(30,10)",
        "12345678901234567890.0123456789",
        "12345678901234567890.0123456788",
    ),
    ("c_double", "DOUBLE", "1.0000000000000002e300", "1e300"),
    ("c_float", "FLOAT", "0.1", "0.1000001"),
    ("c_bit", "BIT(5)", "b'10101'", "b'10100'"),
    ("c_bool", "BOOLEAN", "1", "0"),
    ("c_date", "DATE", "'2026-01-02'", "'2026-01-03'"),
    (
        "c_datetime",
        "DATETIME(6)",
        "'2026-01-02 03:04:05.600000'",
        "'2026-01-02 03:04:05.600001'",
    ),
    (
        "c_timestamp",
        "TIMESTAMP(6) NULL",
        "'2026-01-02 03:04:05.000001'",
        "'2026-01-02 03:04:05'",
    ),
    ("c_time", "TIME(6)", "'-838:59:59.000000'", "'-838:59:58.999999'"),
    ("c_year", "YEAR", "2026", "2025"),
    ("c_char", "CHAR(4)", "'ab'", "'ac'"),
    ("c_varchar", "VARCHAR(20)", "'a '", "'a'"),
    ("c_text", "TEXT", "'x'", "'X'"),
    ("c_longtext", "LONGTEXT", '\'{"b": 1, "a": 2}\'', '\'{"a": 2, "b": 1}\''),
    ("c_json", "JSON", "'{\"b\": 1.10}'", "'{\"b\": 1.1}'"),
    ("c_enum", "ENUM('x','y')", "'x'", "'y'"),
    ("c_set", "SET('a','b')", "'a,b'", "'a'"),
    ("c_binary", "BINARY(2)", "x'00ff'", "x'00fe'"),
    ("c_varbinary", "VARBINARY(4)", "x'00'", "x'0000'"),
    ("c_blob", "BLOB", "x'00ff'", "x'00ff00'"),
    ("c_point", "POINT NULL", "POINT(1, 2)", "POINT(1, 2.0000000000000004)"),
    (
        "c_geometry",
        "GEOMETRY NULL",
        "ST_GeomFromText('LINESTRING(0 0, 1 1)')",
        "ST_GeomFromText('LINESTRING(0 0, 1 2)')",
    ),
]


@pytest.fixture
def database() -> Iterator[Any]:
    import pymysql

    args = json.loads(_ARGS or "{}")
    name = "openadapt_snapshot_types_" + secrets.token_hex(4)
    admin = pymysql.connect(**args, autocommit=True)
    with admin.cursor() as cur:
        cur.execute(f"CREATE DATABASE `{name}`")
        cur.execute(f"USE `{name}`")
        cols = ", ".join(f"{c} {t}" for c, t, _, _ in COLUMNS)
        cur.execute(f"CREATE TABLE t (id INT PRIMARY KEY AUTO_INCREMENT, {cols})")
        values = ", ".join(v for _, _, v, _ in COLUMNS)
        cur.execute(
            f"INSERT INTO t ({', '.join(c for c, *_ in COLUMNS)}) VALUES ({values})"
        )
        cur.execute(
            f"INSERT INTO t ({', '.join(c for c, *_ in COLUMNS)}) VALUES ({values})"
        )
        cur.execute(
            f"INSERT INTO t ({', '.join(c for c, *_ in COLUMNS)}) VALUES "
            f"({', '.join('NULL' for _ in COLUMNS)})"
        )

    def connect(**extra: Any) -> Any:
        return pymysql.connect(**args, database=name, **extra)

    try:
        yield admin, name, connect
    finally:
        with admin.cursor() as cur:
            cur.execute(f"DROP DATABASE `{name}`")
        admin.close()


def _snap(connect: Any, before: Any = None) -> Any:
    return take_snapshot(connect, "mysql", key=b"k" * 32, before=before)


@live
def test_every_type_is_read_and_a_reread_is_equal(database: Any) -> None:
    _, _, connect = database
    a = _snap(connect)
    b = _snap(connect, before=a)
    assert a.tables["t"].count == 3
    assert diff(a, b, key=b"k" * 32) == {}


@live
def test_a_dict_cursor_reads_the_same(database: Any) -> None:
    import pymysql

    _, _, connect = database
    a = _snap(connect)
    b = _snap(lambda: connect(cursorclass=pymysql.cursors.DictCursor), before=a)
    assert diff(a, b, key=b"k" * 32) == {}


@live
@pytest.mark.parametrize(
    "column, _type, _old, new", COLUMNS, ids=[c[0] for c in COLUMNS]
)
def test_a_one_step_change_to_any_column_shows(
    database: Any, column: str, _type: str, _old: str, new: str
) -> None:
    admin, name, connect = database
    a = _snap(connect)
    with admin.cursor() as cur:
        cur.execute(f"UPDATE `{name}`.t SET {column} = {new} WHERE id = 1")
        assert cur.rowcount == 1, f"{column}: the server stored the same value"
    b = _snap(connect, before=a)
    moved = diff(a, b, key=b"k" * 32)
    assert list(moved) == ["t"]
    assert (moved["t"].changed, moved["t"].changed_columns) == (1, [column])
    assert b.tables["t"].values, "the changed row's plain values are kept"


@live
@pytest.mark.parametrize(
    "column, _type, old, _new", COLUMNS, ids=[c[0] for c in COLUMNS]
)
def test_a_null_and_a_value_differ(
    database: Any, column: str, _type: str, old: str, _new: str
) -> None:
    admin, name, connect = database
    a = _snap(connect)
    with admin.cursor() as cur:
        cur.execute(f"UPDATE `{name}`.t SET {column} = {old} WHERE id = 3")
    moved = diff(a, _snap(connect, before=a), key=b"k" * 32)
    assert (moved["t"].changed, moved["t"].changed_columns) == (1, [column])
