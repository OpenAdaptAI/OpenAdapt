"""Read a system's own event or audit log, described as data.

API and SaaS systems give no database to diff, so a write can't be checked
against the whole system. What they do give is the record itself and, often,
a log of what each caller changed. This module reads that log. Which log,
where its fields are and how it pages are all in the tx file
(``systems[].audit``); no code here names an app. Three common shapes are
covered by the same reader:

* a FHIR ``AuditEvent`` search (a Bundle, ``link[relation=next]`` paging);
* an event stream (``data`` plus ``has_more``, paged by the last item's id);
* a REST activity feed (a plain list, paged by page number).

A *cursor* is taken before a write: the newest event's server time and the
ids at that time. After the write, :meth:`AuditReader.after` reads newest
first until it passes the cursor, so the window is bounded by the server's own
clock and the local clock never decides what is in it. Each event is
normalized to ``(id, at, actor, resource, action)`` and classified as
``create``, ``update``, ``delete``, ``read`` or ``unknown``.

The honest scope: a log shows what it records. A system whose log omits an
operation (many activity feeds don't record edits) can't show a collateral
edit of that kind; :attr:`AuditSource.logs` declares what the log records, and
the result says what was not checked.
"""

from __future__ import annotations

import datetime as _dt
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

Op = Literal["create", "update", "delete"]
Kind = Literal["create", "update", "delete", "read", "unknown"]
_ALL_OPS: tuple[Op, ...] = ("create", "update", "delete")


class AuditError(RuntimeError):
    """The log could not be read to the cursor. Nothing is known about the window."""


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid")


# -- paths ---------------------------------------------------------------------

_SELECT = re.compile(r"^([A-Za-z0-9_$-]+)\[([A-Za-z0-9_-]+)=([^\]]*)\]$")


def pick(value: Any, path: str) -> list[Any]:
    """Every value at a dotted ``path``.

    ``*`` fans out over a list, an integer indexes one (``-1`` is the last),
    and ``link[relation=next]`` keeps the list items whose ``relation`` is
    ``next``. An empty path is the value itself.
    """
    current = [value]
    for part in [p for p in path.split(".") if p] if path else []:
        out: list[Any] = []
        sel = _SELECT.match(part)
        for item in current:
            if sel:
                name, key, want = sel.groups()
                got = item.get(name) if isinstance(item, Mapping) else None
                if isinstance(got, list):
                    out += [x for x in got if isinstance(x, Mapping) and str(x.get(key)) == want]
            elif part == "*":
                if isinstance(item, list):
                    out += item
            elif re.fullmatch(r"-?\d+", part) and isinstance(item, list):
                i = int(part)
                if -len(item) <= i < len(item):
                    out.append(item[i])
            elif isinstance(item, Mapping) and part in item:
                out.append(item[part])
        current = out
    return [x for x in current if x is not None]


def first(value: Any, path: str) -> Any:
    got = pick(value, path)
    return got[0] if got else None


class Part(_Closed):
    path: str
    #: Keep only group 1 of this pattern (``^(\\d+)\\|`` takes ``12`` from ``12|title``).
    regex: Optional[str] = None


class FieldSpec(_Closed):
    """Where one event field is: one path, or several joined by ``sep``.

    A path that fans out (``agent.*.who.reference``) gives every value, so an
    event can name more than one actor or resource.
    """

    parts: list[Part] = Field(min_length=1)
    sep: str = ""

    @model_validator(mode="before")
    @classmethod
    def _short(cls, value: Any) -> Any:
        if isinstance(value, str):
            return {"parts": [{"path": value}]}
        if isinstance(value, Mapping) and "path" in value:
            return {"parts": [dict(value)]}
        return value

    def values(self, item: Any) -> list[str]:
        columns: list[list[str]] = []
        for part in self.parts:
            got = []
            for raw in pick(item, part.path):
                text = str(raw)
                if part.regex:
                    m = re.search(part.regex, text)
                    if not m:
                        continue
                    text = m.group(1)
                got.append(text)
            if not got:
                return []
            columns.append(got)
        if len(columns) == 1:
            return columns[0]
        # Several parts: one value per combination (in practice each part has one).
        out = [""]
        for i, col in enumerate(columns):
            out = [o + (self.sep if i else "") + c for o in out for c in col]
        return out


