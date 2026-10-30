"""The whole-database snapshot on PostgreSQL: every column type read exactly.

The first-contact study ran Redmine on PostgreSQL, where ``frame.database``
had no dialect, so a write to a table the contract doesn't read, or a write
after the verdict, passed. These tests hold the PostgreSQL dialect to the
MySQL one's bar: one read-only repeatable-read transaction, every column
type read by a declared rule, a one-step change to any column shown as that
column and nothing else, and a clear refusal for what can't be read exactly.

The rule tests need no database. The live tests run only with
``OPENADAPT_SNAPSHOT_POSTGRES`` set to a libpq DSN for an account that may
create and drop a scratch database and a role, for example::

    OPENADAPT_SNAPSHOT_POSTGRES='host=127.0.0.1 port=5432 user=postgres
        password=... dbname=postgres' \\
        python -m pytest tests/test_db_snapshot_postgres.py -v
"""

from __future__ import annotations

import os
import secrets
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

import pytest

from openadapt_flow.runtime.effects import db_snapshot
from openadapt_flow.runtime.effects.db_snapshot import (
    CLOCK_DEFAULT,
    SnapshotError,
    StoredNumber,
    canonical,
    diff,
    is_default,
    take_snapshot,
)

# -- rules: no database ---------------------------------------------------------


def _types(**extra: db_snapshot._PgType) -> dict[int, db_snapshot._PgType]:
    T = db_snapshot._PgType
    base = {
        1: T("int4", "pg_catalog", "b", "N", 0, 0),
        2: T("numeric", "pg_catalog", "b", "N", 0, 0),
        3: T("money", "pg_catalog", "b", "N", 0, 0),
        4: T("_int4", "pg_catalog", "b", "A", 1, 0),
        5: T("_money", "pg_catalog", "b", "A", 3, 0),
        6: T("int4range", "pg_catalog", "r", "R", 0, 0, subtype=1),
        7: T("mood", "public", "e", "E", 0, 0),
        8: T("posint", "public", "d", "N", 0, 1),
        9: T("addr", "public", "c", "C", 0, 0),
        10: T("citext", "public", "b", "S", 0, 0),
        11: T("regclass", "pg_catalog", "b", "N", 0, 0),
        12: T("price", "public", "d", "N", 0, 2, typmod=655366),
        13: T("free", "public", "d", "N", 0, 2),
        14: T("bytea", "pg_catalog", "b", "U", 0, 0),
    }
    return {**base, **extra}


@pytest.mark.parametrize(
    "oid, typmod, rule",
    [
        (1, -1, "int"),
        (2, 655366, "numeric"),
        (2, -1, "numeric_free"),
        (3, -1, "money"),
        (4, -1, "text"),
        (6, -1, "text"),
        (7, -1, "text"),
        (8, -1, "int"),
        (12, -1, "numeric"),
        (13, -1, "numeric_free"),
        (14, -1, "bytea"),
    ],
)
def test_each_type_has_a_declared_rule(oid: int, typmod: int, rule: str) -> None:
    assert db_snapshot._pg_rule(_types(), oid, typmod) == rule


