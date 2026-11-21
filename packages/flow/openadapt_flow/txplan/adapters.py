"""Read, list, write and compensate, one adapter per kind of system.

Every adapter speaks in *records*: plain dicts keyed by a record id that is a
string. A *frame* is every record a system shows in the scope a plan touches
(a REST collection, every table of a database, a mailbox), keyed by
``"<collection>/<id>"``. A frame diff is what the plan's dry run predicts and
what the read-back after apply and after undo measures.

* :class:`RestAdapter`: JSON over HTTP. No standard dry run exists, so the
  prediction is computed from the baseline and the planned fields (the
  ``computed`` dry run), with declared server-set and derived fields.
* :class:`SqlAdapter`: SQLite, MySQL/MariaDB or PostgreSQL over DB-API. The
  dry run is a **staged transaction**: the planned statements run inside a
  transaction, every table is read before and after inside it, and the
  transaction is rolled back. Triggers and cascades show up in the diff.
* :class:`MailAdapter`: SMTP out, a Mailpit API to read back. A send is
  irreversible, so it waits in the holdback queue; the Message-ID is derived
  from the idempotency key, so a release after a crash finds the message it
  already sent instead of sending a second.
"""

from __future__ import annotations

import base64
import datetime as _dt
import decimal
import hashlib
import importlib
import json
import os
import re
import time
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.parse import quote

from openadapt_flow.txplan.spec import (
    Collection,
    MailSystem,
    RestSystem,
    SqlSystem,
    TxSpecError,
)

Record = dict[str, Any]
Frame = dict[str, str]  # "<collection>/<id>" -> digest of the normalized record


class AdapterError(RuntimeError):
    """A read or a write failed. For a write, delivery is unknown."""


class ChangedSinceApply(AdapterError):
    """Undo found a row no longer as the apply left it."""


class Diverged(AdapterError):
    """The statement did something other than the plan showed. Nothing landed."""


def after_raw(
    diff: Mapping[str, Any],
    frame: Mapping[str, Mapping[str, str]],
    rows: Mapping[str, Any],
) -> dict[str, Any]:
    """The column digests a statement left: each changed row by id, and each
    added row without its primary key (a sequence may give it another id)."""
    added: dict[str, list[dict[str, str]]] = {}
    for rid in diff["added"]:
        table = rid.split("/", 1)[0]
        pk = set((rows.get(rid) or {}).get("pk") or [])
        added.setdefault(table, []).append(
            {c: d for c, d in frame[rid].items() if c not in pk}
        )
    kinds: dict[str, str] = {}
    recent: set[str] = set()
    for rid in [*diff["added"], *diff["changed"]]:
        table = rid.split("/", 1)[0]
        for c, v in ((rows.get(rid) or {}).get("row") or {}).items():
            when = _as_time(v)
            if kinds.get(f"{table}.{c}") != "other":
                kinds[f"{table}.{c}"] = "time" if when is not None else "other"
            if when is not None and abs(time.time() - when) < 600:
                recent.add(f"{table}.{c}")  # "now", to the second: may differ at apply
    return {
        "changed": {rid: dict(frame[rid]) for rid in diff["changed"]},
        "added": added,
        "kinds": kinds,
        "recent_time": sorted(recent),
    }


_TIME_TEXT = re.compile(
    r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$"
)


