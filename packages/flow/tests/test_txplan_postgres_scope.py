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
    _reset_remote(cur)
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
    _reset_remote(cur)
    con.close()


def _reset_remote(cur: Any) -> None:
    """Drop what the foreign-table test makes: it would cap every later write."""
    cur.execute("DROP EXTENSION IF EXISTS postgres_fdw CASCADE")
    cur.execute("DROP DATABASE IF EXISTS remote WITH (FORCE)")


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


def test_a_trigger_that_truncates_another_schema_is_never_verified(
    db: Any, tmp_path: Path
) -> None:
    # TRUNCATE zeroes the transaction's row counts for the table, so the
    # count check sees nothing, and the frame never reads side.notes.
    cluster, cur = db
    cur.execute(
        "INSERT INTO side.notes (customer) SELECT g FROM generate_series(1, 4) g"
    )
    cur.execute(
        "CREATE FUNCTION wipe_elsewhere() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN TRUNCATE side.notes; RETURN NULL; END $$"
    )
    cur.execute(
        "CREATE TRIGGER wipe AFTER UPDATE ON customers "
        "FOR EACH STATEMENT EXECUTE FUNCTION wipe_elsewhere()"
    )
    try:
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
    finally:
        cur.execute("DROP FUNCTION wipe_elsewhere() CASCADE")
    assert isinstance(out, dict), out  # capped, not refused
    assert out["outcome"] == "COMPLETED_UNVERIFIED", out
    assert "truncate" in out["tx"]["not_covered"]
    assert _notes(cur) == 0 and _balance(cur, 3) == 9  # it ran, unverified


def test_a_planned_truncate_of_another_schema_is_never_verified(
    db: Any, tmp_path: Path
) -> None:
    cluster, cur = db
    cur.execute(
        "INSERT INTO side.notes (customer) SELECT g FROM generate_series(1, 4) g"
    )
    out = _attempt(
        _ctx(cluster, tmp_path),
        {"system": "crm", "op": "sql", "fields": {"sql": "TRUNCATE side.notes"}},
    )
    assert not (isinstance(out, dict) and out["outcome"] == "VERIFIED"), out
    if isinstance(out, dict):
        assert out["outcome"] == "COMPLETED_UNVERIFIED", out
        assert "truncate" in out["tx"]["not_covered"]


def test_a_trigger_writing_a_foreign_table_is_never_verified(
    db: Any, tmp_path: Path
) -> None:
    # postgres_fdw over loopback: the delete runs in another backend, so this
    # transaction's row counts never see it, and pg_tables never frames the
    # foreign table.
    cluster, cur = db
    cur.execute("CREATE DATABASE remote")
    import psycopg2

    far = psycopg2.connect(**{**cluster, "dbname": "remote"})
    far.autocommit = True
    far.cursor().execute(
        "CREATE TABLE notes (id integer PRIMARY KEY); "
        "INSERT INTO notes SELECT generate_series(1, 4)"
    )
    cur.execute(
        f"""
        CREATE EXTENSION postgres_fdw;
        CREATE SERVER loop FOREIGN DATA WRAPPER postgres_fdw
          OPTIONS (host '{cluster["host"]}', port '{cluster["port"]}', dbname 'remote');
        CREATE USER MAPPING FOR CURRENT_USER SERVER loop OPTIONS (user '{cluster["user"]}');
        CREATE FOREIGN TABLE far_notes (id integer) SERVER loop
          OPTIONS (table_name 'notes');
        CREATE FUNCTION wipe_far() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN DELETE FROM far_notes; RETURN NULL; END $$;
        CREATE TRIGGER far AFTER UPDATE ON customers
          FOR EACH STATEMENT EXECUTE FUNCTION wipe_far();
        """
    )
    try:
        out = _attempt(
            _ctx(cluster, tmp_path),
            {
                "system": "crm",
                "collection": "customers",
                "op": "update",
                "id": 2,
                "fields": {"balance": 8},
            },
        )
        fc = far.cursor()
        fc.execute("SELECT count(*) FROM notes")
        left = int(fc.fetchone()[0])
    finally:
        far.close()
        cur.execute("DROP FUNCTION wipe_far() CASCADE")
    assert isinstance(out, dict), out  # capped, not refused
    assert out["outcome"] == "COMPLETED_UNVERIFIED", out
    assert {"foreign_data", "remote_link"} <= set(out["tx"]["not_covered"])
    assert left == 0 and _balance(cur, 2) == 8  # it ran, unverified