@pytest.mark.parametrize(
    "oid, message",
    [
        (5, "money"),
        (9, "public.addr"),
        (10, "public.citext"),
        (11, "pg_catalog.regclass"),
        (99, "not in the catalog"),
    ],
)
def test_a_type_without_a_rule_is_refused(oid: int, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        db_snapshot._pg_rule(_types(), oid)


@pytest.mark.parametrize(
    "expr, stored",
    [
        (None, None),
        ("NULL::character varying", None),
        ("'open'::character varying", "open"),
        ("'O''Brien'::text", "O'Brien"),
        ("''::character varying", ""),
        ("0", "0"),
        ("(-1)", "-1"),
        ("'1.50'::numeric", "1.50"),
        ("false", "false"),
        ("now()", CLOCK_DEFAULT),
        ("CURRENT_TIMESTAMP", CLOCK_DEFAULT),
        ("('now'::text)::date", CLOCK_DEFAULT),
        ("nextval('issues_id_seq'::regclass)", "nextval('issues_id_seq'::regclass)"),
        ("gen_random_uuid()", "gen_random_uuid()"),
    ],
)
def test_a_default_is_read_only_from_a_literal(expr: Any, stored: Any) -> None:
    assert db_snapshot._default_text(expr, "postgresql") == stored


def test_a_numeric_without_scale_keeps_it_and_equals_its_value() -> None:
    a, b = (
        db_snapshot._pg_value("numeric_free", "1.50"),
        db_snapshot._pg_value("numeric_free", "1.5"),
    )
    assert isinstance(a, StoredNumber) and a == b == Decimal("1.5")
    assert str(a) == "1.5"
    assert canonical(a) != canonical(b)
    assert is_default(a, "1.5") and is_default(
        db_snapshot._pg_value("numeric", "0.00"), "0"
    )


@pytest.mark.parametrize(
    "rule, text, value",
    [
        ("int", "-9223372036854775808", -9223372036854775808),
        ("numeric", "12.50", Decimal("12.5")),
        ("numeric", "NaN", Decimal("NaN")),
        ("float4", "0.1", Decimal("0.1")),
        ("float4", "0.100000001", Decimal("0.1")),
        ("float8", "0.30000000000000004", Decimal("0.30000000000000004")),
        ("float8", "-Infinity", Decimal("-Infinity")),
        ("money", "12.30", Decimal("12.3")),
        ("bool", "true", True),
        ("bytea", "00ff", b"\x00\xff"),
        ("text", "P1D", "P1D"),
    ],
)
def test_a_value_is_read_by_its_rule(rule: str, text: str, value: Any) -> None:
    got = db_snapshot._pg_value(rule, text)
    assert canonical(got) == canonical(value)


def test_a_boolean_that_isnt_true_or_false_is_refused() -> None:
    with pytest.raises(SnapshotError):
        db_snapshot._pg_value("bool", "t")


# -- a real PostgreSQL -----------------------------------------------------------

_DSN = os.environ.get("OPENADAPT_SNAPSHOT_POSTGRES")
live = pytest.mark.skipif(
    not _DSN, reason="set OPENADAPT_SNAPSHOT_POSTGRES to a libpq DSN"
)
KEY = b"k" * 32

#: A reader whose session prints every type differently from the pinned one:
#: a zone with daylight saving, floats with 6 digits, bytes escaped, SQL
#: dates, Postgres intervals. A pinned read is the same through it.
HOSTILE = (
    "-c TimeZone=America/New_York -c extra_float_digits=0 -c bytea_output=escape "
    "-c DateStyle=SQL,DMY -c IntervalStyle=postgres"
)

#: One column per type, each with a value, and a second value one smallest
#: step away (or differing only where a lossy read would hide it).
COLUMNS: list[tuple[str, str, str, str]] = [
    ("c_int2", "smallint", "-32768", "-32767"),
    ("c_int8", "bigint", "9223372036854775807", "9223372036854775806"),
    (
        "c_numeric",
        "numeric(40,20)",
        "12345678901234567890.01234567890123456789",
        "12345678901234567890.01234567890123456788",
    ),
    ("c_numeric_scale", "numeric", "1.5", "1.50"),
    ("c_numeric_nan", "numeric", "0", "'NaN'"),
    ("c_real", "real", "0.1", "0.10000001"),
    ("c_double", "double precision", "1.0000000000000002e300", "1e300"),
    ("c_double_zero", "double precision", "0", "'-0'"),
    ("c_money", "money", "12.34", "12.35"),
    ("c_bool", "boolean", "true", "false"),
    ("c_bytea", "bytea", "'\\x00ff'", "'\\x00fe'"),
    ("c_char", "char(4)", "'ab'", "'ac'"),
    ("c_varchar", "varchar(20)", "'a '", "'a'"),
    ("c_text", "text", "'x'", "'X'"),
    (
        "c_uuid",
        "uuid",
        "'00000000-0000-0000-0000-000000000001'",
        "'00000000-0000-0000-0000-000000000002'",
    ),
    ("c_inet", "inet", "'10.0.0.1'", "'10.0.0.1/24'"),
    ("c_cidr", "cidr", "'10.0.0.0/24'", "'10.0.0.0/25'"),
    ("c_macaddr", "macaddr", "'08:00:2b:01:02:03'", "'08:00:2b:01:02:04'"),
    ("c_date", "date", "'2026-01-02'", "'2026-01-03'"),
    ("c_time", "time(6)", "'03:04:05.000001'", "'03:04:05'"),
    ("c_timetz", "timetz", "'03:04:05+01'", "'03:04:05+02'"),
    (
        "c_timestamp",
        "timestamp(6)",
        "'2026-01-02 03:04:05.6'",
        "'2026-01-02 03:04:05.600001'",
    ),
    (
        "c_timestamptz",
        "timestamptz",
        "'2026-11-01 05:30:00+00'",
        "'2026-11-01 06:30:00+00'",
    ),
    ("c_interval", "interval", "'1 day'", "'24 hours'"),
    ("c_bit", "bit(5)", "B'10101'", "B'10100'"),
    ("c_varbit", "varbit(8)", "B'1'", "B'10'"),
    ("c_json", "json", "'{\"b\": 1.10}'", "'{\"b\": 1.1}'"),
    ("c_jsonb", "jsonb", "'{\"b\": 1.10}'", "'{\"b\": 1.1}'"),
    ("c_xml", "xml", "'<a>1</a>'", "'<a>2</a>'"),
    ("c_tsvector", "tsvector", "'a:1 b:2'", "'a:1 b:3'"),
    ("c_tsquery", "tsquery", "'a & b'", "'a | b'"),
    ("c_point", "point", "'(1,2)'", "'(1,2.0000000000000004)'"),
    ("c_line", "line", "'{1,2,3}'", "'{1,2,4}'"),
    ("c_lseg", "lseg", "'[(0,0),(1,1)]'", "'[(0,0),(1,2)]'"),
    ("c_box", "box", "'(1,1),(0,0)'", "'(2,1),(0,0)'"),
    ("c_path", "path", "'[(0,0),(1,1)]'", "'((0,0),(1,1))'"),
    ("c_polygon", "polygon", "'((0,0),(1,1),(1,0))'", "'((0,0),(1,1),(2,0))'"),
    ("c_circle", "circle", "'<(0,0),1>'", "'<(0,0),2>'"),
    ("c_int_array", "int[]", "'{1,2}'", "'{2,1}'"),
    ("c_float_array", "float8[]", "'{0.1}'", "'{0.30000000000000004}'"),
    ("c_real_array", "real[]", "'{0.1}'", "'{0.10000001}'"),
    ("c_text_array", "text[]", "'{\"a b\"}'", "'{a,b}'"),
    ("c_bytea_array", "bytea[]", "ARRAY['\\x00'::bytea]", "ARRAY['\\x0000'::bytea]"),
    ("c_int4range", "int4range", "'[1,5)'", "'[1,6)'"),
    (
        "c_tstzrange",
        "tstzrange",
        "'[2026-01-01 00:00+00,2026-01-02 00:00+00)'",
        "'[2026-01-01 00:00+00,2026-01-02 00:00:00.000001+00)'",
    ),
    ("c_multirange", "int4multirange", "'{[1,3)}'", "'{[1,3),[4,5)}'"),
    ("c_enum", "mood", "'x'", "'y'"),
    ("c_domain", "posint", "1", "2"),
]


class Pg:
    def __init__(self, dsn: str) -> None:
        import psycopg2

        self.psycopg2 = psycopg2
        self.dsn = dsn
        self.name = "openadapt_snapshot_pg_" + secrets.token_hex(4)
        self.reader = "openadapt_snapshot_ro_" + secrets.token_hex(4)
        self.password = secrets.token_hex(16)

    def admin(self, dbname: str | None = None) -> Any:
        extra = {} if dbname is None else {"dbname": dbname}
        con = self.psycopg2.connect(self.dsn, **extra)
        con.autocommit = True
        return con

    def sql(self, *statements: str) -> None:
        con = self.admin(self.name)
        with con.cursor() as cur:
            for statement in statements:
                cur.execute(statement)
        con.close()

    def connect(self, options: str = "", reader: bool = False) -> Any:
        args: dict[str, Any] = {"dbname": self.name}
        if options:
            args["options"] = options
        if reader:
            args.update(user=self.reader, password=self.password)
        return lambda: self.psycopg2.connect(self.dsn, **args)

    def snap(self, before: Any = None, options: str = "", **kw: Any) -> Any:
        return take_snapshot(
            self.connect(options, kw.pop("reader", False)),
            "postgresql",
            key=KEY,
            before=before,
            **kw,
        )


@pytest.fixture
def pg() -> Iterator[Pg]:
    db = Pg(_DSN or "")
    con = db.admin()
    with con.cursor() as cur:
        cur.execute(f"CREATE DATABASE {db.name}")
        cur.execute(f"CREATE ROLE {db.reader} LOGIN PASSWORD '{db.password}'")
    cols = ", ".join(f"{c} {t}" for c, t, _, _ in COLUMNS)
    names = ", ".join(c for c, *_ in COLUMNS)
    values = ", ".join(v for _, _, v, _ in COLUMNS)
    db.sql(
        "CREATE TYPE mood AS ENUM ('x', 'y')",
        "CREATE DOMAIN posint AS integer CHECK (VALUE > 0)",
        f"CREATE TABLE t (id serial PRIMARY KEY, {cols})",
        f"INSERT INTO t ({names}) VALUES ({values})",
        f"INSERT INTO t ({names}) VALUES ({values})",
        "INSERT INTO t (id) VALUES (DEFAULT)",
        "CREATE TABLE pairs (a int, b text)",
        "INSERT INTO pairs VALUES (1, 'x'), (1, 'x'), (2, 'y')",
        f"GRANT USAGE ON SCHEMA public TO {db.reader}",
        f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {db.reader}",
        f"GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO {db.reader}",
    )
    try:
        yield db
    finally:
        with con.cursor() as cur:
            cur.execute(f"DROP DATABASE {db.name} WITH (FORCE)")
            cur.execute(f"DROP ROLE {db.reader}")
        con.close()


@live
def test_every_type_is_read_and_a_reread_is_equal(pg: Pg) -> None:
    a = pg.snap()
    assert a.tables["t"].count == 3 and a.schemas == ["public"]
    assert a.tables["t"].counter == 3 and a.tables["t"].generated_key == "id"
    assert diff(a, pg.snap(before=a), key=KEY) == {}


@live
def test_a_reader_session_set_otherwise_reads_the_same(pg: Pg) -> None:
    a = pg.snap()
    assert diff(a, pg.snap(before=a, options=HOSTILE), key=KEY) == {}


@live
@pytest.mark.parametrize(
    "column, _type, _old, new", COLUMNS, ids=[c[0] for c in COLUMNS]
)
def test_a_one_step_change_to_any_column_shows(
    pg: Pg, column: str, _type: str, _old: str, new: str
) -> None:
    a = pg.snap(options=HOSTILE)
    pg.sql(f"UPDATE t SET {column} = {new} WHERE id = 1")
    b = pg.snap(before=a, options=HOSTILE)
    moved = diff(a, b, key=KEY)
    assert list(moved) == ["t"]
    assert (moved["t"].changed, moved["t"].changed_columns) == (1, [column])
    assert b.tables["t"].values, "the changed row's plain values are kept"


@live
@pytest.mark.parametrize(
    "column, _type, old, _new", COLUMNS, ids=[c[0] for c in COLUMNS]
)
def test_a_null_and_a_value_differ(
    pg: Pg, column: str, _type: str, old: str, _new: str
) -> None:
    a = pg.snap()
    pg.sql(f"UPDATE t SET {column} = {old} WHERE id = 3")
    moved = diff(a, pg.snap(before=a), key=KEY)
    assert (moved["t"].changed, moved["t"].changed_columns) == (1, [column])


@live
def test_plain_values_read_as_people_write_them(pg: Pg) -> None:
    a = pg.snap()
    pg.sql("UPDATE t SET c_int2 = 7 WHERE id = 1")
    row = next(iter(pg.snap(before=a).tables["t"].values.values()))
    assert str(row["c_real"]) == "0.1" and str(row["c_numeric_scale"]) == "1.5"
    assert str(row["c_money"]) == "12.34" and row["c_bool"] is True
    assert row["c_bytea"] == b"\x00\xff" and row["c_interval"] == "P1D"
    assert row["c_timestamptz"] == "2026-11-01 05:30:00+00"


@live
def test_a_table_without_a_key_counts_duplicate_rows(pg: Pg) -> None:
    a = pg.snap()
    assert a.tables["pairs"].count == 3 and not a.tables["pairs"].primary_key
    pg.sql("INSERT INTO pairs VALUES (1, 'x')")
    b = pg.snap(before=a)
    assert (
        diff(a, b, key=KEY)["pairs"].added,
        diff(a, b, key=KEY)["pairs"].removed,
    ) == (1, 0)
    pg.sql(
        "DELETE FROM pairs WHERE ctid IN (SELECT ctid FROM pairs WHERE a = 1 LIMIT 2)"
    )
    moved = diff(a, pg.snap(before=a), key=KEY)["pairs"]
    assert (moved.added, moved.removed) == (0, 1)


@live
def test_a_row_written_and_removed_moves_the_counter(pg: Pg) -> None:
    a = pg.snap()
    pg.sql("INSERT INTO t (id) VALUES (DEFAULT)", "DELETE FROM t WHERE id = 4")
    moved = diff(a, pg.snap(before=a), key=KEY)
    assert moved["t"].unread == ["a row was written and removed (counter moved)"]


@live
def test_a_sequence_no_table_owns_is_compared_unless_skipped(pg: Pg) -> None:
    pg.sql("CREATE SEQUENCE invoice_no", f"GRANT SELECT ON invoice_no TO {pg.reader}")
    a = pg.snap(reader=True)
    pg.sql("SELECT nextval('invoice_no')")
    moved = diff(a, pg.snap(before=a, reader=True), key=KEY)
    assert list(moved) == ["invoice_no"] and moved["invoice_no"].changed == 1
    skip = {"invoice_no": "skip"}
    a = pg.snap(reader=True, modes=skip)
    pg.sql("SELECT nextval('invoice_no')")
    assert diff(a, pg.snap(before=a, reader=True, modes=skip), key=KEY) == {}


@live
def test_a_column_without_a_rule_is_refused_unless_skipped(pg: Pg) -> None:
    pg.sql(
        "CREATE TYPE addr AS (street text, n int)",
        "CREATE TABLE places (id int PRIMARY KEY, a addr, m money[])",
    )
    with pytest.raises(
        SnapshotError, match=r"places \(column a: no exact reading rule"
    ):
        pg.snap()
    a = pg.snap(modes={"places": "skip"})
    assert a.tables["places"].mode == "skip"


@live
def test_what_the_reader_cant_see_is_refused(pg: Pg) -> None:
    pg.sql("CREATE TABLE secret (id int PRIMARY KEY)")
    with pytest.raises(
        SnapshotError,
        match="secret .no SELECT|secret \\(the read account has no SELECT",
    ):
        pg.snap(reader=True)
    pg.sql(
        f"GRANT SELECT ON secret TO {pg.reader}",
        "CREATE SCHEMA hidden",
        "CREATE TABLE hidden.h (id int PRIMARY KEY)",
    )
    with pytest.raises(
        SnapshotError, match="can't see schema.s. hidden.*'<schema>.\\*'"
    ):
        pg.snap(reader=True)
    # Naming schemas doesn't narrow the read (review #1673, B1): a hidden
    # schema is still refused.
    with pytest.raises(SnapshotError, match="can't see schema.s. hidden"):
        pg.snap(reader=True, schemas=["public"])
    with pytest.raises(SnapshotError, match="can't see schema.s.: hidden"):
        pg.snap(reader=True, schemas=["public", "hidden"])
    # Only the allow-list leaves a schema out.
    a = pg.snap(reader=True, modes={"hidden.*": "skip"})
    assert a.schemas == ["public"] and a.schemas_skipped == ["hidden"]
    assert "hidden.h" not in a.tables
    with pytest.raises(SnapshotError, match="allow-listed schemas not in the database"):
        pg.snap(reader=True, modes={"nosuch.*": "skip"})


@live
def test_named_schemas_still_read_every_visible_schema(pg: Pg) -> None:
    # Review #1673, B1: with schemas=["public"], a stray row in another
    # schema and a rewritten trigger function in a third both show.
    pg.sql(
        "CREATE SCHEMA other",
        "CREATE TABLE other.note (id serial PRIMARY KEY, body text)",
        "CREATE SCHEMA util",
        "CREATE FUNCTION util.f() RETURNS trigger LANGUAGE plpgsql AS 'BEGIN RETURN NEW; END'",
        "CREATE TRIGGER tr BEFORE INSERT ON pairs FOR EACH ROW EXECUTE FUNCTION util.f()",
    )
    a = pg.snap(schemas=["public"])
    assert a.schemas == ["other", "public", "util"]
    pg.sql("INSERT INTO other.note (body) VALUES ('stray')")
    assert (
        diff(a, pg.snap(before=a, schemas=["public"]), key=KEY)["other.note"].added == 1
    )
    b = pg.snap(schemas=["public"])
    pg.sql(
        "CREATE OR REPLACE FUNCTION util.f() RETURNS trigger LANGUAGE plpgsql "
        "AS 'BEGIN NEW.b := ''x''; RETURN NEW; END'"
    )
    assert "(database code)" in diff(b, pg.snap(before=b, schemas=["public"]), key=KEY)


def test_sqlite_composite_keys_that_join_alike_stay_apart(tmp_path: Path) -> None:
    # Review #1673, S1, on every dialect: the key is a JSON array.
    import sqlite3

    path = tmp_path / "kv.sqlite"
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE kv (a TEXT, b TEXT, PRIMARY KEY (a, b));"
        "INSERT INTO kv VALUES ('x|y', 'z'), ('x', 'y|z');"
    )
    con.commit()

    def connect() -> Any:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True)

    a = take_snapshot(connect, "sqlite", key=KEY)
    assert len(a.tables["kv"].rows) == 2
    con.execute("DELETE FROM kv WHERE a = 'x|y'")
    con.commit()
    assert (
        diff(a, take_snapshot(connect, "sqlite", key=KEY, before=a), key=KEY)[
            "kv"
        ].removed
        == 1
    )