class Fields(_Closed):
    id: FieldSpec
    at: FieldSpec
    actor: FieldSpec
    resource: FieldSpec
    action: FieldSpec


class Paging(_Closed):
    #: ``link``: ``path`` is the next page's URL. ``cursor``: send the last
    #: item's id as ``param`` while ``has_more`` is true. ``page``: count
    #: ``param`` up from 1 until a page comes back short or empty.
    kind: Literal["link", "cursor", "page", "none"] = "none"
    path: Optional[str] = None
    param: Optional[str] = None
    has_more: Optional[str] = None
    size_param: Optional[str] = None
    size: int = Field(default=50, ge=1, le=1000)


class Actor(_Closed):
    """Who this token is in the log.

    ``path`` + ``field``: ask the system (a "who am I" read). ``value_env``:
    an environment variable names it. ``any``: the log names no caller, so
    every event that came through the API counts (an event stream for one
    account); ``field`` on the event must then be non-empty.
    """

    path: Optional[str] = None
    field: Optional[str] = None
    value_env: Optional[str] = None
    any: bool = False

    @model_validator(mode="after")
    def _check(self) -> "Actor":
        if not (self.path and self.field) and not self.value_env and not self.any:
            raise ValueError("audit.actor: give path and field, value_env, or any")
        return self


class Lag(_Closed):
    """How long the log may trail the write. Waiting for it is the retried class."""

    attempts: int = Field(default=12, ge=1, le=200)
    wait_s: float = Field(default=0.5, ge=0, le=60)
    #: For a write the log doesn't record (so its own event can't show the
    #: log has caught up): wait this long once before reading the window.
    settle_s: float = Field(default=0.0, ge=0, le=60)


class AuditSource(_Closed):
    """``systems[].audit``: the system's own log of changes."""

    #: A label for people (``fhir_audit_event``, ``event_stream``, ``activity_feed``).
    pattern: str = Field(default="activity_feed", max_length=40)
    base_url: Optional[str] = None
    base_url_env: Optional[str] = None
    #: The list read; ``{actor}`` in the path or a param is the actor's id.
    path: str
    params: dict[str, str] = Field(default_factory=dict)
    #: Where the events are in a page (``entry.*.resource``, ``data``; empty
    #: for a bare list).
    items: str = ""
    paging: Paging = Field(default_factory=Paging)
    fields: Fields
    #: Server action names per kind. Anything else is ``unknown``: logged,
    #: never a stop by itself.
    actions: dict[Kind, list[str]] = Field(default_factory=dict)
    #: The writes this log records. A write it doesn't record can't be
    #: confirmed in it, and a collateral write of that kind can't be seen.
    logs: list[Op] = Field(default_factory=lambda: list(_ALL_OPS))
    actor: Actor
    lag: Lag = Field(default_factory=Lag)
    #: Pages to read, newest first, before giving up on reaching the cursor.
    #: The list must be served newest first.
    max_pages: int = Field(default=40, ge=1, le=10_000)
    #: The log may legitimately hold no events yet (a new account). Otherwise
    #: an empty log is read as unreadable.
    may_be_empty: bool = False
    #: How far out of request order the log's server times can be. The cursor
    #: keeps the ids of every event this close to the newest one, and a window
    #: reads back this far before the cursor. An event stamped more than this
    #: before an event written earlier is outside the window.
    skew_s: float = Field(default=2.0, ge=0, le=600)


