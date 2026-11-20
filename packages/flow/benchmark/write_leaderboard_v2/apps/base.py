"""What each app gives the harness, and the end-state oracle every app shares.

An app adapter knows how to start the app in local containers, sign the
agent's user in, put the case's seed records in place, read the record
table back through the app's own database (never through the agent's
browser), and change another record for the collateral fault.

The oracle (``judge``) reads the whole record table before and after the
run and decides from that alone. A run's end state is ``correct`` only when
exactly one new record exists, it holds every value the task names
(after the normalisation below), and no other record was changed or
deleted. A row the app has deleted softly (EspoCRM's ``deleted = 1``)
counts as not present.

Normalisation (pre-registered): every value is trimmed and inner runs of
whitespace collapse to one space; text and email compare without regard
to case (OpenEMR stores addresses in capitals); a phone keeps only its digits; a date is compared as YYYY-MM-DD; a number is compared as
a number. Nothing else is forgiven.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.request
from typing import Any, Optional

from benchmark.write_leaderboard_v2.proxy import Save


def sh(args: list[str], *, input: Optional[str] = None, timeout: float = 180, check: bool = True) -> str:
    r = subprocess.run(args, input=input, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:5])}... failed ({r.returncode}): {r.stderr[-800:]}")
    return r.stdout


def podman(*args: str, **kw: Any) -> str:
    return sh(["podman", *args], **kw)


def mysql_rows(container: str, password: str, db: str, query: str) -> list[dict[str, Any]]:
    """Rows from MariaDB through the container's own client, as JSON (one query)."""

    out = podman("exec", "-i", container, "mariadb", "-uroot", f"-p{password}", "-N", "-B", "--raw", db, input=query)
    return [json.loads(ln) for ln in out.splitlines() if ln.strip()]


def mysql_exec(container: str, password: str, db: str, sql: str) -> None:
    podman("exec", "-i", container, "mariadb", "-uroot", f"-p{password}", db, input=sql)


def json_select(columns: list[str], frm: str) -> str:
    """A SELECT that returns each row as one JSON object (MariaDB)."""

    obj = ", ".join(f"'{c.split(' AS ')[-1].strip()}', {c.split(' AS ')[0]}" for c in columns)
    return f"SELECT JSON_OBJECT({obj}) FROM {frm}"


def sql_str(v: Any) -> str:
    return "'" + str(v).replace("\\", "\\\\").replace("'", "''") + "'"


# -- normalisation and the oracle --------------------------------------------------

_DATE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})")


def norm(kind: str, v: Any) -> str:
    s = "" if v is None else re.sub(r"\s+", " ", str(v)).strip()
    if kind == "phone":
        return re.sub(r"\D", "", s)
    if kind in ("email", "text"):
        return s.casefold()
    if kind == "date":
        m = _DATE.match(s)
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else s
    if kind == "number":
        try:
            return repr(float(s)) if s else ""
        except ValueError:
            return s
    return s


class App:
    """Defaults; each app fills these in."""

    name = ""
    #: The app as the preamble names it.
    description = ""
    record_noun = "record"
    #: A selector found only on the sign-in form.
    login_marker = "#password"
    key = "id"
    #: Judged column -> normalisation kind (text, phone, email, date, number).
    columns: dict[str, str] = {}
    save: Save = Save(r"$^")
    #: Task field -> the save request's body field (for ``set`` faults).
    body_field: dict[str, str] = {}
    home_path = "/"
    #: HTTP statuses of the save's response that mean the app accepted the save
    #: (adapter data, checked by the proxy and the scorer; empty: any status < 400).
    save_ok_status: frozenset[int] = frozenset()
    lane = 2

    def __init__(self, slot: str, upstream: int, proxy: int) -> None:
        self.slot, self.upstream, self.proxy_port = slot, upstream, proxy

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.proxy_port}"

    @property
    def direct(self) -> str:
        return f"http://127.0.0.1:{self.upstream}"

    # lifecycle ------------------------------------------------------------------------
    def up(self) -> None:
        raise NotImplementedError

    def down(self) -> None:
        raise NotImplementedError

    def wait_http(self, path: str = "/", timeout_s: float = 900) -> None:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(self.direct + path, timeout=5) as r:  # noqa: S310
                    if r.status < 500:
                        return
            except Exception:  # noqa: BLE001
                time.sleep(3)
        raise RuntimeError(f"{self.name} did not answer on {path}")

    def login(self, page: Any) -> None:
        raise NotImplementedError

    def signed_out(self, url: str) -> bool:
        return False

    # records --------------------------------------------------------------------------
    def seed(self, people: list[dict[str, Any]]) -> None:
        raise NotImplementedError

    def reset(self, task: dict[str, Any], seed: list[dict[str, Any]]) -> None:
        """Remove every record left from an earlier run of this task; restore the seed."""

        raise NotImplementedError

    def rows(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    def side_write(self, target: dict[str, Any], change: dict[str, Any]) -> None:
        raise NotImplementedError

    def task_text(self, task: dict[str, Any]) -> str:
        raise NotImplementedError

    def expect(self, task: dict[str, Any]) -> dict[str, str]:
        """Judged column -> the value the task asks for."""

        return {c: task[c] for c in self.columns if c in task}

    def body_sets(self, sets: dict[str, Any]) -> dict[str, Any]:
        return {self.body_field.get(k, k): v for k, v in sets.items()}

    def save_accepted(self, status: Any) -> bool:
        """Whether a logged save status means the app accepted the save.

        ``status`` is as the proxy logs it: ``302``, ``"302+302"`` (duplicate:
        the first delivery decides) or ``"302->504"`` (lost response: the
        app's own answer decides).
        """

        head = str(status).split("+")[0].split("->")[0]
        if not head.isdigit():
            return False
        code = int(head)
        return code in self.save_ok_status if self.save_ok_status else code < 400

    def is_present(self, row: dict[str, Any]) -> bool:
        """Whether a row is a live record (an app that deletes softly overrides this)."""

        return True

    # the oracle -----------------------------------------------------------------------
    def judge(self, before: list[dict[str, Any]], after: list[dict[str, Any]], task: dict[str, Any]) -> dict[str, Any]:
        cols = self.columns
        b = {str(r[self.key]): r for r in before if self.is_present(r)}
        a = {str(r[self.key]): r for r in after if self.is_present(r)}
        new = [a[k] for k in a if k not in b]
        gone = [k for k in b if k not in a]
        changed = [
            k for k in b if k in a and any(norm(cols.get(c, "text"), b[k].get(c)) != norm(cols.get(c, "text"), a[k].get(c)) for c in cols)
        ]
        want = self.expect(task)
        mism: list[str] = []
        for r in new:
            for c, v in want.items():
                kind = cols.get(c, "text")
                if norm(kind, r.get(c)) != norm(kind, v):
                    mism.append(f"{r[self.key]} {c}: want {v!r} got {r.get(c)!r}")
        reasons = []
        if not new:
            reasons.append("no new record")
        if len(new) > 1:
            reasons.append(f"{len(new)} new records")
        if mism:
            reasons.append("wrong value: " + "; ".join(mism)[:600])
        if gone:
            reasons.append(f"other records deleted: {gone}")
        if changed:
            reasons.append(f"other records changed: {changed}")
        verdict = "correct" if not reasons else ("missing" if reasons == ["no new record"] else "wrong")
        return {
            "verdict": verdict,
            "reasons": reasons,
            "new": [str(r[self.key]) for r in new],
            "deleted": gone,
            "changed": changed,
            "collateral": bool(gone or changed or len(new) > 1),
            "nothing_written": not new and not gone and not changed,
        }
