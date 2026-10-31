"""Learned excuses (``frame.learned``): learn, then enforce, on real engines.

An app that writes on every request (the acting user's ``last_login``, a
token's ``last_seen``, a first-login property row) makes the whole-database
frame refute every correct write. ``openadapt_flow.learn`` proposes excuses
from checked writes; ``frame.learned`` enforces exactly the approved moves.

These tests attack the enforcement: a learned ``actor_row`` excuse must
never cover another user's row, another column of the user's row, a removal
when only changes were learned, or a row added for another user; a learned
``target`` excuse must never cover a look-alike of the target. Each runs on
SQLite, and on MariaDB/MySQL and PostgreSQL when a scratch server is given:

    OPENADAPT_SNAPSHOT_MYSQL='{"host": "127.0.0.1", "port": 3306,
        "user": "root", "password": "..."}'
    OPENADAPT_SNAPSHOT_POSTGRES='host=127.0.0.1 port=5432 user=postgres
        password=... dbname=postgres'

Records are made up.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

import pytest
import yaml

from openadapt_flow import learn
from openadapt_flow.runtime.effects.learned import LearnedExcuse, LearnedFrame
from openadapt_flow.verifier import Actor, Contract, begin_write, verify_write

ACTOR = "2"
OTHER = "3"
DIGEST = "sha256:" + "ab" * 32

_MYSQL = os.environ.get("OPENADAPT_SNAPSHOT_MYSQL")
_PG = os.environ.get("OPENADAPT_SNAPSHOT_POSTGRES")


@pytest.fixture(autouse=True)
def _homes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENADAPT_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("OPENADAPT_SEAL_HOME", str(tmp_path / "seal"))


# -- engines -------------------------------------------------------------------------


@dataclass
class Engine:
    """A scratch database: ``run(sql, *args)`` writes as the app does;
    ``read`` is the contract's read-only connection."""

    dialect: str
    read: dict[str, Any]
    run: Callable[..., None]
    schema: Optional[Callable[[], Any]] = None
    cleanup: list[Callable[[], None]] = field(default_factory=list)

    def ddl(self) -> list[str]:
        key = {
            "sqlite": "INTEGER PRIMARY KEY",
            "mysql": "INT AUTO_INCREMENT PRIMARY KEY",
            "postgresql": "SERIAL PRIMARY KEY",
        }[self.dialect]
        return [
            f"CREATE TABLE app_user (id {key}, name VARCHAR(50), "
            "email VARCHAR(80), last_login VARCHAR(30))",
            f"CREATE TABLE api_token (id {key}, user_id INTEGER NOT NULL "
            "REFERENCES app_user(id), label VARCHAR(30), last_seen VARCHAR(30))",
            "CREATE TABLE user_property (user_id INTEGER NOT NULL REFERENCES "
            "app_user(id), property VARCHAR(50) NOT NULL, value VARCHAR(50), "
            "PRIMARY KEY (user_id, property))",
            f"CREATE TABLE note (id {key}, title VARCHAR(80), body VARCHAR(200), "
            "stamp VARCHAR(30))",
            f"CREATE TABLE asset (id {key}, tag VARCHAR(20), name VARCHAR(50), "
            "assigned_to INTEGER, modified_at VARCHAR(30))",
            "INSERT INTO app_user (id, name, email, last_login) VALUES "
            "(1, 'admin', 'admin@example.test', 't0'), "
            "(2, 'agentbot', 'bot@example.test', 't0'), "
            "(3, 'colleague', 'col@example.test', 't0')",
            "INSERT INTO api_token (id, user_id, label, last_seen) VALUES "
            "(1, 2, 'bot', 't0'), (2, 3, 'colleague', 't0')",
            "INSERT INTO asset (id, tag, name, assigned_to, modified_at) VALUES "
            "(1, 'LT-100', 'Laptop A', NULL, 't0'), "
            "(2, 'LT-1000', 'Laptop A', NULL, 't0')",
        ]


def _sqlite(tmp_path: Path) -> Engine:
    db = tmp_path / "app.sqlite"

    def run(sql: str, *args: Any) -> None:
        con = sqlite3.connect(db)
        con.execute(sql.replace("%s", "?"), args)
        con.commit()
        con.close()

    return Engine("sqlite", {"sqlite_database": str(db)}, run)