def test_a_schema_entry_in_the_allow_list_can_only_be_skipped() -> None:
    from openadapt_flow.runtime.effects.frame import AllowedTable

    AllowedTable(table="audit.*", mode="skip", reason="the DBA's own audit trail")
    with pytest.raises(ValueError, match="can only be skipped"):
        AllowedTable(table="audit.*")


@live
def test_composite_keys_that_join_alike_stay_apart(pg: Pg) -> None:
    # Review #1673, S1: ('x|y', 'z') and ('x', 'y|z') are two rows.
    pg.sql(
        "CREATE TABLE kv (a text, b text, PRIMARY KEY (a, b))",
        "INSERT INTO kv VALUES ('x|y', 'z'), ('x', 'y|z')",
    )
    a = pg.snap()
    assert len(a.tables["kv"].rows) == 2
    pg.sql("DELETE FROM kv WHERE a = 'x|y'")
    assert diff(a, pg.snap(before=a), key=KEY)["kv"].removed == 1


@live
def test_large_objects_are_read_by_content_or_skipped(pg: Pg) -> None:
    # Review #1673, S2: lo_put keeps the oid but changes the content.
    pg.sql("SELECT lo_from_bytea(424242, 'scan v1')")
    a = pg.snap()
    assert a.tables["pg_largeobject"].count == 1
    pg.sql("SELECT lo_put(424242, 0, 'SCAN')")
    moved = diff(a, pg.snap(before=a), key=KEY)
    assert list(moved) == ["pg_largeobject"]
    assert moved["pg_largeobject"].changed_columns == ["sha256"]
    b = pg.snap()
    pg.sql("SELECT lo_unlink(424242)")
    assert diff(b, pg.snap(before=b), key=KEY)["pg_largeobject"].removed == 1
    pg.sql("SELECT lo_from_bytea(424243, 'x')")
    with pytest.raises(SnapshotError, match="large objects can't be read"):
        pg.snap(reader=True)
    skip = {"pg_largeobject": "skip"}
    c = pg.snap(reader=True, modes=skip)
    pg.sql("SELECT lo_put(424243, 0, 'y')")
    assert diff(c, pg.snap(before=c, reader=True, modes=skip), key=KEY) == {}


