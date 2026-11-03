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
            f"CREATE TABLE api_token (id {key}, user_id INTEGER NOT NULL, "
            "issued_by INTEGER, label VARCHAR(30), last_seen VARCHAR(30), "
            "FOREIGN KEY (user_id) REFERENCES app_user(id), "
            "FOREIGN KEY (issued_by) REFERENCES app_user(id))",
            "CREATE TABLE user_property (user_id INTEGER NOT NULL, "
            "property VARCHAR(50) NOT NULL, value VARCHAR(50), "
            "PRIMARY KEY (user_id, property), "
            "FOREIGN KEY (user_id) REFERENCES app_user(id))",
            f"CREATE TABLE note (id {key}, title VARCHAR(80), body VARCHAR(200), "
            "stamp VARCHAR(30))",
            f"CREATE TABLE asset (id {key}, tag VARCHAR(20), name VARCHAR(50), "
            "assigned_to INTEGER, modified_at VARCHAR(30))",
            "INSERT INTO app_user (id, name, email, last_login) VALUES "
            "(1, 'admin', 'admin@example.test', 't0'), "
            "(2, 'agentbot', 'bot@example.test', 't0'), "
            "(3, 'colleague', 'col@example.test', 't0')",
            "INSERT INTO api_token (id, user_id, issued_by, label, last_seen) VALUES "
            "(1, 2, 2, 'bot', 't0'), (2, 3, 2, 'colleague', 't0')",
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
    "actor_columns": ["id"],
}
SEEN = {
    "table": "api_token",
    "columns": ["last_seen"],
    "change": "changed",
    "relation": "actor_row",
    "actor_columns": ["user_id"],
}
FIRST_LOGIN = {
    "table": "user_property",
    "columns": ["property", "user_id", "value"],
    "change": "added",
    "relation": "actor_row",
    "actor_columns": ["user_id"],
    "max_rows": 1,
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
ACTOR_EXCUSES = learned_block(LOGIN, SEEN, FIRST_LOGIN)

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


def write_note(e: Engine, title: str, stamp: Optional[str] = None) -> None:
    """The agent's note. ``stamp`` set: a column the write doesn't declare,
    on the written row."""

    request_bookkeeping(e)
    e.run(
        "INSERT INTO note (title, body, stamp) VALUES (%s, %s, %s)",
        title,
        "Body text",
        stamp,
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
        lambda: write_note(engine, t, stamp="s1"),
        tmp_path,
    )
    assert before.outcome.value == "REFUTED"
    moves = before.evidence["frame"]["moves"]
    by_table = {m["table"]: m for m in moves}
    assert by_table["app_user"]["actor_columns"] == ["id"]
    assert by_table["app_user"]["columns"] == ["last_login"]
    assert by_table["api_token"]["actor_columns"] == ["issued_by", "user_id"]
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
    assert len(used) == 3
    assert after.evidence["frame"]["learned"]["proposal_sha256"] == DIGEST

    # Collateral on the written row stays a stop, learned excuses or not.
    t = _title()
    _refuted(
        check(
            engine,
            note_contract(engine, ACTOR_EXCUSES),
            {"title": t, "body": "Body text"},
            lambda: write_note(engine, t, stamp="s2"),
            tmp_path,
        )
    )


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


# -- the written row: never excused -------------------------------------------------


@pytest.mark.parametrize("excuse", [STAMP, TOUCHED], ids=["added", "changed"])
def test_a_target_excuse_is_refused_at_load(engine: Engine, excuse: dict) -> None:
    """Collateral on the written row is never learned: a ``frame.learned``
    entry with ``relation: target`` doesn't load, whatever the engine."""

    with pytest.raises(Exception, match="can't cover the written row"):
        Contract.from_dict(asset_contract(engine, learned_block(excuse, LOGIN)))


def test_collateral_on_the_written_row_stays_a_stop(
    engine: Engine, tmp_path: Path
) -> None:
    """With every actor excuse learned, a correct rename that also moves an
    undeclared column of the asset is refuted, and so is a look-alike asset
    moved beside it (the retest's every-row fail-open)."""

    contract = asset_contract(engine, learned_block(LOGIN, SEEN, FIRST_LOGIN))

    def correct() -> None:
        request_bookkeeping(engine)
        engine.run("UPDATE asset SET name = 'Laptop C' WHERE tag = 'LT-100'")

    ok = check(
        engine, contract, {"tag": "LT-100", "name": "Laptop C"}, correct, tmp_path
    )
    assert ok.outcome.value == "CONFIRMED", ok.reasons

    def stray() -> None:
        request_bookkeeping(engine)
        engine.run(
            "UPDATE asset SET name = 'Laptop D', modified_at = 'm1' "
            "WHERE tag = 'LT-100'"
        )

    _refuted(
        check(engine, contract, {"tag": "LT-100", "name": "Laptop D"}, stray, tmp_path)
    )

    def look_alike() -> None:
        request_bookkeeping(engine)
        engine.run("UPDATE asset SET name = 'Laptop E' WHERE tag = 'LT-100'")
        engine.run("UPDATE asset SET assigned_to = 2 WHERE tag = 'LT-1000'")

    _refuted(
        check(
            engine,
            contract,
            {"tag": "LT-100", "name": "Laptop E"},
            look_alike,
            tmp_path,
        )
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
    #: What the shadow's body recording gives (``learn.record_bodies``).
    sent_fields: Optional[set[str]] = None


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
        collector(
            _Ep(
                i + 1,
                "create_note",
                Contract.load(path),
                sent_fields={"title", "body"},
            ),
            result,
        )
    # A wrong write (the declared record missing) teaches nothing.
    wrong = check(
        engine,
        note_contract(engine),
        {"title": "never written", "body": "Body text"},
        lambda: request_bookkeeping(engine),
        tmp_path,
    )
    collector(_Ep(6, "create_note", Contract.load(path), sent_fields=set()), wrong)

    episodes = learn.read_episodes([collector.path])
    schema = engine.schema() if engine.schema else None
    users = learn.users_from(
        "app_user",
        None if schema else "id",
        schema,
        []
        if schema
        else ["api_token.user_id", "api_token.issued_by", "user_property.user_id"],
    )
    if schema is not None:
        # The user columns come from the schema's foreign keys.
        assert users.columns == {
            "api_token": {"issued_by": "foreign_key", "user_id": "foreign_key"},
            "user_property": {"user_id": "foreign_key"},
        }
    proposal = learn.propose(episodes, users=users)
    kind = proposal["kinds"]["create_note"]
    assert kind["learnable_episodes"] == 5
    assert kind["ignored"] == 1
    assert kind["ignored_by_reason"] == {"the declared effects didn't hold": 1}
    got = {
        (
            e["table"],
            e["change"],
            e["relation"],
            tuple(e.get("actor_columns") or ()),
        ): e["columns"]
        for e in kind["excuses"]
    }
    assert got == {
        ("api_token", "changed", "actor_row", ("issued_by", "user_id")): ["last_seen"],
        ("app_user", "changed", "actor_row", ("id",)): ["last_login"],
        ("user_property", "added", "actor_row", ("user_id",)): [
            "property",
            "user_id",
            "value",
        ],
    }
    out, md = learn.write_proposal(proposal, tmp_path / "learn" / "proposal.yaml")
    assert "Proposed excuses" in md.read_text()
    loaded = learn.load_proposal(out)
    done = learn.approve(loaded, by="Test Reviewer", contracts={"create_note": [path]})
    assert done == [("create_note", path, 3)]
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

    # The colleague's token, which the bot issued: one user column holds the
    # bot's id, the other the colleague's. The colleague's own request moves
    # it inside the window (a concurrent edit with no audit trail). The
    # learned excuse needs both user columns, so it is collateral.
    t = _title()

    def colleague_request() -> None:
        write_note(engine, t)
        engine.run("UPDATE api_token SET last_seen = 'c1' WHERE id = 2")

    _refuted(
        check(
            engine,
            approved,
            {"title": t, "body": "Body text"},
            colleague_request,
            tmp_path,
        )
    )


def test_actor_row_never_covers_a_row_the_actor_issued_for_another_user(
    engine: Engine, tmp_path: Path
) -> None:
    """A token's ``issued_by`` holds the bot's id on the colleague's token
    too. An excuse learned with every user column of the bot's own token
    (``issued_by`` and ``user_id``) never covers the colleague's."""

    seen_both = {**SEEN, "actor_columns": ["issued_by", "user_id"]}
    block = learned_block(LOGIN, seen_both, FIRST_LOGIN)
    t = _title()
    ok = check(
        engine,
        note_contract(engine, block),
        {"title": t, "body": "Body text"},
        lambda: write_note(engine, t),
        tmp_path,
    )
    assert ok.outcome.value == "CONFIRMED", ok.reasons
    t = _title()

    def act() -> None:
        write_note(engine, t)
        engine.run("UPDATE api_token SET last_seen = 'c2' WHERE id = 2")

    _refuted(
        check(
            engine,
            note_contract(engine, block),
            {"title": t, "body": "Body text"},
            act,
            tmp_path,
        )
    )


def test_the_learner_requires_every_user_column_that_holds_the_actor() -> None:
    users = learn.Users(
        "app_user",
        "id",
        {"api_token": {"issued_by": "foreign_key", "user_id": "foreign_key"}},
    )
    eps = [
        _ep(
            "k",
            [_move("api_token", ["last_seen"], actor_columns=["issued_by", "user_id"])],
        )
        for _ in range(5)
    ]
    (e,) = learn.propose(eps, users=users)["kinds"]["k"]["excuses"]
    assert e["actor_columns"] == ["issued_by", "user_id"]


def _learn_assets(
    engine: Engine,
    tmp_path: Path,
    act: Callable[[int], None],
    body: Callable[[int], dict[str, Any]],
    recorded: bool = True,
) -> dict[str, Any]:
    """Five learning writes renaming asset LT-100 through the collector."""

    collector = learn.Collector(tmp_path / f"ep-{secrets.token_hex(3)}.jsonl")
    for i in range(5):
        contract = asset_contract(engine)
        result = check(
            engine,
            contract,
            {"tag": "LT-100", "name": f"Laptop L{i}"},
            lambda i=i: act(i),
            tmp_path,
        )
        sent = learn.body_fields(body(i)) if recorded else None
        collector(
            _Ep(i + 1, "rename", Contract.from_dict(contract), sent_fields=sent),
            result,
        )
    users = learn.users_from(
        "app_user", "id", None, ["api_token.user_id", "api_token.issued_by"]
    )
    users.columns.setdefault("user_property", {})["user_id"] = "person"
    return learn.propose(learn.read_episodes([collector.path]), users=users)


def test_the_agents_own_stray_column_is_never_learned(
    engine: Engine, tmp_path: Path
) -> None:
    """The review's attack: every learning write renames the asset (right)
    and reassigns it (wrong, undeclared). The delta holds, so all five
    teach. ``assigned_to`` is in the agent's own request body, so it is
    never proposed, and a later wrong reassignment is refuted."""

    def act(i: int) -> None:
        request_bookkeeping(engine)
        engine.run(
            "UPDATE asset SET name = %s, assigned_to = %s, modified_at = %s "
            "WHERE tag = 'LT-100'",
            f"Laptop L{i}",
            3 if i % 2 == 0 else 1,
            f"m{i}",
        )

    proposal = _learn_assets(
        engine,
        tmp_path,
        act,
        lambda i: {"name": f"Laptop L{i}", "assigned_to": 3 if i % 2 == 0 else 1},
    )
    kind = proposal["kinds"]["rename"]
    assert kind["request_bodies_recorded"] is True
    assert [e for e in kind["excuses"] if e["table"] == "asset"] == []
    stops = [s for s in kind["stays_a_stop"] if s["table"] == "asset"]
    assert stops and stops[0]["why"].startswith("the written row")

    path = tmp_path / "asset.yaml"
    path.write_text(yaml.safe_dump(asset_contract(engine)))
    learn.approve(proposal, by="Test Reviewer", contracts={"rename": [path]})
    approved = yaml.safe_load(path.read_text())

    def wrong() -> None:
        request_bookkeeping(engine)
        engine.run(
            "UPDATE asset SET name = 'Laptop Z', assigned_to = 1, modified_at = 'mz' "
            "WHERE tag = 'LT-100'"
        )

    _refuted(
        check(engine, approved, {"tag": "LT-100", "name": "Laptop Z"}, wrong, tmp_path)
    )


def test_the_apps_own_column_on_the_written_row_is_never_learned(
    engine: Engine, tmp_path: Path
) -> None:
    """``modified_at`` the app sets itself, and the agent's bodies never
    sent: still not learned (a body field of another name could set it).
    Without recorded bodies, nothing at all is learned."""

    def act(i: int) -> None:
        request_bookkeeping(engine)
        engine.run(
            "UPDATE asset SET name = %s, modified_at = %s WHERE tag = 'LT-100'",
            f"Laptop L{i}",
            f"m{i}",
        )

    with_bodies = _learn_assets(
        engine, tmp_path, act, lambda i: {"name": f"Laptop L{i}"}
    )["kinds"]["rename"]
    assert [e for e in with_bodies["excuses"] if e["table"] == "asset"] == []
    assert {e["table"] for e in with_bodies["excuses"]} == {
        "app_user",
        "api_token",
        "user_property",
    }
    stops = [s for s in with_bodies["stays_a_stop"] if s["table"] == "asset"]
    assert "bookkeeping answer" in stops[0]["why"]
    without = _learn_assets(engine, tmp_path, act, lambda i: {}, recorded=False)
    kind = without["kinds"]["rename"]
    assert kind["request_bodies_recorded"] is False
    assert kind["excuses"] == []
    out, md = learn.write_proposal(without, tmp_path / "nobodies.yaml")
    text = md.read_text()
    assert "request bodies weren't recorded" in text
    assert "Collateral on the row a write declares is never learned" in text


def test_an_added_actor_row_excuse_never_covers_more_rows_than_learned(
    engine: Engine, tmp_path: Path
) -> None:
    """Learned: one first-login row per write. Twenty more rows for the
    actor, with any property, are collateral."""

    t = _title()

    def act() -> None:
        write_note(engine, t)
        for n in range(20):
            engine.run(
                "INSERT INTO user_property (user_id, property, value) "
                "VALUES (2, %s, 'off')",
                f"auth.mfa_required.{n}",
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
    t = _title()

    def two() -> None:
        write_note(engine, t)
        engine.run(
            "INSERT INTO user_property (user_id, property, value) "
            "VALUES (2, 'second', 'x')"
        )

    _refuted(
        check(
            engine,
            note_contract(engine, ACTOR_EXCUSES),
            {"title": t, "body": "Body text"},
            two,
            tmp_path,
        )
    )


def test_an_excuse_covers_exactly_its_column_set(
    engine: Engine, tmp_path: Path
) -> None:
    """Learned: ``last_login`` and ``email`` moving together. A row moving
    ``last_login`` alone is a set never seen: collateral."""

    both = {**LOGIN, "columns": ["email", "last_login"]}
    block = learned_block(both, SEEN, FIRST_LOGIN)
    t = _title()
    _refuted(
        check(
            engine,
            note_contract(engine, block),
            {"title": t, "body": "Body text"},
            lambda: write_note(engine, t),
            tmp_path,
        )
    )


def test_the_written_row_is_never_learned_when_another_actor_also_wrote_it(
    engine: Engine, tmp_path: Path
) -> None:
    """Round 2's attack. Each learning write leaves the declared name as it
    was, and an app worker under another account reassigns the same asset.
    The row is the written row (and the actor owns it, so it would look like
    the actor's row): it is never learned, and a later reassignment of
    another asset the actor owns is refuted."""

    engine.run("ALTER TABLE asset ADD COLUMN modified_by INTEGER")
    engine.run("ALTER TABLE asset ADD COLUMN owner_id INTEGER")
    engine.run("UPDATE asset SET owner_id = 2, modified_by = 2")

    def contract(block: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        c = asset_contract(engine, block)
        c["frame"]["attribution"] = {
            "kind": "record_field",
            "actor_field": "modified_by",
            "self": ["2"],
            "server_set": ["modified_by"],
        }
        return c

    collector = learn.Collector(tmp_path / "ep.jsonl")
    for i in range(5):

        def act(i: int = i) -> None:
            request_bookkeeping(engine)
            engine.run("UPDATE asset SET name = 'Laptop A' WHERE tag = 'LT-100'")
            engine.run(
                "UPDATE asset SET assigned_to = %s, modified_by = 9 "
                "WHERE tag = 'LT-100'",
                5 + i,
            )

        result = check(
            engine, contract(), {"tag": "LT-100", "name": "Laptop A"}, act, tmp_path
        )
        moves = [m for m in result.evidence["frame"]["moves"] if m["table"] == "asset"]
        assert moves and all(m["target"] is True for m in moves)
        collector(
            _Ep(
                i + 1,
                "rename",
                Contract.from_dict(contract()),
                sent_fields={"tag", "name"},
            ),
            result,
        )
        engine.run("UPDATE asset SET modified_by = 2 WHERE tag = 'LT-100'")
    users = learn.users_from(
        "app_user",
        "id",
        None,
        [
            "asset.owner_id",
            "api_token.user_id",
            "api_token.issued_by",
            "user_property.user_id",
        ],
    )
    proposal = learn.propose(learn.read_episodes([collector.path]), users=users)
    kind = proposal["kinds"]["rename"]
    assert [e for e in kind["excuses"] if e["table"] == "asset"] == []
    assert any(
        s["table"] == "asset" and s["why"].startswith("the written row")
        for s in kind["stays_a_stop"]
    )
    path = tmp_path / "asset.yaml"
    path.write_text(yaml.safe_dump(contract()))
    learn.approve(proposal, by="R", contracts={"rename": [path]})
    approved = yaml.safe_load(path.read_text())
    engine.run("UPDATE asset SET modified_by = 7 WHERE id = 2")

    def wrong() -> None:
        request_bookkeeping(engine)
        engine.run(
            "UPDATE asset SET name = 'Laptop X', modified_by = 2 WHERE tag = 'LT-100'"
        )
        engine.run("UPDATE asset SET assigned_to = 3, modified_by = 2 WHERE id = 2")

    _refuted(
        check(engine, approved, {"tag": "LT-100", "name": "Laptop X"}, wrong, tmp_path)
    )


# -- the learner's rules (no database) -----------------------------------------------


def test_the_proposal_warns_on_every_actor_row_excuse(tmp_path: Path) -> None:
    eps = [
        _ep(
            "k",
            [
                _move("app_user", ["last_login"], actor_columns=["id"]),
                _move("api_token", ["last_seen"], actor_columns=["user_id"]),
            ],
        )
    ] * 5
    p = learn.propose(eps, users=USERS)
    md = learn.render_markdown(p)
    for e in p["kinds"]["k"]["excuses"]:
        line = next(x for x in md.splitlines() if x.startswith(f"- {e['id']}:"))
        assert "another name for the same column isn't caught" in line
        assert all(f"`{c}`" in line for c in e["columns"])


def test_a_column_the_contract_or_the_agent_names_is_never_learned() -> None:
    def eps(**kw: Any) -> list[dict[str, Any]]:
        return [
            _ep(
                "k",
                [
                    _move("api_token", ["last_seen"], actor_columns=["user_id"]),
                    _move("app_user", ["last_login"], actor_columns=["id"]),
                ],
                **kw,
            )
        ] * 5

    base = learn.propose(eps(), users=USERS)["kinds"]["k"]["excuses"]
    assert {e["table"] for e in base} == {"api_token", "app_user"}
    for kw in (
        {"contract_fields": ["last_seen", "last_login"]},
        {"sent_fields": ["Last_Seen", "user.last_login"]},
        {"param_names": ["last_seen", "last_login"]},
    ):
        k = learn.propose(eps(**kw), users=USERS)["kinds"]["k"]
        assert k["excuses"] == [], kw
        assert len(k["stays_a_stop"]) == 2
        assert all("agent's own requests" in s["why"] for s in k["stays_a_stop"])
    # Without recorded bodies, nothing is learned.
    k = learn.propose(eps(sent_fields=None), users=USERS)["kinds"]["k"]
    assert k["excuses"] == [] and k["request_bodies_recorded"] is False


def test_the_written_row_is_never_proposed() -> None:
    eps = [
        _ep(
            "k",
            [
                _move("note", ["stamp"], target=True, change="added"),
                _move("asset", ["modified_at"], target=True),
            ],
        )
    ] * 6
    k = learn.propose(eps, users=USERS)["kinds"]["k"]
    assert k["excuses"] == []
    assert len(k["stays_a_stop"]) == 2
    assert all(s["why"].startswith("the written row") for s in k["stays_a_stop"])


def test_approve_writes_every_file_or_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    e = Engine(
        "sqlite", {"sqlite_database": str(tmp_path / "x.sqlite")}, lambda *a: None
    )
    files = []
    for name in ("a.yaml", "b.yaml", "c.yaml"):
        f = tmp_path / name
        f.write_text(yaml.safe_dump(note_contract(e)))
        files.append(f)
    before = [f.read_text() for f in files]
    eps = [_ep("k", [_move("app_user", ["last_login"], actor_columns=["id"])])] * 5
    p = learn.propose(eps, users=USERS)
    real = os.replace
    calls = []

    def flaky(src: Any, dst: Any) -> None:
        calls.append(dst)
        if len(calls) == 2:
            raise OSError("disk full")
        real(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    with pytest.raises(learn.LearnError, match="nothing was written"):
        learn.approve(p, by="R", contracts={"k": files})
    assert [f.read_text() for f in files] == before
    assert not list(tmp_path.glob(".*.tmp"))


def _ep(kind: str, moves: list[dict[str, Any]], **kw: Any) -> dict[str, Any]:
    return {
        "schema_version": learn.EPISODE_SCHEMA,
        "kind": kind,
        "mode": "sql",
        "outcome": kw.get("outcome", "REFUTED"),
        "delta": kw.get("delta", "held"),
        "frame": kw.get("frame", "refuted"),
        "moves": moves,
        "sent_fields": kw.get("sent_fields", []),
        "param_names": kw.get("param_names", []),
        "contract_fields": kw.get("contract_fields", []),
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
                else [_move("api_token", ["last_seen"], actor_columns=["user_id"])],
            )
            for i in range(total)
        ]

    assert learn.propose(eps(4, 4), users=USERS)["kinds"]["k"]["excuses"] == []
    assert len(learn.propose(eps(3, 5), users=USERS)["kinds"]["k"]["excuses"]) == 1
    tables = [
        e["table"]
        for e in learn.propose(eps(2, 5), users=USERS)["kinds"]["k"]["excuses"]
    ]
    assert tables == ["api_token"]
    # 10 episodes need 6.
    tables = [
        e["table"]
        for e in learn.propose(eps(5, 10), users=USERS)["kinds"]["k"]["excuses"]
    ]
    assert "app_user" not in tables


def test_an_excuse_is_one_column_set_seen_together_never_a_union() -> None:
    """``a`` with ``b`` 3 times, ``a`` alone twice, ``b`` alone twice: each
    column passes 3 of 5 on its own (and more), but only the set seen
    together 3 times is proposed, as itself."""

    sets = [["a", "b"]] * 3 + [["a"]] * 2 + [["b"]] * 2
    eps = [_ep("k", [_move("app_user", cols, actor_columns=["id"])]) for cols in sets]
    p = learn.propose(eps, users=USERS)["kinds"]["k"]
    # 7 learnable: 3 of 5 -> 5 needed; no set reaches it.
    assert p["excuses"] == []
    eps = [
        _ep("k", [_move("app_user", ["a"] if i % 2 else ["b"], actor_columns=["id"])])
        for i in range(6)
    ]
    # a seen 3 of 6, b 3 of 6: 4 needed, and never together.
    assert learn.propose(eps, users=USERS)["kinds"]["k"]["excuses"] == []
    eps = [_ep("k", [_move("app_user", ["a", "b"], actor_columns=["id"])])] * 5
    (e,) = learn.propose(eps, users=USERS)["kinds"]["k"]["excuses"]
    assert e["columns"] == ["a", "b"]


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


def test_without_users_nothing_is_excused() -> None:
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
    assert k["excuses"] == []


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
                _move("api_token", ["last_seen"], actor_columns=["user_id"]),
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
    learn.approve(p, by="R", contracts={"k": [good]}, reject=[ids["api_token"]])
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
        ({**LOGIN, "actor_columns": None}, "names its actor_columns"),
        (STAMP, "can't cover the written row"),
        (TOUCHED, "can't cover the written row"),
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
        "user_id",
        "value",
    ]


def test_manifest_contracts_include_every_variant_file(tmp_path: Path) -> None:
    (tmp_path / "manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "kinds": {
                    "fix": {
                        "variants": [
                            {
                                "fields": ["a"],
                                "update": "c/a.update.yaml",
                                "found": "c/a.found.yaml",
                            },
                        ]
                    },
                    "add": {"variants": [{"create": "c/b.create.yaml"}]},
                }
            }
        )
    )
    got = learn.contracts_from_manifest(tmp_path / "manifest.yaml")
    base = tmp_path.resolve()
    assert {k: set(v) for k, v in got.items()} == {
        "fix": {base / "c/a.update.yaml", base / "c/a.found.yaml"},
        "add": {base / "c/b.create.yaml"},
    }


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

            def _email(self, con: Any) -> None:
                # A query parameter the app stores on the acting user's row.
                from urllib.parse import parse_qs, urlsplit

                q = parse_qs(urlsplit(self.path).query)
                if "email" in q:
                    con.execute(
                        "UPDATE app_user SET email = ? WHERE id = 2", (q["email"][0],)
                    )

            def do_GET(self) -> None:
                con = sqlite3.connect(app.db)
                self._email(con)
                con.commit()
                con.close()
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def do_POST(self) -> None:
                doc = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                con = sqlite3.connect(app.db)
                con.execute(
                    "UPDATE app_user SET last_login = ? WHERE id = 2",
                    (secrets.token_hex(4),),
                )
                self._email(con)
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
    learn.record_bodies(sh)
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
    assert all(ep["sent_fields"] == ["body", "title"] for ep in eps)
    users = learn.users_from("app_user", "id", None)
    p = learn.propose(eps, users=users)
    got = sorted(
        (x["table"], x["relation"]) for x in p["kinds"]["create_note"]["excuses"]
    )
    # The note's stamp, set by the app on the written row, stays a stop.
    assert got == [("app_user", "actor_row")]
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