def _mysql() -> Engine:
    import pymysql

    from openadapt_flow.generate.sql_store import read_schema

    args = json.loads(_MYSQL or "{}")
    name = "openadapt_learn_" + secrets.token_hex(4)
    admin = pymysql.connect(**args, autocommit=True)
    with admin.cursor() as cur:
        cur.execute(f"CREATE DATABASE `{name}`")
    os.environ["OPENADAPT_LEARN_MYSQL_PW"] = str(args.get("password", ""))
    connect_args = {k: v for k, v in args.items() if k != "password"}

    def run(sql: str, *a: Any) -> None:
        con = pymysql.connect(**args, database=name, autocommit=True)
        with con.cursor() as cur:
            cur.execute(sql, a or None)
        con.close()

    def schema() -> Any:
        return read_schema(lambda: pymysql.connect(**args, database=name), "mysql")

    e = Engine(
        "mysql",
        {
            "sql_driver": "pymysql",
            "sql_connect_args": {**connect_args, "database": name},
            "sql_password_env": "OPENADAPT_LEARN_MYSQL_PW",
        },
        run,
        schema,
    )

    def drop() -> None:
        with admin.cursor() as cur:
            cur.execute(f"DROP DATABASE `{name}`")
        admin.close()

    e.cleanup.append(drop)
    return e


def _postgres() -> Engine:
    import psycopg2
    from psycopg2.extensions import parse_dsn

    from openadapt_flow.generate.sql_store import read_schema

    dsn = parse_dsn(_PG or "")
    name = "openadapt_learn_" + secrets.token_hex(4)
    admin = psycopg2.connect(**dsn)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f"CREATE DATABASE {name}")
    os.environ["OPENADAPT_LEARN_PG_PW"] = str(dsn.get("password", ""))
    connect_args = {k: v for k, v in dsn.items() if k != "password"}
    connect_args["dbname"] = name

    def run(sql: str, *a: Any) -> None:
        con = psycopg2.connect(**{**dsn, "dbname": name})
        con.autocommit = True
        with con.cursor() as cur:
            cur.execute(sql, a or None)
        con.close()

    def schema() -> Any:
        return read_schema(
            lambda: psycopg2.connect(**{**dsn, "dbname": name}), "postgresql"
        )

    e = Engine(
        "postgresql",
        {
            "sql_driver": "psycopg2",
            "sql_connect_args": connect_args,
            "sql_password_env": "OPENADAPT_LEARN_PG_PW",
        },
        run,
        schema,
    )

    def drop() -> None:
        with admin.cursor() as cur:
            cur.execute(f"DROP DATABASE {name} WITH (FORCE)")
        admin.close()

    e.cleanup.append(drop)
    return e


ENGINES = [
    "sqlite",
    pytest.param(
        "mysql",
        marks=pytest.mark.skipif(not _MYSQL, reason="set OPENADAPT_SNAPSHOT_MYSQL"),
    ),
    pytest.param(
        "postgresql",
        marks=pytest.mark.skipif(not _PG, reason="set OPENADAPT_SNAPSHOT_POSTGRES"),
    ),
]


@pytest.fixture(params=ENGINES)
def engine(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Engine]:
    e = {"sqlite": lambda: _sqlite(tmp_path), "mysql": _mysql, "postgresql": _postgres}[
        request.param
    ]()
    try:
        for statement in e.ddl():
            e.run(statement)
        if e.dialect == "postgresql":
            for table in ("app_user", "api_token", "note", "asset"):
                e.run(
                    f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                    f"(SELECT MAX(id) FROM {table}))"
                )
        yield e
    finally:
        for fn in e.cleanup:
            fn()


# -- contracts -----------------------------------------------------------------------


def _verifier(e: Engine, query: str) -> dict[str, Any]:
    return {"kind": "sql", **e.read, "sql_query": query}


def note_contract(
    e: Engine, learned: Optional[dict[str, Any]] = None
) -> dict[str, Any]:
    """Create a note: the declared row, nothing else."""

    frame: dict[str, Any] = {
        "identity_field": "id",
        "database": {
            "dialect": e.dialect,
            "read": dict(e.read),
            "views": {"note": ""},
            "new_row_fields": {"note": ["id"]},
            "settle_s": 0,
        },
    }
    if learned is not None:
        frame["learned"] = learned
    return {
        "schema_version": "openadapt.verify/v1",
        "verifier": _verifier(e, "SELECT * FROM note"),
        "record": {
            "name": "note",
            "match": ["title"],
            "fields": {"title": "title", "body": "body"},
        },
        "frame": frame,
    }


def asset_contract(
    e: Engine, learned: Optional[dict[str, Any]] = None
) -> dict[str, Any]:
    """Rename one asset, found by its tag: only ``name`` may change."""

    frame: dict[str, Any] = {
        "identity_field": "id",
        "target_fields": ["name"],
        "database": {
            "dialect": e.dialect,
            "read": dict(e.read),
            "views": {"asset": ""},
            "settle_s": 0,
        },
    }
    if learned is not None:
        frame["learned"] = learned
    return {
        "schema_version": "openadapt.verify/v1",
        "verifier": _verifier(e, "SELECT * FROM asset"),
        "effects": [
            {
                "kind": "record_written",
                "match": {"tag": {"param": "tag"}, "name": {"param": "name"}},
                "expected_count": 1,
            }
        ],
        "frame": frame,
    }


def learned_block(*excuses: dict[str, Any]) -> dict[str, Any]:
    return {
        "excuses": list(excuses),
        "proposal_sha256": DIGEST,
        "approved_by": "Test Reviewer",
        "approved_at": "2026-10-31T00:00:00Z",
    }


