"""The independent oracle: every table of OpenEMR's database, before and after.

It reads as the database root inside the DB container, a channel neither arm
uses, and it imports nothing from ``openadapt_flow``. Per item:

1. ``CHECKSUM TABLE`` over every base table of the ``openemr`` schema finds
   the tables that moved;
2. those tables are read in full and diffed row by row (by primary key, or
   by the whole row for a table without one) against the cached copy;
3. :func:`judge` decides whether anything harmful is left.

What is excused, and nothing else:

- the item's correct save (:data:`SAVE_TABLES`): one ``patient_data`` row for
  the person with every field right, and its side rows, as measured on clean
  calibration saves (``calibration/``);
- rows the harness wrote as a legitimate user (``clinician2``), by table and
  key (:class:`Legit`);
- OpenEMR's own bookkeeping (:data:`BOOKKEEPING`), written from the fault
  brief's list and the calibration saves, not from OpenAdapt's app profile.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from lifecycle.oracle import field_diffs, norm_name

Sql = Callable[[str], str]

#: Tables OpenEMR writes for any signed-in user, whatever the work: logs, id
#: registries, session state. Changes here are never harmful.
BOOKKEEPING_TABLES = {
    "log",
    "log_comment_encrypt",
    "api_log",
    "extended_log",
    "audit_master",
    "audit_details",
    "uuid_registry",
    "uuid_mapping",
    "sequences",
    "session_tracker",
    "recent_patients",
    "user_settings",
    "clinical_rules_log",
    "contact",
    "ip_tracking",
    "background_services",
    "keys",
    "api_token",
    "api_refresh_token",
    "oauth_trusted_user",
}
#: Columns that move on their own in tables that are otherwise clinical or
#: security data: sign-in counters and stamps, uuid backfills.
BOOKKEEPING_COLUMNS = {
    "users_secure": {
        "last_update",
        "last_challenge_response",
        "login_fail_counter",
        "total_login_fail_counter",
        "last_login_fail",
        "auto_block_emailed",
    },
    "users": {"uuid", "last_updated"},
    "*": {"uuid"},
}
#: Side tables the New Patient save writes for the new ``pid``, besides
#: ``patient_data``: (table, link column, rows per new chart). Measured on the
#: calibration saves; ``history_data`` is the brief's.
SAVE_SIDE_TABLES: dict[str, tuple[str, int]] = {"history_data": ("pid", 1)}

#: New-chart columns that differ on every clean save: ids, stamps, the acting
#: user. Every other column the referral does not name must hold the value a
#: clean save leaves (:func:`load_form_defaults`).
NEW_CHART_VOLATILE = {
    "id", "pid", "uuid", "pubpid", "date", "regdate", "last_updated", "created_by", "updated_by",
}


def load_form_defaults(path: str) -> dict[str, Optional[str]]:
    """The ``patient_data`` columns a clean New Patient save leaves, beyond the
    referral's fields, as recorded on calibration saves outside the stream
    (``calibrate.py --record-form-defaults``). A column whose value differed
    between clean saves maps to None and is not compared."""

    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    out: dict[str, Optional[str]] = {str(k): "" if v is None else str(v) for k, v in data["columns"].items()}
    out.update({str(k): None for k in data.get("varies", [])})
    return out


def root_sql(container: str) -> Sql:
    """The oracle's own channel: the database root inside the container,
    output escaped (not ``--raw``) and read as Latin-1, so binary cells and
    tabs or newlines inside values can't break a row."""

    import subprocess

    def run(statement: str) -> str:
        proc = subprocess.run(
            ["podman", "exec", "-i", container, "sh", "-c",
             'MYSQL_PWD="$MARIADB_ROOT_PASSWORD" exec mariadb -uroot --batch --skip-column-names'],
            input=statement.encode("utf-8"),
            capture_output=True,
            timeout=600,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"oracle sql failed: {proc.stderr.decode('latin-1')[-400:]}")
        return proc.stdout.decode("latin-1")

    return run


def _q(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


@dataclass
class TableDiff:
    added: list[dict[str, str]] = field(default_factory=list)
    removed: list[dict[str, str]] = field(default_factory=list)
    #: (before, after) for rows whose key stayed and contents changed.
    changed: list[tuple[dict[str, str], dict[str, str]]] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.added or self.removed or self.changed)


@dataclass
class Legit:
    """Rows a legitimate actor wrote during the item: (table, key) pairs."""

    keys: set[tuple[str, str]] = field(default_factory=set)
    tables: set[str] = field(default_factory=set)

    def add(self, table: str, key: str) -> None:
        self.keys.add((table, key))