class ScopedCollection(_Closed):
    """``collections[].scoped``: resource-scoped verification of one collection."""

    #: How a record of this collection appears in the log's ``resource`` field
    #: (``Patient/{id}``). Placeholders come from the write's fields, path
    #: values and ``id``.
    audit_ref: str
    #: A create's marker: merged into the body, with ``{marker}`` filled with
    #: a value derived from the idempotency key.
    marker: Optional[dict[str, Any]] = None
    #: Search for records holding a marker (``/Patient?identifier=urn:x|{marker}``)
    #: and where the matches are in its answer.
    search: Optional[str] = None
    search_items: str = ""
    #: A server-side guard on create, as one header (FHIR ``If-None-Exist``).
    condition_header: Optional[dict[str, str]] = None
    #: Fields that name the record a create makes (an issue's title, a
    #: patient's identifier). The create's idempotency slot is derived from
    #: them, so a regenerated payload for the same record gets the same key
    #: and is refused if it differs. Without them, a task's creates are slots
    #: ``1``, ``2``... and the caller passes ``slot``.
    identity: list[str] = Field(default_factory=list)
    #: How write bodies go out: JSON, or a form (``a[b]=c`` for nesting).
    encoding: Literal["json", "form"] = "json"
    #: Fixed fields of every create body (FHIR ``resourceType``).
    create_body: dict[str, Any] = Field(default_factory=dict)
    #: Paths the server sets on any write, left out of the comparison (globs:
    #: ``meta.*``, ``updated_at``).
    volatile: list[str] = Field(default_factory=list)
    #: Per field: how a value reads back (``null_empty``, ``casefold``...).
    codecs: dict[str, str] = Field(default_factory=dict)
    #: An update shows in the log only when it changes one of these fields
    #: (a feed that records a close but not a title edit: ``[state]``).
    #: Unset: every update the source ``logs`` shows.
    logged_fields: Optional[list[str]] = None
    readback_attempts: int = Field(default=6, ge=1, le=100)
    readback_wait_s: float = Field(default=0.5, ge=0, le=30)


# -- events --------------------------------------------------------------------