LOGIN = {
    "table": "app_user",
    "columns": ["last_login"],
    "change": "changed",
    "relation": "actor_row",
    "actor_column": "id",
}
SEEN = {
    "table": "api_token",
    "columns": ["last_seen"],
    "change": "changed",
    "relation": "actor_row",
    "actor_column": "user_id",
}
FIRST_LOGIN = {
    "table": "user_property",
    "columns": ["property", "user_id", "value"],
    "change": "added",
    "relation": "actor_row",
    "actor_column": "user_id",
}
STAMP = {
    "table": "note",
    "columns": ["stamp"],
    "change": "added",
    "relation": "target",
}
TOUCHED = {
    "table": "asset",
    "columns": ["modified_at"],
    "change": "changed",
    "relation": "target",
}
ACTOR_EXCUSES = learned_block(LOGIN, SEEN, FIRST_LOGIN, STAMP)

_n = [0]


def request_bookkeeping(e: Engine, user: str = ACTOR) -> None:
    """What the app writes on every request by ``user``."""

    _n[0] += 1
    stamp = f"t{_n[0]}"
    e.run("UPDATE app_user SET last_login = %s WHERE id = %s", stamp, int(user))
    e.run("UPDATE api_token SET last_seen = %s WHERE user_id = %s", stamp, int(user))
    e.run(
        "INSERT INTO user_property (user_id, property, value) VALUES (%s, %s, %s)",
        int(user),
        f"login.{_n[0]}",
        stamp,
    )


def write_note(e: Engine, title: str) -> None:
    request_bookkeeping(e)
    e.run(
        "INSERT INTO note (title, body, stamp) VALUES (%s, %s, %s)",
        title,
        "Body text",
        "s1",
    )


def check(
    e: Engine,
    contract: dict[str, Any],
    params: dict[str, Any],
    act: Callable[[], None],
    tmp_path: Path,
    actor: str = ACTOR,
) -> Any:
    c = Contract.from_dict(contract, name="c")
    pending = begin_write(
        c, params, actor=Actor("agent", actor), ledger=None, workdir=tmp_path / "w"
    )
    act()
    return verify_write(pending, seal=False)


def _title() -> str:
    return "Note " + secrets.token_hex(3)


# -- before and after: a correct write ---------------------------------------------


def test_bookkeeping_refutes_a_correct_write_until_learned(
    engine: Engine, tmp_path: Path
) -> None:
    t = _title()
    before = check(
        engine,
        note_contract(engine),
        {"title": t, "body": "Body text"},
        lambda: write_note(engine, t),
        tmp_path,
    )
    assert before.outcome.value == "REFUTED"
    moves = before.evidence["frame"]["moves"]
    by_table = {m["table"]: m for m in moves}
    assert by_table["app_user"]["actor_columns"] == ["id"]
    assert by_table["app_user"]["columns"] == ["last_login"]
    assert by_table["api_token"]["actor_columns"] == ["user_id"]
    assert by_table["user_property"]["change"] == "added"
    assert by_table["note"]["target"] is True
    assert by_table["note"]["columns"] == ["stamp"]

    t = _title()
    after = check(
        engine,
        note_contract(engine, ACTOR_EXCUSES),
        {"title": t, "body": "Body text"},
        lambda: write_note(engine, t),
        tmp_path,
    )
    assert after.outcome.value == "CONFIRMED", after.reasons
    used = after.evidence["frame"]["learned"]["used"]
    assert len(used) == 4
    assert after.evidence["frame"]["learned"]["proposal_sha256"] == DIGEST


# -- adversarial: what an actor_row excuse must never cover ------------------------


def _refuted(result: Any) -> None:
    assert result.outcome.value == "REFUTED", result.reasons
    assert result.checked["frame"] == "refuted"
    assert any("outside the declared write moved" in r for r in result.reasons)


def test_actor_row_never_covers_another_users_row(
    engine: Engine, tmp_path: Path
) -> None:
    t = _title()

    def act() -> None:
        write_note(engine, t)
        engine.run("UPDATE app_user SET last_login = 'x' WHERE id = 3")

    _refuted(
        check(
            engine,
            note_contract(engine, ACTOR_EXCUSES),
            {"title": t, "body": "Body text"},
            act,
            tmp_path,
        )
    )


def test_actor_row_never_covers_another_users_token(
    engine: Engine, tmp_path: Path
) -> None:
    t = _title()

    def act() -> None:
        write_note(engine, t)
        engine.run("UPDATE api_token SET last_seen = 'x' WHERE user_id = 3")

    _refuted(
        check(
            engine,
            note_contract(engine, ACTOR_EXCUSES),
            {"title": t, "body": "Body text"},
            act,
            tmp_path,
        )
    )


