"""The task-generation workbench: one synthetic records app per profile.

    python -m openadapt_flow.taskgen.workbench <app> --port 9771 \\
        --dsn "host=127.0.0.1 port=9770 user=wb_admin dbname=wb_<app>"

A small server-rendered web app whose tables, screens and operations come
from a profile (:mod:`openadapt_flow.taskgen.profile`). Its store is a real
PostgreSQL database, so the contracts that judge it are the ones
:mod:`openadapt_flow.generate.verify_contract` writes from the schema and
demonstrations, read through a SELECT-only account the agent never sees.

Screens: sign-in, a search list per record type, a record page with its
child records, and new / edit / cancel / merge forms. Every control has a
stable id (``#edit``, ``#in-phone``, ``#open-patient-104729``) so a scripted
solution can drive it.

``POST /__reset`` (blocked for the agent) restores the store to a task's
seed rows and switches on the task's mechanisms:

- ``expire_on_save``: the session has expired by the first save; that
  save is dropped and the browser is sent to sign in again.
- ``banner_lie_once``: the first save shows "Saved" and stores nothing.
- ``late_write_s``: saves land that many seconds later, in the background.
- ``concurrent``: when the agent opens the edit form of that record, a
  second user changes it. A save from the stale form is refused with a
  "changed by another user" page, as an app with optimistic locking does.
- ``interrupt``: after that many requests every page shows a message.
- ``fault``: a planted app fault, to check the oracle refutes it:
  ``drop_write``, ``duplicate_write``, ``wrong_record``, ``partial_write``,
  ``lost_update`` (no lock: the stale form overwrites the second user's
  change) and ``late_write_lost`` (a late write never lands).

All data is fake. Nothing here leaves localhost.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import decimal
import html
import json
import secrets
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping, Optional
from urllib.parse import parse_qs, quote, urlencode, urlparse

from openadapt_flow.taskgen.profile import EntitySpec, Profile, load_profile

FAULTS = (
    "drop_write",
    "duplicate_write",
    "wrong_record",
    "partial_write",
    "lost_update",
    "late_write_lost",
)
#: Paths a browser agent must never reach: every ``/__`` route needs the
#: admin token, and sign-in never redirects into one.
BLOCKED_PATHS = ("/__reset", "/__state")


def _q(name: str) -> str:
    if '"' in name:
        raise ValueError(f"bad identifier {name!r}")
    return f'"{name}"'


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, decimal.Decimal):
        return format(value, "f")
    if isinstance(value, (_dt.date, _dt.datetime)):
        return value.isoformat()
    return str(value)


def ddl(profile: Profile) -> list[str]:
    """The app's schema: one table per record type, plus its audit log."""

    out = []
    for e in profile.entities.values():
        cols = ["id SERIAL PRIMARY KEY"]
        for f in e.fields:
            col = f"{_q(f.name)} {f.sql_type}"
            if f.type == "ref":
                col += f" REFERENCES {_q(profile.entity(f.entity).table)}(id)"
            if f.default is not None:
                col += f" NOT NULL DEFAULT '{f.default}'"
            out_col = col
            cols.append(out_col)
        if e.key:
            cols.append(f"UNIQUE ({_q(e.key)})")
        cols += ["version INTEGER NOT NULL DEFAULT 1", "updated_by TEXT"]
        out.append(f"CREATE TABLE {_q(e.table)} ({', '.join(cols)})")
    out.append(
        "CREATE TABLE audit_log (id SERIAL PRIMARY KEY, actor TEXT NOT NULL, "
        "table_name TEXT NOT NULL, record_id INTEGER NOT NULL, event TEXT NOT NULL)"
    )
    return out