def server_time(value: Any) -> Optional[float]:
    """Seconds since the epoch for an ISO time or an epoch number."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) / (1000.0 if value > 1e11 else 1.0)
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return server_time(float(text))
    text = text.replace("Z", "+00:00")
    try:
        at = _dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=_dt.timezone.utc)
    return at.timestamp()


@dataclass(frozen=True)
class Event:
    id: str
    at: float
    actors: tuple[str, ...]
    resources: tuple[str, ...]
    action: str
    kind: Kind


@dataclass
class Cursor:
    at: Optional[float]
    ids: frozenset[str] = frozenset()

    def to_json(self) -> dict[str, Any]:
        return {"at": self.at, "ids": sorted(self.ids)}

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> "Cursor":
        return cls(at=raw.get("at"), ids=frozenset(raw.get("ids") or ()))


@dataclass
class Window:
    """The events after a cursor, by this actor."""

    events: list[Event] = field(default_factory=list)
    pages: int = 0
    other_actors: int = 0


class AuditReader:
    """One audit source, read over an adapter's HTTP client."""

    def __init__(self, source: AuditSource, http: Any) -> None:
        self.source = source
        self.http = http
        self._actor: Optional[str] = None
        base = source.base_url or (os.environ.get(source.base_url_env, "") if source.base_url_env else "")
        self.base = base.rstrip("/")

    # who -------------------------------------------------------------------

    def actor(self) -> Optional[str]:
        """This token's id in the log; ``None`` when the log names no caller."""
        a = self.source.actor
        if a.any:
            return None
        if self._actor is None:
            if a.value_env:
                self._actor = os.environ.get(a.value_env, "")
            else:
                got = self._get(str(a.path), {})
                self._actor = str(first(got, str(a.field)) or "")
            if not self._actor:
                raise AuditError("the log's actor for this token is unknown")
        return self._actor

    def _mine(self, event: Event) -> bool:
        if self.source.actor.any:
            return bool(event.actors)
        return self.actor() in event.actors

    # reads -----------------------------------------------------------------

    def _url(self, path: str) -> str:
        if path.startswith(("http://", "https://")) or not self.base:
            return path
        return self.base + path

    def _fill(self, text: str) -> str:
        if "{actor}" in text:
            return text.replace("{actor}", self.actor() or "")
        return text

    def _get(self, path: str, params: Mapping[str, Any]) -> Any:
        try:
            resp = self.http.request("GET", self._url(path), params=dict(params) or None)
        except Exception as exc:  # noqa: BLE001 - transport: the window is unknown
            raise AuditError(f"the log read failed: {type(exc).__name__}") from exc
        if resp.status_code >= 400:
            raise AuditError(f"the log read returned {resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:
            raise AuditError("the log read was not JSON") from exc

    def classify(self, action: str) -> Kind:
        for kind, names in self.source.actions.items():
            if action in names:
                return kind
        return "unknown"

    def _event(self, item: Any) -> Optional[Event]:
        f = self.source.fields
        ids = f.id.values(item)
        ats = f.at.values(item)
        at = server_time(ats[0]) if ats else None
        if not ids or at is None:
            return None
        actions = f.action.values(item)
        action = actions[0] if actions else ""
        return Event(
            id=ids[0],
            at=at,
            actors=tuple(f.actor.values(item)),
            resources=tuple(f.resource.values(item)),
            action=action,
            kind=self.classify(action),
        )

    def pages(self) -> Any:
        """Pages of events, newest first."""
        s = self.source
        params: dict[str, Any] = {k: self._fill(v) for k, v in s.params.items()}
        path: Optional[str] = self._fill(s.path)
        page_no = 1
        for _ in range(s.max_pages):
            assert path is not None
            q: dict[str, Any] = dict(params)
            if s.paging.kind == "page":
                q[str(s.paging.param)] = page_no
            if s.paging.size_param:
                q[s.paging.size_param] = s.paging.size
            got = self._get(path, q)
            raw = pick(got, s.items) if s.items else (got if isinstance(got, list) else [])
            if s.items and len(raw) == 1 and isinstance(raw[0], list):
                raw = raw[0]
            items = [e for e in (self._event(x) for x in raw) if e is not None]
            yield items
            if s.paging.kind == "link":
                nxt = first(got, str(s.paging.path))
                if not nxt:
                    return
                path, params = str(nxt), {}
            elif s.paging.kind == "cursor":
                if s.paging.has_more and not first(got, s.paging.has_more):
                    return
                if not raw:
                    return
                last = first(raw[-1], s.fields.id.parts[0].path)
                params = {**params, str(s.paging.param): last}
            elif s.paging.kind == "page":
                if len(raw) < s.paging.size or not raw:
                    return
                page_no += 1
            else:
                return
        raise AuditError("the log did not reach the cursor within max_pages")

    def cursor(self, sleep: Any = time.sleep) -> Cursor:
        """The newest event's server time, and the ids of every event within
        ``skew_s`` of it.

        Server times are not always in request order (an event can be stamped
        after one that was written later), so the window is placed by ids, and
        time only bounds how far back to look.

        An empty first page is read again: a log that is empty for a moment
        (a search that came back empty) would otherwise make every older event
        look new. Still empty after the lag budget, the log is unreadable,
        unless the source says it may be empty.
        """
        skew = self.source.skew_s
        for _ in range(self.source.lag.attempts):
            top: Optional[float] = None
            ids: set[str] = set()
            for items in self.pages():
                if not items:
                    break
                if top is None:
                    top = max(e.at for e in items)
                ids.update(e.id for e in items if e.at >= top - skew)
                if any(e.at < top - skew for e in items):
                    break
            if top is not None:
                return Cursor(at=top, ids=frozenset(ids))
            if self.source.may_be_empty:
                return Cursor(at=None)
            sleep(self.source.lag.wait_s)
        raise AuditError("the log returned no events; the window can't be placed")

    def after(self, cursor: Cursor) -> Window:
        """Every event after ``cursor`` by this actor, read to the cursor."""
        win = Window()
        seen: set[str] = set()
        reached = False
        for items in self.pages():
            win.pages += 1
            older = False
            for e in items:
                if cursor.at is not None and e.at < cursor.at - self.source.skew_s:
                    older = True
                    continue
                if e.id in cursor.ids:
                    reached = True
                    continue
                if e.id in seen:
                    continue
                seen.add(e.id)
                if self._mine(e):
                    win.events.append(e)
                else:
                    win.other_actors += 1
            if older:
                return win
            if not items:
                break
        if cursor.at is not None and not reached:
            # The cursor's own events are in the log, so a read that ends
            # without reaching any of them is incomplete.
            raise AuditError("the log ended before the cursor")
        return win


# -- the window's verdict --------------------------------------------------------


@dataclass
class Audit:
    """What the log says about one write."""

    own: list[Event]
    collateral: list[Event]
    same_collection_creates: list[Event]
    unknown: list[Event]
    caught_up: bool
    attempts: int
    pages: int
    logged_ops: tuple[str, ...]

    def summary(self) -> dict[str, Any]:
        return {
            "own_events": len(self.own),
            "collateral_events": len(self.collateral),
            "same_collection_creates": len(self.same_collection_creates),
            "unknown_events": len(self.unknown),
            "unknown_actions": sorted({e.action for e in self.unknown})[:20],
            "caught_up": self.caught_up,
            "attempts": self.attempts,
            "pages": self.pages,
            "logged_ops": list(self.logged_ops),
        }


def check_window(
    reader: AuditReader,
    cursor: Cursor,
    *,
    op: Op,
    target: str,
    collection_prefix: Optional[str],
    expect_own: int = 1,
    logged: Optional[bool] = None,
    sleep: Any = time.sleep,
) -> Audit:
    """Read the window until the write's own event shows (or the lag runs out).

    ``target`` is the record's ``audit_ref``. A mutation of another resource
    by the same actor is collateral; a create in the same collection is
    reported apart, so the caller can tell a duplicate from a collateral
    write. Reads are ignored; unknown actions are kept for the log.
    """
    src = reader.source
    logged = op in src.logs if logged is None else logged
    attempts = 0
    win = Window()
    if not logged and src.lag.settle_s:
        sleep(src.lag.settle_s)
    error: Optional[AuditError] = None
    for attempts in range(1, src.lag.attempts + 1):
        try:
            win = reader.after(cursor)
        except AuditError as exc:
            # A read that didn't reach the cursor: timing, read again.
            error = exc
            sleep(src.lag.wait_s)
            continue
        error = None
        own = [e for e in win.events if target in e.resources and e.kind == op]
        if not logged or len(own) >= expect_own:
            break
        sleep(src.lag.wait_s)
    if error is not None:
        raise error
    own, collateral, creates, unknown = [], [], [], []
    for e in win.events:
        if e.kind == "read":
            continue
        if e.kind == "unknown":
            unknown.append(e)
            continue
        if target in e.resources:
            own.append(e)
            continue
        if (
            e.kind == "create"
            and collection_prefix
            and any(r.startswith(collection_prefix) for r in e.resources)
        ):
            creates.append(e)
        else:
            collateral.append(e)
    caught = (not logged) or sum(1 for e in own if e.kind == op) >= expect_own
    return Audit(
        own=own,
        collateral=collateral,
        same_collection_creates=creates,
        unknown=unknown,
        caught_up=caught,
        attempts=attempts,
        pages=win.pages,
        logged_ops=tuple(src.logs),
    )


def fill(template: str, values: Mapping[str, Any]) -> str:
    """``{name}`` placeholders from ``values``; a missing one is an error."""

    def one(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in values or values[name] in (None, ""):
            raise KeyError(f"the template needs {name}")
        return str(values[name])

    return re.sub(r"\{([a-z_][a-z0-9_]*)\}", one, template)


def fill_json(value: Any, values: Mapping[str, Any]) -> Any:
    if isinstance(value, str):
        return fill(value, values)
    if isinstance(value, list):
        return [fill_json(v, values) for v in value]
    if isinstance(value, Mapping):
        return {k: fill_json(v, values) for k, v in value.items()}
    return value


def prefix_of(audit_ref: str) -> Optional[str]:
    """The fixed text before the first placeholder (``Patient/`` of ``Patient/{id}``)."""
    head = audit_ref.split("{", 1)[0]
    return head or None


def kinds(events: Sequence[Event]) -> dict[str, int]:
    out: dict[str, int] = {}
    for e in events:
        out[e.kind] = out.get(e.kind, 0) + 1
    return out