def test_actor_row_never_covers_another_column_of_the_user_row(
    engine: Engine, tmp_path: Path
) -> None:
    t = _title()

    def act() -> None:
        write_note(engine, t)
        engine.run("UPDATE app_user SET email = 'evil@example.test' WHERE id = 2")

    _refuted(
        check(
            engine,
            note_contract(engine, ACTOR_EXCUSES),
            {"title": t, "body": "Body text"},
            act,
            tmp_path,
        )
    )


def test_actor_row_never_covers_a_row_moved_to_the_actor(
    engine: Engine, tmp_path: Path
) -> None:
    """The colleague's token handed to the bot: its user column moved."""

    t = _title()

    def act() -> None:
        write_note(engine, t)
        engine.run("UPDATE api_token SET user_id = 2, last_seen = 'y' WHERE id = 2")

    _refuted(
        check(
            engine,
            note_contract(engine, ACTOR_EXCUSES),
            {"title": t, "body": "Body text"},
            act,
            tmp_path,
        )
    )


def test_actor_row_never_covers_a_removal_when_changes_were_learned(
    engine: Engine, tmp_path: Path
) -> None:
    t = _title()

    def act() -> None:
        write_note(engine, t)
        engine.run("DELETE FROM api_token WHERE user_id = 2")

    _refuted(
        check(
            engine,
            note_contract(engine, ACTOR_EXCUSES),
            {"title": t, "body": "Body text"},
            act,
            tmp_path,
        )
    )


def test_actor_row_never_covers_a_row_added_for_another_user(
    engine: Engine, tmp_path: Path
) -> None:
    t = _title()

    def act() -> None:
        write_note(engine, t)
        engine.run(
            "INSERT INTO user_property (user_id, property, value) "
            "VALUES (3, 'login.x', 'x')"
        )

    _refuted(
        check(
            engine,
            note_contract(engine, ACTOR_EXCUSES),
            {"title": t, "body": "Body text"},
            act,
            tmp_path,
        )
    )


def test_actor_row_never_covers_an_added_row_setting_another_column(
    engine: Engine, tmp_path: Path
) -> None:
    """A token added for the actor: only changes to tokens were learned."""

    t = _title()

    def act() -> None:
        write_note(engine, t)
        engine.run(
            "INSERT INTO api_token (user_id, label, last_seen) VALUES (2, 'new', 'z')"
        )

    _refuted(
        check(
            engine,
            note_contract(engine, ACTOR_EXCUSES),
            {"title": t, "body": "Body text"},
            act,
            tmp_path,
        )
    )


def test_actor_row_never_covers_a_column_a_large_table_didnt_compare(
    engine: Engine, tmp_path: Path
) -> None:
    """Over ``max_rows`` the baseline keeps some columns only. The token's
    label isn't kept, so a label change beside ``last_seen`` can't be seen
    as a column: the row is never excused."""

    contract = note_contract(engine, ACTOR_EXCUSES)
    contract["frame"]["database"]["max_rows"] = 1
    t = _title()

    def act() -> None:
        write_note(engine, t)
        engine.run("UPDATE api_token SET label = 'stolen' WHERE user_id = 2")

    result = check(engine, contract, {"title": t, "body": "Body text"}, act, tmp_path)
    _refuted(result)
    moves = {m["table"]: m for m in result.evidence["frame"]["moves"]}
    assert "(columns not compared)" in moves["api_token"]["columns"]


def test_actor_row_follows_this_runs_actor(engine: Engine, tmp_path: Path) -> None:
    """The same moves, but the run's actor is the colleague: the bot's rows
    are now another user's."""

    t = _title()
    _refuted(
        check(
            engine,
            note_contract(engine, ACTOR_EXCUSES),
            {"title": t, "body": "Body text"},
            lambda: write_note(engine, t),
            tmp_path,
            actor=OTHER,
        )
    )


# -- adversarial: what a target excuse must never cover ------------------------------


def test_target_excuse_never_covers_a_look_alike(
    engine: Engine, tmp_path: Path
) -> None:
    contract = asset_contract(engine, learned_block(TOUCHED, LOGIN))

    def correct() -> None:
        request_bookkeeping(engine)
        engine.run(
            "UPDATE asset SET name = 'Laptop B', modified_at = 'm1' WHERE tag = 'LT-100'"
        )

    ok = check(
        engine, contract, {"tag": "LT-100", "name": "Laptop B"}, correct, tmp_path
    )
    # user_property and api_token moves aren't excused by this block.
    assert ok.outcome.value == "REFUTED"
    tables = {m["table"] for m in ok.evidence["frame"]["moves"]}
    assert tables == {"api_token", "user_property"}

    contract = asset_contract(engine, learned_block(TOUCHED, LOGIN, SEEN, FIRST_LOGIN))

    def correct2() -> None:
        request_bookkeeping(engine)
        engine.run(
            "UPDATE asset SET name = 'Laptop C', modified_at = 'm2' WHERE tag = 'LT-100'"
        )

    ok = check(
        engine, contract, {"tag": "LT-100", "name": "Laptop C"}, correct2, tmp_path
    )
    assert ok.outcome.value == "CONFIRMED", ok.reasons

    def look_alike() -> None:
        correct_rename = "UPDATE asset SET name = 'Laptop D' WHERE tag = 'LT-100'"
        request_bookkeeping(engine)
        engine.run(correct_rename)
        engine.run("UPDATE asset SET modified_at = 'm3' WHERE tag = 'LT-1000'")

    _refuted(
        check(
            engine,
            contract,
            {"tag": "LT-100", "name": "Laptop D"},
            look_alike,
            tmp_path,
        )
    )


