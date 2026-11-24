"""The app's record table, read and changed through its own database.

The grader never looks through the agent's browser: it reads the table with
the database's own client, inside the database container (``docker exec``).
``--docker`` replaces the ``docker`` command, for example with one that runs
it on another host over SSH, so the harness can drive a stack it did not
start on this machine.
"""

from __future__ import annotations

import http.client
import json
import re
import shlex
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from openadapt_flow.agent_test.packs import AppPack


class Docker:
    """``docker`` (or a replacement command line) with the stack's project name."""

    def __init__(self, command: str = "docker", project: str = "oatest") -> None:
        self.argv = shlex.split(command) if command else ["docker"]
        self.project = project

    def run(
        self,
        *args: str,
        input: Optional[str] = None,
        timeout: float = 300,
        check: bool = True,
    ) -> str:
        r = subprocess.run(
            [*self.argv, *args],
            input=input,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
        if check and r.returncode != 0:
            raise RuntimeError(
                f"docker {' '.join(args[:3])} failed ({r.returncode}): {r.stderr.strip()[-600:]}"
            )
        return r.stdout

    def container(self, service: str) -> str:
        return f"{self.project}-{service}"


def sql_str(v: Any) -> str:
    if v is None:
        return "NULL"
    return "'" + str(v).replace("\\", "\\\\").replace("'", "''") + "'"


class Store:
    """One app's record table (MariaDB), as the pack describes it."""

    def __init__(self, app: AppPack, docker: Docker) -> None:
        self.app = app
        self.docker = docker
        s = app.store
        self.table = s["table"]
        self.key = s["key"]
        self.key_text = s.get("key_kind") == "text"
        self.present_where = s.get("present_where", "")
        self.identity = list(s["identity"])
        self.container = docker.container(s["service"])

    def _client(self, sql: str, *, json_rows: bool) -> str:
        s = self.app.store
        args = [
            "exec",
            "-i",
            self.container,
            "mariadb",
            # Binary columns (a binary uuid, say) as hex, so dumps stay text.
            "--binary-as-hex",
            f"-u{s['user']}",
            f"-p{s['password']}",
        ]
        if json_rows:
            args += ["-N", "-B", "--raw"]
        args.append(s["database"])
        last: Exception = RuntimeError("no attempt")
        for _ in range(3):
            try:
                return self.docker.run(*args, input=sql, timeout=120)
            except (RuntimeError, subprocess.TimeoutExpired) as exc:
                last = exc
                time.sleep(2)
        raise last

    def exec(self, sql: str) -> None:
        self._client(sql, json_rows=False)

    def query(self, sql: str) -> list[dict[str, Any]]:
        out = self._client(sql, json_rows=True)
        return [json.loads(ln) for ln in out.splitlines() if ln.strip().startswith("{")]

    def dump_raw(self) -> str:
        """The whole table, every column, as the client's tab-separated text (for hand checks)."""

        return self._client(
            f"SELECT * FROM {self.table} ORDER BY {self.key}", json_rows=False
        )

    # -- the table ------------------------------------------------------------------
    def rows(self) -> list[dict[str, Any]]:
        """Every live row: the key and every judged column, as text."""

        parts = [f"'__key', {self.key}"]
        for f in self.app.fields.values():
            parts.append(f"'{f.name}', {f.column}")
        where = f" WHERE {self.present_where}" if self.present_where else ""
        q = f"SELECT JSON_OBJECT({', '.join(parts)}) FROM {self.table}{where} ORDER BY {self.key}"
        rows = self.query(q)
        for r in rows:
            r["__key"] = str(r["__key"])
        return rows

    def _key_sql(self, key: str) -> str:
        if self.key_text:
            return sql_str(key)
        if not re.fullmatch(r"-?\d+", str(key)):
            raise ValueError(f"unexpected key {key!r}")
        return str(int(key))

    def _set_sql(self, values: dict[str, Any]) -> str:
        cols = []
        for name, v in values.items():
            cols.append(f"{self.app.fields[name].column} = {sql_str(v)}")
        return ", ".join(cols)

    def update_sql(self, key: str, values: dict[str, Any]) -> str:
        return f"UPDATE {self.table} SET {self._set_sql(values)} WHERE {self.key} = {self._key_sql(key)};"

    def delete_sql(self, key: str) -> str:
        return f"DELETE FROM {self.table} WHERE {self.key} = {self._key_sql(key)};"

    def insert_sql(self, values: dict[str, Any]) -> str:
        quoted = {k: sql_str(v) for k, v in values.items()}
        for name in self.app.fields:
            quoted.setdefault(name, "''")
        sql = re.sub(
            r"\{([A-Za-z_][A-Za-z0-9_]*)\}",
            lambda m: quoted[m.group(1)] if m.group(1) in quoted else m.group(0),
            self.app.store["insert_sql"],
        )
        return sql.rstrip().rstrip(";") + ";"

    def update(self, key: str, values: dict[str, Any]) -> None:
        self.exec(self.update_sql(key, values))

    def delete(self, key: str) -> None:
        self.exec(self.delete_sql(key))

    def insert(self, values: dict[str, Any]) -> None:
        self.exec(self.insert_sql(values))

    def ident(self, row: dict[str, Any]) -> tuple[str, ...]:
        return tuple(str(row.get(i, "")).strip().casefold() for i in self.identity)

    def find(
        self, target: dict[str, Any], rows: Optional[list[dict[str, Any]]] = None
    ) -> list[str]:
        """Keys of the rows whose identity fields match ``target``."""

        want = self.ident(target)
        return [
            r["__key"]
            for r in (rows if rows is not None else self.rows())
            if self.ident(r) == want
        ]

    def reset(self, seed: list[dict[str, Any]]) -> None:
        """Every row not in the seed removed; the seed rows back to their values.

        One client call: the statements run in one batch.
        """

        stmts: list[str] = []
        if self.present_where:
            # A soft-deleted seed row is live again before the table is read.
            m = re.fullmatch(r"\s*([A-Za-z_]+)\s*=\s*0\s*", self.present_where)
            if m and seed:
                ids = " OR ".join(
                    "("
                    + " AND ".join(
                        f"{self.app.fields[i].column} = {sql_str(p.get(i, ''))}"
                        for i in self.identity
                    )
                    + ")"
                    for p in seed
                )
                self.exec(f"UPDATE {self.table} SET {m.group(1)} = 0 WHERE {ids};")
        keep = {self.ident(p) for p in seed}
        rows = self.rows()
        have: dict[tuple[str, ...], list[str]] = {}
        for r in rows:
            if self.ident(r) not in keep:
                stmts.append(self.delete_sql(r["__key"]))
            else:
                have.setdefault(self.ident(r), []).append(r["__key"])
        for p in seed:
            keys = have.get(self.ident(p), [])
            stmts.extend(self.delete_sql(extra) for extra in keys[1:])
            values = {k: v for k, v in p.items() if k in self.app.fields}
            stmts.append(
                self.update_sql(keys[0], values) if keys else self.insert_sql(values)
            )
        if stmts:
            self.exec("\n".join(stmts))


def wait_http(url: str, timeout_s: float = 900) -> None:
    deadline = time.time() + timeout_s
    last = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=10) as r:  # noqa: S310 - the local stack
                if r.status < 500:
                    return
        except (urllib.error.URLError, http.client.HTTPException, OSError, TimeoutError) as exc:
            last = str(exc)
        time.sleep(5)
    raise RuntimeError(f"{url} did not answer within {timeout_s:.0f} s ({last})")