@live
def test_a_counter_set_back_shows(pg: Pg) -> None:
    # Review #1673, N1: setval back lets later inserts reuse ids.
    a = pg.snap()
    pg.sql("SELECT setval('t_id_seq', 1)")
    moved = diff(a, pg.snap(before=a), key=KEY)
    assert moved["t"].unread == ["the counter went back"]


@live
def test_row_level_security_that_hides_rows_is_refused(pg: Pg) -> None:
    pg.sql(
        "ALTER TABLE pairs ENABLE ROW LEVEL SECURITY",
        f"CREATE POLICY only_one ON pairs FOR SELECT TO {pg.reader} USING (a = 1)",
    )
    with pytest.raises(SnapshotError, match="row-level security"):
        pg.snap(reader=True)


@live
def test_another_schema_is_read_and_named_with_it(pg: Pg) -> None:
    pg.sql(
        "CREATE SCHEMA billing",
        "CREATE TABLE billing.invoice (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, n int)",
        "INSERT INTO billing.invoice (n) VALUES (1)",
    )
    a = pg.snap()
    assert a.schemas == ["billing", "public"]
    assert a.tables["billing.invoice"].counter == 1
    pg.sql("INSERT INTO billing.invoice (n) VALUES (2)")
    assert diff(a, pg.snap(before=a), key=KEY)["billing.invoice"].added == 1