def test_excused_column_never_reaches_every_row(engine: Engine, tmp_path: Path) -> None:
    """The fail-open the first retest found: a column excused for the
    target was excused on every row of the table. A learned target excuse
    on ``assigned_to`` covers the target only; a second asset checked out
    beside it is collateral."""

    checkout = {
        "table": "asset",
        "columns": ["assigned_to", "modified_at"],
        "change": "changed",
        "relation": "target",
    }
    contract = asset_contract(engine, learned_block(checkout, LOGIN, SEEN, FIRST_LOGIN))

    def act() -> None:
        request_bookkeeping(engine)
        engine.run(
            "UPDATE asset SET name = 'Laptop E', assigned_to = 2, modified_at = 'm4' "
            "WHERE tag = 'LT-100'"
        )
        engine.run("UPDATE asset SET assigned_to = 2 WHERE tag = 'LT-1000'")

    _refuted(
        check(engine, contract, {"tag": "LT-100", "name": "Laptop E"}, act, tmp_path)
    )


def test_target_excuse_never_covers_another_column_of_the_target(
    engine: Engine, tmp_path: Path
) -> None:
    contract = asset_contract(engine, learned_block(TOUCHED, LOGIN, SEEN, FIRST_LOGIN))

    def act() -> None:
        request_bookkeeping(engine)
        engine.run(
            "UPDATE asset SET name = 'Laptop F', assigned_to = 3, modified_at = 'm5' "
            "WHERE tag = 'LT-100'"
        )

    _refuted(
        check(engine, contract, {"tag": "LT-100", "name": "Laptop F"}, act, tmp_path)
    )


# -- learning: propose and approve -------------------------------------------------


@dataclass
class _Ep:
    id: int
    name: str
    contract: Any = None
    kind: str = "sql"
    writes: list[Any] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)


def test_learn_propose_approve_on_real_writes(engine: Engine, tmp_path: Path) -> None:
    """Five correct writes, collected through the shadow's hook, give a
    proposal; one approval writes it into the contract; then correct writes
    confirm and each fault still stops."""

    path = tmp_path / "note.verify.yaml"
    path.write_text(yaml.safe_dump(note_contract(engine)), encoding="utf-8")
    collector = learn.Collector(tmp_path / "learn" / "episodes.jsonl")
    for i in range(5):
        t = _title()
        result = check(
            engine,
            note_contract(engine),
            {"title": t, "body": "Body text"},
            lambda: write_note(engine, t),
            tmp_path,
        )
        collector(_Ep(i + 1, "create_note", Contract.load(path)), result)
    # A wrong write (the declared record missing) teaches nothing.
    wrong = check(
        engine,
        note_contract(engine),
        {"title": "never written", "body": "Body text"},
        lambda: request_bookkeeping(engine),
        tmp_path,
    )
    collector(_Ep(6, "create_note", Contract.load(path)), wrong)

    episodes = learn.read_episodes([collector.path])
    schema = engine.schema() if engine.schema else None
    users = learn.users_from(
        "app_user",
        None if schema else "id",
        schema,
        [] if schema else ["api_token.user_id", "user_property.user_id"],
    )
    if schema is not None:
        # The user columns come from the schema's foreign keys.
        assert users.columns == {
            "api_token": {"user_id": "foreign_key"},
            "user_property": {"user_id": "foreign_key"},
        }
    proposal = learn.propose(episodes, users=users)
    kind = proposal["kinds"]["create_note"]
    assert kind["learnable_episodes"] == 5
    assert kind["ignored"] == 1
    assert kind["ignored_by_reason"] == {"the declared effects didn't hold": 1}
    got = {
        (e["table"], e["change"], e["relation"], e.get("actor_column")): e["columns"]
        for e in kind["excuses"]
    }
    assert got == {
        ("api_token", "changed", "actor_row", "user_id"): ["last_seen"],
        ("app_user", "changed", "actor_row", "id"): ["last_login"],
        ("note", "added", "target", None): ["stamp"],
        ("user_property", "added", "actor_row", "user_id"): [
            "property",
            "user_id",
            "value",
        ],
    }
    out, md = learn.write_proposal(proposal, tmp_path / "learn" / "proposal.yaml")
    assert "Proposed excuses" in md.read_text()
    loaded = learn.load_proposal(out)
    done = learn.approve(loaded, by="Test Reviewer", contracts={"create_note": [path]})
    assert done == [("create_note", path, 4)]
    approved = yaml.safe_load(path.read_text())
    assert (
        approved["frame"]["learned"]["proposal_sha256"] == proposal["proposal_sha256"]
    )
    assert approved["frame"]["learned"]["approved_by"] == "Test Reviewer"

    t = _title()
    ok = check(
        engine,
        approved,
        {"title": t, "body": "Body text"},
        lambda: write_note(engine, t),
        tmp_path,
    )
    assert ok.outcome.value == "CONFIRMED", ok.reasons

    t = _title()

    def fault() -> None:
        write_note(engine, t)
        engine.run("UPDATE app_user SET last_login = 'x' WHERE id = 3")

    _refuted(
        check(engine, approved, {"title": t, "body": "Body text"}, fault, tmp_path)
    )