def test_a_trigger_that_stays_in_the_schema_is_verified(
    db: Any, tmp_path: Path
) -> None:
    # The inventory is an allowlist, not a refusal of triggers: a PL/pgSQL
    # trigger that writes the frame's own schema and calls only built-ins
    # keeps the write VERIFIED.
    cluster, cur = db
    cur.execute(
        """
        CREATE TABLE audit (id serial PRIMARY KEY, customer integer, at timestamptz);
        CREATE FUNCTION note_here() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN INSERT INTO audit (customer, at) VALUES (NEW.id, now()); RETURN NULL; END $$;
        CREATE TRIGGER here AFTER UPDATE ON customers
          FOR EACH ROW EXECUTE FUNCTION note_here();
        """
    )
    try:
        out = _attempt(
            _ctx(cluster, tmp_path),
            {
                "system": "crm",
                "collection": "customers",
                "op": "update",
                "id": 4,
                "fields": {"balance": 1},
            },
        )
    finally:
        cur.execute("DROP FUNCTION note_here() CASCADE; DROP TABLE audit")
    assert isinstance(out, dict) and out["outcome"] == "VERIFIED", out


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
        if isinstance(self._last, Exception):
            raise self._last

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


# -- always on: the closed write-scope inventory, one cap at a time ------------
# Each case is a planned update run through ``_exec`` on a recording cursor
# whose catalog answers hold exactly one open item. The head before the
# inventory reports nothing for any of them.

_UPDATE = {"collection": "customers", "op": "update", "id": 1, "fields": {"b": 2}}


def _pg(**over: Any) -> dict[str, Any]:
    """Catalog answers for a closed PostgreSQL scope, with ``over`` applied."""
    base: dict[str, Any] = {
        "pg_stat_xact_user_tables": [(0,)],
        "current_schemas": [("public", ["public"])],
        "pg_foreign_data_wrapper": [(0,)],
        "pg_extension": [(0,)],
        "pg_operator": [(0,)],
        "pg_rewrite": [(0,)],
        "pg_depend": [(0,)],
        "pg_trigger": [],
        "nspname = ANY": [],
        "proname = ANY": [],
    }
    return {**base, **over}


def _plpgsql(body: str) -> list[tuple[Any, ...]]:
    return [("plpgsql", False, False, "public", body)]