class Store:
    """The app's PostgreSQL database, through one serialized connection."""

    def __init__(self, profile: Profile, dsn: str) -> None:
        import psycopg2

        self.profile = profile
        self.conn = psycopg2.connect(dsn)
        self.conn.autocommit = True
        self.lock = threading.RLock()

    def sql(self, query: str, args: tuple = ()) -> list[tuple]:
        with self.lock:
            cur = self.conn.cursor()
            cur.execute(query, args)
            rows = cur.fetchall() if cur.description else []
            cur.close()
            return rows

    def create_schema(self, reader: Optional[str] = None) -> None:
        with self.lock:
            self.sql("DROP SCHEMA public CASCADE")
            self.sql("CREATE SCHEMA public")
            for stmt in ddl(self.profile):
                self.sql(stmt)
            if reader:
                self.sql(f"GRANT USAGE ON SCHEMA public TO {_q(reader)}")
                self.sql(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {_q(reader)}")
                self.sql(
                    f"GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO {_q(reader)}"
                )

    def reset(self, rows: Mapping[str, list[Mapping[str, Any]]]) -> dict[str, int]:
        """Empty every table, restart the ids and insert the seed rows in
        order. ``$alias`` in a ref field names a row seeded earlier (its
        ``_as``). Returns alias -> id."""

        tables = ", ".join(_q(e.table) for e in self.profile.entities.values())
        aliases: dict[str, int] = {}
        with self.lock:
            self.sql(f"TRUNCATE {tables}, audit_log RESTART IDENTITY CASCADE")
            # Seed parents before children: entities in profile order.
            for name in self.profile.entities:
                for row in rows.get(name, []):
                    values = {
                        k: aliases[v[1:]]
                        if isinstance(v, str) and v.startswith("$")
                        else v
                        for k, v in row.items()
                        if k != "_as"
                    }
                    rid = self.insert(name, values, actor="seed", audit=False)
                    if row.get("_as"):
                        aliases[str(row["_as"])] = rid
        return aliases

    def insert(
        self, entity: str, values: Mapping[str, Any], *, actor: str, audit: bool = True
    ) -> int:
        e = self.profile.entity(entity)
        cols = [k for k in values if values[k] not in (None, "")]
        names = ", ".join([_q(c) for c in cols] + ["updated_by"])
        marks = ", ".join(["%s"] * (len(cols) + 1))
        with self.lock:
            rid = self.sql(
                f"INSERT INTO {_q(e.table)} ({names}) VALUES ({marks}) RETURNING id",
                tuple(values[c] for c in cols) + (actor,),
            )[0][0]
            if audit:
                self.audit(actor, e.table, rid, "insert")
        return int(rid)

    def update(
        self, entity: str, rid: int, values: Mapping[str, Any], *, actor: str
    ) -> None:
        e = self.profile.entity(entity)
        if not values:
            return
        sets = ", ".join(f"{_q(k)} = %s" for k in values)
        with self.lock:
            self.sql(
                f"UPDATE {_q(e.table)} SET {sets}, version = version + 1, "
                "updated_by = %s WHERE id = %s",
                tuple(None if v == "" else v for v in values.values()) + (actor, rid),
            )
            self.audit(actor, e.table, rid, "update")

    def audit(self, actor: str, table: str, rid: int, event: str) -> None:
        self.sql(
            "INSERT INTO audit_log (actor, table_name, record_id, event) VALUES (%s, %s, %s, %s)",
            (actor, table, rid, event),
        )

    def get(self, entity: str, rid: int) -> Optional[dict[str, Any]]:
        e = self.profile.entity(entity)
        cols = ["id"] + [f.name for f in e.fields] + ["version", "updated_by"]
        rows = self.sql(
            f"SELECT {', '.join(_q(c) for c in cols)} FROM {_q(e.table)} WHERE id = %s",
            (rid,),
        )
        return dict(zip(cols, rows[0])) if rows else None

    def find(
        self, entity: str, where: str = "", args: tuple = ()
    ) -> list[dict[str, Any]]:
        e = self.profile.entity(entity)
        cols = ["id"] + [f.name for f in e.fields] + ["version"]
        rows = self.sql(
            f"SELECT {', '.join(_q(c) for c in cols)} FROM {_q(e.table)} "
            f"{('WHERE ' + where) if where else ''} ORDER BY id",
            args,
        )
        return [dict(zip(cols, r)) for r in rows]

    def dump(self) -> dict[str, list[dict[str, str]]]:
        out = {}
        for e in self.profile.entities.values():
            out[e.name] = [
                {k: _text(v) for k, v in r.items()} for r in self.find(e.name)
            ]
        return out


class Mechanisms:
    """The task's adversarial mechanisms and planted fault (from ``setup``)."""

    def __init__(self, setup: Optional[Mapping[str, Any]] = None) -> None:
        s = dict(setup or {})
        self.expire_on_save: bool = bool(s.get("expire_on_save"))
        self.banner_lie_once: bool = bool(s.get("banner_lie_once"))
        self.late_write_s: Optional[float] = s.get("late_write_s")
        self.concurrent: Optional[dict[str, Any]] = s.get("concurrent")
        self.interrupt: Optional[dict[str, Any]] = s.get("interrupt")
        self.fault: Optional[str] = s.get("fault")
        if self.fault is not None and self.fault not in FAULTS:
            raise ValueError(f"fault {self.fault!r} not in {FAULTS}")
        self.requests = 0
        self.lied = False
        self.expired = False
        self.concurrent_done = False


class Workbench:
    def __init__(self, profile: Profile, store: Store) -> None:
        self.profile = profile
        self.store = store
        self.sessions: dict[str, dict[str, Any]] = {}
        self.mech = Mechanisms()
        self.flash: dict[str, str] = {}
        self.timers: list[threading.Timer] = []
        #: Required on ``/__reset`` and ``/__state`` (see ``Handler._admin``).
        self.admin_token: Optional[str] = secrets.token_hex(16)

    # -- reset --------------------------------------------------------------

    def reset(self, setup: Mapping[str, Any]) -> dict[str, Any]:
        for t in self.timers:
            t.cancel()
        self.timers = []
        self.sessions = {}
        self.flash = {}
        aliases = self.store.reset(setup.get("rows", {}))
        self.mech = Mechanisms(setup)
        if self.mech.concurrent:
            c = dict(self.mech.concurrent)
            c["id"] = aliases.get(str(c.get("record", "")).lstrip("$"), c.get("id"))
            self.mech.concurrent = c
        return {"ok": True, "ids": aliases}

    # -- saves --------------------------------------------------------------

    def _apply(self, fn: Callable[[], None]) -> str:
        """Run one save under the task's mechanisms; the flash to show."""

        m = self.mech
        if m.banner_lie_once and not m.lied:
            m.lied = True
            return "Saved."
        if m.fault == "drop_write":
            return "Saved."
        if m.late_write_s or m.fault == "late_write_lost":
            if m.fault != "late_write_lost":
                t = threading.Timer(float(m.late_write_s or 2.0), fn)
                t.daemon = True
                t.start()
                self.timers.append(t)
            return "Saved. Processing: the change appears within a few seconds."
        fn()
        return "Saved."

    def _other_record(self, entity: str, rid: int) -> int:
        """The record a ``wrong_record`` fault lands on instead."""
        ids = [r["id"] for r in self.store.find(entity) if r["id"] != rid]
        return min(ids, key=lambda i: abs(i - rid)) if ids else rid

    def save_create(
        self, entity: str, values: dict[str, Any], actor: str
    ) -> tuple[str, Optional[int]]:
        e = self.profile.entity(entity)
        m = self.mech
        if m.fault == "partial_write" and e.editable:
            values[e.editable[-1].name] = None
        if m.fault == "wrong_record" and e.parent:
            pf = e.parent[1]
            if values.get(pf):
                values[pf] = self._other_record(e.parent[0], int(values[pf]))
        created: dict[str, int] = {}

        def run() -> None:
            created["id"] = self.store.insert(entity, values, actor=actor)
            if m.fault == "duplicate_write":
                self.store.insert(entity, values, actor=actor)

        msg = self._apply(run)
        return msg, created.get("id")

    def save_update(
        self,
        entity: str,
        rid: int,
        values: dict[str, Any],
        actor: str,
        version: Optional[int],
    ) -> Optional[str]:
        """The flash, or None when the save is refused as stale."""

        m = self.mech
        current = self.store.get(entity, rid)
        if current is None:
            return "No such record."
        if (
            version is not None
            and version != current["version"]
            and m.fault != "lost_update"
        ):
            return None
        e = self.profile.entity(entity)
        if m.fault == "partial_write":
            changed = [k for k in values if _text(current.get(k)) != _text(values[k])]
            if changed:
                values.pop(changed[-1])
        if (
            m.fault == "lost_update"
            and version is not None
            and version != current["version"]
        ):
            # No lock: the stale form's every field overwrites the record.
            stale = m.concurrent.get("before", {}) if m.concurrent else {}
            for k, v in stale.items():
                values.setdefault(k, v)
        target = self._other_record(entity, rid) if m.fault == "wrong_record" else rid
        del e
        return self._apply(
            lambda: self.store.update(entity, target, values, actor=actor)
        )

    # -- concurrency --------------------------------------------------------

    def on_edit_form(self, entity: str, rid: int) -> None:
        c = self.mech.concurrent
        if (
            not c
            or self.mech.concurrent_done
            or c.get("entity") != entity
            or c.get("id") != rid
        ):
            return
        self.mech.concurrent_done = True
        before = self.store.get(entity, rid) or {}
        c["before"] = {k: _text(before.get(k)) for k in c["set"]}
        target = int(c.get("other_id") or rid)
        self.store.update(
            entity, target, dict(c["set"]), actor=self.profile.other["user"]
        )


# -- HTML -------------------------------------------------------------------

CSS = """
body{font-family:system-ui,sans-serif;margin:0;background:#f6f7f9;color:#1d232b}
header{background:#1f3a5f;color:#fff;padding:10px 20px;display:flex;gap:18px;align-items:center}
header a{color:#fff}main{padding:20px;max-width:980px}
table{border-collapse:collapse;background:#fff;width:100%}td,th{border:1px solid #d7dce2;padding:6px 8px;text-align:left}
dl{display:grid;grid-template-columns:200px 1fr;background:#fff;padding:12px;border:1px solid #d7dce2}
dt{font-weight:600}.flash{background:#e3f6e8;border:1px solid #67b37a;padding:8px;margin-bottom:12px}
.warn{background:#fff3d6;border:1px solid #d9a400;padding:8px;margin-bottom:12px}
.inbox{background:#e8eefc;border:1px solid #5b7bd5;padding:8px;margin-bottom:12px}
label{display:block;margin-top:8px}input,select{padding:5px;min-width:320px}button,.btn{margin-top:12px;padding:6px 12px}
"""


def _e(v: Any) -> str:
    return html.escape(_text(v), quote=True)


def slug(value: Any) -> str:
    return (
        "".join(ch if ch.isalnum() else "-" for ch in _text(value)).strip("-").lower()
    )


class Handler(BaseHTTPRequestHandler):
    wb: Workbench

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass

    # -- plumbing -----------------------------------------------------------

    def _send(
        self,
        status: int,
        body: str,
        ctype: str = "text/html; charset=utf-8",
        headers: Optional[dict] = None,
    ) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location: str, headers: Optional[dict] = None) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _form(self) -> dict[str, str]:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8") if n else ""
        if "json" in (self.headers.get("Content-Type") or ""):
            return json.loads(raw or "{}")
        return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}

    def _admin(self) -> bool:
        """The reset and state routes need the workbench's admin token
        (``X-Workbench-Token``), which no browser session holds. A
        workbench served with ``admin_token=None`` leaves them to the
        environment's own path blocking."""
        token = self.wb.admin_token
        if token is None:
            return True
        given = self.headers.get("X-Workbench-Token") or ""
        return secrets.compare_digest(given, token)

    def _sid(self) -> Optional[str]:
        c = SimpleCookie(self.headers.get("Cookie") or "")
        return c["wbsid"].value if "wbsid" in c else None

    def _session(self) -> Optional[dict[str, Any]]:
        """The signed-in session, counted against the task's session TTL."""

        sid = self._sid()
        s = self.wb.sessions.get(sid or "")
        if s is None:
            return None
        self.wb.mech.requests += 1
        s["requests"] += 1
        return s

    def _page(self, title: str, body: str, *, user: Optional[str] = None) -> str:
        wb = self.wb
        nav = " ".join(
            f'<a id="nav-{e.name}" href="/e/{e.name}">{_e(e.label)}s</a>'
            for e in wb.profile.entities.values()
            if e.list
        )
        sid = self._sid() or ""
        flash = wb.flash.pop(sid, "")
        top = (
            f'<div class="flash" id="flash" role="status">{_e(flash)}</div>'
            if flash
            else ""
        )
        it = wb.mech.interrupt
        if user and it and wb.mech.requests >= int(it.get("after_requests", 0)):
            top += (
                f'<div class="inbox" id="inbox" role="alert"><b>Message from '
                f"{_e(it.get('from', 'the front desk'))}:</b> {_e(it.get('text', ''))}</div>"
            )
        who = (
            f'<span id="whoami">Signed in as {_e(user)}</span> <a id="signout" href="/logout">Sign out</a>'
            if user
            else ""
        )
        return (
            f"<!doctype html><html><head><meta charset='utf-8'><title>{_e(title)} - {_e(wb.profile.title)}</title>"
            f"<style>{CSS}</style></head><body><header><b>{_e(wb.profile.title)}</b>{nav}"
            f"<span style='flex:1'></span>{who}</header><main>{top}<h1>{_e(title)}</h1>{body}</main></body></html>"
        )

    def _title(self, e: EntitySpec, row: Mapping[str, Any]) -> str:
        try:
            return e.title.format(**{k: _text(v) for k, v in row.items()})
        except (KeyError, IndexError):
            return e.label

    def _ref_text(self, entity: str, rid: Any) -> str:
        if rid in (None, ""):
            return ""
        e = self.wb.profile.entity(entity)
        row = self.wb.store.get(entity, int(rid))
        if row is None:
            return ""
        key = f"{_text(row[e.key])} " if e.key else ""
        return f"{key}{self._title(e, row)}"

    def _row_anchor(self, e: EntitySpec, row: Mapping[str, Any]) -> str:
        return f"open-{e.name}-{slug(row[e.key]) if e.key else row['id']}"

    # -- routes -------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        path, query = url.path, {k: v[0] for k, v in parse_qs(url.query).items()}
        if path.startswith("/__"):
            if path != "/__state" or not self._admin():
                return self._send(404, "not found", "text/plain")
            return self._send(200, json.dumps(self.wb.store.dump()), "application/json")
        if path == "/health":
            return self._send(200, "ok", "text/plain")
        if path == "/login":
            return self._login_form(query)
        if path == "/logout":
            self.wb.sessions.pop(self._sid() or "", None)
            return self._redirect("/login")
        s = self._session()
        if s is None:
            expired = "&expired=1" if self._sid() else ""
            return self._redirect(f"/login?next={quote(self.path)}{expired}")
        parts = [p for p in path.split("/") if p]
        try:
            if not parts:
                return self._home(s)
            if parts[0] == "e" and len(parts) == 2:
                return self._list(s, parts[1], query)
            if parts[0] == "e" and len(parts) == 3 and parts[2] == "new":
                return self._new_form(s, parts[1], query)
            if parts[0] == "e" and len(parts) == 3:
                return self._view(s, parts[1], int(parts[2]))
            if parts[0] == "e" and len(parts) == 4:
                return self._op_form(s, parts[1], int(parts[2]), parts[3])
        except (KeyError, ValueError) as exc:
            return self._send(
                404, self._page("Not found", f"<p>{_e(exc)}</p>", user=s["user"])
            )
        return self._send(404, self._page("Not found", "", user=s["user"]))

    def do_POST(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        path = url.path
        form = self._form()
        if path.startswith("/__"):
            if path != "/__reset" or not self._admin():
                return self._send(404, "not found", "text/plain")
            return self._send(200, json.dumps(self.wb.reset(form)), "application/json")
        if path == "/login":
            return self._login(form, {k: v[0] for k, v in parse_qs(url.query).items()})
        s = self._session()
        m = self.wb.mech
        if s is not None and m.expire_on_save and not m.expired:
            m.expired = True
            self.wb.sessions.pop(self._sid() or "", None)
            s = None
        if s is None:
            # The save is lost with the session, as in an app whose session
            # expired while the form was open.
            return self._redirect("/login?expired=1&next=/")
        parts = [p for p in path.split("/") if p]
        if parts[:1] == ["e"] and len(parts) == 3 and parts[2] == "new":
            return self._create(s, parts[1], form)
        if parts[:1] == ["e"] and len(parts) == 4:
            return self._op(s, parts[1], int(parts[2]), parts[3], form)
        return self._send(404, "not found", "text/plain")

    # -- sign in ------------------------------------------------------------

    def _login_form(self, query: Mapping[str, str]) -> None:
        note = (
            '<p class="warn" id="expired">Your session expired. Sign in again.</p>'
            if query.get("expired")
            else ""
        )
        nxt = query.get("next", "/")
        body = (
            f'{note}<form method="post" action="/login?{urlencode({"next": nxt})}">'
            '<label>User name <input id="username" name="username"></label>'
            '<label>Password <input id="password" name="password" type="password"></label>'
            '<button id="signin" type="submit">Sign in</button></form>'
        )
        self._send(200, self._page("Sign in", body))

    def _login(self, form: Mapping[str, str], query: Mapping[str, str]) -> None:
        agent = self.wb.profile.agent
        if (
            form.get("username") != agent["user"]
            or form.get("password") != agent["password"]
        ):
            return self._send(
                401,
                self._page(
                    "Sign in",
                    '<p class="warn" id="bad-login">Wrong user name or password.</p>',
                ),
            )
        sid = secrets.token_hex(16)
        self.wb.sessions[sid] = {"user": agent["user"], "requests": 0}
        nxt = query.get("next") or "/"
        if not nxt.startswith("/") or nxt.startswith("//") or nxt.startswith("/__"):
            nxt = "/"
        self._redirect(nxt, {"Set-Cookie": f"wbsid={sid}; Path=/; HttpOnly"})

    # -- screens ------------------------------------------------------------

    def _home(self, s: Mapping[str, Any]) -> None:
        links = "".join(
            f'<li><a id="home-{e.name}" href="/e/{e.name}">{_e(e.label)}s</a></li>'
            for e in self.wb.profile.entities.values()
            if e.list
        )
        self._send(200, self._page("Home", f"<ul>{links}</ul>", user=s["user"]))

    def _list(self, s: Mapping[str, Any], name: str, query: Mapping[str, str]) -> None:
        e = self.wb.profile.entity(name)
        q = query.get("q", "").strip()
        rows = self.wb.store.find(name)
        if q:
            ql = q.lower()
            rows = [
                r
                for r in rows
                if any(
                    ql in _text(r.get(f)).lower()
                    for f in e.search or [x.name for x in e.fields]
                )
            ]
        cols = [f for f in e.fields if f.type != "ref"][:6]
        head = "".join(f"<th>{_e(f.label)}</th>" for f in cols)
        body_rows = "".join(
            "<tr>"
            + "".join(
                f"<td>{'<a id=%s href=/e/%s/%s>%s</a>' % (self._row_anchor(e, r), name, r['id'], _e(r[f.name])) if i == 0 else _e(r[f.name])}</td>"
                for i, f in enumerate(cols)
            )
            + "</tr>"
            for r in rows
        )
        new = (
            f'<a class="btn" id="new-{name}" href="/e/{name}/new">New {_e(e.label.lower())}</a>'
            if "create" in e.ops and not e.parent
            else ""
        )
        body = (
            f'<form method="get"><input id="search-q" name="q" value="{_e(q)}" placeholder="Search">'
            f'<button id="search-go" type="submit">Search</button></form>{new}'
            f"<table><tr>{head}</tr>{body_rows}</table>"
        )
        self._send(200, self._page(f"{e.label}s", body, user=s["user"]))

    def _view(self, s: Mapping[str, Any], name: str, rid: int) -> None:
        wb = self.wb
        e = wb.profile.entity(name)
        row = wb.store.get(name, rid)
        if row is None:
            raise KeyError(f"no {name} {rid}")
        items = []
        for f in e.fields:
            val = (
                self._ref_text(f.entity, row[f.name])
                if f.type == "ref"
                else _text(row[f.name])
            )
            items.append(f'<dt>{_e(f.label)}</dt><dd id="f-{f.name}">{_e(val)}</dd>')
        buttons = []
        if "update" in e.ops:
            buttons.append(
                f'<a class="btn" id="edit" href="/e/{name}/{rid}/edit">Edit</a>'
            )
        if (
            "cancel" in e.ops
            and row.get(e.cancel["status_field"]) != e.cancel["status_value"]
        ):
            buttons.append(
                f'<a class="btn" id="cancel" href="/e/{name}/{rid}/cancel">Cancel this {_e(e.label.lower())}</a>'
            )
        if (
            "merge" in e.ops
            and row.get(e.merge["status_field"]) != e.merge["status_value"]
        ):
            buttons.append(
                f'<a class="btn" id="merge" href="/e/{name}/{rid}/merge">Merge into another record</a>'
            )
        children = []
        for c in wb.profile.entities.values():
            if not c.parent or c.parent[0] != name:
                continue
            kids = wb.store.find(c.name, f"{_q(c.parent[1])} = %s", (rid,))
            cols = [f for f in c.fields if f.type != "ref"][:4]
            trs = "".join(
                f'<tr><td><a id="{self._row_anchor(c, k)}" href="/e/{c.name}/{k["id"]}">{_e(self._title(c, k))}</a></td>'
                + "".join(f"<td>{_e(k[f.name])}</td>" for f in cols)
                + "</tr>"
                for k in kids
            )
            add = (
                f'<a class="btn" id="new-{c.name}" href="/e/{c.name}/new?parent={rid}">New {_e(c.label.lower())}</a>'
                if "create" in c.ops
                else ""
            )
            heads = "".join(f"<th>{_e(f.label)}</th>" for f in cols)
            children.append(
                f"<h2>{_e(c.label)}s</h2>{add}<table><tr><th></th>{heads}</tr>{trs}</table>"
            )
        body = f"<dl>{''.join(items)}</dl>{' '.join(buttons)}{''.join(children)}"
        self._send(200, self._page(self._title(e, row), body, user=s["user"]))

    def _input(self, f: Any, value: Any) -> str:
        if f.type == "choice":
            opts = "".join(
                f'<option value="{_e(c)}"{" selected" if _text(value) == c else ""}>{_e(c)}</option>'
                for c in f.choices
            )
            return f'<label>{_e(f.label)} <select id="in-{f.name}" name="{f.name}"><option value=""></option>{opts}</select></label>'
        return f'<label>{_e(f.label)} <input id="in-{f.name}" name="{f.name}" value="{_e(value)}"></label>'

    def _new_form(
        self, s: Mapping[str, Any], name: str, query: Mapping[str, str]
    ) -> None:
        e = self.wb.profile.entity(name)
        if "create" not in e.ops:
            raise KeyError(f"{name} can't be created here")
        parent = query.get("parent", "")
        fields = "".join(self._input(f, "") for f in e.editable)
        if e.key:
            fields = (
                self._input(
                    type(
                        "K",
                        (),
                        {"type": "text", "name": e.key, "label": e.field(e.key).label},
                    )(),
                    "",
                )
                + fields
            )
        hidden = f'<input type="hidden" name="__parent" value="{_e(parent)}">'
        body = f'<form method="post" action="/e/{name}/new">{hidden}{fields}<button id="save" type="submit">Save</button></form>'
        self._send(200, self._page(f"New {e.label.lower()}", body, user=s["user"]))

    def _op_form(self, s: Mapping[str, Any], name: str, rid: int, op: str) -> None:
        wb = self.wb
        e = wb.profile.entity(name)
        row = wb.store.get(name, rid)
        if row is None:
            raise KeyError(f"no {name} {rid}")
        # The form shows the record as it is now; the second user saves right
        # after, so the form the agent fills is stale.
        wb.on_edit_form(name, rid)
        ver = f'<input type="hidden" name="__version" value="{row["version"]}">'
        if op == "edit" and "update" in e.ops:
            fields = "".join(self._input(f, row[f.name]) for f in e.editable)
            body = f'<form method="post" action="/e/{name}/{rid}/edit">{ver}{fields}<button id="save" type="submit">Save</button></form>'
        elif op == "cancel" and "cancel" in e.ops:
            rf = e.field(e.cancel["reason_field"])
            body = (
                f'<form method="post" action="/e/{name}/{rid}/cancel">{ver}'
                f'<label>{_e(rf.label)} <input id="in-{rf.name}" name="{rf.name}"></label>'
                f'<button id="confirm-cancel" type="submit">Confirm</button></form>'
            )
        elif op == "merge" and "merge" in e.ops:
            others = wb.store.find(
                name,
                f"id <> %s AND {_q(e.merge['status_field'])} <> %s",
                (rid, e.merge["status_value"]),
            )
            opts = "".join(
                f'<option value="{_e(o[e.key])}">{_e(o[e.key])} · {_e(self._title(e, o))}'
                + (
                    f" · {_e(o[wb.profile.names['birth']])}"
                    if wb.profile.names["birth"] in o
                    else ""
                )
                + "</option>"
                for o in others
            )
            body = (
                f"<p>This record will be marked as {_e(e.merge['status_value'])} and point to the record you pick.</p>"
                f'<form method="post" action="/e/{name}/{rid}/merge">{ver}'
                f'<label>Keep this record <select id="in-merge-into" name="into"><option value=""></option>{opts}</select></label>'
                f'<button id="confirm-merge" type="submit">Merge</button></form>'
            )
        else:
            raise KeyError(f"{op} is not available on {name}")
        self._send(
            200, self._page(f"{op.title()} {self._title(e, row)}", body, user=s["user"])
        )

    def _values(self, e: EntitySpec, form: Mapping[str, str]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in e.editable:
            if f.name in form:
                out[f.name] = form[f.name].strip()
        return out

    def _create(self, s: Mapping[str, Any], name: str, form: Mapping[str, str]) -> None:
        wb = self.wb
        e = wb.profile.entity(name)
        values = self._values(e, form)
        if e.key:
            values[e.key] = form.get(e.key, "").strip()
            if not values[e.key]:
                return self._send(
                    400,
                    self._page(
                        "Not saved",
                        f'<p class="warn" id="error">{_e(e.field(e.key).label)} is required.</p>',
                        user=s["user"],
                    ),
                )
        if e.parent:
            if not form.get("__parent"):
                return self._send(
                    400,
                    self._page(
                        "Not saved",
                        '<p class="warn" id="error">Open the parent record first.</p>',
                        user=s["user"],
                    ),
                )
            values[e.parent[1]] = int(form["__parent"])
        for f in e.fields:
            if f.default is not None:
                values.setdefault(f.name, f.default)
        try:
            msg, rid = wb.save_create(name, values, s["user"])
        except Exception as exc:  # noqa: BLE001 - shown to the user, as an app would
            return self._send(
                400,
                self._page(
                    "Not saved",
                    f'<p class="warn" id="error">{_e(exc)}</p>',
                    user=s["user"],
                ),
            )
        wb.flash[self._sid() or ""] = msg
        back = values.get(e.parent[1]) if e.parent else rid
        target = (
            f"/e/{e.parent[0]}/{back}"
            if e.parent
            else (f"/e/{name}/{rid}" if rid else f"/e/{name}")
        )
        self._redirect(target)

    def _op(
        self,
        s: Mapping[str, Any],
        name: str,
        rid: int,
        op: str,
        form: Mapping[str, str],
    ) -> None:
        wb = self.wb
        e = wb.profile.entity(name)
        version = int(form["__version"]) if form.get("__version") else None
        if op == "edit":
            values = self._values(e, form)
        elif op == "cancel":
            values = {
                e.cancel["status_field"]: e.cancel["status_value"],
                e.cancel["reason_field"]: form.get(
                    e.cancel["reason_field"], ""
                ).strip(),
            }
        elif op == "merge":
            into = wb.store.find(name, f"{_q(e.key)} = %s", (form.get("into", ""),))
            if not into:
                return self._send(
                    400,
                    self._page(
                        "Not merged",
                        '<p class="warn" id="error">Pick the record to keep.</p>',
                        user=s["user"],
                    ),
                )
            values = {
                e.merge["status_field"]: e.merge["status_value"],
                e.merge["into_field"]: into[0]["id"],
            }
        else:
            return self._send(404, "not found", "text/plain")
        try:
            msg = wb.save_update(name, rid, values, s["user"], version)
        except Exception as exc:  # noqa: BLE001 - shown to the user
            return self._send(
                400,
                self._page(
                    "Not saved",
                    f'<p class="warn" id="error">{_e(exc)}</p>',
                    user=s["user"],
                ),
            )
        if msg is None:
            who = wb.profile.other["user"]
            body = (
                f'<p class="warn" id="conflict">{_e(who)} changed this record while you were editing it. '
                f"Nothing was saved. Open the form again to see their change, then make yours.</p>"
                f'<a class="btn" id="reload" href="/e/{name}/{rid}/{op}">Open the form again</a>'
            )
            return self._send(409, self._page("Not saved", body, user=s["user"]))
        wb.flash[self._sid() or ""] = msg
        self._redirect(f"/e/{name}/{rid}")


def serve(
    profile: Profile,
    dsn: str,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    reader: Optional[str] = None,
    create: bool = True,
    admin_token: Optional[str] = "random",
) -> tuple[str, Workbench, Callable[[], None]]:
    """Start the workbench; returns (url, workbench, stop).

    ``admin_token`` guards the reset and state routes: ``"random"`` (the
    default) makes one, held in ``workbench.admin_token``; None leaves the
    routes to the caller's path blocking."""

    store = Store(profile, dsn)
    if create:
        store.create_schema(reader)
    wb = Workbench(profile, store)
    if admin_token != "random":
        wb.admin_token = admin_token
    handler = type("BoundHandler", (Handler,), {"wb": wb})
    httpd = ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    def stop() -> None:
        httpd.shutdown()
        httpd.server_close()

    return f"http://{host}:{httpd.server_address[1]}", wb, stop


def serve_env() -> tuple[str, Workbench, Callable[[], None]]:
    """``serve`` for an OpenEnv ``inprocess`` app: the profile, the admin
    connection and the read-only role come from ``WB_PROFILE``, ``WB_DSN``
    and ``WB_READER``."""

    import os

    return serve(
        load_profile(os.environ["WB_PROFILE"]),
        os.environ["WB_DSN"],
        reader=os.environ.get("WB_READER", "wb_reader"),
        # The environment blocks /__ paths in the agent's browser
        # (agent.blocked_paths) and posts its resets with WB_ADMIN_TOKEN.
        admin_token=os.environ.get("WB_ADMIN_TOKEN"),
    )


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("profile")
    ap.add_argument("--dsn", required=True)
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--reader", help="role to GRANT SELECT on every table")
    args = ap.parse_args(argv)
    url, _wb, _stop = serve(
        load_profile(args.profile), args.dsn, port=args.port, reader=args.reader
    )
    print(url, flush=True)
    threading.Event().wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