# -- the learner's rules (no database) -----------------------------------------------


def _ep(kind: str, moves: list[dict[str, Any]], **kw: Any) -> dict[str, Any]:
    return {
        "schema_version": learn.EPISODE_SCHEMA,
        "kind": kind,
        "mode": "sql",
        "outcome": kw.get("outcome", "REFUTED"),
        "delta": kw.get("delta", "held"),
        "frame": kw.get("frame", "refuted"),
        "moves": moves,
    }


def _move(table: str, columns: list[str], **kw: Any) -> dict[str, Any]:
    return {
        "row": True,
        "table": table,
        "change": kw.get("change", "changed"),
        "columns": columns,
        "target": kw.get("target", False),
        "attribution": "unattributed",
        "actor_columns": kw.get("actor_columns", []),
    }


USERS = learn.Users("app_user", "id", {"api_token": {"user_id": "foreign_key"}})


def test_a_row_that_is_neither_target_nor_actor_stays_a_stop_however_often() -> None:
    eps = [
        _ep(
            "k",
            [
                _move("app_user", ["last_login"], actor_columns=["id"]),
                _move("settings", ["value"]),
            ],
        )
        for _ in range(10)
    ]
    p = learn.propose(eps, users=USERS)
    k = p["kinds"]["k"]
    assert [e["table"] for e in k["excuses"]] == ["app_user"]
    assert k["stays_a_stop"] == [
        {
            "table": "settings",
            "change": "changed",
            "why": "not the write's own row, and not the acting user's row",
            "seen": 10,
            "of": 10,
        }
    ]


def test_another_users_row_is_not_the_actors() -> None:
    # The row's user column didn't hold the actor's id (actor_columns empty).
    eps = [_ep("k", [_move("api_token", ["last_seen"])]) for _ in range(6)]
    k = learn.propose(eps, users=USERS)["kinds"]["k"]
    assert k["excuses"] == []
    assert k["stays_a_stop"][0]["table"] == "api_token"


def test_an_actor_column_that_isnt_a_user_column_is_not_used() -> None:
    eps = [
        _ep("k", [_move("audit_log", ["note"], actor_columns=["created_by"])])
        for _ in range(6)
    ]
    k = learn.propose(eps, users=USERS)["kinds"]["k"]
    assert k["excuses"] == []


def test_removals_and_unnamed_columns_are_never_excused() -> None:
    eps = [
        _ep(
            "k",
            [
                _move("api_token", [], change="removed", actor_columns=["user_id"]),
                _move("app_user", ["(columns not compared)"], actor_columns=["id"]),
            ],
        )
        for _ in range(6)
    ]
    k = learn.propose(eps, users=USERS)["kinds"]["k"]
    assert k["excuses"] == []
    assert len(k["stays_a_stop"]) == 2


def test_k_of_n() -> None:
    def eps(seen: int, total: int) -> list[dict[str, Any]]:
        return [
            _ep(
                "k",
                [_move("app_user", ["last_login"], actor_columns=["id"])]
                if i < seen
                else [_move("note", ["stamp"], target=True)],
            )
            for i in range(total)
        ]

    assert learn.propose(eps(4, 4), users=USERS)["kinds"]["k"]["excuses"] == []
    assert len(learn.propose(eps(3, 5), users=USERS)["kinds"]["k"]["excuses"]) == 1
    tables = [
        e["table"]
        for e in learn.propose(eps(2, 5), users=USERS)["kinds"]["k"]["excuses"]
    ]
    assert tables == ["note"]
    # 10 episodes need 6.
    tables = [
        e["table"]
        for e in learn.propose(eps(5, 10), users=USERS)["kinds"]["k"]["excuses"]
    ]
    assert "app_user" not in tables


def test_columns_below_the_threshold_are_left_out() -> None:
    eps = [
        _ep(
            "k",
            [
                _move(
                    "app_user",
                    ["last_login", "email"] if i == 0 else ["last_login"],
                    actor_columns=["id"],
                )
            ],
        )
        for i in range(5)
    ]
    (e,) = learn.propose(eps, users=USERS)["kinds"]["k"]["excuses"]
    assert e["columns"] == ["last_login"]
    assert e["columns_below_threshold"] == ["email"]