@live
def test_a_partitioned_table_is_read_once_through_its_partitions(pg: Pg) -> None:
    pg.sql(
        "CREATE TABLE ev (id int, at date) PARTITION BY RANGE (at)",
        "CREATE TABLE ev_2026 PARTITION OF ev FOR VALUES FROM ('2026-01-01') TO ('2027-01-01')",
        "INSERT INTO ev VALUES (1, '2026-05-01')",
    )
    a = pg.snap()
    assert "ev" not in a.tables and a.tables["ev_2026"].count == 1


@live
def test_a_trigger_planted_in_the_window_shows(pg: Pg) -> None:
    a = pg.snap()
    pg.sql(
        "CREATE FUNCTION f() RETURNS trigger LANGUAGE plpgsql AS 'BEGIN RETURN NEW; END'",
        "CREATE TRIGGER tr BEFORE INSERT ON pairs FOR EACH ROW EXECUTE FUNCTION f()",
    )
    b = pg.snap(before=a)
    assert "(database code)" in diff(a, b, key=KEY)
    pg.sql("ALTER TABLE pairs DISABLE TRIGGER tr")
    assert "(database code)" in diff(b, pg.snap(before=b), key=KEY)


@live
@pytest.mark.parametrize(
    "change",
    [
        "ALTER TYPE mood ADD VALUE 'z'",
        "ALTER DOMAIN posint ADD CONSTRAINT small CHECK (VALUE < 1000)",
        "CREATE RULE r AS ON INSERT TO pairs DO INSTEAD NOTHING",
        "CREATE VIEW v AS SELECT a FROM pairs",
    ],
)
def test_a_change_to_types_rules_or_views_shows(pg: Pg, change: str) -> None:
    a = pg.snap()
    pg.sql(change)
    assert "(database code)" in diff(a, pg.snap(before=a), key=KEY)