@pytest.mark.parametrize(
    ("answers", "sql", "gap"),
    [
        (_pg(current_schemas=[("public", ["public", "side"])]), "", "search_path_open"),
        (_pg(pg_foreign_data_wrapper=[(1,)]), "", "foreign_data"),
        (_pg(pg_extension=[(1,)]), "", "remote_link"),
        (_pg(pg_operator=[(2,)]), "", "user_operator"),
        (_pg(pg_rewrite=[(1,)]), "", "rule"),
        (_pg(pg_depend=[(1,)]), "", "user_function_default"),
        (_pg(pg_trigger=[("c", False, False, "public", "x")]), "", "trigger_not_local"),
        (
            _pg(pg_trigger=[("plpgsql", True, False, "public", "")]),
            "",
            "trigger_not_local",
        ),
        (
            _pg(pg_trigger=[("plpgsql", False, True, "public", "")]),
            "",
            "trigger_not_local",
        ),
        (
            _pg(pg_trigger=_plpgsql("BEGIN TRUNCATE notes; RETURN NULL; END")),
            "",
            "truncate",
        ),
        (
            _pg(pg_trigger=_plpgsql("BEGIN EXECUTE 'x'; RETURN NULL; END")),
            "",
            "dynamic_sql",
        ),
        (_pg(pg_trigger=_plpgsql("BEGIN COPY t TO '/tmp/x'; END")), "", "copy"),
        (
            _pg(
                pg_trigger=_plpgsql("BEGIN INSERT INTO side.n VALUES (NEW.id); END"),
                **{"nspname = ANY": [("side",)]},
            ),
            "",
            "other_schema",
        ),
        (
            _pg(
                pg_trigger=_plpgsql("BEGIN NEW.b := stamp(NEW.b); RETURN NEW; END"),
                **{"proname = ANY": [("stamp", "public")]},
            ),
            "",
            "user_function",
        ),
        (
            _pg(
                pg_trigger=_plpgsql("BEGIN PERFORM lo_unlink(NEW.b); RETURN NEW; END"),
                **{"proname = ANY": [("lo_unlink", "pg_catalog")]},
            ),
            "",
            "system_function_with_effects",
        ),
        (_pg(pg_trigger=_plpgsql("BEGIN x := 'open; END")), "", "body_unparsed"),
        # The planned statement gets the same body test as a trigger.
        (_pg(), "TRUNCATE notes", "truncate"),
        (
            _pg(**{"proname = ANY": [("wipe", "public")]}),
            "UPDATE customers SET b = wipe(b)",
            "user_function",
        ),
        (
            _pg(current_schemas=RuntimeError("permission denied")),
            "",
            "scope_unreadable",
        ),
    ],
)
def test_postgres_scope_inventory_caps_each_open_item(
    answers: dict[str, Any], sql: str, gap: str
) -> None:
    write = (
        {"collection": "statement", "op": "sql", "fields": {"sql": sql}}
        if sql
        else _UPDATE
    )
    cur = _Cursor(answers)
    result = _adapter("postgresql")._exec(cur, write, None)
    assert gap in (result.get("not_covered") or []), (gap, result, cur.sql)
    if gap == "scope_unreadable":
        # Read under a savepoint: the write's transaction is still usable.
        assert "ROLLBACK TO SAVEPOINT oa_scope" in cur.sql


def test_postgres_scope_inventory_is_empty_for_a_local_trigger() -> None:
    body = (
        "BEGIN NEW.at := now(); INSERT INTO audit (c) VALUES (NEW.id); RETURN NEW; END"
    )
    cur = _Cursor(
        _pg(
            pg_trigger=_plpgsql(body),
            **{"proname = ANY": [("now", "pg_catalog")]},
        )
    )
    assert _adapter("postgresql")._exec(cur, _UPDATE, None)["not_covered"] == []


def _my(**over: Any) -> dict[str, Any]:
    """Catalog answers for a closed MySQL scope, with ``over`` applied."""
    base: dict[str, Any] = {
        "SELECT DATABASE()": [("crm",)],
        "SHOW GRANTS": [
            ("GRANT SELECT, INSERT, UPDATE, DELETE, TRIGGER ON `crm`.* TO `app`@`%`",)
        ],
        "table_type = 'BASE TABLE'": [("customers",)],
        "UPPER(engine)": [(0,)],
        "information_schema.triggers": [],
    }
    return {**base, **over}


