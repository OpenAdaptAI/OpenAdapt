"""A minimal DB-API connection to a fixture's MariaDB through ``docker exec``.

The OpenEMR fixture publishes no database port. This lets a ``kind: sql``
verifier (``sql_driver: verification_coverage.exec_mariadb``) read it as a
SELECT-only user, the way a deployment would read through its own driver.

One connection is one ``mariadb`` client session, kept open for the
connection's life, so a transaction spans statements (the whole-database
snapshot reads everything inside one ``START TRANSACTION WITH CONSISTENT
SNAPSHOT``). After each statement a marker query ends its output. The client
runs with ``--force``, so a failed statement (an optional ``SET``) raises in
Python without ending the session.

Benchmark plumbing only: no query parameters, rows as text (``NULL`` is None).

Copied from ``verification_coverage/exec_mariadb.py`` on
``measure/phase0-independent`` (40dfb075c), with two changes for this
experiment: it runs ``podman exec`` (the benchmark machine), and it reads
output as Latin-1, so a binary cell can't stop the decode.
"""

from __future__ import annotations

import os
import queue
import subprocess
import threading
import time
from typing import Any, Optional

_MARK = "__oa_end__"


class Error(Exception):
    pass


class _Cursor:
    def __init__(self, conn: "Connection") -> None:
        self._conn = conn
        self.description: Optional[list[tuple[str, ...]]] = None
        self._rows: list[tuple[Any, ...]] = []

    def execute(self, sql: str, params: Any = None) -> None:
        if params:
            raise ValueError("exec_mariadb takes no query parameters")
        lines = self._conn._run(sql)
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


class Connection:
    def __init__(self, container: str, user: str, password: str, database: str):
        self._proc = subprocess.Popen(
            [
                os.environ.get("INCREMENTAL_VALUE_CONTAINER_CLI", "podman"),
                "exec",
                "-i",
                "-e",
                "MYSQL_PWD",
                container,
                "mariadb",
                "-u",
                user,
                "--batch",
                "--raw",
                "--unbuffered",
                "--force",
                database,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="latin-1",
            bufsize=1,
            env={**os.environ, "MYSQL_PWD": password},
        )
        self._errors: "queue.Queue[str]" = queue.Queue()
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self) -> None:
        assert self._proc.stderr is not None
        for line in self._proc.stderr:
            self._errors.put(line.strip())

    def _run(self, sql: str, timeout_s: float = 300.0) -> list[str]:
        assert self._proc.stdin is not None and self._proc.stdout is not None
        statement = sql.strip().rstrip(";")
        self._proc.stdin.write(f"{statement};\nSELECT '{_MARK}' AS {_MARK};\n")
        self._proc.stdin.flush()
        out: list[str] = []
        done = threading.Event()

        def read() -> None:
            assert self._proc.stdout is not None
            for line in self._proc.stdout:
                line = line.rstrip("\n")
                if line == _MARK and out and out[-1] == _MARK:
                    out.pop()  # the marker's header line
                    break
                out.append(line)
            done.set()

        threading.Thread(target=read, daemon=True).start()
        if not done.wait(timeout_s):
            self.close()
            raise Error(f"no answer in {timeout_s:.0f} s")
        time.sleep(0.02)  # stderr is its own pipe: let a statement's error land
        errors = []
        while not self._errors.empty():
            errors.append(self._errors.get())
        if self._proc.poll() is not None:
            raise Error("the mariadb session ended: " + " ".join(errors))
        if errors:
            raise Error(" ".join(errors))
        return out

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def close(self) -> None:
        try:
            if self._proc.stdin is not None and not self._proc.stdin.closed:
                self._proc.stdin.close()
            self._proc.wait(timeout=10)
        except Exception:  # noqa: BLE001 - closing best effort
            self._proc.kill()


def connect(
    container: str, user: str, password: str = "", database: str = "openemr"
) -> Connection:
    return Connection(container, user, password, database)
