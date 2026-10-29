"""A skill's shipped SQL read-back is one parameterized, read-only SELECT.

A public registry bundle may ship a minimal SQL record check (the typed
fields, keyed by the identity field, on the app's documented schema). The
rule (``skills.shipped_sql_problems``) holds at ``skill qualify`` and when the
index is built, so a bundle whose query could write, call out or splice text
never ships.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from openadapt_flow import skills

ROOT = Path(__file__).resolve().parents[1]

ODOO_SELECT = (
    "SELECT id::text AS id, name, email, phone, street, city, zip "
    "FROM res_partner WHERE email = %(email)s AND active"
)
PARAMS = {"email": {"param": "email"}}


def test_the_minimal_select_passes():
    assert skills.shipped_sql_problems(ODOO_SELECT, PARAMS) == []
    assert skills.shipped_sql_problems(ODOO_SELECT + ";", PARAMS) == []


@pytest.mark.parametrize(
    "query, message",
    [
        (ODOO_SELECT + "; DELETE FROM res_partner", "single statement"),
        (
            "UPDATE res_partner SET active = false WHERE email = %(email)s",
            "start with SELECT",
        ),
        ("SELECT * INTO stolen FROM res_partner WHERE email = %(email)s", "into"),
        ("SELECT pg_read_file('/etc/passwd')", "literals"),
        ("SELECT pg_sleep(9) FROM res_partner WHERE email = %(email)s", "pg_sleep"),
        ("SELECT name FROM res_partner WHERE email = '" + "x@y.z'", "literals"),
        (
            "SELECT name FROM res_partner WHERE email = ${EMAIL}",
            "literals or \\$\\{\\}",
        ),
        ("SELECT name FROM res_partner -- WHERE email = %(email)s", "comments"),
        ("SELECT name FROM res_partner WHERE email = %(other)s", "has no value"),
        ("SELECT name FROM res_partner WHERE email = %s", r"only as %\(name\)s"),
        ("SELECT name FROM res_partner FOR UPDATE", "update"),
    ],
)
def test_a_query_that_is_not_one_read_only_select_is_refused(query, message):
    problems = skills.shipped_sql_problems(query, PARAMS)
    assert any(__import__("re").search(message, p) for p in problems), problems


ECHO_PARAMS = {"email": {"param": "email"}, "name": {"param": "name"}}


@pytest.mark.parametrize(
    "query, message",
    [
        # Writes a sequence: setval and nextval are not the word "set".
        (
            "SELECT setval(%(name)s::regclass, 1) AS id, "
            "nextval(%(email)s::regclass) AS email",
            "plain column names",
        ),
        # Changes a session setting.
        ("SELECT set_config(%(name)s, %(email)s, false) AS id", "plain column names"),
        # Runs SQL taken from a typed input.
        (
            "SELECT query_to_xml(%(name)s, true, true, %(email)s)::text AS id",
            "plain column names",
        ),
        # Echoes the inputs as the stored fields: any write would match.
        (
            "SELECT id::text AS id, %(name)s AS name, %(email)s AS email "
            "FROM res_partner WHERE id = (SELECT max(id) FROM res_partner)",
            "plain column names",
        ),
        (
            "SELECT name FROM res_partner, query_to_xml(%(name)s, true, true, "
            "%(email)s) WHERE email = %(email)s",
            "only after WHERE",
        ),
        (
            "SELECT name FROM lower(%(name)s) WHERE email = %(email)s",
            "not functions",
        ),
        ("SELECT name, email", "SELECT <columns> FROM <table>"),
    ],
)
def test_a_query_whose_effect_is_not_a_plain_read_is_refused(query, message):
    problems = skills.shipped_sql_problems(query, ECHO_PARAMS)
    assert any(__import__("re").search(message, p) for p in problems), problems


def test_the_read_back_runs_on_a_read_only_connection(tmp_path):
    """Whatever the query says, the driver refuses a write from the read-back."""

    import sqlite3

    from openadapt_flow.deployment import EffectsConfig, build_effect_verifier

    db = tmp_path / "sor.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, who TEXT)")
    conn.commit()
    conn.close()
    verifier = build_effect_verifier(
        EffectsConfig(
            kind="sql", sql_query="SELECT id, who FROM t", sqlite_database=str(db)
        )
    )
    read_back = verifier._connect()
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        read_back.execute("INSERT INTO t (who) VALUES ('x')")
    read_back.close()


def test_every_shipped_driver_has_a_read_only_session():
    from openadapt_flow.runtime.effects.sql import (
        READ_ONLY_DRIVERS,
        read_only_connection,
    )

    assert skills.SHIPPED_SQL_DRIVERS <= READ_ONLY_DRIVERS
    with pytest.raises(ValueError, match="no driver-level read-only"):
        read_only_connection(object(), "pymssql")


@pytest.mark.parametrize(
    "driver, expected",
    [
        ("psycopg2", [("set_session", {"readonly": True})]),
        ("pymysql", [("execute", "SET SESSION TRANSACTION READ ONLY")]),
    ],
)
def test_the_driver_is_asked_for_a_read_only_session(driver, expected):
    from openadapt_flow.runtime.effects.sql import read_only_connection

    calls = []

    class Cursor:
        def execute(self, sql):
            calls.append(("execute", sql))

        def close(self):
            pass

    class Conn:
        def set_session(self, **kw):
            calls.append(("set_session", kw))

        def cursor(self):
            return Cursor()

    read_only_connection(Conn(), driver)
    assert calls == expected


def test_a_bound_value_must_be_a_typed_input():
    problems = skills.shipped_sql_problems(ODOO_SELECT, {"email": "x@y.z"})
    assert any("must be {param: ...}" in p for p in problems)


def _odoo_contract(query: str = ODOO_SELECT) -> dict:
    return {
        "schema_version": "openadapt.verify/v1",
        "app": {
            "target": "web",
            "name": "Odoo",
            "version": "18.0",
            "url": "${APP_URL}",
            "storage_state": "${SIGNED_IN_STATE}",
            "reset": {"command": "${RESET_COMMAND}"},
        },
        "verifier": {
            "kind": "sql",
            "sql_query": query,
            "sql_query_params": PARAMS,
            "sql_driver": "psycopg2",
            "sql_connect_args": {
                "host": "${DB_HOST}",
                "port": "${DB_PORT}",
                "user": "${DB_USER}",
                "dbname": "${DB_NAME}",
            },
            "sql_password_env": "${DB_PASSWORD_VAR}",
        },
        "record": {"name": "contact", "match": ["email"], "fields": {"email": "email"}},
    }


KINDS = {
    "APP_URL": "url",
    "SIGNED_IN_STATE": "path",
    "RESET_COMMAND": "command",
    "DB_HOST": "url",
    "DB_PORT": "command",
    "DB_USER": "command",
    "DB_NAME": "command",
    "DB_PASSWORD_VAR": "env_var_name",
}


def test_a_shipped_sql_contract_is_checked_as_a_whole():
    assert skills.shipped_contract_problems(_odoo_contract(), KINDS) == []
    bad = _odoo_contract(ODOO_SELECT + "; DROP TABLE res_partner")
    assert any(
        "single statement" in p for p in skills.shipped_contract_problems(bad, KINDS)
    )


def test_the_index_build_refuses_a_bundle_whose_sql_could_write(tmp_path, monkeypatch):
    import runpy

    root = tmp_path / "flow"
    shutil.copytree(ROOT / "registry", root / "registry")
    bundle = root / "registry/bundles/openemr/new-referral"
    (bundle / "verify.yaml").write_text(
        yaml.safe_dump(
            _odoo_contract("DELETE FROM res_partner WHERE email = %(email)s")
        )
    )
    sources = json.loads((root / "registry/sources.json").read_text())
    for item in sources["workflows"]:
        if item["id"] == "openemr/new-referral":
            item.update(
                contract="verify.yaml",
                contract_vars=[
                    {"name": name, "kind": kind} for name, kind in KINDS.items()
                ],
            )
    (root / "registry/sources.json").write_text(json.dumps(sources))
    script = runpy.run_path(str(ROOT / "scripts/build_registry_index.py"))
    monkeypatch.setitem(script["build"].__globals__, "ROOT", root)
    with pytest.raises(SystemExit, match="start with SELECT"):
        script["build"]()