@pytest.mark.parametrize("how", ["write", "read"])
def test_query_string_fields_count_as_sent(tmp_path: Path, how: str) -> None:
    """Round 2: the agent sends ``email`` in a query string (on the write
    itself, or on a read inside the write's window) and the app stores it
    on the actor's own row. ``email`` counts as sent, so the actor's row
    moving ``email`` with ``last_login`` is never learned."""

    import http.client

    from openadapt_flow.shadow import Route, Shadow

    e = _sqlite(tmp_path)
    for statement in e.ddl():
        e.run(statement)
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
    learn.record_bodies(sh)
    sh.on_checked = collector
    try:
        host, port = sh.server.server_address[:2]
        for i in range(5):
            query = f"?email=bot{i}%40example.test"
            conn = http.client.HTTPConnection(host, port)
            conn.request(
                "POST",
                "/api/notes" + (query if how == "write" else ""),
                json.dumps({"title": f"Note {i}", "body": "B"}),
                {"Content-Type": "application/json"},
            )
            assert conn.getresponse().status == 201
            conn.close()
            if how == "read":
                conn = http.client.HTTPConnection(host, port)
                conn.request("GET", "/api/me" + query)
                assert conn.getresponse().status == 200
                conn.close()
            sh.flush()
    finally:
        sh.stop()
    eps = learn.read_episodes([collector.path])
    assert len(eps) == 5 and all("email" in ep["sent_fields"] for ep in eps)
    p = learn.propose(eps, users=learn.users_from("app_user", "id", None))
    k = p["kinds"]["create_note"]
    assert [x for x in k["excuses"] if x["table"] == "app_user"] == []
    assert any(
        s["table"] == "app_user" and "email" in s["why"] for s in k["stays_a_stop"]
    )
