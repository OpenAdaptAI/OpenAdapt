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
        info = {"host": "127.0.0.1", "port": port, "user": "lab", "dbname": "postgres"}
        import psycopg2

        con = psycopg2.connect(**info)
        try:
            cur = con.cursor()
            cur.execute("SHOW server_version_num")
            if int(cur.fetchone()[0]) < 130000:
                pytest.skip("these tests need PostgreSQL 13 or later")
        finally:
            con.close()
        yield info
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


def _ctx(
    cluster: dict[str, Any], tmp_path: Path, reviewed: tuple[str, ...] = ()
) -> Ctx:
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
                    "reviewed_triggers": list(reviewed),
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


def _update(rid: int, balance: int) -> dict[str, Any]:
    return {
        "system": "crm",
        "collection": "customers",
        "op": "update",
        "id": rid,
        "fields": {"balance": balance},
    }


def _sql(text: str) -> dict[str, Any]:
    return {"system": "crm", "op": "sql", "fields": {"sql": text}}


def _capped(out: Any, code: str) -> None:
    """Capped, not refused, with ``code`` among the reasons."""
    assert isinstance(out, dict), out
    assert out["outcome"] == "COMPLETED_UNVERIFIED", out
    assert code in out["tx"]["not_covered"], out["tx"]


_WIPE = (
    "CREATE FUNCTION wipe() RETURNS boolean LANGUAGE plpgsql AS $$ "
    "BEGIN TRUNCATE side.notes; RETURN true; END $$"
)


@pytest.fixture
def notes(db: Any) -> Any:
    """Four rows in side.notes, and wipe(), which truncates them (TRUNCATE
    zeroes the transaction's row counts, so no count check sees it)."""
    cluster, cur = db
    cur.execute(
        "INSERT INTO side.notes (customer) SELECT g FROM generate_series(1, 4) g"
    )
    cur.execute(_WIPE)
    yield cluster, cur
    cur.execute("DROP FUNCTION IF EXISTS wipe() CASCADE")


def test_a_trigger_that_truncates_another_schema_is_never_verified(
    notes: Any, tmp_path: Path
) -> None:
    cluster, cur = notes
    cur.execute(
        "CREATE FUNCTION wipe_t() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN TRUNCATE side.notes; RETURN NULL; END $$;"
        "CREATE TRIGGER wipe AFTER UPDATE ON customers "
        "FOR EACH STATEMENT EXECUTE FUNCTION wipe_t()"
    )
    try:
        out = _attempt(_ctx(cluster, tmp_path), _update(3, 9))
    finally:
        cur.execute("DROP FUNCTION wipe_t() CASCADE")
    _capped(out, "trigger")
    assert _notes(cur) == 0 and _balance(cur, 3) == 9  # it ran, unverified


def test_a_planned_truncate_of_another_schema_is_never_verified(
    notes: Any, tmp_path: Path
) -> None:
    cluster, cur = notes
    out = _attempt(_ctx(cluster, tmp_path), _sql("TRUNCATE side.notes"))
    _capped(out, "truncate")


def test_a_trigger_writing_a_foreign_table_is_never_verified(
    db: Any, tmp_path: Path
) -> None:
    # postgres_fdw over loopback: the delete runs in another backend, so this
    # transaction's row counts never see it, and pg_tables never frames the
    # foreign table.
    cluster, cur = db
    cur.execute(
        "SELECT count(*) FROM pg_available_extensions WHERE name = 'postgres_fdw'"
    )
    if not int(cur.fetchone()[0]):
        pytest.skip("the postgres_fdw contrib module is not installed")
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
        out = _attempt(_ctx(cluster, tmp_path), _update(2, 8))
        fc = far.cursor()
        fc.execute("SELECT count(*) FROM notes")
        left = int(fc.fetchone()[0])
    finally:
        far.close()
        cur.execute("DROP FUNCTION wipe_far() CASCADE")
    _capped(out, "foreign_data")
    assert "remote_link" in out["tx"]["not_covered"]
    assert left == 0 and _balance(cur, 2) == 8  # it ran, unverified


@pytest.mark.parametrize("how", ["from", "when", "pg_catalog"])
def test_a_trigger_reaching_a_user_function_another_way_is_never_verified(
    notes: Any, tmp_path: Path, how: str
) -> None:
    # The trigger's own body looks local; the TRUNCATE is in a helper called
    # from a FROM clause, from the WHEN condition, or the trigger function sits
    # in pg_catalog (a superuser can put it there).
    cluster, cur = notes
    fn = "pg_catalog.calls_wipe" if how == "pg_catalog" else "calls_wipe"
    body = "PERFORM 1 FROM wipe();" if how in ("from", "pg_catalog") else ""
    when = "WHEN (wipe())" if how == "when" else ""
    cur.execute(
        f"CREATE FUNCTION {fn}() RETURNS trigger LANGUAGE plpgsql AS $$ "
        f"BEGIN {body} RETURN NULL; END $$;"
        f"CREATE TRIGGER via AFTER UPDATE ON customers FOR EACH ROW {when} "
        f"EXECUTE FUNCTION {fn}()"
    )
    try:
        out = _attempt(_ctx(cluster, tmp_path), _update(5, 3))
    finally:
        cur.execute(f"DROP FUNCTION {fn}() CASCADE")
    _capped(out, "trigger")
    assert _notes(cur) == 0


