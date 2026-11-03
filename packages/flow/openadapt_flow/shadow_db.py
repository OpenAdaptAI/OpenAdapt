"""Shadow mode for a client that writes to its database directly: a Windows
thick client, say, which no HTTP proxy can stand in front of. See
``docs/SHADOW_MODE.md``, "Database mode".

There is no proxy. The agent announces each action, before and after the
click that saves::

    POST /begin  {"action": "create_patient", "values": {...}}
    POST /end    {"action_id": 3, "claimed": "done"}

``begin`` reads the database before the action (``verifier.begin_write``,
the whole-database frame) and replies once it has. ``end`` has the action
checked as SQL mode checks a write (``verify_write``, then the contract's
recheck). The database is also watched between actions: the shadow keeps
its own chain of whole-database snapshots from start to stop, so a change
no announced action covers shows, with the app's audit (``audit_sql``)
saying whether the agent's own user made it.

By default nothing is refused. With ``guard`` on, ``begin`` refuses an
action (409) it can't check or that follows anything not CONFIRMED, and
``end`` waits for the verdict, so an agent that obeys stops at the first
write that isn't right.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
import traceback
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler
from typing import Any, Optional

from openadapt_flow.shadow import (
    _MISSING,
    CONFIRMED,
    REFUTED,
    UNCERTAIN,
    Episode,
    Route,
    Shadow,
    ShadowConfigError,
    Write,
    _dotted,
)

GAP = "between announced actions"


@dataclass
class Mark:
    """One read of the database for the watch between actions."""

    at: float
    snapshot: Any = None  # db_snapshot.Snapshot, or None when unreadable
    audit: Optional[set[str]] = None  # the agent's audit entry ids
    errors: list[str] = field(default_factory=list)


class DatabaseShadow(Shadow):
    """Database mode: announced actions, checked through the database frame."""

    proxy = False

    def __init__(
        self,
        *,
        routes: list[Route],
        actor_id: str = "agent",
        out: Any,
        listen: tuple[str, int] = ("127.0.0.1", 0),
        hold_ms: float = 30000.0,
        quiet_s: float = 5.0,
        receipts: bool = False,
        guard: bool = False,
        audit_sql: Optional[str] = None,
        max_action_s: float = 600.0,
        check_timeout_s: float = 120.0,
    ) -> None:
        frames = [r.contract.config.frame for r in routes]
        dbs = [f.database if f is not None else None for f in frames]
        if any(d is None for d in dbs):
            raise ShadowConfigError(
                "database mode needs frame.database in every action's contract: "
                "the whole database is what shows a write no action covers"
            )
        reads = {json.dumps([d.dialect, d.read], sort_keys=True) for d in dbs}
        if len(reads) != 1:
            raise ShadowConfigError(
                "every action's contract must read the same database "
                "(frame.database.dialect and read)"
            )
        names = [r.name for r in routes]
        if len(set(names)) != len(names):
            raise ShadowConfigError("two actions have the same name")
        if audit_sql is not None and "%(actor)s" not in audit_sql:
            raise ShadowConfigError(
                "audit_sql must select the agent's entries with %(actor)s"
            )
        super().__init__(
            upstream=None,
            mode="sql",
            routes=routes,
            actor_id=actor_id,
            out=out,
            listen=listen,
            hold_ms=hold_ms,
            recheck_s=None,
            quiet_s=quiet_s,
            receipts=receipts,
        )
        self.guard = guard
        self.audit_sql = audit_sql
        self.max_action_s = max_action_s
        self.check_timeout_s = check_timeout_s
        #: The contract whose frame the watch reads with (its allow-list,
        #: ignored columns and app profile).
        self._watch_cfg = routes[0].contract.config
        self._salt = secrets.token_bytes(32)
        self._gap_from: Optional[Mark] = None
        self._begun = 0
        self.gaps_watched = 0
        self.gaps_clean = 0
        self.refusals: list[dict[str, Any]] = []
        self._enders: list[threading.Thread] = []

    # -- lifecycle -------------------------------------------------------------------

    def start(self) -> "DatabaseShadow":
        self._gap_from = self._mark()
        super().start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self.server.shutdown()
        self.server.server_close()
        with self._lock:
            still = [e for e in self.episodes if e.in_flight]
        for ep in still:
            self._end(
                ep,
                "the shadow stopped before the agent announced the end",
                wait=True,
            )
        # An end's own read of the database must land before the last one.
        for t in list(self._enders):
            t.join(timeout=self.check_timeout_s)
        self.flush()
        with self._lock:
            start, self._gap_from = self._gap_from, None
        if start is not None:
            self._judge_gap(start, self._mark())
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _checker(self) -> None:
        while not self._stop.wait(0.25):
            try:
                with self._lock:
                    stale = [
                        e
                        for e in self.episodes
                        if e.in_flight
                        and e.writes
                        and time.time() - e.writes[0].started > self.max_action_s
                    ]
                for ep in stale:
                    self._end(
                        ep,
                        f"the agent announced no end within {self.max_action_s:.0f} s",
                    )
                if self.idle_s() >= self.quiet_s:
                    self._check_due()
                    self.run_rechecks()
            except Exception as exc:  # noqa: BLE001
                self._note_error(f"checker: {exc!r}")

    def target(self) -> str:
        db = self._watch_cfg.frame.database
        args = db.read.get("sql_connect_args") or {}
        where = db.read.get("sqlite_database") or "{}:{}/{}".format(
            args.get("host", "?"), args.get("port", "?"), args.get("database", "?")
        )
        return f"{db.dialect} database {where}"

    # -- the endpoints ---------------------------------------------------------------

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        shadow = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_: Any) -> None:
                pass

            def _reply(self, status: int, doc: dict[str, Any]) -> None:
                body = json.dumps(doc, default=str).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    doc = json.loads(self.rfile.read(n) or b"{}")
                    if not isinstance(doc, dict):
                        raise ValueError("the body is not a JSON object")
                except (ValueError, OSError) as exc:
                    self._reply(400, {"error": f"unreadable body: {exc}"})
                    return
                path = self.path.split("?")[0].rstrip("/")
                try:
                    if path == "/begin":
                        self._reply(*shadow.begin(doc))
                    elif path == "/end":
                        self._reply(*shadow.end(doc))
                    else:
                        self._reply(404, {"error": "POST /begin or /end"})
                except Exception as exc:  # noqa: BLE001 - never a silent pass
                    shadow._note_error(traceback.format_exc(limit=3))
                    self._reply(500, {"error": f"the shadow failed: {exc!r}"})

            def do_GET(self) -> None:
                if self.path.split("?")[0].rstrip("/") == "/status":
                    self._reply(200, shadow.status())
                else:
                    self._reply(404, {"error": "GET /status"})

        return Handler

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "actions": len(self.writes),
                "open": [e.id for e in self.episodes if e.in_flight],
                "halted": self._halt_reason() if self.guard else "",
            }

    def begin(self, doc: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Read the database before an action. Returns (status, reply)."""

        t0 = time.perf_counter()
        action = str(doc.get("action") or "")
        values = doc.get("values")
        if self.guard:
            why = self._guard_refusal(action, values)
            if why:
                return self._refuse(action, why)
        # The action before must be checked first: its window is open until
        # the database is read after it.
        self._wait_checked(self.check_timeout_s if self.guard else self.hold_s)
        with self._lock:
            self._seq += 1
            self._begun += 1
            write = Write(self._seq, "ACTION", action, started=time.time())
            self.writes.append(write)
            self._last_write = write.started
            earlier = [e for e in self.episodes if e.kind != "gap" and not e.checked]
            ep = self._action_episode(action, values)
            ep.writes.append(write)
            ep.in_flight = 1
            if earlier:
                note = (
                    "another announced action was still open or unchecked "
                    f"(check {', '.join(str(e.id) for e in earlier)}): the "
                    "database can't say which change belongs to which"
                )
                ep.baseline_note = ep.baseline_note or note
                for e in earlier:
                    e.evidence["overlapped_by"] = ep.id
            gap_from, self._gap_from = self._gap_from, None
        if ep.baseline is not None:
            self._wait_baseline(ep)
        # Read once more for the watch: what moved since the last action
        # ended is a change no announced action covers. Read after the
        # baseline, so nothing falls between the two windows.
        mark = self._mark()
        gap = self._judge_gap(gap_from, mark) if gap_from is not None else None
        write.held_ms = (time.perf_counter() - t0) * 1000
        write.status = 200
        if self.guard:
            why = ""
            if earlier or ep.kind != "sql" or ep.baseline_note:
                why = ep.baseline_note or "the action can't be checked"
            elif ep.baseline is not None and ep.baseline.exception(timeout=0):
                why = f"the database couldn't be read first: {ep.baseline.exception()}"
            elif gap is not None:
                why = f"{gap.verdict} {GAP}: " + "; ".join(gap.reasons)
            if why:
                self._end(ep, "refused: " + why)
                return self._refuse(action, why, write=write)
        reply = {
            "action_id": ep.id,
            "checked": ep.kind == "sql" and not ep.baseline_note,
        }
        if ep.baseline_note:
            reply["note"] = ep.baseline_note
        return 200, reply

    def end(self, doc: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """The action is done. Its check starts now; with ``guard`` the
        reply waits for the verdict."""

        try:
            ep_id = int(doc.get("action_id"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 400, {"error": "end needs the action_id begin gave"}
        with self._lock:
            ep = next((e for e in self.episodes if e.id == ep_id), None)
            if ep is None or ep.kind == "gap":
                return 404, {"error": f"no action {ep_id}"}
            if not ep.in_flight:
                return 409, {"error": f"action {ep_id} already ended"}
            ep.evidence["claimed"] = str(doc.get("claimed") or "done")
        self._end(ep, "")
        if not self.guard:
            return 202, {"action_id": ep_id}
        deadline = time.time() + self.check_timeout_s
        while not ep.checked and time.time() < deadline:
            time.sleep(0.05)
        with self._lock:
            verdict = ep.verdict if ep.checked else "PENDING"
            return 200, {
                "action_id": ep_id,
                "verdict": verdict,
                "reasons": list(ep.reasons),
                "halted": self._halt_reason(),
            }

    # -- actions ----------------------------------------------------------------------

    def _action_episode(self, action: str, values: Any) -> Episode:
        route = next((r for r in self.routes if r.name == action), None)
        if route is None:
            return self._unchecked(
                action or "(no action)", f"no action named {action!r}"
            )
        if not isinstance(values, dict):
            return self._unchecked(route.name, "begin gave no values object")
        params = {
            p: where["literal"] if isinstance(where, dict) else _dotted(values, where)
            for p, where in route.params.items()
        }
        missing = sorted(p for p, v in params.items() if v is _MISSING)
        if missing:
            return self._unchecked(
                route.name, "the action doesn't give " + ", ".join(missing)
            )
        ep = self._new("sql", route.name, contract=route.contract, params=params)
        ep.baseline = self._pool.submit(self._sql_baseline, route.contract, params)
        return ep

    def _end(self, ep: Episode, why: str, *, wait: bool = False) -> None:
        """Close an action and check it (``why``: it didn't end normally;
        ``wait``: check it before returning)."""

        with self._lock:
            if not ep.in_flight:
                return
            ep.in_flight = 0
            ep.last_end = time.time()
            self._last_write = max(self._last_write, ep.last_end)
            if why:
                ep.baseline_note = ep.baseline_note or why
                ep.writes[-1].error = why
            gen = self._begun
        if wait:
            self._after_end(gen)
        else:
            t = threading.Thread(target=self._after_end, args=(gen,), daemon=True)
            with self._lock:
                self._enders.append(t)
            t.start()

    def _after_end(self, gen: int) -> None:
        # The watch reads first, then the check: a change between the two
        # reads is seen by both, never by neither.
        mark = self._mark()
        with self._lock:
            if self._begun == gen and not any(e.in_flight for e in self.episodes):
                self._gap_from = mark
        try:
            self._check_due()
        except Exception as exc:  # noqa: BLE001
            self._note_error(f"check after end: {exc!r}")

    def _check(self, ep: Episode) -> None:
        super()._check(ep)
        if ep.kind != "sql":
            return
        if ep.evidence.get("overlapped_by") and ep.verdict != UNCERTAIN:
            ep.reasons.insert(
                0,
                f"action {ep.evidence['overlapped_by']} began before this one was "
                f"checked; the check alone said {ep.verdict}",
            )
            ep.verdict = UNCERTAIN
        result = ep.result
        claimed = ep.evidence.get("claimed", "done")
        if (
            claimed != "done"
            and ep.verdict != CONFIRMED
            and result is not None
            and result.observed == "absent"
        ):
            # The agent said it didn't finish, and nothing of it landed:
            # nothing landed wrong, and nothing is there to confirm.
            ep.verdict = UNCERTAIN
            ep.reasons.insert(
                0, f"the agent said {claimed!r} and nothing of the action landed"
            )

    def _wait_checked(self, wait_s: float) -> None:
        deadline = time.time() + wait_s
        while time.time() < deadline:
            with self._lock:
                if all(
                    e.checked
                    for e in self.episodes
                    if e.kind != "gap" and not e.in_flight
                ):
                    return
            time.sleep(0.05)

    def results(self) -> dict[str, Any]:
        doc = super().results()
        with self._lock:
            doc["watch"] = {
                "gaps_watched": self.gaps_watched,
                "gaps_clean": self.gaps_clean,
                "audit": bool(self.audit_sql),
            }
            doc["guard"] = self.guard
            doc["refusals"] = list(self.refusals)
        return doc

    # -- the guard --------------------------------------------------------------------

    def _halt_reason(self) -> str:
        for e in self.episodes:
            if not e.checked:
                continue
            late = e.late_verdict if e.late_verdict in (REFUTED, UNCERTAIN) else ""
            if e.verdict != CONFIRMED or late:
                what = e.name if e.kind != "gap" else GAP
                return (
                    f"check {e.id} ({what}) is {late or e.verdict}: a person "
                    "reviews it before the agent writes again"
                )
        return ""

    def _guard_refusal(self, action: str, values: Any) -> str:
        with self._lock:
            halted = self._halt_reason()
            if halted:
                return halted
            if any(e.in_flight for e in self.episodes):
                return "another announced action is still open"
        if not any(r.name == action for r in self.routes):
            return f"no action named {action!r}"
        return ""

    def _refuse(
        self, action: str, why: str, *, write: Optional[Write] = None
    ) -> tuple[int, dict[str, Any]]:
        with self._lock:
            self.refusals.append({"action": action, "why": why, "at": time.time()})
            if write is not None:
                write.status = 409
        return 409, {"refused": why, "action": action}

    # -- the watch between actions ----------------------------------------------------

    def _mark(self) -> Mark:
        from openadapt_flow.verify_run import _db_connect, _read_database

        mark = Mark(at=time.time())
        mark.snapshot = _read_database(self._watch_cfg, self._salt, errors=mark.errors)
        if self.audit_sql:
            try:
                conn = _db_connect(self._watch_cfg.frame.database.read)()
                try:
                    cur = conn.cursor()
                    sql = self.audit_sql
                    if self._watch_cfg.frame.database.dialect == "sqlite":
                        sql = sql.replace("%(actor)s", ":actor")
                    cur.execute(sql, {"actor": self.actor_id})
                    mark.audit = {str(row[0]) for row in cur.fetchall()}
                finally:
                    conn.close()
            except Exception as exc:  # noqa: BLE001 - unreadable, never clean
                mark.errors.append(f"the audit couldn't be read: {exc}")
        return mark

    def _judge_gap(self, start: Mark, end: Mark) -> Optional[Episode]:
        """Judge what moved between two watch reads. Returns the finding,
        or None when nothing moved."""

        from openadapt_flow.runtime.effects.db_snapshot import diff, mac_key

        with self._lock:
            self.gaps_watched += 1
        reasons: list[str] = []
        verdict = ""
        moved: dict[str, Any] = {}
        if start.snapshot is None or end.snapshot is None:
            verdict = UNCERTAIN
            reasons.append(
                "the database couldn't be read "
                + GAP
                + ": "
                + "; ".join(start.errors + end.errors)
            )
        elif not (start.snapshot.code_visible and end.snapshot.code_visible):
            # As the frame: a trigger planted between the reads could pass.
            verdict = UNCERTAIN
            reasons.append(
                "the read account can't see the database's triggers (on MySQL "
                f"it needs TRIGGER), so a change {GAP} can't be ruled out"
            )
        else:
            moved = diff(start.snapshot, end.snapshot, key=mac_key(self._salt))
        new: set[str] = set()
        if self.audit_sql:
            if start.audit is None or end.audit is None:
                if moved or not verdict:
                    verdict = verdict or (UNCERTAIN if moved else "")
                    if moved:
                        reasons.append(
                            "the audit couldn't be read, so who changed it is unknown"
                        )
            else:
                new = end.audit - start.audit
                gone = start.audit - end.audit
                if new:
                    verdict = REFUTED
                    reasons.insert(
                        0,
                        f"the agent's user wrote {GAP}: {len(new)} audit "
                        "entr" + ("y" if len(new) == 1 else "ies") + " no announced "
                        "action covers",
                    )
                if gone and verdict != REFUTED:
                    verdict = UNCERTAIN
                    reasons.append(
                        f"{len(gone)} of the agent's audit entries disappeared {GAP}"
                    )
        if moved and verdict != REFUTED:
            verdict = UNCERTAIN
            who = (
                "the audit names no write by the agent's user, but not every "
                "change is audited"
                if self.audit_sql and end.audit is not None and start.audit is not None
                else "who changed it wasn't checked"
            )
            reasons.append(f"the database changed {GAP}; {who}")
        if not verdict:
            with self._lock:
                self.gaps_clean += 1
            return None
        tables = {
            name: {
                "added": d.added,
                "removed": d.removed,
                "changed": d.changed,
                "columns": list(d.changed_columns),
                "unread": list(d.unread),
            }
            for name, d in moved.items()
        }
        if tables:
            reasons.append(
                "moved: "
                + ", ".join(
                    f"{n} (+{t['added']} -{t['removed']} ~{t['changed']})"
                    + (f" {', '.join(t['unread'])}" if t["unread"] else "")
                    for n, t in sorted(tables.items())
                )
            )
        with self._lock:
            ep = self._new("gap", GAP)
            ep.verdict, ep.reasons = verdict, reasons
            ep.checked, ep.checked_at = True, time.time()
            ep.evidence = {
                "from": _iso(start.at),
                "until": _iso(end.at),
                "tables": tables,
                "agent_audit_entries": len(new),
            }
        return ep


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f"{t % 1:.3f}"[1:] + "Z"
