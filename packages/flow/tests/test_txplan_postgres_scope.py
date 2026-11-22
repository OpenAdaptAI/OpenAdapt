"""Unobserved write scope on a real PostgreSQL cluster (issue 2481, item 1).

The frame reads one schema. A trigger that writes another schema must stop
the write, whether it runs at once or is a deferred constraint trigger that
fires only at COMMIT, after every check. The cluster is a throwaway one made
with ``initdb`` in the test's folder; the test skips when PostgreSQL or
psycopg2 is not installed.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any, Iterator

import pytest

pytest.importorskip("cryptography")

from openadapt_flow.txplan import engine  # noqa: E402
from openadapt_flow.txplan.adapters import AdapterError  # noqa: E402
from openadapt_flow.txplan.engine import Ctx, TxRefused  # noqa: E402
from openadapt_flow.txplan.spec import TxSpec  # noqa: E402
from openadapt_flow.txplan.state import State  # noqa: E402


def _bin(name: str) -> str:
    found = shutil.which(name)
    if not found:
        pytest.skip(f"{name} is not installed")
    return found


@pytest.fixture(scope="module")
def cluster(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    pytest.importorskip("psycopg2")
    initdb, pg_ctl = _bin("initdb"), _bin("pg_ctl")
    root = tmp_path_factory.mktemp("pg")
    data = root / "data"
    # A valid locale: macOS PostgreSQL refuses to start without one.
    env = {**os.environ, "LC_ALL": "C", "LANG": "C"}
    subprocess.run(
        [initdb, "-D", str(data), "-U", "lab", "-A", "trust", "--no-sync"],
        check=True,
        capture_output=True,
        env=env,
    )
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    # Loopback TCP only: a temp folder's socket path can pass the 103-byte limit.
    opts = (
        f"-p {port} -c listen_addresses=127.0.0.1 "
        "-c unix_socket_directories='' -c fsync=off"
    )
    subprocess.run(
        [pg_ctl, "-D", str(data), "-o", opts, "-l", str(root / "log"), "-w", "start"],
        check=True,
        capture_output=True,
        env=env,
    )
    try:
        yield {"host": "127.0.0.1", "port": port, "user": "lab", "dbname": "postgres"}
    finally:
        subprocess.run(
            [pg_ctl, "-D", str(data), "-m", "immediate", "stop"],
            capture_output=True,
            env=env,
        )


@pytest.fixture
def db(cluster: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OPENADAPT_SEAL_HOME", str(tmp_path / "seal"))
    import psycopg2

    con = psycopg2.connect(**cluster)
    con.autocommit = True
    cur = con.cursor()
    cur.execute(
        """
        DROP SCHEMA IF EXISTS side CASCADE;
        DROP TABLE IF EXISTS customers CASCADE;
        DROP FUNCTION IF EXISTS note_elsewhere() CASCADE;
        CREATE TABLE customers (id integer PRIMARY KEY, balance integer);
        INSERT INTO customers SELECT g, g * 10 FROM generate_series(1, 5) g;
        CREATE SCHEMA side;
        CREATE TABLE side.notes (id serial PRIMARY KEY, customer integer);
        CREATE FUNCTION note_elsewhere() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN INSERT INTO side.notes (customer) VALUES (NEW.id); RETURN NULL; END $$;
        """
    )
    yield cluster, cur
    con.close()


def _ctx(cluster: dict[str, Any], tmp_path: Path) -> Ctx:
    spec = TxSpec.model_validate(
        {
            "schema_version": "openadapt.tx/v1",
            "systems": [
                {
                    "name": "crm",
                    "kind": "sql",
                    "dialect": "postgresql",
                    "driver": "psycopg2",
                    "connect_args": cluster,
                    "tables": {"customers": "id"},
                }
            ],
        }
    )
    return Ctx(spec=spec, state=State(tmp_path / "state"))


def _attempt(ctx: Ctx, write: dict[str, Any]) -> Any:
    """Plan, approve and apply; a refusal anywhere is the fail-closed answer."""
    try:
        plan = engine.make_plan(ctx, "T-pg", [write])
        engine.approve_plan(
            ctx.state, plan["digest"], approver="lab", decided_by="automation"
        )
        return engine.apply_plan(ctx, plan["digest"])
    except (TxRefused, AdapterError) as exc:
        return exc


def _notes(cur: Any) -> int:
    cur.execute("SELECT count(*) FROM side.notes")
    return int(cur.fetchone()[0])


def _balance(cur: Any, rid: int) -> int:
    cur.execute("SELECT balance FROM customers WHERE id = %s", (rid,))
    return int(cur.fetchone()[0])


def test_a_deferred_trigger_writing_another_schema_is_never_verified(
    db: Any, tmp_path: Path
) -> None:
    # The constraint trigger fires at COMMIT: a check before the commit sees
    # nothing outside the schema, and the frame never reads side.notes.
    cluster, cur = db
    cur.execute(
        "CREATE CONSTRAINT TRIGGER late AFTER UPDATE ON customers "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION note_elsewhere()"
    )
    out = _attempt(
        _ctx(cluster, tmp_path),
        {
            "system": "crm",
            "op": "sql",
            "fields": {"sql": "UPDATE customers SET balance = 7 WHERE id = 2"},
        },
    )
    assert not (isinstance(out, dict) and out["outcome"] == "VERIFIED"), out
    assert _notes(cur) == 0 and _balance(cur, 2) == 20


def test_an_immediate_trigger_on_a_planned_update_writing_another_schema_is_stopped(
    db: Any, tmp_path: Path
) -> None:
    # A planned update (not raw SQL) never ran the scope check at all.
    cluster, cur = db
    cur.execute(
        "CREATE TRIGGER now_ AFTER UPDATE ON customers "
        "FOR EACH ROW EXECUTE FUNCTION note_elsewhere()"
    )
    out = _attempt(
        _ctx(cluster, tmp_path),
        {
            "system": "crm",
            "collection": "customers",
            "op": "update",
            "id": 3,
            "fields": {"balance": 9},
        },
    )
    assert not (isinstance(out, dict) and out["outcome"] == "VERIFIED"), out
    assert _notes(cur) == 0 and _balance(cur, 3) == 30


def test_a_write_that_stays_in_the_schema_is_verified(db: Any, tmp_path: Path) -> None:
    cluster, cur = db
    out = _attempt(
        _ctx(cluster, tmp_path),
        {
            "system": "crm",
            "op": "sql",
            "fields": {"sql": "UPDATE customers SET balance = 7 WHERE id = 2"},
        },
    )
    assert isinstance(out, dict) and out["outcome"] == "VERIFIED", out
    assert _balance(cur, 2) == 7


# -- always on: the statement order, and MySQL's trigger inventory -------------
# These run without a server. They pin what the real-cluster tests above
# measure, so a CI job without PostgreSQL still catches a regression; the
# MySQL branch has no server in this suite at all.


class _Cursor:
    """A DB-API cursor that records statements and answers the scope queries."""

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.sql: list[str] = []
        self.rowcount = 1
        self._last: Any = None
        self.lastrowid = 1

    def execute(self, sql: str, params: Any = None) -> None:
        self.sql.append(sql)
        self._last = next((v for k, v in self.answers.items() if k in sql), [(0,)])

    def fetchone(self) -> Any:
        return self._last[0]

    def fetchall(self) -> Any:
        return list(self._last)


def _adapter(dialect: str) -> Any:
    from openadapt_flow.txplan.adapters import SqlAdapter
    from openadapt_flow.txplan.spec import SqlSystem

    return SqlAdapter(
        SqlSystem(
            name="crm",
            kind="sql",
            dialect=dialect,  # type: ignore[arg-type]
            driver="x",
            tables={"customers": "id"},
        )
    )


def test_postgres_fires_deferred_triggers_before_the_scope_check_on_every_op() -> None:
    for write in (
        {"collection": "customers", "op": "update", "id": 1, "fields": {"b": 2}},
        {"collection": "customers", "op": "delete", "id": 1, "fields": {}},
        {"collection": "statement", "op": "sql", "fields": {"sql": "UPDATE c SET b=1"}},
    ):
        cur = _Cursor({"pg_stat_xact_user_tables": [(0,)]})
        _adapter("postgresql")._exec(cur, write, None)
        order = [s for s in cur.sql if "SET CONSTRAINTS" in s or "pg_stat_xact" in s]
        assert (
            order
            == [
                "SET CONSTRAINTS ALL IMMEDIATE",
                order[1],
            ]
            and "pg_stat_xact" in order[1]
        ), (write["op"], cur.sql)


def test_mysql_refuses_a_trigger_that_writes_another_database_or_calls_a_routine() -> (
    None
):
    from openadapt_flow.txplan.adapters import Diverged

    def cur(body: str) -> _Cursor:
        return _Cursor(
            {
                "SELECT DATABASE()": [("crm",)],
                "SHOW DATABASES": [("crm",), ("ledger",)],
                "information_schema.triggers": [(body,)],
            }
        )

    adapter = _adapter("mysql")
    for body in (
        "INSERT INTO ledger.notes (customer) VALUES (NEW.id)",
        "CALL note_it(NEW.id)",
    ):
        with pytest.raises(Diverged):
            adapter._refuse_unobserved_writes(cur(body), "UPDATE customers SET b = 1")
    # A trigger that stays in the database, reading NEW and OLD, is fine.
    adapter._refuse_unobserved_writes(
        cur("INSERT INTO notes (customer) VALUES (NEW.id)"),
        "UPDATE customers SET b = 1",
    )
