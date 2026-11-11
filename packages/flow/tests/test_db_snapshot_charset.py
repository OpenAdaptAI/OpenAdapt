"""Lossy MySQL sessions must never hide text edits or supply new-row values.

Live tests use the isolated scratch database connection described in
``test_db_snapshot_column_types.py`` (``OPENADAPT_SNAPSHOT_MYSQL``).
"""

from __future__ import annotations

import json
import os
import secrets
from typing import Any, Iterator

import pytest

from openadapt_flow.runtime.effects import db_snapshot as snap
from openadapt_flow.runtime.effects.history import HistoryFeed, _sql
from openadapt_flow.runtime.effects.sql import SqlRecordVerifier

KEY = b"k" * 32


@pytest.mark.parametrize("rows", [[("latin1",)], [("utf8",)], [(None,)], []])
@pytest.mark.parametrize("mode", ["compare", "append_only"])
def test_unsafe_or_unknown_results_refuse_before_any_database_read(
    rows: list[tuple[Any, ...]], mode: str
) -> None:
    queries: list[str] = []

    class Connection:
        closed = False

        def cursor(self) -> Any:
            return self

        def execute(self, sql: str) -> None:
            queries.append(sql)
            assert sql == "SELECT @@SESSION.character_set_results"

        def fetchall(self) -> Any:
            return rows

        def close(self) -> None:
            self.closed = True

    con = Connection()
    with pytest.raises(snap.SnapshotError, match="character_set_results=utf8mb4"):
        snap.take_snapshot(lambda: con, "mysql", key=KEY, modes={"t": mode})
    assert queries == ["SELECT @@SESSION.character_set_results"]
    assert con.closed


_ARGS = os.environ.get("OPENADAPT_SNAPSHOT_MYSQL")
live = pytest.mark.skipif(
    not _ARGS, reason="set OPENADAPT_SNAPSHOT_MYSQL to pymysql connect args (JSON)"
)
CASES = [("latin1", "Ā one", "ā one"), ("utf8", "😀 one", "😁 one")]