def test_a_dollar_sign_identifier_cannot_hide_a_call(
    notes: Any, tmp_path: Path
) -> None:
    # PostgreSQL allows $ in identifiers. A text strip that reads $a$ as a
    # dollar quote hides wipe() from the planned statement.
    cluster, cur = notes
    cur.execute('ALTER TABLE customers ADD COLUMN "v$a$" integer')
    try:
        out = _attempt(
            _ctx(cluster, tmp_path),
            _sql(
                "UPDATE customers SET v$a$ = 1 WHERE id = 2 AND wipe() AND v$a$ IS NULL"
            ),
        )
    finally:
        cur.execute('ALTER TABLE customers DROP COLUMN "v$a$"')
    _capped(out, "user_function")
    assert _notes(cur) == 0


@pytest.mark.parametrize("how", ["domain", "cast", "view", "query_to_xml"])
def test_a_user_function_reached_through_the_catalog_is_never_verified(
    notes: Any, tmp_path: Path, how: str
) -> None:
    # Each reaches wipe() without naming it in any trigger or statement text:
    # a domain's CHECK, an assignment cast, a view, or SQL passed as a string.
    cluster, cur = notes
    write: dict[str, Any]
    if how == "domain":
        cur.execute(
            "CREATE DOMAIN checked AS integer CHECK (VALUE IS NULL OR wipe());"
            "ALTER TABLE customers ADD COLUMN flag checked"
        )
        write = {
            "system": "crm",
            "collection": "customers",
            "op": "update",
            "id": 1,
            "fields": {"flag": 1},
        }
        code, undo = (
            "user_type",
            "ALTER TABLE customers DROP COLUMN flag; DROP DOMAIN checked",
        )
    elif how == "cast":
        cur.execute(
            "CREATE TYPE token AS (v text);"
            "CREATE FUNCTION to_int(token) RETURNS integer LANGUAGE plpgsql AS $$ "
            "BEGIN PERFORM wipe(); RETURN 1; END $$;"
            "CREATE CAST (token AS integer) WITH FUNCTION to_int(token) AS ASSIGNMENT"
        )
        write = _sql("UPDATE customers SET balance = ROW('x')::token WHERE id = 1")
        code, undo = "user_cast_or_operator", "DROP TYPE token CASCADE"
    elif how == "view":
        cur.execute("CREATE VIEW gate AS SELECT wipe() AS ok")
        write = _sql(
            "UPDATE customers SET balance = 1 WHERE id = 1 AND (SELECT ok FROM gate)"
        )
        code, undo = "user_function", "DROP VIEW gate"
    else:
        write = _sql(
            "UPDATE customers SET balance = 1 WHERE id = 1 AND "
            "query_to_xml('SELECT wipe()', true, false, '') IS NOT NULL"
        )
        code, undo = "system_function_with_effects", "SELECT 1"
    try:
        out = _attempt(_ctx(cluster, tmp_path), write)
    finally:
        cur.execute(undo)
    _capped(out, code)
    assert _notes(cur) == 0


def test_an_event_trigger_in_the_database_caps_every_write(
    db: Any, tmp_path: Path
) -> None:
    cluster, cur = db
    cur.execute(
        "CREATE FUNCTION on_ddl() RETURNS event_trigger LANGUAGE plpgsql AS $$ "
        "BEGIN NULL; END $$;"
        "CREATE EVENT TRIGGER watch ON ddl_command_end EXECUTE FUNCTION on_ddl()"
    )
    try:
        out = _attempt(_ctx(cluster, tmp_path), _update(4, 1))
    finally:
        cur.execute("DROP EVENT TRIGGER watch; DROP FUNCTION on_ddl()")
    _capped(out, "event_trigger")


