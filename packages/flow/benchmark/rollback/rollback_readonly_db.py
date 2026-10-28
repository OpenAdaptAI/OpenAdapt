"""A DB-API connection to a fixture's MariaDB through one ``docker exec`` session.

The OpenEMR fixture publishes no database port. The rollback's whole-database
read (``runtime.effects.db_snapshot.take_snapshot``) runs every statement of a
snapshot inside one read-only transaction on one connection, so this keeps a
single ``mariadb`` process open in the DB container and feeds it statement by
statement; after each, a sentinel ``SELECT`` marks where its output ends. A
statement that fails ends the session (no ``--force``), and the read raises:
fail-closed. Same design as the RL grader's reader, so the two read the
database the same way.

Benchmark plumbing only, for a test copy: it reads as MariaDB root (so
triggers, routines and events are visible to the snapshot's code digest), and
the snapshot itself opens ``START TRANSACTION ... READ ONLY``.
"""

from __future__ import annotations

import subprocess
from typing import Any, Optional

_END = "__OA_ROLLBACK_STATEMENT_END__"


class Connection:
    """One ``mariadb`` session in the DB container."""

    def __init__(self, container: str, database: str = "openemr") -> None:
        self._proc = subprocess.Popen(
            [
                "docker",
                "exec",
                "-i",
                container,
                "sh",
                "-c",
                'MYSQL_PWD="$MARIADB_ROOT_PASSWORD" exec mariadb -uroot -n --batch '
                f"--raw --skip-column-names {database}",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

    def run(self, sql: str) -> list[tuple[Optional[str], ...]]:
        assert self._proc.stdin is not None and self._proc.stdout is not None
        self._proc.stdin.write(sql.rstrip().rstrip(";") + f";\nSELECT '{_END}';\n")
        self._proc.stdin.flush()
        rows: list[tuple[Optional[str], ...]] = []
        while True:
            line = self._proc.stdout.readline()
            if line == "":
                err = self._proc.stderr.read() if self._proc.stderr else ""
                raise RuntimeError(f"database read failed: {err[-400:]}")
            line = line.rstrip("\n")
            if line == _END:
                return rows
            rows.append(tuple(None if c == "NULL" else c for c in line.split("\t")))

    def cursor(self) -> "_Cursor":
        return _Cursor(self)

    def close(self) -> None:
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
            self._proc.wait(timeout=30)
        except Exception:  # noqa: BLE001
            self._proc.kill()


class _Cursor:
    def __init__(self, con: Connection) -> None:
        self._con = con
        self._rows: list[tuple[Optional[str], ...]] = []
        #: Not known from ``--skip-column-names`` output; the snapshot names
        #: its columns itself, and a caller that needs names selects them.
        self.description: Optional[list[tuple[str, ...]]] = None

    def execute(self, sql: str, params: Any = None) -> None:
        if params:
            raise NotImplementedError("this reader sends literal SQL only")
        self._rows = self._con.run(sql)

    def fetchall(self) -> list[tuple[Optional[str], ...]]:
        return list(self._rows)

    def close(self) -> None:
        return None


def connect(container: str, database: str = "openemr") -> Connection:
    return Connection(container, database)


class OneShotConnection:
    """One ``mariadb -e`` per statement, with column names: for an effect
    verifier's single ``SELECT`` (it needs ``cursor.description``)."""

    def __init__(self, container: str, database: str = "openemr") -> None:
        self.container = container
        self.database = database

    def cursor(self) -> "_OneShotCursor":
        return _OneShotCursor(self)

    def close(self) -> None:
        return None


class _OneShotCursor:
    def __init__(self, con: OneShotConnection) -> None:
        self._con = con
        self.description: Optional[list[tuple[str, ...]]] = None
        self._rows: list[tuple[Optional[str], ...]] = []

    def execute(self, sql: str, params: Any = None) -> None:
        if params:
            raise NotImplementedError("this reader sends literal SQL only")
        out = subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                self._con.container,
                "sh",
                "-c",
                'MYSQL_PWD="$MARIADB_ROOT_PASSWORD" exec mariadb -uroot --batch '
                f"--raw {self._con.database}",
            ],
            input=sql,
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        ).stdout
        lines = out.splitlines()
        self.description = [(n,) for n in lines[0].split("\t")] if lines else []
        self._rows = [
            tuple(None if c == "NULL" else c for c in line.split("\t"))
            for line in lines[1:]
        ]

    def fetchall(self) -> list[tuple[Optional[str], ...]]:
        return list(self._rows)

    def close(self) -> None:
        return None
