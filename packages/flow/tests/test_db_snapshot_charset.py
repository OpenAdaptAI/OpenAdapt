"""Lossy MySQL sessions must never hide text edits or supply new-row values.

Live tests use the isolated scratch database connection described in
``test_db_snapshot_column_types.py`` (``OPENADAPT_SNAPSHOT_MYSQL``).
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from types import ModuleType
from typing import Any, Iterator

import pytest

from openadapt_flow.deployment import EffectsConfig, build_effect_verifier
from openadapt_flow.runtime.effects import Effect, EffectKind, Verdict
from openadapt_flow.runtime.effects import db_snapshot as snap
from openadapt_flow.runtime.effects.history import HistoryFeed, _sql
from openadapt_flow.runtime.effects.sql import SqlRecordVerifier, sqlite_read_connection

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
    native_connect = pymysql.connect
    admin = native_connect(**(args | {"charset": "utf8mb4", "autocommit": True}))
    try:
        with admin.cursor() as cur:
            cur.execute(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4")
            cur.execute(f"USE `{name}`")
            cur.execute(
                "CREATE TABLE t (id INT PRIMARY KEY AUTO_INCREMENT, "
                "body TEXT CHARACTER SET utf8mb4, payload VARBINARY(4))"
            )

        def connect(charset: str = "utf8mb4", **extra: Any) -> Any:
            return native_connect(
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
@pytest.mark.parametrize("entrypoint", ["direct", "configured"])
@pytest.mark.parametrize("charset", ["latin1", "utf8", "utf8mb4"])
def test_mysql_effect_readback_requires_exact_result_charset(
    charset_database: Any,
    charset: str,
    entrypoint: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pymysql
    from pymysql.cursors import Cursor

    admin, connect = charset_database
    baseline_body = "Synthetic résumé"
    new_body = "Synthetic example 😀"
    with admin.cursor() as cursor:
        cursor.execute("INSERT INTO t (body) VALUES (%s)", (baseline_body,))

    sessions: list[tuple[Any, list[str]]] = []

    def open_session(charset: str) -> Any:
        statements: list[str] = []

        class RecordingCursor(Cursor):
            def execute(self, query: str, args: Any = None) -> Any:
                statements.append(query)
                return super().execute(query, args)

        session = connect(charset, cursorclass=RecordingCursor)
        statements.clear()
        sessions.append((session, statements))
        return session

    query = "SELECT id, body FROM t"
    if entrypoint == "configured":
        monkeypatch.setattr(pymysql, "connect", open_session)
        verifier = build_effect_verifier(
            EffectsConfig(
                kind="sql",
                sql_driver="pymysql",
                sql_connect_args={"charset": charset},
                sql_query=query,
            )
        )
    else:
        verifier = SqlRecordVerifier(
            lambda: open_session(charset), query, source_metadata=True
        )
    assert isinstance(verifier, SqlRecordVerifier)
    expected = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"body": new_body},
        expected_count=1,
        count_new_only=True,
        timeout_s=1.0,
    )
    exact = charset == "utf8mb4"
    error = "MySQL reads require character_set_results=utf8mb4"
    charset_query = "SELECT @@SESSION.character_set_results"
    read_only = "SET SESSION TRANSACTION READ ONLY"
    try:
        before = verifier.capture_pre_state()
        assert before.reachable is exact and before.readback_complete is exact
        if exact:
            assert before.records == [{"id": 1, "body": baseline_body}]
            assert "read_error" not in before.detail
        else:
            assert before.records == []
            assert before.detail["read_error"] == error

        with admin.cursor() as cursor:
            cursor.execute("INSERT INTO t (body) VALUES (%s)", (new_body,))
        result = verifier.verify(expected, before)
        if exact:
            assert result.verdict is Verdict.CONFIRMED and result.observed_count == 1
            assert result.matched_records == [{"id": 2, "body": new_body}]
        else:
            assert result.verdict is Verdict.INDETERMINATE
            assert result.unavailable and result.should_halt
            assert result.reason == error and result.matched_records == []

        expected_statements = [charset_query, query] if exact else [charset_query]
        if entrypoint == "configured":
            expected_statements.insert(0, read_only)
        assert len(sessions) == 2 and sessions[0][0] is not sessions[1][0]
        for session, statements in sessions:
            assert not session.open
            assert statements == expected_statements
    finally:
        for session, _ in sessions:
            if session.open:
                session.close()


@live
@pytest.mark.parametrize("entrypoint", ["direct", "configured"])
@pytest.mark.parametrize("charset", ["latin1", "utf8mb4"])
def test_explicit_mysql_dialect_checks_opaque_connector_on_its_native_session(
    charset_database: Any,
    charset: str,
    entrypoint: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pymysql.cursors import Cursor

    admin, connect = charset_database
    body = "Synthetic résumé 😀"
    with admin.cursor() as cursor:
        cursor.execute("INSERT INTO t (body) VALUES (%s)", (body,))

    statements: list[str] = []

    class RecordingCursor(Cursor):
        def execute(self, query: str, args: Any = None) -> Any:
            statements.append(query)
            return super().execute(query, args)

    native = connect(charset, cursorclass=RecordingCursor)
    statements.clear()
    opens = 0

    class OpaqueConnection:
        def cursor(self) -> Any:
            return native.cursor()

        def close(self) -> None:
            native.close()

    def open_opaque() -> OpaqueConnection:
        nonlocal opens
        opens += 1
        return OpaqueConnection()

    query = "SELECT id, body FROM t"
    try:
        if entrypoint == "configured":
            driver = ModuleType("_synthetic_opaque_connector")
            driver.connect = open_opaque
            monkeypatch.setitem(sys.modules, driver.__name__, driver)
            verifier = build_effect_verifier(
                EffectsConfig(
                    kind="sql",
                    sql_driver=driver.__name__,
                    sql_dialect="mysql",
                    sql_query=query,
                )
            )
        else:
            verifier = SqlRecordVerifier(open_opaque, query, sql_dialect="mysql")
        assert isinstance(verifier, SqlRecordVerifier)
        state = verifier.capture_pre_state()
        expected_statements = ["SELECT @@SESSION.character_set_results"]
        exact = charset == "utf8mb4"
        assert state.reachable is exact and state.readback_complete is exact
        if exact:
            assert state.records == [{"id": 1, "body": body}]
            assert "read_error" not in state.detail
            expected_statements.append(query)
        else:
            assert state.records == []
            assert state.detail["read_error"] == (
                "MySQL reads require character_set_results=utf8mb4"
            )
        # Both statements, when allowed, ran on this one native connection.
        # The preflight never changes the native session's configuration.
        assert opens == 1
        assert statements == expected_statements
        assert not native.open
    finally:
        if native.open:
            native.close()


@live
@pytest.mark.parametrize("entrypoint", ["direct", "configured"])
def test_undeclared_opaque_sql_connector_refuses_before_opening_a_cursor(
    charset_database: Any, entrypoint: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, connect = charset_database
    sessions: list[Any] = []
    cursor_calls = 0

    class OpaqueConnection:
        def __init__(self, session: Any) -> None:
            self.session = session

        def cursor(self) -> Any:
            nonlocal cursor_calls
            cursor_calls += 1
            return self.session.cursor()

        def close(self) -> None:
            self.session.close()

    def open_opaque() -> OpaqueConnection:
        session = connect("latin1")
        sessions.append(session)
        return OpaqueConnection(session)

    query = "SELECT id FROM t"
    try:
        if entrypoint == "configured":
            driver = ModuleType("_synthetic_undeclared_connector")
            driver.connect = open_opaque
            monkeypatch.setitem(sys.modules, driver.__name__, driver)
            verifier = build_effect_verifier(
                EffectsConfig(kind="sql", sql_driver=driver.__name__, sql_query=query)
            )
        else:
            verifier = SqlRecordVerifier(open_opaque, query, source_metadata=True)
        assert isinstance(verifier, SqlRecordVerifier)
        error = "SQL read dialect is unidentified; declare sql_dialect"
        before = verifier.capture_pre_state()
        assert before.reachable is False and before.readback_complete is False
        assert before.records == [] and before.detail["read_error"] == error
        expected = Effect(
            kind=EffectKind.RECORD_WRITTEN, match={"id": "1"}, timeout_s=1.0
        )
        verdict = verifier.verify(expected, before)
        assert verdict.verdict is Verdict.INDETERMINATE
        assert verdict.should_halt and verdict.unavailable
        assert verdict.reason == error and verdict.matched_records == []
        assert cursor_calls == 0
        assert len(sessions) == 2 and sessions[0] is not sessions[1]
        assert all(not session.open for session in sessions)
    finally:
        for session in sessions:
            if session.open:
                session.close()


@pytest.mark.parametrize("entrypoint", ["direct", "configured"])
def test_sqlite_effect_readback_keeps_exact_values_and_source(
    tmp_path: Path, entrypoint: str
) -> None:
    database = str(tmp_path / "records.sqlite3")
    body = "Synthetic example 😀"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, body TEXT)")
        connection.execute("INSERT INTO t VALUES (1, ?)", (body,))

    query = "SELECT * FROM t"
    if entrypoint == "configured":
        verifier = build_effect_verifier(
            EffectsConfig(kind="sql", sqlite_database=database, sql_query=query)
        )
    else:
        verifier = SqlRecordVerifier(
            lambda: sqlite_read_connection(database), query, source_metadata=True
        )
    assert isinstance(verifier, SqlRecordVerifier)
    state = verifier.capture_pre_state()
    assert state.reachable and state.readback_complete is True
    assert state.records == [{"id": 1, "body": body}]
    assert "read_error" not in state.detail
    assert verifier.source_relation == ("main", "t", 0)


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