def test_a_reviewed_trigger_counts_only_while_its_digest_matches(
    db: Any, tmp_path: Path
) -> None:
    # Coverage comes from the catalog, not from reading the body: a trigger
    # caps the write until an operator lists its digest, and again as soon
    # as its function changes.
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
        from openadapt_flow.txplan.adapters import SqlAdapter

        ctx = _ctx(cluster, tmp_path)
        _capped(_attempt(ctx, _update(4, 1)), "trigger")
        adapter = ctx.adapter("crm")
        assert isinstance(adapter, SqlAdapter)
        digests = tuple(adapter.trigger_digests().values())
        assert len(digests) == 1
        out = _attempt(_ctx(cluster, tmp_path / "b", digests), _update(4, 2))
        assert isinstance(out, dict) and out["outcome"] == "VERIFIED", out
        cur.execute(
            "CREATE OR REPLACE FUNCTION note_here() RETURNS trigger LANGUAGE plpgsql "
            "AS $$ BEGIN INSERT INTO audit (customer) VALUES (NEW.id); RETURN NULL; END $$"
        )
        _capped(
            _attempt(_ctx(cluster, tmp_path / "c", digests), _update(4, 3)), "trigger"
        )
    finally:
        cur.execute("DROP FUNCTION note_here() CASCADE; DROP TABLE audit")


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


# -- always on: the catalog inventory on a recording cursor --------------------
# Each case answers the catalog queries with exactly one open item.

_UPDATE = {"collection": "customers", "op": "update", "id": 1, "fields": {"b": 2}}
_PLAN = [([{"Plan": {"Node Type": "ModifyTable", "Schema": "public", "Plans": []}}],)]


def _pg(**over: Any) -> dict[str, Any]:
    """Catalog answers for a closed PostgreSQL scope, with ``over`` applied."""
    base: dict[str, Any] = {
        "pg_stat_xact_user_tables": [(0,)],
        "current_schemas": [("public", ["public"])],
        "pg_foreign_data_wrapper": [(0,)],
        "pg_extension": [(0,)],
        "pg_event_trigger": [(0,)],
        "pg_cast": [(0,)],
        "pg_get_triggerdef": [],
        "a.classid::regclass::text": [],
        "d.refclassid::regclass::text": [],
        "pg_attribute a JOIN": [(0,)],
        "pg_get_expr(adbin": [],
        "EXPLAIN": _PLAN,
        "proname = ANY": [],
    }
    return {**base, **over}


def _explain(*outputs: str, schema: str = "public") -> list[tuple[Any, ...]]:
    return [
        (
            [
                {
                    "Plan": {
                        "Node Type": "Seq Scan",
                        "Schema": schema,
                        "Output": list(outputs),
                    }
                }
            ],
        )
    ]


