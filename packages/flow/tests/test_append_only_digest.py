"""Append-only row bodies are incremental; historical rows still get checked."""

from __future__ import annotations

import copy
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from openadapt_flow.runtime.effects import db_snapshot as snap
from tests.test_db_snapshot_column_types import COLUMNS, database, live  # noqa: F401

KEY = b"k" * 32


def _take(connect: Any, before: Any = None, **kw: Any) -> Any:
    return snap.take_snapshot(
        connect, "mysql", key=KEY, modes={"t": "append_only"}, before=before, **kw
    )


@live
def test_historical_payloads_stay_on_the_server_and_only_new_bodies_are_read(
    database: Any,
) -> None:
    import pymysql

    admin, _, connect = database
    with admin.cursor() as cur:
        cur.execute("ALTER TABLE t ADD body LONGTEXT")
        cur.execute("UPDATE t SET body = REPEAT('x', 1048576)")
    payload_reads: list[str] = []
    digest_cell_sizes: list[int] = []

    class Cursor(pymysql.cursors.Cursor):
        def execute(self, sql: str, args: Any = None) -> Any:
            self.query = sql
            if "JSON_OBJECT(" in sql and "SHA2(" not in sql:
                payload_reads.append(sql)
            return super().execute(sql, args)

        def fetchall(self) -> Any:
            rows = super().fetchall()
            if "SHA2(" in self.query:
                digest_cell_sizes.extend(
                    len(v) for row in rows for v in row if isinstance(v, (str, bytes))
                )
            return rows

    reader = lambda: connect(cursorclass=Cursor)
    before = _take(reader)
    assert payload_reads == []
    assert max(digest_cell_sizes) <= 64
    assert before.tables["t"].row_hash_format == "mysql-json-sha256/v1"
    assert before.tables["t"].values == {}
    restored = snap.Snapshot.from_json(before.to_json())
    assert snap.same_state(before, _take(reader, before=restored))
    assert payload_reads == []
    with admin.cursor() as cur:
        cur.execute("INSERT INTO t (body) VALUES (REPEAT('y', 1048576))")
        ident = int(cur.lastrowid)
    after = _take(reader, before=restored)
    assert len(after.tables["t"].rows) == len(before.tables["t"].rows) + 1
    assert len(after.tables["t"].values) == 1
    assert next(iter(after.tables["t"].values.values()))["body"] == "y" * 1048576
    assert len(payload_reads) == 1
    assert f"IN ({ident})" in payload_reads[0]
    assert snap.unread_reasons(before.tables["t"], after.tables["t"], "mysql") == []


@live
@pytest.mark.parametrize("column,_type,_old,new", COLUMNS, ids=[c[0] for c in COLUMNS])
def test_an_old_append_only_row_still_refuses_every_stored_type_change(
    database: Any, column: str, _type: str, _old: str, new: str
) -> None:
    admin, _, connect = database
    before = _take(connect)
    with admin.cursor() as cur:
        cur.execute(f"UPDATE t SET {column} = {new} WHERE id = 1")
    after = _take(connect, before=before)
    a, b = before.tables["t"], after.tables["t"]
    assert "existing rows changed or removed" in snap.unread_reasons(a, b, "mysql")
    assert sum(a.rows[k][0] != b.rows[k][0] for k in a.rows) == 1
    assert b.values == {}  # changed historical bodies were not transferred


@live
def test_an_append_in_an_old_id_gap_is_new_even_below_max_id(database: Any) -> None:
    admin, _, connect = database
    with admin.cursor() as cur:
        cur.execute("DELETE FROM t WHERE id = 2")
    before = _take(connect)
    assert before.tables["t"].max_auto == 3
    with admin.cursor() as cur:
        cur.execute("INSERT INTO t (id) VALUES (2)")
    after = _take(connect, before=before)
    assert len(after.tables["t"].values) == 1
    assert next(iter(after.tables["t"].values.values()))["id"] == 2
    assert snap.unread_reasons(before.tables["t"], after.tables["t"], "mysql") == []