@pytest.mark.parametrize(
    ("answers", "sql", "gap"),
    [
        # A least-privilege account sees no triggers: an empty list proves nothing.
        (
            _my(**{"SHOW GRANTS": [("GRANT SELECT, UPDATE ON `crm`.* TO `app`@`%`",)]}),
            "",
            "trigger_privilege_unproven",
        ),
        (
            _my(**{"SHOW GRANTS": [("GRANT TRIGGER ON `other`.* TO `app`@`%`",)]}),
            "",
            "trigger_privilege_unproven",
        ),
        # A wildcard grant counts only for the name it spells.
        (
            _my(**{"SHOW GRANTS": [("GRANT ALL PRIVILEGES ON `cr%`.* TO `app`@`%`",)]}),
            "",
            "trigger_privilege_unproven",
        ),
        (
            _my(
                **{
                    "SHOW GRANTS": [
                        ("GRANT ALL PRIVILEGES ON *.* TO `app`@`%`",),
                        ("REVOKE TRIGGER ON `crm`.* FROM `app`@`%`",),
                    ]
                }
            ),
            "",
            "trigger_privilege_unproven",
        ),
        (
            _my(**{"SHOW GRANTS": [("GRANT `writer`@`%` TO `app`@`%`",)]}),
            "",
            "trigger_privilege_unproven",
        ),
        (
            _my(
                **{
                    "SHOW GRANTS": [
                        ("GRANT TRIGGER ON `crm`.`orders` TO `app`@`%`",),
                    ]
                }
            ),
            "",
            "trigger_privilege_unproven",
        ),
        (_my(**{"UPPER(engine)": [(1,)]}), "", "foreign_data"),
        (
            _my(**{"information_schema.triggers": [("CALL note_it(NEW.id)",)]}),
            "",
            "routine_call",
        ),
        (
            _my(
                **{
                    "information_schema.triggers": [
                        ("INSERT INTO ledger.notes (customer) VALUES (NEW.id)",)
                    ]
                }
            ),
            "",
            "other_qualifier",
        ),
        (
            _my(**{"information_schema.triggers": [("SET NEW.c = stamp(NEW.c)",)]}),
            "",
            "user_function",
        ),
        (_my(), "UPDATE customers SET c = stamp(c)", "user_function"),
        (_my(**{"SHOW GRANTS": RuntimeError("denied")}), "", "scope_unreadable"),
    ],
)
def test_mysql_scope_inventory_caps_each_open_item(
    answers: dict[str, Any], sql: str, gap: str
) -> None:
    write = (
        {"collection": "statement", "op": "sql", "fields": {"sql": sql}}
        if sql
        else _UPDATE
    )
    answers.setdefault("SHOW DATABASES", [("crm",), ("ledger",)])
    result = _adapter("mysql")._exec(_Cursor(answers), write, None)
    assert gap in (result.get("not_covered") or []), (gap, result)


@pytest.mark.parametrize(
    "grants",
    [
        ["GRANT ALL PRIVILEGES ON *.* TO `root`@`localhost` WITH GRANT OPTION"],
        ["GRANT SELECT, UPDATE, TRIGGER ON `crm`.* TO `app`@`%`"],
        ["GRANT TRIGGER, SELECT (a, b) ON `crm`.`customers` TO `app`@`%`"],
    ],
)
def test_mysql_scope_inventory_is_empty_when_every_trigger_is_visible_and_local(
    grants: list[str],
) -> None:
    answers = _my(
        **{
            "SHOW GRANTS": [(g,) for g in grants],
            "information_schema.triggers": [
                ("INSERT INTO notes (customer, at) VALUES (NEW.id, NOW())",),
                ("SET NEW.total = IFNULL(OLD.total, 0) + crm.NEW.x",),
            ],
        }
    )
    result = _adapter("mysql")._exec(_Cursor(answers), _UPDATE, None)
    assert result["not_covered"] == [], result


def test_mysql_still_refuses_a_planned_statement_naming_another_database() -> None:
    from openadapt_flow.txplan.adapters import Diverged

    answers = _my(**{"SHOW DATABASES": [("crm",), ("ledger",)]})
    with pytest.raises(Diverged):
        _adapter("mysql")._exec(
            _Cursor(answers),
            {
                "collection": "statement",
                "op": "sql",
                "fields": {"sql": "UPDATE ledger.notes SET c = 1"},
            },
            None,
        )