@pytest.mark.parametrize(
    ("answers", "sql", "gap"),
    [
        (_pg(current_schemas=[("public", ["public", "side"])]), "", "search_path_open"),
        (_pg(pg_foreign_data_wrapper=[(1,)]), "", "foreign_data"),
        (_pg(pg_extension=[(1,)]), "", "remote_link"),
        (_pg(pg_event_trigger=[(1,)]), "", "event_trigger"),
        (_pg(pg_cast=[(1,)]), "", "user_cast_or_operator"),
        (
            _pg(
                pg_get_triggerdef=[
                    ("t", "CREATE TRIGGER t ...", "CREATE FUNCTION f ...")
                ]
            ),
            "",
            "trigger",
        ),
        (
            _pg(**{"a.classid::regclass::text": [("pg_rewrite", 20000, False)]}),
            "",
            "rule",
        ),
        (
            _pg(**{"a.classid::regclass::text": [("pg_foo", 20000, None)]}),
            "",
            "user_object",
        ),
        (
            _pg(**{"d.refclassid::regclass::text": [("pg_proc", 20001, None)]}),
            "",
            "user_function",
        ),
        (
            _pg(**{"d.refclassid::regclass::text": [("pg_type", 20002, False)]}),
            "",
            "user_type",
        ),
        (
            _pg(**{"d.refclassid::regclass::text": [("pg_class", 20003, False)]}),
            "",
            "other_relation",
        ),
        (_pg(**{"pg_attribute a JOIN": [(1,)]}), "", "user_type"),
        (
            _pg(
                **{
                    "pg_get_expr(adbin": [
                        ("query_to_xml('SELECT f()'::text, true, false, ''::text)",)
                    ],
                    "proname = ANY": [("query_to_xml", 2925)],
                }
            ),
            "",
            "system_function_with_effects",
        ),
        (_pg(), "TRUNCATE notes", "truncate"),
        (_pg(), "DROP TABLE notes CASCADE", "planned_ddl"),
        (
            _pg(EXPLAIN=_explain("wipe()"), **{"proname = ANY": [("wipe", 16500)]}),
            "UPDATE customers SET b = 1",
            "user_function",
        ),
        (
            _pg(EXPLAIN=_explain("x", schema="side")),
            "UPDATE customers SET b = 1",
            "other_schema",
        ),
        (
            # The deparse quotes "v$a$": the call after it is still found.
            _pg(
                EXPLAIN=_explain('"v$a$"', "wipe()"),
                **{"proname = ANY": [("wipe", 16500)]},
            ),
            "UPDATE customers SET v$a$ = 1 WHERE wipe()",
            "user_function",
        ),
        (
            _pg(current_schemas=RuntimeError("permission denied")),
            "",
            "scope_unreadable",
        ),
        (
            _pg(EXPLAIN=RuntimeError("syntax")),
            "UPDATE customers SET b = 1",
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


def test_postgres_scope_inventory_is_empty_for_a_reviewed_trigger() -> None:
    import hashlib

    row = ("t", "CREATE TRIGGER t ...", "CREATE FUNCTION f ...")
    digest = hashlib.sha256(f"{row[1]}\n{row[2]}".encode()).hexdigest()
    adapter = _adapter("postgresql")
    adapter.system.reviewed_triggers = [digest]
    cur = _Cursor(_pg(pg_get_triggerdef=[row]))
    assert adapter._exec(cur, _UPDATE, None)["not_covered"] == []


def test_a_postgres_entry_with_no_recorded_scope_is_capped_but_sqlite_is_not() -> None:
    w = {"collection": "customers", "op": "update", "id": 1}
    assert _adapter("postgresql").not_covered(w, {"id": "1"}) == ["scope_unrecorded"]
    assert _adapter("mysql").not_covered(w, None) == ["scope_unrecorded"]
    from openadapt_flow.txplan.adapters import SqlAdapter
    from openadapt_flow.txplan.spec import SqlSystem

    lite = SqlAdapter(
        SqlSystem(
            name="crm", kind="sql", dialect="sqlite", path="x.db", tables={"c": "id"}
        )
    )
    assert lite.not_covered(w, {"id": "1"}) == []


def _my(**over: Any) -> dict[str, Any]:
    """Catalog answers for a closed MySQL scope, with ``over`` applied."""
    base: dict[str, Any] = {
        "SELECT DATABASE()": [("crm",)],
        "SHOW GRANTS": [
            ("GRANT SELECT, INSERT, UPDATE, DELETE, TRIGGER ON `crm`.* TO `app`@`%`",)
        ],
        "UPPER(engine)": [(0,)],
        "information_schema.triggers": [],
        "information_schema.routines": [(0,)],
    }
    return {**base, **over}


_MY_TRIGGER = [
    ("t", "customers", "AFTER", "UPDATE", "INSERT INTO notes (c) VALUES (NEW.id)")
]


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
        # Table-level grants don't prove the database's other tables visible:
        # a hidden table's trigger can write another database.
        (
            _my(
                **{
                    "SHOW GRANTS": [
                        ("GRANT SELECT, TRIGGER ON `crm`.`customers` TO `app`@`%`",)
                    ]
                }
            ),
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
        (_my(**{"UPPER(engine)": [(1,)]}), "", "foreign_data"),
        # Any trigger, however local its body reads, until it is reviewed.
        (_my(**{"information_schema.triggers": _MY_TRIGGER}), "", "trigger"),
        (_my(**{"information_schema.routines": [(1,)]}), "", "routine"),
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
    answers.setdefault("SHOW DATABASES", [("crm",)])
    result = _adapter("mysql")._exec(_Cursor(answers), write, None)
    assert gap in (result.get("not_covered") or []), (gap, result)


@pytest.mark.parametrize(
    "grants",
    [
        ["GRANT ALL PRIVILEGES ON *.* TO `root`@`localhost` WITH GRANT OPTION"],
        ["GRANT SELECT, UPDATE, TRIGGER ON `crm`.* TO `app`@`%`"],
        ["GRANT SELECT ON *.* TO `app`@`%`", "GRANT TRIGGER ON `crm`.* TO `app`@`%`"],
    ],
)
def test_mysql_scope_inventory_is_empty_with_visible_grants_and_reviewed_triggers(
    grants: list[str],
) -> None:
    import hashlib

    digest = hashlib.sha256("\n".join(_MY_TRIGGER[0][1:]).encode()).hexdigest()
    adapter = _adapter("mysql")
    adapter.system.reviewed_triggers = [digest]
    answers = _my(
        **{
            "SHOW GRANTS": [(g,) for g in grants],
            "information_schema.triggers": _MY_TRIGGER,
        }
    )
    result = adapter._exec(_Cursor(answers), _UPDATE, None)
    assert result["not_covered"] == [], result


def test_mysql_reads_a_backticked_insert_target_as_a_table() -> None:
    from openadapt_flow.txplan.adapters import sql_tokens

    calls = sql_tokens("INSERT INTO `notes` (`a`, b) VALUES (NOW())", "mysql")[1]
    assert {c.lower() for c in calls} == {"now"}


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