@live
def test_replacing_an_old_row_with_a_new_row_cannot_hide_behind_equal_counts(
    database: Any,
) -> None:
    admin, _, connect = database
    before = _take(connect)
    with admin.cursor() as cur:
        cur.execute("DELETE FROM t WHERE id = 1")
        cur.execute("INSERT INTO t (id) VALUES (4)")
    after = _take(connect, before=before)
    assert before.tables["t"].count == after.tables["t"].count
    assert "existing rows changed or removed" in snap.unread_reasons(
        before.tables["t"], after.tables["t"], "mysql"
    )


@live
def test_two_old_payload_edits_cannot_cancel_each_other(database: Any) -> None:
    admin, _, connect = database
    before = _take(connect)
    with admin.cursor() as cur:
        cur.execute("UPDATE t SET c_text = 'changed' WHERE id IN (1, 2)")
    after = _take(connect, before=before)
    a, b = before.tables["t"], after.tables["t"]
    assert sum(a.rows[k][0] != b.rows[k][0] for k in a.rows) == 2
    assert "existing rows changed or removed" in snap.unread_reasons(a, b, "mysql")


@live
def test_explicitly_ignored_columns_keep_their_existing_meaning(database: Any) -> None:
    admin, _, connect = database
    before = _take(connect, ignore={"t": ["c_text"]})
    with admin.cursor() as cur:
        cur.execute("UPDATE t SET c_text = 'changed' WHERE id = 1")
    after = _take(connect, before=before, ignore={"t": ["c_text"]})
    assert snap.same_state(before, after)


@live
@pytest.mark.parametrize("bad", [None, "0" * 63, "g" * 64])
def test_missing_or_malformed_server_digests_refuse_the_snapshot(
    database: Any, bad: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, connect = database
    read = snap._Session.rows

    def rows(self: Any, sql: str, **kw: Any) -> Any:
        values = read(self, sql, **kw)
        if "SHA2(" in sql:
            values[0] = (values[0][0], bad, *values[0][2:])
        return values

    monkeypatch.setattr(snap._Session, "rows", rows)
    with pytest.raises(snap.SnapshotError, match="server digest could not be read"):
        _take(connect)


@live
def test_a_legacy_baseline_keeps_its_full_row_encoding(
    database: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, connect = database
    # The original append-only encoding has the same contents MAC as a
    # compared row, with no per-cell MACs. Build that exact persisted shape.
    before = snap.take_snapshot(connect, "mysql", key=KEY)
    table = before.tables["t"]
    table.mode = "append_only"
    table.cell_columns = []
    for value in table.rows.values():
        value[1] = ""
    restored = snap.Snapshot.from_json(before.to_json())
    assert "row_hash_format" not in before.to_json()["tables"]["t"]

    def forbidden(*args: Any, **kw: Any) -> Any:
        pytest.fail("a legacy baseline must not switch hash encodings")

    monkeypatch.setattr(snap, "_read_append_only_mysql", forbidden)
    after = _take(connect, before=restored)
    assert snap.same_state(before, after)
    assert after.tables["t"].row_hash_format == ""


def test_a_hash_format_mismatch_never_holds_a_frame(tmp_path: Path) -> None:
    path = tmp_path / "synthetic.sqlite"
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, body TEXT)")
        con.execute("INSERT INTO t VALUES (1, 'synthetic')")
    connect = lambda: sqlite3.connect(path)
    before = snap.take_snapshot(connect, "sqlite", key=KEY)
    after = copy.deepcopy(before)
    after.tables["t"].row_hash_format = "unknown/future-format"
    assert snap.unread_reasons(before.tables["t"], after.tables["t"]) == [
        "row hash format changed"
    ]
    assert not snap.same_state(before, after)
    assert not snap.settled_state(before, after)
    with pytest.raises(snap.SnapshotError, match="unknown row hash format"):
        snap.take_snapshot(connect, "sqlite", key=KEY, before=after)
