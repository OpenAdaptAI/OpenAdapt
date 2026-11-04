"""Zero-touch fault plants that keep out of the store's unique indexes.

An app keeps a product code unique, and an agenda row unique per (event,
kind, element). A fault planted by copying another row's value into such a
column (a wrong value, an unrelated edit on another row, a late change) is
refused by the store itself (MySQL 1062, PostgreSQL 23505): the case errors
instead of testing the contract, and the kind can't be admitted. Each plant
must be one the store takes, and still a real change.

Real MySQL (``mysqld`` on PATH or ``OA_TEST_MYSQLD``, with ``pymysql``) and
real PostgreSQL; each is skipped when not installed. A unit test covers the
dry read's sample values. Records are made up.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator

import pytest

from openadapt_flow.generate import verify_contract as gen
from openadapt_flow.generate import zero_touch as zt

try:  # the PostgreSQL server the generator's tests start (needs psycopg2)
    from tests.test_generate_verify_contract import pg  # noqa: F401
except pytest.skip.Exception:

    @pytest.fixture(scope="module")
    def pg() -> Any:
        pytest.skip("psycopg2 is not installed")


MYSQLD = os.environ.get("OA_TEST_MYSQLD") or shutil.which("mysqld")

TABLES = (
    "CREATE TABLE product (rowid integer PRIMARY KEY, ref varchar(32) NOT NULL,"
    " label varchar(64), price integer)",
    "CREATE UNIQUE INDEX uk_product_ref ON product (ref)",
    "CREATE TABLE res (id integer PRIMARY KEY, fk_event integer NOT NULL,"
    " kind varchar(16) NOT NULL, fk_element integer NOT NULL)",
    "CREATE UNIQUE INDEX uk_res ON res (fk_event, kind, fk_element)",
    "INSERT INTO product VALUES (1, 'CAF-500', 'Coffee', 9), (2, 'PAN-001', 'Basket', 59),"
    " (3, 'MIEL-500', 'Honey', 12)",
    # Every other event already has a row for element 1 and for element 3:
    # another row's event, with a row's own kind and element, is taken.
    "INSERT INTO res VALUES (1, 1, 'user', 1), (2, 2, 'user', 1), (3, 3, 'user', 1),"
    " (4, 2, 'user', 3), (5, 3, 'user', 3)",
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def mysql() -> Iterator[dict[str, Any]]:
    pymysql = pytest.importorskip("pymysql")
    if not MYSQLD or not os.path.exists(MYSQLD):
        pytest.skip("MySQL/MariaDB (mysqld) is not installed")
    root = Path(tempfile.mkdtemp(prefix="oa-zt-my-"))
    port = _free_port()
    base = str(Path(MYSQLD).resolve().parent.parent)
    init = subprocess.run(
        [MYSQLD, "--no-defaults", "--initialize-insecure", f"--basedir={base}",
         f"--datadir={root / 'data'}"],
        capture_output=True,
    )  # fmt: skip
    if init.returncode != 0:  # MariaDB initializes with its own script
        shutil.rmtree(root, ignore_errors=True)
        pytest.skip("mysqld --initialize-insecure is not supported here")
    proc = subprocess.Popen(
        [MYSQLD, "--no-defaults", f"--basedir={base}", f"--datadir={root / 'data'}",
         f"--port={port}", "--bind-address=127.0.0.1", f"--socket={root / 's'}",
         f"--pid-file={root / 'pid'}", f"--log-error={root / 'err'}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    try:
        for _ in range(120):
            try:
                pymysql.connect(host="127.0.0.1", port=port, user="root").close()
                break
            except Exception:  # noqa: BLE001 - still starting
                time.sleep(0.5)
        else:
            pytest.skip("mysqld did not start")
        yield {"port": port}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(root, ignore_errors=True)


def _sandbox_mysql(
    mysql: dict[str, Any], db: str, tables: tuple[str, ...] = TABLES
) -> Any:
    import pymysql

    c = pymysql.connect(
        host="127.0.0.1", port=mysql["port"], user="root", autocommit=True
    )
    cur = c.cursor()
    cur.execute(f"CREATE DATABASE {db}")
    cur.execute(f"USE {db}")
    for sql in tables:
        cur.execute(sql)
    c.close()
    read = {"sql_driver": "pymysql", "sql_connect_args": {"host": "127.0.0.1",
            "port": mysql["port"], "user": "root", "database": db}}  # fmt: skip
    sb = zt.Sandbox(read, read)
    sb.create(zt.store.read_schema(zt.store.connector(read), "mysql"), "root@localhost")
    return sb


def _sandbox_pg(
    pg: dict[str, Any],
    db: str,
    tables: tuple[str, ...] = TABLES,  # noqa: F811
) -> Any:
    psycopg2 = pytest.importorskip("psycopg2")
    admin = psycopg2.connect(
        host="127.0.0.1", port=pg["port"], user="admin", dbname="postgres"
    )
    admin.autocommit = True
    admin.cursor().execute(f"CREATE DATABASE {db}")
    admin.close()
    c = psycopg2.connect(host="127.0.0.1", port=pg["port"], user="admin", dbname=db)
    c.autocommit = True
    for sql in tables:
        c.cursor().execute(sql)
    c.close()
    read = {"sql_driver": "psycopg2", "sql_connect_args": {"host": "127.0.0.1",
            "port": pg["port"], "user": "admin", "dbname": db}}  # fmt: skip
    sb = zt.Sandbox(read, read)
    sb.create(zt.store.read_schema(zt.store.connector(read), "postgresql"), "admin")
    return sb


#: The write: a new product and a new agenda row (event 1, element 3).
WRITE = [
    zt.Op("product", {"rowid": "10"},
          {"rowid": "10", "ref": "THE-250", "label": "Tea", "price": "4"}),
    zt.Op("res", {"id": "10"},
          {"id": "10", "fk_event": "1", "kind": "user", "fk_element": "3"}),
]  # fmt: skip
VALUES = {"ref": "THE-250", "label": "Tea", "price": 4}
PLANTED = (
    "wrong_value",
    "bookkeeping",
    "unrelated_edit",
    "delayed",
    "collateral_target",
)


def _check_every_plant_is_taken(sb: Any) -> None:
    demo = type("D", (), {"values": VALUES})()
    cases = zt.faults(sb.schema, sb, demo, WRITE)
    names = {c.name: c for c in cases}
    # The plants the store refused before: a product code copied from
    # another product, and another event that, with this row's kind and
    # element, another agenda row already holds.
    for name in ("wrong_value:product.ref", "unrelated_edit:product.ref",
                 "bookkeeping:res.fk_event", "unrelated_edit:res.fk_event"):  # fmt: skip
        assert name in names and not names[name].unplantable, name
    checked = 0
    for case in cases:
        if case.cls not in PLANTED or case.unplantable:
            continue
        before = {
            (op.table, tuple(sorted(op.key.items()))): sb.get(op.table, op.key)
            for op in [*case.write, *case.late]
        }
        sb.put_many(case.write)  # the store takes the plant
        if case.late:
            sb.put_many(case.late)
        sb.put_many([zt.Op(t, dict(k), row) for (t, k), row in before.items()])
        checked += 1
    assert checked >= 8
    # Each is still a fault: the planted value differs from the written one.
    wrong = names["wrong_value:product.ref"].write[0].row
    assert wrong is not None and wrong["ref"] != "THE-250"
    fk = names["bookkeeping:res.fk_event"].write[1].row
    assert fk is not None and fk["fk_event"] != "1"
    edit = names["unrelated_edit:product.ref"].write[-1]
    assert (
        edit.row is not None and edit.row["ref"] != sb.get("product", edit.key)["ref"]
    )


def test_plants_keep_out_of_unique_indexes_on_mysql(mysql: dict[str, Any]) -> None:
    sb = _sandbox_mysql(mysql, "zt_uq_my")
    try:
        _check_every_plant_is_taken(sb)
    finally:
        sb.drop()


def test_plants_keep_out_of_unique_indexes_on_postgresql(pg: Any) -> None:  # noqa: F811
    sb = _sandbox_pg(pg, "zt_uq_pg")
    try:
        _check_every_plant_is_taken(sb)
    finally:
        sb.drop()


def test_a_unique_index_with_no_free_value_leaves_the_fault_unplantable() -> None:
    """Fail closed: when every value the column could take is held, the
    case is unplantable (which fails admission), never a colliding write."""

    from openadapt_flow.generate.sql_store import Column, Table

    t = Table("res", [Column("id", "int", False, None), Column("flag", "bool", False, None),
                      Column("k", "int", False, None)], ["id"])  # fmt: skip

    class Full:
        def unique_indexes(self, table: str) -> dict[str, list[str]]:
            return {"uk": ["flag", "k"]}

        def holds(
            self, table: str, values: dict[str, Any], exclude: Any = None
        ) -> bool:
            return True  # every combination is taken

        def rows(self, table: str, limit: int = 50) -> list[dict[str, Any]]:
            return []

        def distinct(self, table: str, column: str, limit: int = 5) -> list[str]:
            return ["true", "false"]

    assert (
        zt.plant_free(Full(), t, "flag", {"id": "1", "flag": "true", "k": "1"}) is None
    )


# -- an update's own old value ---------------------------------------------------
#
# An update changes a unique column (state A -> B, on_hold true -> false). A
# missed edit leaves the row's old value, which only the row being written
# holds: a legal state the store takes. Counting the target row itself as a
# collision turned that fault into an invalid plant (an ENUM value MySQL
# refuses) or an unplantable one (a unique boolean has no third value).

ENUM_TABLES = (
    "CREATE TABLE ticket (id integer PRIMARY KEY, state ENUM('A','B') NOT NULL,"
    " UNIQUE KEY uk_state (state))",
    "INSERT INTO ticket VALUES (1, 'A')",
)
BOOL_TABLES = (
    "CREATE TABLE ticket (id integer PRIMARY KEY, on_hold boolean NOT NULL)",
    "CREATE UNIQUE INDEX uk_hold ON ticket (on_hold)",
    "INSERT INTO ticket VALUES (1, true)",
)


def _check_the_old_value_is_planted(sb: Any, col: str, old: str, new: str) -> None:
    write = [zt.Op("ticket", {"id": "1"}, {"id": "1", col: new})]
    demo = type("D", (), {"values": {col: new}})()
    cases = [c for c in zt.faults(sb.schema, sb, demo, write)
             if c.name.endswith(f":ticket.{col}") and c.cls != "unrelated_edit"]  # fmt: skip
    assert cases
    for case in cases:
        assert not case.unplantable, case.name
        ops = [op for op in [*case.write, *case.late] if op.table == "ticket"]
        assert ops and ops[-1].row is not None
        assert ops[-1].row[col] == old, (case.name, ops[-1].row[col])
        sb.put_many(case.write)  # the store takes the plant
        if case.late:
            sb.put_many(case.late)
        assert sb.get("ticket", {"id": "1"})[col] == old
    # Fail closed is kept: another row holding the value still collides.
    t = sb.schema.tables["ticket"]
    assert zt.unique_free(sb, t, col, {"id": "2", col: new}, old) is False


def test_an_updates_old_enum_value_is_planted_on_mysql(mysql: dict[str, Any]) -> None:
    sb = _sandbox_mysql(mysql, "zt_uq_my_enum", ENUM_TABLES)
    try:
        _check_the_old_value_is_planted(sb, "state", "A", "B")
    finally:
        sb.drop()


def test_an_updates_old_boolean_is_planted_on_postgresql(pg: Any) -> None:  # noqa: F811
    sb = _sandbox_pg(pg, "zt_uq_pg_bool", BOOL_TABLES)
    try:
        _check_the_old_value_is_planted(sb, "on_hold", "true", "false")
    finally:
        sb.drop()


@pytest.mark.parametrize("with_holds", [True, False])
def test_only_another_row_holding_the_value_collides(with_holds: bool) -> None:
    """The target row (by primary key) is left out of the collision check,
    by the store's lookup and by the row scan alike; any other row still
    collides, and a row with no whole key leaves nothing out."""

    from openadapt_flow.generate.sql_store import Column, Table

    t = Table("ticket", [Column("id", "int", False, None),
                         Column("state", "text", False, None)], ["id"])  # fmt: skip
    stored = [{"id": "1", "state": "A"}, {"id": "2", "state": "C"}]

    class Store:
        def unique_indexes(self, table: str) -> dict[str, list[str]]:
            return {"uk": ["state"]}

        def rows(self, table: str, limit: int = 50) -> list[dict[str, Any]]:
            return stored

    class Holding(Store):
        def holds(
            self, table: str, values: dict[str, Any], exclude: Any = None
        ) -> bool:
            return any(
                all(r[c] == v for c, v in values.items())
                and not (exclude and all(r[c] == v for c, v in exclude.items()))
                for r in stored
            )

    sb = Holding() if with_holds else Store()
    assert zt.unique_free(sb, t, "state", {"id": "1", "state": "B"}, "A")
    assert not zt.unique_free(sb, t, "state", {"id": "1", "state": "B"}, "C")
    assert not zt.unique_free(sb, t, "state", {"id": "3", "state": "B"}, "A")
    assert not zt.unique_free(sb, t, "state", {"id": None, "state": "B"}, "A")


# -- the dry read's sample values ------------------------------------------------


def test_a_demonstration_with_no_list_items_does_not_hide_the_others() -> None:
    """The first invoice demonstration had no lines; the dry read must
    still get ``lines.0.qty`` from another one (else every variant with a
    line fails its dry read and the kind gets no contract)."""

    def demo(lines: list[dict[str, Any]]) -> Any:
        return type("D", (), {"values": {"customer": "Ann", "lines": lines}})()

    plan = gen.KindPlan("create_invoice", demos=[
        demo([]), demo([{"product_ref": "THE-250", "qty": 3}])])  # fmt: skip
    variant = gen.Variant("create_invoice", ["customer"], lists={"lines": 2})
    got = gen.flatten(gen.sample_values(plan, variant))
    assert got["lines.0.qty"] == 3 and got["lines.1.qty"] == 3
    assert got["customer"] == "Ann"