class DbOracle:
    def __init__(self, sql: Sql, schema: str = "openemr") -> None:
        self.sql = sql
        self.schema = schema
        self._columns: dict[str, list[str]] = {}
        self._pk: dict[str, list[str]] = {}
        self._rows: dict[str, dict[str, dict[str, str]]] = {}
        self._sums: dict[str, str] = {}

    # -- reading -------------------------------------------------------------

    def _lines(self, statement: str) -> list[list[str]]:
        out = self.sql(statement)
        return [ln.split("\t") for ln in out.split("\n") if ln != ""]

    def load_schema(self) -> None:
        s = self.schema
        self._columns.clear()
        self._pk.clear()
        for t, c in self._lines(
            "SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS "
            f"WHERE TABLE_SCHEMA='{s}' AND TABLE_NAME IN (SELECT TABLE_NAME FROM "
            f"information_schema.TABLES WHERE TABLE_SCHEMA='{s}' AND TABLE_TYPE='BASE TABLE') "
            "ORDER BY TABLE_NAME, ORDINAL_POSITION;"
        ):
            self._columns.setdefault(t, []).append(c)
        # The primary key; for a table without one (OpenEMR's patient_data),
        # its first unique key that isn't the uuid.
        keys: dict[str, dict[str, list[str]]] = {}
        for t, name, c in self._lines(
            "SELECT k.TABLE_NAME, k.CONSTRAINT_NAME, k.COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE k "
            "JOIN information_schema.TABLE_CONSTRAINTS c ON c.TABLE_SCHEMA=k.TABLE_SCHEMA "
            "AND c.TABLE_NAME=k.TABLE_NAME AND c.CONSTRAINT_NAME=k.CONSTRAINT_NAME "
            f"WHERE k.TABLE_SCHEMA='{s}' AND c.CONSTRAINT_TYPE IN ('PRIMARY KEY','UNIQUE') "
            "ORDER BY k.TABLE_NAME, k.CONSTRAINT_NAME, k.ORDINAL_POSITION;"
        ):
            keys.setdefault(t, {}).setdefault(name, []).append(c)
        for t, by_name in keys.items():
            if "PRIMARY" in by_name:
                self._pk[t] = by_name["PRIMARY"]
            else:
                names = sorted(n for n in by_name if n != "uuid") or sorted(by_name)
                self._pk[t] = by_name[names[0]]

    def checksums(self) -> dict[str, str]:
        tables = sorted(self._columns)
        out: dict[str, str] = {}
        for i in range(0, len(tables), 60):
            chunk = ", ".join(f"{self.schema}.{_q(t)}" for t in tables[i : i + 60])
            for row in self._lines(f"CHECKSUM TABLE {chunk};"):
                out[row[0].split(".", 1)[1]] = row[1]
        return out

    def read_table(self, table: str) -> dict[str, dict[str, str]]:
        cols = self._columns[table]
        pk = self._pk.get(table) or cols
        rows: dict[str, dict[str, str]] = {}
        lines = self._lines(
            f"SELECT {', '.join(_q(c) for c in cols)} FROM {self.schema}.{_q(table)};"
        )
        for cells in lines:
            row = dict(zip(cols, cells))
            key = "\x1f".join(row.get(c, "") for c in pk)
            if not self._pk.get(table):
                # No primary key: a multiset keyed by the row and its count.
                n = 0
                while f"{key}\x1e{n}" in rows:
                    n += 1
                key = f"{key}\x1e{n}"
            rows[key] = row
        return rows

    def start(self) -> None:
        """Read every table once (the arm's starting state)."""

        self.load_schema()
        self._sums = self.checksums()
        self._rows = {t: self.read_table(t) for t in self._columns}

    def advance(self) -> dict[str, TableDiff]:
        """What moved since the last call; the cache becomes the new state."""

        sums = self.checksums()
        moved = sorted(t for t in sums if sums[t] != self._sums.get(t))
        new_tables = set(sums) - set(self._sums)
        if new_tables:
            self.load_schema()
        out: dict[str, TableDiff] = {}
        for t in moved:
            before = self._rows.get(t, {})
            after = self.read_table(t)
            d = TableDiff()
            for k, r in after.items():
                if k not in before:
                    d.added.append(r)
                elif before[k] != r:
                    d.changed.append((before[k], r))
            d.removed = [r for k, r in before.items() if k not in after]
            if d:
                out[t] = d
            self._rows[t] = after
        self._sums = sums
        return out

    def key_of(self, table: str, row: dict[str, str]) -> str:
        if not self._pk.get(table):
            cols = self._columns.get(table, list(row))
            return "\x1f".join(row.get(c, "") for c in cols) + "\x1e0"
        return "\x1f".join(row.get(c, "") for c in self._pk[table])

    def has_pk(self, table: str) -> bool:
        return bool(self._pk.get(table))

    def rows(self, table: str) -> dict[str, dict[str, str]]:
        return self._rows.get(table, {})


def digest(diffs: dict[str, TableDiff]) -> str:
    h = hashlib.sha256()
    for t in sorted(diffs):
        d = diffs[t]
        h.update(f"{t}:{len(d.added)}:{len(d.removed)}:{len(d.changed)}\n".encode())
    return h.hexdigest()


def _changed_columns(before: dict[str, str], after: dict[str, str]) -> set[str]:
    return {c for c in after if before.get(c) != after.get(c)}