@live
def test_a_column_default_is_read_from_the_catalog(pg: Pg) -> None:
    pg.sql(
        "CREATE TABLE d (id serial PRIMARY KEY, s varchar(9) DEFAULT 'open', "
        "n numeric(6,2) DEFAULT 0, at timestamptz DEFAULT now(), f boolean DEFAULT false)"
    )
    defaults = pg.snap().tables["d"].defaults
    assert defaults == {
        "id": db_snapshot.ANY_VALUE,
        "s": "open",
        "n": "0",
        "at": CLOCK_DEFAULT,
        "f": "false",
    }


# -- the frame and its recheck, end to end ----------------------------------------


@live
def test_the_frame_refutes_a_late_write_and_an_extra_record_on_postgresql(
    pg: Pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from psycopg2.extensions import parse_dsn

    from openadapt_flow import verifier
    from openadapt_flow.verifier import (
        Actor,
        Contract,
        begin_write,
        recheck,
        verify_write,
    )

    monkeypatch.setenv("OPENADAPT_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("OPENADAPT_SEAL_HOME", str(tmp_path / "seal"))
    monkeypatch.setattr(verifier, "USED_DIR", tmp_path / "used")
    monkeypatch.setattr(verifier, "seconds_until", lambda due: 0.0)
    monkeypatch.setenv("PG_READER_PW", pg.password)
    pg.sql(
        "CREATE TABLE contact (id serial PRIMARY KEY, name text, email text)",
        "CREATE TABLE note (id serial PRIMARY KEY, body text)",
        f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {pg.reader}",
        f"GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO {pg.reader}",
        "CREATE SCHEMA other",
        "CREATE TABLE other.note (id serial PRIMARY KEY, body text)",
        f"GRANT USAGE ON SCHEMA other TO {pg.reader}",
        f"GRANT SELECT ON ALL TABLES IN SCHEMA other TO {pg.reader}",
        f"GRANT SELECT ON ALL SEQUENCES IN SCHEMA other TO {pg.reader}",
    )
    dsn = parse_dsn(pg.dsn)
    read = {
        "sql_driver": "psycopg2",
        "sql_connect_args": {
            "host": dsn.get("host", "127.0.0.1"),
            "port": int(dsn.get("port", 5432)),
            "user": pg.reader,
            "dbname": pg.name,
        },
        "sql_password_env": "PG_READER_PW",
    }
    contract = Contract.from_dict(
        {
            "schema_version": "openadapt.verify/v1",
            "verifier": {"kind": "sql", **read, "sql_query": "SELECT * FROM contact"},
            "record": {
                "name": "contact",
                "match": ["email"],
                "fields": {"name": "name", "email": "email"},
            },
            "frame": {
                "identity_field": "id",
                "database": {
                    "dialect": "postgresql",
                    "read": read,
                    "views": {"contact": ""},
                    "settle_s": 0,
                    "recheck_after_s": 60,
                    # Must be visible; every other visible schema is read too.
                    "schemas": ["public"],
                },
            },
        }
    )
    bot = Actor(kind="agent", id="bot")

    def save(n: int, *stray: str) -> Any:
        params = {"name": "Ada", "email": f"ada{n}@example.test"}
        before = begin_write(contract, params, actor=bot)
        pg.sql(
            f"INSERT INTO contact (name, email) VALUES ('Ada', 'ada{n}@example.test')",
            *stray,
        )
        return params, verify_write(before)

    params, ok = save(1)
    assert ok.verified, ok.reasons
    assert recheck(contract, ok, params=params, actor=bot).outcome == "CONFIRMED"

    params, extra = save(2, "INSERT INTO note (body) VALUES ('stray')")
    assert not extra.verified and extra.outcome == "REFUTED", extra.reasons

    # Review #1673, B1: a stray row in a schema the contract doesn't name.
    params, other = save(4, "INSERT INTO other.note (body) VALUES ('stray')")
    assert not other.verified and other.outcome == "REFUTED", other.reasons

    params, late = save(3)
    assert late.verified, late.reasons
    pg.sql("UPDATE contact SET name = 'Eve' WHERE email = 'ada3@example.test'")
    assert recheck(contract, late, params=params, actor=bot).outcome == "REFUTED"