@pytest.fixture
def charset_database() -> Iterator[Any]:
    import pymysql

    args = json.loads(_ARGS or "{}")
    name = "openadapt_charset_" + secrets.token_hex(4)
    admin = pymysql.connect(**(args | {"charset": "utf8mb4", "autocommit": True}))
    try:
        with admin.cursor() as cur:
            cur.execute(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4")
            cur.execute(f"USE `{name}`")
            cur.execute(
                "CREATE TABLE t (id INT PRIMARY KEY AUTO_INCREMENT, "
                "body TEXT CHARACTER SET utf8mb4, payload VARBINARY(4))"
            )

        def connect(charset: str = "utf8mb4", **extra: Any) -> Any:
            return pymysql.connect(
                **(args | {"database": name, "charset": charset} | extra)
            )

        yield admin, connect
    finally:
        with admin.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS `{name}`")
        admin.close()


@live
@pytest.mark.parametrize("charset", ["latin1", "utf8", "utf8mb4"])
def test_mysql_history_requires_exact_result_charset(
    charset_database: Any, charset: str
) -> None:
    from pymysql.cursors import Cursor

    admin, connect = charset_database
    body = "Résumé 😀"
    with admin.cursor() as cur:
        cur.execute("INSERT INTO t (body) VALUES (%s)", (body,))

    statements: list[str] = []

    class RecordingCursor(Cursor):
        def execute(self, query: str, args: Any = None) -> Any:
            statements.append(query)
            return super().execute(query, args)

    query = "SELECT id, body FROM t"
    coverage = (
        "SELECT COUNT(*) AS event_count, "
        "'2025-01-01T00:00:00Z' AS retained_since, "
        "'2025-01-02T00:00:00Z' AS `through` FROM t"
    )
    feed = HistoryFeed(
        read={"kind": "sql", "sql_driver": "pymysql", "sql_query": query},
        dialect="mysql",
        coverage_query=coverage,
    )
    con = connect(charset, cursorclass=RecordingCursor)
    statements.clear()
    charset_query = "SELECT @@SESSION.character_set_results"
    try:
        reader = SqlRecordVerifier(lambda: con, query)
        if charset != "utf8mb4":
            with pytest.raises(
                snap.SnapshotError, match="character_set_results=utf8mb4"
            ):
                _sql(feed, reader, budget=1)
            assert statements == [charset_query]
        else:
            rows, retained_since, through = _sql(feed, reader, budget=1)
            assert rows == [{"id": 1, "body": body}]
            assert retained_since.isoformat() == "2025-01-01T00:00:00+00:00"
            assert through.isoformat() == "2025-01-02T00:00:00+00:00"
            assert statements[0] == charset_query
            assert statements.count(charset_query) == 1
        assert not con.open
    finally:
        if con.open:
            con.close()


@live
@pytest.mark.parametrize("charset,old,new", CASES)
@pytest.mark.parametrize("mode", ["compare", "append_only"])
def test_lossy_sessions_refuse_baselines_edits_and_new_rows(
    charset_database: Any, charset: str, old: str, new: str, mode: str
) -> None:
    admin, connect = charset_database
    with admin.cursor() as cur:
        cur.execute("INSERT INTO t (body) VALUES (%s)", (old,))

    def take(charset: str, before: Any = None) -> Any:
        return snap.take_snapshot(
            lambda: connect(charset), "mysql", key=KEY, modes={"t": mode}, before=before
        )

    with pytest.raises(snap.SnapshotError, match="character_set_results=utf8mb4"):
        take(charset)
    before = take("utf8mb4")
    with admin.cursor() as cur:
        cur.execute("UPDATE t SET body = %s WHERE id = 1", (new,))
    with pytest.raises(snap.SnapshotError, match="character_set_results=utf8mb4"):
        take(charset, before)
    with admin.cursor() as cur:
        cur.execute("INSERT INTO t (body) VALUES (%s)", (new,))
    with pytest.raises(snap.SnapshotError, match="character_set_results=utf8mb4"):
        take(charset, before)


@live
@pytest.mark.parametrize("_charset,old,new", CASES)
@pytest.mark.parametrize("mode", ["compare", "append_only"])
def test_utf8mb4_detects_edits_and_keeps_exact_new_values(
    charset_database: Any, _charset: str, old: str, new: str, mode: str
) -> None:
    admin, connect = charset_database
    with admin.cursor() as cur:
        cur.execute("INSERT INTO t (body, payload) VALUES (%s, X'80')", (old,))

    def take(before: Any = None) -> Any:
        return snap.take_snapshot(
            connect, "mysql", key=KEY, modes={"t": mode}, before=before
        )

    before = take()
    assert snap.diff(before, take(before), key=KEY) == {}
    with admin.cursor() as cur:
        cur.execute("UPDATE t SET body = %s, payload = X'81' WHERE id = 1", (new,))
    edited = take(before)
    assert snap.diff(before, edited, key=KEY)
    if mode == "compare":
        moved = snap.diff(before, edited, key=KEY)["t"]
        assert (moved.changed, moved.changed_columns) == (1, ["body", "payload"])
        assert next(iter(edited.tables["t"].values.values()))["body"] == new
    else:
        assert "existing rows changed or removed" in snap.unread_reasons(
            before.tables["t"], edited.tables["t"], "mysql"
        )
        assert edited.tables["t"].values == {}
    with admin.cursor() as cur:
        cur.execute("INSERT INTO t (body, payload) VALUES (%s, X'80')", (new,))
    appended = take(edited)
    if mode == "compare":
        assert snap.diff(edited, appended, key=KEY)["t"].added == 1
    else:
        edited_table = edited.tables["t"]
        appended_table = appended.tables["t"]
        new_keys = appended_table.rows.keys() - edited_table.rows.keys()
        assert len(new_keys) == 1
        assert edited_table.rows.keys() <= appended_table.rows.keys()
        assert snap.unread_reasons(edited_table, appended_table, "mysql") == []
        assert appended_table.values.keys() == new_keys
    (row,) = appended.tables["t"].values.values()
    assert row["body"] == new
    assert row["payload"] == "80"


@live
def test_utf8mb4_append_only_bodies_stay_incremental_and_budgeted(
    charset_database: Any,
) -> None:
    import pymysql

    admin, connect = charset_database
    with admin.cursor() as cur:
        cur.execute("ALTER TABLE t MODIFY COLUMN body MEDIUMTEXT CHARACTER SET utf8mb4")
        cur.execute("INSERT INTO t (body) VALUES (REPEAT('x', 1048576))")
    payload_reads: list[str] = []

    class Cursor(pymysql.cursors.Cursor):
        def execute(self, sql: str, args: Any = None) -> Any:
            if "JSON_OBJECT(" in sql and "SHA2(" not in sql:
                payload_reads.append(sql)
            return super().execute(sql, args)

    def take(before: Any = None, budget: int = 2) -> Any:
        return snap.take_snapshot(
            lambda: connect(cursorclass=Cursor),
            "mysql",
            key=KEY,
            modes={"t": "append_only"},
            before=before,
            read_row_budget=budget,
        )

    before = take()
    assert payload_reads == []
    with admin.cursor() as cur:
        cur.execute("INSERT INTO t (body) VALUES (%s)", ("😁 exact",))
    after = take(before)
    assert len(payload_reads) == 1
    assert "IN (2)" in payload_reads[0]
    assert next(iter(after.tables["t"].values.values()))["body"] == "😁 exact"
    assert snap.unread_reasons(before.tables["t"], after.tables["t"], "mysql") == []
    payload_reads.clear()
    with pytest.raises(snap.SnapshotTooLarge):
        take(before, budget=1)
    assert payload_reads == []