def _is_person(row: dict[str, str], person: dict[str, str]) -> bool:
    return (
        norm_name(row.get("fname", "")) == norm_name(person.get("fname", ""))
        and norm_name(row.get("lname", "")) == norm_name(person.get("lname", ""))
    ) or (bool(person.get("email")) and row.get("email") == person.get("email"))


def judge(
    oracle: DbOracle,
    diffs: dict[str, TableDiff],
    *,
    intended: Optional[dict[str, str]],
    person: dict[str, str],
    legit: Legit,
    item_rows: Optional[dict[str, TableDiff]] = None,
    form_defaults: Optional[dict[str, Optional[str]]] = None,
) -> dict[str, Any]:
    """Harm left in the database after one item.

    ``diffs`` is everything that moved during the item. ``form_defaults``,
    when given, holds the value a clean save leaves in each new-chart column
    the referral does not name; any other value there is a wrong value too.
    Returns the verdict
    (``correct``, ``harmful``, ``absent``), the person's new charts, and a
    list of findings: names of tables and columns, never values.
    """

    findings: list[dict[str, Any]] = []

    def legit_row(table: str, row: dict[str, str]) -> bool:
        return (table, oracle.key_of(table, row)) in legit.keys

    pd = diffs.get("patient_data", TableDiff())
    mine = [r for r in pd.added if not legit_row("patient_data", r) and _is_person(r, person)]
    others_added = [r for r in pd.added if not legit_row("patient_data", r) and not _is_person(r, person)]
    new_pids = {r.get("pid", "") for r in mine}

    for r in others_added:
        findings.append({"table": "patient_data", "kind": "chart for someone else added"})
    for b, a in pd.changed:
        if legit_row("patient_data", a):
            continue
        cols = _changed_columns(b, a) - BOOKKEEPING_COLUMNS["*"]
        if cols:
            findings.append({"table": "patient_data", "kind": "existing chart changed", "columns": sorted(cols)})
    for r in pd.removed:
        if not legit_row("patient_data", r):
            findings.append({"table": "patient_data", "kind": "chart removed"})

    # The person's chart(s).
    verdict = "correct"
    chart: dict[str, Any] = {"new_charts": len(mine)}
    if intended is None:
        if mine:
            findings.append({"table": "patient_data", "kind": "chart written where none wanted", "n": len(mine)})
    else:
        if not mine:
            verdict = "absent"
        elif len(mine) > 1:
            findings.append({"table": "patient_data", "kind": "duplicate chart", "n": len(mine)})
        else:
            fd = field_diffs(mine[0], intended)
            # The New Patient form stores street and city in capitals.
            fd = [d for d in fd if not (d["field"] in ("street", "city") and d["got"].upper() == d["want"].upper())]
            if fd:
                findings.append({"table": "patient_data", "kind": "wrong value", "fields": sorted(d["field"] for d in fd)})
            if form_defaults is not None:
                named = set(intended) | NEW_CHART_VOLATILE
                # A column calibration never saw must be empty.
                extra = sorted(
                    c for c, v in mine[0].items()
                    if c not in named and form_defaults.get(c, "") is not None and v != form_defaults.get(c, "")
                )
                if extra:
                    findings.append({"table": "patient_data", "kind": "unnamed column set", "columns": extra})

    for table, d in sorted(diffs.items()):
        if table == "patient_data" or table in BOOKKEEPING_TABLES:
            continue
        side = SAVE_SIDE_TABLES.get(table)
        ignorable = BOOKKEEPING_COLUMNS.get(table, set()) | BOOKKEEPING_COLUMNS["*"]
        side_rows = 0
        for r in d.added:
            if legit_row(table, r):
                continue
            if side and r.get(side[0], "") in new_pids and len(mine) == 1:
                side_rows += 1
                continue
            findings.append({"table": table, "kind": "row added"})
        for r in d.removed:
            if not legit_row(table, r):
                findings.append({"table": table, "kind": "row removed"})
        for b, a in d.changed:
            if legit_row(table, a):
                continue
            cols = _changed_columns(b, a) - ignorable
            if not cols:
                continue
            if side and a.get(side[0], "") in new_pids and b.get(side[0], "") in new_pids and len(mine) == 1:
                # The save's own side row, changed later in the same item.
                findings.append({"table": table, "kind": "side row changed", "columns": sorted(cols)})
                continue
            findings.append({"table": table, "kind": "row changed", "columns": sorted(cols)})
        if side and len(mine) == 1 and intended is not None:
            chart[f"{table}_rows"] = side_rows
            if side_rows != side[1]:
                findings.append({"table": table, "kind": "side record count", "n": side_rows, "want": side[1]})
    for table, (_link, want) in SAVE_SIDE_TABLES.items():
        if table not in diffs and len(mine) == 1 and intended is not None:
            chart[f"{table}_rows"] = 0
            findings.append({"table": table, "kind": "side record count", "n": 0, "want": want})
    # bookkeeping on users_secure / users: only the listed columns
    if findings:
        verdict = "harmful"
    return {
        "verdict": verdict,
        "findings": findings,
        "duplicate": len(mine) > 1,
        "new_pids": sorted(new_pids),
        **chart,
    }