def _as_time(v: Any) -> Optional[float]:
    """A tagged datetime, or ISO-looking text, as epoch seconds (naive = UTC or
    local, whichever is closer to now)."""
    raw = v.get("v") if isinstance(v, dict) and v.get("$t") == "dt" else v
    if not isinstance(raw, str) or not _TIME_TEXT.match(raw):
        return None
    try:
        t = _dt.datetime.fromisoformat(raw.replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return None
    if t.tzinfo is not None:
        return t.timestamp()
    utc = t.replace(tzinfo=_dt.timezone.utc).timestamp()
    local = t.timestamp()
    return min((utc, local), key=lambda x: abs(time.time() - x))


def volatile_columns(a: Mapping[str, Any], b: Mapping[str, Any]) -> list[str]:
    """``table.column`` values that differ between two staged runs of the same
    statement (``now()``, ``random()``, a time default): not compared."""
    out: set[str] = set(a.get("recent_time", [])) | set(b.get("recent_time", []))
    for rid in set(a["changed"]) | set(b["changed"]):
        x, y = a["changed"].get(rid, {}), b["changed"].get(rid, {})
        t = rid.split("/", 1)[0]
        out |= {f"{t}.{c}" for c in set(x) | set(y) if x.get(c) != y.get(c)}
    for t in set(a["added"]) | set(b["added"]):
        xs, ys = a["added"].get(t, []), b["added"].get(t, [])
        for c in {c for r in [*xs, *ys] for c in r}:
            if sorted(r.get(c, "") for r in xs) != sorted(r.get(c, "") for r in ys):
                out.add(f"{t}.{c}")
    return sorted(out)


def after_image(raw: Mapping[str, Any], volatile: Sequence[str]) -> dict[str, Any]:
    """``after_raw`` without the volatile columns, in a comparable form."""
    skip = set(volatile)
    changed = {
        rid: {
            c: d for c, d in cols.items() if f"{rid.split('/', 1)[0]}.{c}" not in skip
        }
        for rid, cols in sorted(raw["changed"].items())
    }
    added = {
        t: sorted(
            digest({c: d for c, d in r.items() if f"{t}.{c}" not in skip}) for r in rs
        )
        for t, rs in sorted(raw["added"].items())
    }
    return {"changed": changed, "added": added}


def _shape(diff: Mapping[str, Any]) -> tuple[Any, ...]:
    """A diff, comparable across runs: ids added by a sequence can differ, so
    added rows count per table."""
    added: dict[str, int] = {}
    for rid in diff["added"]:
        t = rid.split("/", 1)[0]
        added[t] = added.get(t, 0) + 1
    return (
        sorted(added.items()),
        sorted(diff["removed"]),
        sorted((k, tuple(v)) for k, v in diff["changed"].items()),
    )


# -- values --------------------------------------------------------------------


def tag(value: Any) -> Any:
    """A JSON form of a database value that decodes to the same type."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"$t": "bytes", "v": base64.b64encode(bytes(value)).decode()}
    if isinstance(value, decimal.Decimal):
        return {"$t": "dec", "v": str(value)}
    if isinstance(value, _dt.datetime):
        return {"$t": "dt", "v": value.isoformat()}
    if isinstance(value, _dt.date):
        return {"$t": "date", "v": value.isoformat()}
    if isinstance(value, _dt.timedelta):
        return {"$t": "td", "v": value.total_seconds()}
    return value


def untag(value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {"$t", "v"}:
        kind, raw = value["$t"], value["v"]
        if kind == "bytes":
            return base64.b64decode(raw)
        if kind == "dec":
            return decimal.Decimal(raw)
        if kind == "dt":
            return _dt.datetime.fromisoformat(raw)
        if kind == "date":
            return _dt.date.fromisoformat(raw)
        if kind == "td":
            return _dt.timedelta(seconds=raw)
    return value


def digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def same(a: Any, b: Any) -> bool:
    """Equal as values (``1`` and ``1.0`` are equal; ``"1"`` and ``1`` aren't; a
    datetime equals its ISO text)."""
    a, b = untag(a), untag(b)
    if isinstance(a, (_dt.date, _dt.datetime)) or isinstance(
        b, (_dt.date, _dt.datetime)
    ):

        def text(v: Any) -> str:
            return (
                v.isoformat()
                if isinstance(v, (_dt.date, _dt.datetime))
                else str(v).replace(" ", "T")
            )

        return text(a) == text(b)
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b or a == b and type(a) is type(b)
    if isinstance(a, (int, float, decimal.Decimal)) and isinstance(
        b, (int, float, decimal.Decimal)
    ):
        return decimal.Decimal(str(a)) == decimal.Decimal(str(b))
    return json.dumps(a, sort_keys=True, default=str) == json.dumps(
        b, sort_keys=True, default=str
    )


# -- diffs ---------------------------------------------------------------------


def diff_frames(
    before: Mapping[str, Mapping[str, str]], after: Mapping[str, Mapping[str, str]]
) -> dict[str, Any]:
    """What moved between two frames of column digests: ids added and removed,
    and the changed columns of each id present in both."""
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed: dict[str, list[str]] = {}
    for rid in set(before) & set(after):
        a, b = before[rid], after[rid]
        cols = sorted(c for c in set(a) | set(b) if a.get(c) != b.get(c))
        if cols:
            changed[rid] = cols
    return {
        "added": added,
        "removed": removed,
        "changed": dict(sorted(changed.items())),
    }


def column_digests(
    record: Mapping[str, Any], drop: Sequence[str] = ()
) -> dict[str, str]:
    return {k: digest(tag(v)) for k, v in record.items() if k not in drop}


# -- REST ----------------------------------------------------------------------


class RestAdapter:
    kind = "rest"
    dry_run = "computed"

    def __init__(self, system: RestSystem, client: Any = None) -> None:
        import httpx

        self.system = system
        headers = {"Accept": "application/json"}
        if system.auth is not None:
            secret = os.environ.get(system.auth.value_env, "")
            if not secret:
                raise TxSpecError(
                    f"system {system.name}: ${system.auth.value_env} is not set"
                )
            headers[system.auth.header] = system.auth.prefix + secret
        self.http = client or httpx.Client(
            base_url=system.url(), headers=headers, timeout=system.timeout_s
        )

    @staticmethod
    def _fill(path: str, values: Mapping[str, Any]) -> str:
        def one(m: re.Match[str]) -> str:
            name = m.group(1)
            if name not in values or values[name] in (None, ""):
                raise AdapterError(f"the path needs {name}")
            # Path-injection class: a value is one path segment, never a
            # separator, dot segment or query.
            text = str(values[name])
            if text in (".", ".."):
                raise AdapterError(f"{name} can't be a dot segment")
            return quote(text, safe="")

        return re.sub(r"\{([a-z_][a-z0-9_]*)\}", one, path)

    def _call(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        params: Optional[Mapping[str, Any]] = None,
        key: Optional[str] = None,
    ) -> Any:
        headers = {}
        if key and self.system.idempotency_header:
            headers[self.system.idempotency_header] = key
        try:
            resp = self.http.request(
                method, path, json=body, params=params, headers=headers
            )
        except Exception as exc:  # noqa: BLE001 - transport failure: delivery unknown
            raise AdapterError(f"{method} failed: {type(exc).__name__}") from exc
        if resp.status_code == 404 and method == "GET":
            return None
        if resp.status_code >= 400:
            raise AdapterError(f"{method} returned {resp.status_code}")
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return {}

    def coll(self, name: Optional[str]) -> Collection:
        return self.system.collection(name)

    def normalize(self, coll: Collection, record: Mapping[str, Any]) -> Record:
        return {k: v for k, v in record.items() if k not in coll.server_fields}

    def read(self, collection: Optional[str], rid: str) -> Optional[Record]:
        coll = self.coll(collection)
        got = self._call(coll.read.method, self._fill(coll.read.path, {"id": rid}))
        if got is None:
            return None
        if not isinstance(got, dict):
            raise AdapterError("a read did not return an object")
        return got

    def frame(self, collection: Optional[str]) -> dict[str, dict[str, str]]:
        coll = self.coll(collection)
        route = coll.list_
        out: dict[str, dict[str, str]] = {}
        page = 1
        while True:
            params: dict[str, Any] = {}
            if route.page_param:
                params[route.page_param] = page
                if route.per_page_param:
                    params[route.per_page_param] = route.per_page
            got = self._call(route.method, route.path, params=params)
            # Missing-evidence class: a missing or malformed list is an error,
            # never an empty collection.
            if isinstance(got, list):
                items = got
            elif isinstance(got, Mapping) and isinstance(got.get("items"), list):
                items = got["items"]
            else:
                raise AdapterError("a list did not return a list")
            fresh = 0
            for item in items:
                rid = f"{coll.name}/{item.get(coll.id_field)}"
                if rid not in out:
                    fresh += 1
                out[rid] = column_digests(self.normalize(coll, item))
            if items and not fresh:
                # A page that repeats rows already read means the pager isn't
                # advancing: the scan can't prove it saw the whole collection.
                raise AdapterError("the list repeated a page")
            if not route.page_param or len(items) < route.per_page:
                return out
            page += 1
            if page > 10_000:
                raise AdapterError("the list never ended")

    def predict(
        self, write: Mapping[str, Any], baseline: Optional[Record]
    ) -> dict[str, Any]:
        coll = self.coll(write.get("collection"))
        fields = dict(write.get("fields") or {})
        op = write["op"]
        if op == "create":
            return {
                "added": {coll.name: 1},
                "changed": {},
                "removed": [],
                "derived": {},
            }
        rid = f"{coll.name}/{write['id']}"
        if op == "delete":
            return {"added": {}, "changed": {}, "removed": [rid], "derived": {}}
        base = baseline or {}
        cols = sorted(
            k
            for k, v in fields.items()
            if k not in coll.server_fields and not same(base.get(k), v)
        )
        derived = sorted({d for c in cols for d in coll.derived.get(c, [])} - set(cols))
        return {
            "added": {},
            "changed": {rid: cols} if cols else {},
            "removed": [],
            "derived": {rid: derived} if derived else {},
        }

    def _body(
        self,
        coll: Collection,
        route: Any,
        base: Optional[Record],
        fields: Mapping[str, Any],
    ) -> Record:
        if route.replace and base is not None:
            body = {k: v for k, v in base.items() if k not in coll.server_fields}
            body.update(fields)
            return body
        return dict(fields)

    def apply(
        self, write: Mapping[str, Any], baseline: Optional[Record], key: str
    ) -> dict[str, Any]:
        coll = self.coll(write.get("collection"))
        op = write["op"]
        fields = dict(write.get("fields") or {})
        route = getattr(coll, op, None)
        if route is None:
            raise AdapterError(f"{coll.name} declares no {op}")
        values = {**(baseline or {}), **fields, "id": write.get("id")}
        got = self._call(
            route.method,
            self._fill(route.path, values),
            None if op == "delete" else self._body(coll, route, baseline, fields),
            key=key,
        )
        if op == "create":
            rid = (got or {}).get(route.id_from or coll.id_field)
            if rid in (None, ""):
                raise AdapterError("the create returned no id")
            return {"id": str(rid)}
        return {"id": str(write["id"])}

    def compensate(
        self,
        write: Mapping[str, Any],
        baseline: Optional[Record],
        result: Mapping[str, Any],
        key: str,
    ) -> None:
        coll = self.coll(write.get("collection"))
        op = write["op"]
        rid = str(result.get("id") or write.get("id"))
        if op == "create":
            route = coll.delete
            if route is None:
                raise AdapterError(
                    f"{coll.name} declares no delete, so a create can't be undone"
                )
            self._call(
                route.method, self._fill(route.path, {"id": rid}), key=key + ":undo"
            )
            return
        if op == "update":
            route = coll.update
            assert route is not None and baseline is not None
            changed = list(write.get("fields") or {})
            restore = (
                {k: v for k, v in baseline.items() if k not in coll.server_fields}
                if route.replace
                else {k: baseline.get(k) for k in changed}
            )
            self._call(
                route.method,
                self._fill(route.path, {**baseline, "id": rid}),
                restore,
                key=key + ":undo",
            )
            return
        raise AdapterError(f"no exact undo for a REST {op}")

    def expected_after(
        self, write: Mapping[str, Any], baseline: Optional[Record]
    ) -> Optional[dict[str, Any]]:
        """The planned fields, for the record read-back after apply."""
        if write["op"] == "delete":
            return None
        coll = self.coll(write.get("collection"))
        return {
            k: v
            for k, v in (write.get("fields") or {}).items()
            if k not in coll.server_fields
        }

    def frame_id(self, write: Mapping[str, Any], result: Mapping[str, Any]) -> str:
        return f"{write.get('collection')}/{result.get('id') or write.get('id')}"


# -- SQL -----------------------------------------------------------------------


class SqlAdapter:
    kind = "sql"
    dry_run = "staged_transaction"

    def __init__(self, system: SqlSystem, root: Any = None) -> None:
        self.system = system
        self.root = root
        self.ph = "?" if system.dialect == "sqlite" else "%s"
        self.q = "`" if system.dialect == "mysql" else '"'

    # connection -------------------------------------------------------------

    def connect(self, *, read_only: bool = False) -> Any:
        s = self.system
        if s.dialect == "sqlite":
            import sqlite3
            from pathlib import Path

            path = Path(str(s.path))
            if not path.is_absolute() and self.root is not None:
                path = Path(self.root) / path
            if read_only:
                uri = path.resolve().as_uri() + "?mode=ro"
                return sqlite3.connect(uri, uri=True, isolation_level=None)
            con = sqlite3.connect(str(path), isolation_level=None)
            # Unenforced-constraint class: a parent delete must not leave
            # orphans that the frame read still calls clean.
            con.execute("PRAGMA foreign_keys = ON")
            return con
        module = importlib.import_module(str(s.driver))
        args = dict(s.connect_args)
        if s.password_env:
            pw = os.environ.get(s.password_env)
            if pw is None:
                raise TxSpecError(f"system {s.name}: ${s.password_env} is not set")
            args["password"] = pw
        con = module.connect(**args)
        if hasattr(con, "autocommit") and callable(getattr(con, "autocommit")):
            con.autocommit(False)  # pymysql
        return con

    def read_mapping(self) -> dict[str, Any]:
        """The read connection in ``frame.database.read`` form, for the verifier."""
        s = self.system
        if s.dialect == "sqlite":
            from pathlib import Path

            path = Path(str(s.path))
            if not path.is_absolute() and self.root is not None:
                path = Path(self.root) / path
            return {"sqlite_database": str(path)}
        return {
            "sql_driver": s.driver,
            "sql_connect_args": dict(s.connect_args),
            **({"sql_password_env": s.password_env} if s.password_env else {}),
        }

    def _begin(self, cur: Any) -> None:
        if self.system.dialect == "postgresql":
            return  # the DB-API driver opens a transaction on the first statement
        cur.execute("BEGIN" if self.system.dialect != "mysql" else "START TRANSACTION")

    def _ident(self, name: str) -> str:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]{0,63}", name):
            raise AdapterError(f"bad identifier {name!r}")
        return f"{self.q}{name}{self.q}"

    # reading ----------------------------------------------------------------

    def _refuse_unobserved_writes(self, cur: Any, sql: str) -> None:
        """Unobserved-scope class: the frame reads one schema (or database), so
        a statement may not change rows anywhere else."""
        d = self.system.dialect
        if d == "postgresql":
            # This transaction's own row counts, for every table it touched.
            cur.execute(
                "SELECT count(*) FROM pg_stat_xact_user_tables "
                "WHERE schemaname <> current_schema() "
                "AND n_tup_ins + n_tup_upd + n_tup_del > 0"
            )
            if int(cur.fetchone()[0]):
                raise Diverged("the statement changed rows outside the current schema")
        elif d == "mysql":
            cur.execute("SELECT DATABASE()")
            current = str(cur.fetchone()[0] or "")
            cur.execute("SHOW DATABASES")
            others = {str(r[0]).lower() for r in cur.fetchall()} - {current.lower()}
            named = {
                m.lower()
                for m in re.findall(
                    r"`?([A-Za-z0-9_$]+)`?\s*\.\s*`?[A-Za-z0-9_$]+", sql
                )
            }
            if named & others:
                raise Diverged("the statement names another database")

    def _tables(self, cur: Any) -> list[str]:
        d = self.system.dialect
        if d == "sqlite":
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        elif d == "mysql":
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_type = 'BASE TABLE'"
            )
        else:
            cur.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
            )
        names = sorted(str(r[0]) for r in cur.fetchall())
        return [n for n in names if n not in self.system.frame_ignore_tables]

    def _pk(self, cur: Any, table: str) -> list[str]:
        if table in self.system.tables:
            return [self.system.tables[table]]
        d = self.system.dialect
        if d == "sqlite":
            cur.execute(f"PRAGMA table_info({self._ident(table)})")
            cols = sorted((r[5], r[1]) for r in cur.fetchall() if r[5])
            return [c for _, c in cols]
        if d == "mysql":
            cur.execute(
                "SELECT column_name FROM information_schema.key_column_usage WHERE "
                "table_schema = DATABASE() AND table_name = %s AND constraint_name = 'PRIMARY' "
                "ORDER BY ordinal_position",
                (table,),
            )
        else:
            cur.execute(
                "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid "
                "AND a.attnum = ANY(i.indkey) WHERE i.indrelid = %s::regclass AND i.indisprimary",
                (table,),
            )
        return [str(r[0]) for r in cur.fetchall()]

    def _ignored(self, table: str, col: str) -> bool:
        rules = self.system.frame_ignore_columns
        return f"{table}.{col}" in rules or f"*.{col}" in rules

    def _rows(self, cur: Any, table: str) -> tuple[list[str], list[tuple[Any, ...]]]:
        cur.execute(f"SELECT * FROM {self._ident(table)}")
        cols = [str(d[0]) for d in cur.description]
        return cols, list(cur.fetchall())

    def frame_cur(
        self, cur: Any, rows_out: Optional[dict[str, Any]] = None
    ) -> dict[str, dict[str, str]]:
        """Every table's rows as column digests. With ``rows_out``, also keep
        each row's tagged values there (in memory, for undo images)."""
        out: dict[str, dict[str, str]] = {}
        total = 0
        for table in self._tables(cur):
            pk = self._pk(cur, table)
            cols, rows = self._rows(cur, table)
            total += len(rows)
            if total > self.system.max_frame_rows:
                raise AdapterError("the database has more rows than max_frame_rows")
            seen: dict[str, int] = {}
            for row in rows:
                rec = dict(zip(cols, row))
                digs = {
                    c: digest(tag(v))
                    for c, v in rec.items()
                    if not self._ignored(table, c)
                }
                if pk and all(c in rec for c in pk):
                    rid = "|".join(str(rec[c]) for c in pk)
                else:  # no key: the whole row is its identity
                    base = digest(digs)
                    seen[base] = seen.get(base, 0) + 1
                    rid = f"~{base}#{seen[base]}"
                out[f"{table}/{rid}"] = digs
                if rows_out is not None:
                    rows_out[f"{table}/{rid}"] = {
                        "pk": pk,
                        "row": {c: tag(v) for c, v in rec.items()},
                    }
        return out

    def frame(self, collection: Optional[str] = None) -> dict[str, dict[str, str]]:
        con = self.connect()
        try:
            cur = con.cursor()
            self._begin(cur)
            try:
                return self.frame_cur(cur)
            finally:
                con.rollback()
        finally:
            con.close()

    def _read_cur(self, cur: Any, table: str, rid: str) -> Optional[Record]:
        key = self.system.tables.get(table)
        if key is None:
            raise AdapterError(f"table {table} is not declared")
        cur.execute(
            f"SELECT * FROM {self._ident(table)} WHERE {self._ident(key)} = {self.ph}",
            (rid,),
        )
        cols = [str(d[0]) for d in cur.description]
        rows = cur.fetchall()
        if len(rows) > 1:
            raise AdapterError(f"{table}.{key} is not unique")
        return {c: tag(v) for c, v in zip(cols, rows[0])} if rows else None

    def read(self, collection: Optional[str], rid: str) -> Optional[Record]:
        con = self.connect()
        try:
            cur = con.cursor()
            self._begin(cur)
            try:
                return self._read_cur(cur, str(collection), rid)
            finally:
                con.rollback()
        finally:
            con.close()

    def normalize(self, coll: Any, record: Mapping[str, Any]) -> Record:
        return dict(record)

    # writing ----------------------------------------------------------------

    def _exec(
        self, cur: Any, write: Mapping[str, Any], baseline: Optional[Record]
    ) -> dict[str, Any]:
        table = str(write["collection"])
        key = self.system.tables.get(table)
        if key is None and write["op"] != "sql":
            raise AdapterError(f"table {table} is not declared")
        op = write["op"]
        if op == "sql":
            cur.execute(str((write.get("fields") or {})["sql"]))
            rows = max(0, int(cur.rowcount or 0))
            self._refuse_unobserved_writes(cur, str(write["fields"]["sql"]))
            return {"rows": rows}
        fields = {k: untag(v) for k, v in (write.get("fields") or {}).items()}
        assert key is not None
        t = self._ident(table)
        if op == "create":
            cols = list(fields)
            sql = (
                f"INSERT INTO {t} ({', '.join(self._ident(c) for c in cols)}) "
                f"VALUES ({', '.join([self.ph] * len(cols))})"
            )
            if self.system.dialect == "postgresql":
                sql += f" RETURNING {self._ident(key)}"
            cur.execute(sql, tuple(fields[c] for c in cols))
            if self.system.dialect == "postgresql":
                rid = cur.fetchone()[0]
            else:
                rid = fields.get(key, cur.lastrowid)
            if cur.rowcount != 1:
                raise AdapterError("the insert did not add one row")
            return {"id": str(rid)}
        rid = str(write["id"])
        if op == "update":
            sets = ", ".join(f"{self._ident(c)} = {self.ph}" for c in fields)
            cur.execute(
                f"UPDATE {t} SET {sets} WHERE {self._ident(key)} = {self.ph}",
                (*fields.values(), rid),
            )
        elif op == "delete":
            cur.execute(f"DELETE FROM {t} WHERE {self._ident(key)} = {self.ph}", (rid,))
        else:
            raise AdapterError(f"no SQL {op}")
        if cur.rowcount != 1 and not (
            op == "update" and self.system.dialect == "mysql"
        ):
            raise AdapterError(f"the {op} touched {cur.rowcount} rows, not one")
        return {"id": rid}

    def stage(
        self, writes: Sequence[Mapping[str, Any]], baselines: Sequence[Optional[Record]]
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Run the writes inside a transaction, read every table before and
        after inside it, and roll back. Returns the frame diff and each
        write's staged result."""
        con = self.connect()
        try:
            cur = con.cursor()
            self._begin(cur)
            try:
                rows: dict[str, Any] = {}
                before = self.frame_cur(cur, rows)
                results = []
                prev = before
                for w, b in zip(writes, baselines):
                    result = self._exec(cur, w, b)
                    if w["op"] == "sql":
                        now_rows: dict[str, Any] = {}
                        now_ = self.frame_cur(cur, now_rows)
                        result["staged_diff"] = diff_frames(prev, now_)
                        result["after_raw"] = after_raw(
                            result["staged_diff"], now_, now_rows
                        )
                        result["before_rows"] = {
                            rid: rows[rid]["row"]
                            for rid in [
                                *result["staged_diff"]["removed"],
                                *result["staged_diff"]["changed"],
                            ]
                            if rid in rows
                        }
                        prev = now_
                    results.append(result)
                after = self.frame_cur(cur)
            finally:
                con.rollback()
        finally:
            con.close()
        return diff_frames(before, after), results

    def predict(
        self, write: Mapping[str, Any], baseline: Optional[Record]
    ) -> dict[str, Any]:
        raise NotImplementedError("SQL plans are staged as one transaction")

    def apply(
        self, write: Mapping[str, Any], baseline: Optional[Record], key: str
    ) -> dict[str, Any]:
        if write["op"] == "sql":
            return self._apply_sql(write)
        con = self.connect()
        try:
            cur = con.cursor()
            self._begin(cur)
            try:
                result = self._exec(cur, write, baseline)
            except Exception:
                con.rollback()
                raise
            con.commit()
            return result
        finally:
            con.close()

    def _apply_sql(self, write: Mapping[str, Any]) -> dict[str, Any]:
        """Run one statement; commit only if its diff, read inside the same
        transaction, is exactly the diff the plan showed. Keep the undo images."""
        want = write.get("staged_diff")
        if not isinstance(want, Mapping):
            raise AdapterError("the plan has no staged diff for this statement")
        # The planned values, not only the shape: without them a statement
        # could commit different values into the planned rows.
        if not isinstance(write.get("staged_after"), Mapping):
            raise AdapterError("the plan has no staged values for this statement")
        con = self.connect()
        try:
            cur = con.cursor()
            self._begin(cur)
            try:
                rows: dict[str, Any] = {}
                before = self.frame_cur(cur, rows)
                result = self._exec(cur, write, None)
                after_rows: dict[str, Any] = {}
                after = self.frame_cur(cur, after_rows)
                diff = diff_frames(before, after)
                got_after = after_image(
                    after_raw(diff, after, after_rows), write.get("volatile") or []
                )
                if _shape(diff) != _shape(want) or got_after != write["staged_after"]:
                    con.rollback()
                    raise Diverged(
                        "the statement's rows or values are not the planned ones; rolled back"
                    )
                images = {
                    "before": {
                        r: rows[r] for r in [*diff["removed"], *diff["changed"]]
                    },
                    "after": {r: after[r] for r in [*diff["added"], *diff["changed"]]},
                    "added_pk": {r: after_rows[r] for r in diff["added"]},
                }
            except Diverged:
                raise
            except Exception:
                con.rollback()
                raise
            con.commit()
        finally:
            con.close()
        # Unverified-success class: what committed is read again, outside the
        # transaction, so a deferred trigger that stored other values in the
        # planned rows is a refuted write, not a confirmed one.
        post_rows: dict[str, Any] = {}
        con = self.connect()
        try:
            cur = con.cursor()
            self._begin(cur)
            try:
                post = self.frame_cur(cur, post_rows)
            finally:
                con.rollback()
        finally:
            con.close()
        post_diff = diff_frames(before, post)
        post_after = after_image(
            after_raw(post_diff, post, post_rows), write.get("volatile") or []
        )
        matches = (
            _shape(post_diff) == _shape(want) and post_after == write["staged_after"]
        )
        return {**result, "post_commit_matches": matches, "_images": images}

    def compensate_sql(self, images: Mapping[str, Any]) -> None:
        """Undo one statement from its images, in one transaction: delete what
        it added, restore what it changed, reinsert what it removed.

        Foreign keys can make the order matter (a cascade removed children
        with their parent), so each row runs under a savepoint and failed rows
        are retried in later passes until none makes progress."""
        steps: list[tuple[str, str, Mapping[str, Any]]] = [
            ("delete", rid, item) for rid, item in images.get("added_pk", {}).items()
        ] + [("restore", rid, item) for rid, item in images.get("before", {}).items()]
        con = self.connect()
        try:
            cur = con.cursor()
            self._begin(cur)
            try:
                # Stale-baseline class: the check and the restore share one
                # transaction, so an edit landing between them can't be
                # overwritten.
                now_rows = self.frame_cur(cur)
                after = images.get("after", {})
                for rid, want in after.items():
                    if now_rows.get(rid) != want:
                        raise ChangedSinceApply(rid)
                for rid in images.get("before", {}):
                    if rid not in after and now_rows.get(rid) is not None:
                        raise ChangedSinceApply(rid)
                left = steps
                n = 0
                while left:
                    failed = []
                    for step in left:
                        n += 1
                        sp = f"oa_undo_{n}"
                        cur.execute(f"SAVEPOINT {sp}")
                        try:
                            if step[0] == "delete":
                                self._delete_row(cur, step[1], step[2])
                            else:
                                self._restore_row(cur, step[1], step[2])
                            cur.execute(f"RELEASE SAVEPOINT {sp}")
                        except AdapterError:
                            raise
                        except Exception:  # noqa: BLE001 - a constraint: retry after the others
                            cur.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                            failed.append(step)
                    if len(failed) == len(left):
                        raise AdapterError(
                            f"{len(failed)} row(s) could not be restored"
                        )
                    left = failed
            except Exception:
                con.rollback()
                raise
            con.commit()
        finally:
            con.close()

    def _restore_row(self, cur: Any, rid: str, item: Mapping[str, Any]) -> None:
        table = rid.split("/", 1)[0]
        pk, row = item["pk"], item["row"]
        if not pk:
            raise AdapterError(f"{table} has no primary key: no generated undo")
        where = " AND ".join(f"{self._ident(c)} = {self.ph}" for c in pk)
        cur.execute(
            f"SELECT 1 FROM {self._ident(table)} WHERE {where}",
            tuple(untag(row[c]) for c in pk),
        )
        if cur.fetchall():
            cols = [c for c in row if c not in pk]
            if cols:
                sets = ", ".join(f"{self._ident(c)} = {self.ph}" for c in cols)
                cur.execute(
                    f"UPDATE {self._ident(table)} SET {sets} WHERE {where}",
                    (*(untag(row[c]) for c in cols), *(untag(row[c]) for c in pk)),
                )
        else:
            cols = list(row)
            cur.execute(
                f"INSERT INTO {self._ident(table)} ({', '.join(self._ident(c) for c in cols)}) "
                f"VALUES ({', '.join([self.ph] * len(cols))})",
                tuple(untag(row[c]) for c in cols),
            )

    def _delete_row(self, cur: Any, rid: str, item: Mapping[str, Any]) -> None:
        table = rid.split("/", 1)[0]
        pk, row = item["pk"], item["row"]
        if not pk:
            raise AdapterError(f"{table} has no primary key: no generated undo")
        where = " AND ".join(f"{self._ident(c)} = {self.ph}" for c in pk)
        cur.execute(
            f"DELETE FROM {self._ident(table)} WHERE {where}",
            tuple(untag(row[c]) for c in pk),
        )

    def current_digests(
        self, rids: Sequence[str]
    ) -> dict[str, Optional[dict[str, str]]]:
        """The current column digests of some frame rows (None when gone)."""
        frame = self.frame()
        return {r: frame.get(r) for r in rids}

    def compensate(
        self,
        write: Mapping[str, Any],
        baseline: Optional[Record],
        result: Mapping[str, Any],
        key: str,
    ) -> None:
        op = write["op"]
        rid = str(result.get("id") or write.get("id"))
        if op == "create":
            inverse = {"op": "delete", "collection": write["collection"], "id": rid}
        elif op == "update":
            assert baseline is not None
            # The whole before-image, not just the planned columns: a column the
            # database set itself (ON UPDATE CURRENT_TIMESTAMP, a trigger on the
            # same row) is restored too, by naming its old value explicitly.
            current = self.read(write["collection"], rid) or {}
            fields = {
                k: v
                for k, v in baseline.items()
                if k in current and not same(current.get(k), v)
            } or {k: baseline.get(k) for k in (write.get("fields") or {})}
            inverse = {
                "op": "update",
                "collection": write["collection"],
                "id": rid,
                "fields": fields,
            }
        elif op == "delete":
            assert baseline is not None
            inverse = {
                "op": "create",
                "collection": write["collection"],
                "fields": dict(baseline),
            }
        else:
            raise AdapterError(f"no SQL undo for {op}")
        self.apply(inverse, None, key + ":undo")

    def expected_after(
        self, write: Mapping[str, Any], baseline: Optional[Record]
    ) -> Optional[dict[str, Any]]:
        if write["op"] == "delete":
            return None
        return {k: tag(untag(v)) for k, v in (write.get("fields") or {}).items()}

    def frame_id(self, write: Mapping[str, Any], result: Mapping[str, Any]) -> str:
        return f"{write['collection']}/{result.get('id') or write.get('id')}"


# -- mail ----------------------------------------------------------------------


class MailAdapter:
    kind = "mail"
    dry_run = "holdback"

    def __init__(self, system: MailSystem, client: Any = None) -> None:
        import httpx

        self.system = system
        self.http = client or httpx.Client(base_url=system.mailbox.url(), timeout=20.0)

    @staticmethod
    def message_id(key: str) -> str:
        return f"<{key}@openadapt.tx>"

    def _messages(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        start = 0
        while True:
            resp = self.http.get(
                "/api/v1/messages", params={"start": start, "limit": 250}
            )
            if resp.status_code >= 400:
                raise AdapterError(f"the mailbox returned {resp.status_code}")
            page = resp.json().get("messages") or []
            out += page
            if len(page) < 250:
                return out
            start += len(page)

    def frame(self, collection: Optional[str] = None) -> dict[str, dict[str, str]]:
        out: dict[str, dict[str, str]] = {}
        counts: dict[str, int] = {}
        for m in self._messages():
            mid = str(m.get("MessageID") or m.get("ID"))
            counts[mid] = counts.get(mid, 0) + 1
            out[f"message/{mid}#{counts[mid]}"] = {
                "subject": digest(m.get("Subject")),
                "to": digest(sorted(a.get("Address", "") for a in m.get("To") or [])),
            }
        return out

    def count(self, key: str) -> int:
        mid = self.message_id(key).strip("<>")
        return sum(
            1
            for m in self._messages()
            if str(m.get("MessageID", "")).strip("<>") == mid
        )

    def predict(
        self, write: Mapping[str, Any], baseline: Optional[Record]
    ) -> dict[str, Any]:
        return {"added": {}, "changed": {}, "removed": [], "derived": {}, "queued": 1}

    def send(
        self,
        write: Mapping[str, Any],
        key: str,
        *,
        smtp_factory: Optional[Callable[..., Any]] = None,
    ) -> None:
        import smtplib
        from email.message import EmailMessage

        fields = dict(write.get("fields") or {})
        msg = EmailMessage()
        msg["From"] = self.system.sender
        to = fields.get("to")
        msg["To"] = ", ".join(to) if isinstance(to, list) else str(to)
        msg["Subject"] = str(fields.get("subject", ""))
        msg["Message-ID"] = self.message_id(key)
        msg["X-OpenAdapt-Key"] = key
        msg.set_content(str(fields.get("body", "")))
        factory = smtp_factory or smtplib.SMTP
        try:
            with factory(
                self.system.smtp.host, self.system.smtp.port, timeout=20
            ) as smtp:
                smtp.send_message(msg)
        except Exception as exc:  # noqa: BLE001 - delivery unknown
            raise AdapterError(f"SMTP failed: {type(exc).__name__}") from exc

    def wait_count(self, key: str, want: int, timeout_s: float = 10.0) -> int:
        deadline = time.monotonic() + timeout_s
        n = self.count(key)
        while n < want and time.monotonic() < deadline:
            time.sleep(0.2)
            n = self.count(key)
        return n


def adapter_for(system: Any, root: Any = None) -> Any:
    if isinstance(system, RestSystem):
        return RestAdapter(system)
    if isinstance(system, SqlSystem):
        return SqlAdapter(system, root)
    if isinstance(system, MailSystem):
        return MailAdapter(system)
    raise TxSpecError(f"unknown system kind {type(system).__name__}")
