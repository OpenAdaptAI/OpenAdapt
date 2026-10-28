"""A minimal DB-API connection to a fixture's MariaDB through ``docker exec``.

The OpenEMR fixture publishes no database port. This lets the rollback's
whole-database reader (``rollback.database.read.sql_driver``) read it as a
SELECT-only user, the way a deployment reads through its own driver.
Benchmark plumbing only: one statement, no parameters, rows as text. Adapted
from ``benchmark/lifecycle_proof/readonly_db.py`` (measure/phase0-independent).
"""

from __future__ import annotations

import os
import subprocess
from typing import Any, Optional


class _Cursor:
    def __init__(self, conn: "Connection") -> None:
        self._conn = conn
        self.description: Optional[list[tuple[str, ...]]] = None
        self._rows: list[tuple[Any, ...]] = []

    def execute(self, sql: str, params: Any = None) -> None:
        if params:
            raise ValueError("exec_mariadb takes no query parameters")
        out = subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                "-e",
                "MYSQL_PWD",
                self._conn.container,
                "mariadb",
                "-u",
                self._conn.user,
                "--batch",
                "--raw",
                self._conn.database,
                "-e",
                sql,
            ],
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, "MYSQL_PWD": self._conn.password},
            check=True,
        ).stdout
        lines = out.splitlines()
        if not lines:
            self.description, self._rows = [], []
            return
        header = lines[0].split("\t")
        self.description = [(name,) for name in header]
        self._rows = [
            tuple(None if cell == "NULL" else cell for cell in line.split("\t"))
            for line in lines[1:]
        ]

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)

    def close(self) -> None:
        return None


class Connection:
    def __init__(self, container: str, user: str, password: str, database: str):
        self.container = container
        self.user = user
        self.password = password
        self.database = database

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def close(self) -> None:
        return None


def connect(
    container: str, user: str, password: str = "", database: str = "openemr"
) -> Connection:
    return Connection(container, user, password, database)