def test_every_other_episode_is_ignored_and_counted() -> None:
    good = [_move("app_user", ["last_login"], actor_columns=["id"])]
    eps = [_ep("k", good) for _ in range(5)] + [
        _ep("k", good, delta="refuted"),
        _ep("k", [], outcome="CONFIRMED", frame="held"),
        _ep("k", good, frame="not_checked", outcome="UNCERTAIN"),
        _ep("k", good + [{"row": False, "table": "", "change": "changed"}]),
        _ep("k", good + [{"truncated": True}]),
        {**_ep("k", good), "mode": "fhir"},
    ]
    p = learn.propose(eps, users=USERS)
    k = p["kinds"]["k"]
    assert k["learnable_episodes"] == 5
    assert k["ignored"] == 6 == p["ignored"]
    assert len(k["excuses"]) == 1


def test_without_users_only_target_rows() -> None:
    eps = [
        _ep(
            "k",
            [
                _move("app_user", ["last_login"], actor_columns=["id"]),
                _move("note", ["stamp"], target=True, change="added"),
            ],
        )
        for _ in range(5)
    ]
    k = learn.propose(eps)["kinds"]["k"]
    assert [e["table"] for e in k["excuses"]] == ["note"]


def test_an_edited_proposal_is_refused(tmp_path: Path) -> None:
    eps = [_ep("k", [_move("app_user", ["last_login"], actor_columns=["id"])])] * 5
    out, _ = learn.write_proposal(learn.propose(eps, users=USERS), tmp_path / "p.yaml")
    data = yaml.safe_load(out.read_text())
    data["kinds"]["k"]["excuses"][0]["columns"].append("email")
    out.write_text(yaml.safe_dump(data, sort_keys=False))
    with pytest.raises(learn.LearnError, match="changed after it was proposed"):
        learn.load_proposal(out)


def test_approve_is_all_or_nothing_and_can_reject(tmp_path: Path) -> None:
    db = tmp_path / "x.sqlite"
    e = Engine("sqlite", {"sqlite_database": str(db)}, lambda *a: None)
    good = tmp_path / "good.yaml"
    good.write_text(yaml.safe_dump(note_contract(e)))
    bad = tmp_path / "bad.yaml"
    no_db = note_contract(e)
    no_db["frame"].pop("database")
    bad.write_text(yaml.safe_dump(no_db))
    eps = [
        _ep(
            "k",
            [
                _move("app_user", ["last_login"], actor_columns=["id"]),
                _move("note", ["stamp"], target=True, change="added"),
            ],
        )
    ] * 5
    p = learn.propose(eps, users=USERS)
    before = good.read_text()
    with pytest.raises(learn.LearnError, match="frame.database"):
        learn.approve(p, by="R", contracts={"k": [good, bad]})
    assert good.read_text() == before
    with pytest.raises(learn.LearnError, match="no proposed excuse"):
        learn.approve(p, by="R", contracts={"k": [good]}, reject=["E9"])
    ids = {e["table"]: e["id"] for e in p["kinds"]["k"]["excuses"]}
    learn.approve(p, by="R", contracts={"k": [good]}, reject=[ids["note"]])
    block = yaml.safe_load(good.read_text())["frame"]["learned"]
    assert [x["table"] for x in block["excuses"]] == ["app_user"]


def test_a_contract_without_learned_keeps_its_digest(tmp_path: Path) -> None:
    e = Engine(
        "sqlite", {"sqlite_database": str(tmp_path / "x.sqlite")}, lambda *a: None
    )
    plain = Contract.from_dict(note_contract(e))
    from openadapt_flow.runtime.effects.frame import FrameSpec

    spec = plain.config.frame
    assert spec is not None and "learned" not in spec.model_dump(mode="json")
    # The frame digest is what it was before the key existed.
    again = FrameSpec.model_validate(spec.model_dump(mode="json", by_alias=True))
    assert again.digest() == spec.digest()
    with_key = Contract.from_dict(note_contract(e, ACTOR_EXCUSES))
    assert with_key.digest != plain.digest
    assert with_key.config.frame.digest() != spec.digest()


@pytest.mark.parametrize(
    "bad, why",
    [
        ({**LOGIN, "table": "*"}, "one table"),
        ({**LOGIN, "columns": ["*"]}, "real columns"),
        ({**LOGIN, "columns": ["(columns not compared)"]}, "real columns"),
        ({**LOGIN, "change": "removed"}, "change"),
        ({**LOGIN, "columns": ["id", "last_login"]}, "actor column change"),
        ({**FIRST_LOGIN, "columns": ["property", "value"]}, "sets its actor column"),
        ({**LOGIN, "actor_column": None}, "names its actor_column"),
        ({**STAMP, "actor_column": "user_id"}, "goes with relation actor_row"),
        ({**LOGIN, "relation": "any_row"}, "relation"),
    ],
)
def test_a_learned_excuse_is_never_wider_than_one_move(bad: dict, why: str) -> None:
    with pytest.raises(Exception, match=why):
        LearnedExcuse.model_validate(bad)


