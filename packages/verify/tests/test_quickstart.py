"""The quickstart's three outcomes on every read path, through the CLI.

PostgreSQL and MySQL run when a disposable database is configured:
OPENADAPT_VERIFY_TEST_POSTGRES / OPENADAPT_VERIFY_TEST_MYSQL hold the driver's
JSON connect arguments and OPENADAPT_QUICKSTART_DB_PASSWORD the password. CI's
clean-install job starts both as service containers.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from openadapt_verify import quickstart

EXPECTED = [
    ("correct", "CONFIRMED"),
    ("wrong value", "REFUTED"),
    ("claimed, not saved", "UNCERTAIN"),
]


def _database(name: str) -> dict:
    raw = os.environ.get(f"OPENADAPT_VERIFY_TEST_{name.upper()}")
    if not raw:
        pytest.skip(f"no disposable {name} database configured")
    return json.loads(raw)


def _cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "openadapt_verify", "quickstart", "--json", *args],
        capture_output=True,
        text=True,
        timeout=600,
    )


def _outcomes(rows: list[dict], path: str) -> list[tuple[str, str]]:
    return [(r["scenario"], r["outcome"]) for r in rows if r["path"] == path]


def test_cli_quickstart_rest_sql_fhir():
    run = _cli()
    assert run.returncode == 0, run.stderr + run.stdout
    rows = json.loads(run.stdout)
    for path in ("rest", "sql/sqlite", "fhir"):
        assert _outcomes(rows, path) == EXPECTED, path
    assert all(r["receipt_checked"] for r in rows)


@pytest.mark.parametrize("dialect", ["postgres", "mysql"])
def test_cli_quickstart_server_databases(dialect):
    args = json.dumps(_database(dialect))
    run = _cli("--path", "sqlite", f"--{dialect}", args)
    assert run.returncode == 0, run.stderr + run.stdout
    rows = json.loads(run.stdout)
    name = "sql/postgresql" if dialect == "postgres" else "sql/mysql"
    assert _outcomes(rows, name) == EXPECTED
    assert all(r["receipt_checked"] for r in rows)


def test_mysql_non_utf8mb4_result_charset_is_never_sent():
    """The #2206/#2211/#2215 charset guard, reached through the slim install."""

    connect = {**_database("mysql"), "charset": "latin1"}
    rows = quickstart.run((), mysql=connect)
    assert [r.outcome for r in rows] == ["NOT SENT"] * 3
    assert not any(r.ok for r in rows)


def test_a_failed_quickstart_exits_nonzero(monkeypatch):
    monkeypatch.setattr(quickstart, "SCENARIOS", (("claimed, not saved", "CONFIRMED"),))
    from openadapt_verify.cli import main

    assert main(["quickstart", "--path", "sqlite"]) == 1