def test_learned_needs_the_database_frame(tmp_path: Path) -> None:
    e = Engine(
        "sqlite", {"sqlite_database": str(tmp_path / "x.sqlite")}, lambda *a: None
    )
    c = note_contract(e, ACTOR_EXCUSES)
    c["frame"].pop("database")
    with pytest.raises(Exception, match="frame.database"):
        Contract.from_dict(c)
    assert LearnedFrame.model_validate(ACTOR_EXCUSES).columns_to_keep() == [
        "id",
        "last_login",
        "last_seen",
        "property",
        "stamp",
        "user_id",
        "value",
    ]


def test_the_cli_proposes_and_approves(tmp_path: Path, capsys: Any) -> None:
    e = Engine(
        "sqlite", {"sqlite_database": str(tmp_path / "x.sqlite")}, lambda *a: None
    )
    contract = tmp_path / "note.yaml"
    contract.write_text(yaml.safe_dump(note_contract(e)))
    episodes = tmp_path / "episodes.jsonl"
    move = _move("api_token", ["last_seen"], actor_columns=["user_id"])
    episodes.write_text(
        "".join(json.dumps(_ep("note", [move])) + "\n" for _ in range(5))
    )
    proposal = tmp_path / "proposal.yaml"
    code = learn.main(
        [
            "propose",
            "--episodes",
            str(episodes),
            "--users",
            "app_user.id",
            "--user-column",
            "api_token.user_id",
            "--out",
            str(proposal),
        ]
    )
    assert code == 0
    md = proposal.with_suffix(".md").read_text()
    assert "`api_token`" in md and "user column" in md
    with pytest.raises(SystemExit):
        learn.main(["approve", str(proposal), "--contract", f"note={contract}"])
    code = learn.main(
        ["approve", str(proposal), "--by", "Dana", "--contract", f"note={contract}"]
    )
    assert code == 0
    block = yaml.safe_load(contract.read_text())["frame"]["learned"]
    assert block["excuses"] == [SEEN]
    assert block["approved_by"] == "Dana"
    assert "1 excuse(s) written into 1 contract(s)" in capsys.readouterr().out


# -- through the shadow proxy ---------------------------------------------------------


class _App:
    """POST /api/notes: saves a note, and, as many apps do, updates the
    acting user's last_login on every request."""

    def __init__(self, db: Path) -> None:
        self.db = db
        app = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *_: Any) -> None:
                pass

            def do_POST(self) -> None:
                doc = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                con = sqlite3.connect(app.db)
                con.execute(
                    "UPDATE app_user SET last_login = ? WHERE id = 2",
                    (secrets.token_hex(4),),
                )
                con.execute(
                    "INSERT INTO note (title, body, stamp) VALUES (?, ?, 's')",
                    (doc["title"], doc["body"]),
                )
                con.commit()
                con.close()
                self.send_response(201)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"


def test_collect_through_the_shadow_hook(tmp_path: Path) -> None:
    import http.client

    from openadapt_flow.shadow import Route, Shadow

    e = _sqlite(tmp_path)
    for s in e.ddl():
        e.run(s)
    app = _App(Path(e.read["sqlite_database"]))
    contract = Contract.from_dict(note_contract(e), name="note")
    collector = learn.Collector(tmp_path / "episodes.jsonl")
    sh = Shadow(
        upstream=app.url,
        mode="sql",
        routes=[
            Route(
                re.compile(r"POST /api/notes"),
                contract,
                {"title": "title", "body": "body"},
                "create_note",
            )
        ],
        actor_id=ACTOR,
        out=tmp_path / "out",
        quiet_s=60,
    ).start()
    sh.on_checked = collector
    try:
        for i in range(5):
            host, port = sh.server.server_address[:2]
            conn = http.client.HTTPConnection(host, port)
            body = json.dumps({"title": f"Note {i}", "body": "B"})
            conn.request(
                "POST", "/api/notes", body, {"Content-Type": "application/json"}
            )
            assert conn.getresponse().status == 201
            conn.close()
            sh.flush()
    finally:
        sh.stop()
    eps = learn.read_episodes([collector.path])
    assert len(eps) == 5
    assert all(ep["kind"] == "create_note" and ep["delta"] == "held" for ep in eps)
    users = learn.users_from("app_user", "id", None)
    p = learn.propose(eps, users=users)
    got = sorted(
        (x["table"], x["relation"]) for x in p["kinds"]["create_note"]["excuses"]
    )
    assert got == [("app_user", "actor_row"), ("note", "target")]
    for ep in eps:
        for m in ep["moves"]:
            assert set(m) <= {
                "row",
                "table",
                "change",
                "columns",
                "target",
                "attribution",
                "actor_columns",
            }
