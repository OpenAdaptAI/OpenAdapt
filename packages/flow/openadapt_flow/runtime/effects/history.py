"""Complete change feeds for a provisional write's final recheck.

A feed covers a whole declared resource type or table, including tombstones.
The checkpoint is read before the ordinary post-save checks, so no write
between their last read and the checkpoint can disappear into the baseline.
Only keyed event digests are persisted. Unknown coverage never confirms.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Literal, Optional
from urllib.parse import urljoin, urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from openadapt_flow.runtime.effects.effect import ValueExpr

HISTORY_SCHEMA = "openadapt.history-checkpoint/v1"
DETAIL = "openadapt:frame_history"
SQL_INVENTORY = "all-retained-events/v1"
_ID = re.compile(r"^[A-Za-z0-9\-.]{1,64}$")


class HistoryFeed(BaseModel):
    """One complete type/table feed, read independently of the writer.

    SQL queries return event_id, record_id, operation, author and occurred_at.
    The coverage query returns exactly one row: retained_since, through and
    event_count. Both queries run in one read-only repeatable-read snapshot.
    Its retention boundary/count must come from the source's CDC/audit
    coverage, independently of the event query (never MIN/COUNT of that
    query's possibly filtered result).
    """

    model_config = ConfigDict(extra="forbid")
    read: dict[str, Any]
    #: A server-controlled history-entry path, never client-authored data.
    author_path: Optional[str] = None
    author_server_set: bool = False
    dialect: Optional[Literal["sqlite", "mysql", "postgresql"]] = None
    coverage_query: Optional[str] = None

    @model_validator(mode="after")
    def _shape(self) -> "HistoryFeed":
        kind = self.read.get("kind")
        if kind not in ("fhir", "sql") or self.read.get("surfaces"):
            raise ValueError("history feeds require one FHIR or SQL read")
        if kind == "fhir":
            if self.read.get("search_params") or self.read.get("search_param_exprs"):
                raise ValueError("type-level history must not filter candidate records")
            if not _ID.fullmatch(str(self.read.get("resource_type") or "")):
                raise ValueError("history needs a plain resource type")
            if self.coverage_query is not None or self.dialect is not None:
                raise ValueError("SQL coverage settings apply only to SQL")
        elif self.dialect is None or not self.coverage_query:
            raise ValueError("SQL history needs dialect and coverage_query")
        if kind == "sql":
            driver = (
                "sqlite3"
                if self.read.get("sqlite_database")
                else self.read.get("sql_driver")
            )
            allowed = {
                "sqlite": {"sqlite3"},
                "mysql": {"pymysql", "MySQLdb", "mysql.connector"},
                "postgresql": {"psycopg", "psycopg2"},
            }
            if driver not in allowed.get(str(self.dialect), set()):
                raise ValueError(
                    "history dialect must match a supported read-only driver"
                )
            if self.read.get("sql_query_params"):
                raise ValueError("history must not filter by candidate parameters")
        if kind == "sql" and self.author_path not in (None, "author"):
            raise ValueError(
                "SQL history attribution uses its server-set author column"
            )
        if bool(self.author_path) != self.author_server_set:
            raise ValueError("history attribution requires a server-set author path")
        return self


class FrameHistory(BaseModel):
    """Setup-qualified whole-type/table feeds and the app's quiet window.

    Feed names are opaque contract scope names. ``surfaces`` maps every
    verifier surface to its feed. Additional feeds declare collateral scope.
    ``setup_evidence_sha256`` pins the sandbox observation used to choose W;
    evaluation cases must never choose it. Feeds must include deletions and
    every version, and the verifier identity must see the whole declared
    scope. These are qualification obligations, not inferred from HTTP 200.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    feeds: dict[str, HistoryFeed] = Field(min_length=1)
    surfaces: dict[str, str] = Field(min_length=1)
    quiet_s: float = Field(gt=0, allow_inf_nan=False)
    setup_evidence_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    self_actors: list[ValueExpr] = Field(alias="self", min_length=1)
    max_events: int = Field(default=100000, ge=1)

    @model_validator(mode="before")
    @classmethod
    def _self(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = dict(data)
            name = "self" if "self" in data else "self_actors"
            if isinstance(data.get(name), list):
                data[name] = [
                    {"literal": v} if isinstance(v, str) else v for v in data[name]
                ]
        return data

    @model_validator(mode="after")
    def _scope(self) -> "FrameHistory":
        if not set(self.surfaces.values()) <= set(self.feeds):
            raise ValueError("every history surface must name a declared feed")
        return self


def instant(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("history time is missing")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("history time has no timezone")
    return dt.astimezone(timezone.utc)


def _mac(key: bytes, value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def _path(row: Mapping[str, Any], path: str) -> Any:
    node: Any = row
    for part in path.split("."):
        if not isinstance(node, Mapping):
            return None
        node = node.get(part)
    return node


def _sql(
    feed: HistoryFeed, reader: Any, budget: int
) -> tuple[list[dict[str, Any]], datetime, datetime]:
    from openadapt_flow.runtime.effects.db_snapshot import _Session
    from openadapt_flow.runtime.effects.sql import assert_read_only_sql

    if reader.query_params:
        raise ValueError("history queries cannot depend on candidate parameters")
    query = assert_read_only_sql(str(feed.coverage_query))
    assert feed.dialect is not None
    session = _Session(reader._connect, feed.dialect)
    try:
        if feed.dialect == "sqlite":
            session.cur.execute("PRAGMA query_only = ON")
        session.begin()
        if feed.dialect == "mysql":
            engines = session.rows(
                "SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE'"
            )
            if any(str(row[0]).lower() != "innodb" for row in engines):
                raise ValueError(
                    "history coverage requires transactional source tables"
                )
        coverage = session.dicts(query, limit=2)
        rows = session.dicts(reader.query, limit=budget + 1)
    finally:
        session.close()
    if len(coverage) != 1 or len(rows) > budget:
        raise ValueError("history coverage or event budget is incomplete")
    cov = coverage[0]
    count = cov.get("event_count")
    if isinstance(count, bool) or not isinstance(count, int) or count != len(rows):
        raise ValueError("history event count disagrees with independent coverage")
    return (
        rows,
        instant(str(cov.get("retained_since"))),
        instant(str(cov.get("through"))),
    )


def _fhir(
    feed: HistoryFeed, reader: Any, since: str, budget: int
) -> tuple[list[dict[str, Any]], datetime, datetime]:
    """Read every page of type-level history, including resource-less deletes."""
    from openadapt_flow.runtime.effects.fhir import MAX_PAGES, _same_origin

    headers = reader._headers()
    if headers is None:
        raise ValueError("history authentication is unavailable")
    headers = {**headers, "Cache-Control": "no-cache, no-store"}
    session = reader._get_session()
    kwargs: dict[str, Any] = {"headers": headers, "timeout": reader.timeout_s}
    if reader._verify_per_request:
        kwargs["verify"] = reader.verify_tls
    if (type(session).__module__ or "").startswith("requests"):
        kwargs["allow_redirects"] = False
    url = f"{reader.base_url}/{reader.resource_type}/_history"
    params: Any = {"_since": since, "_count": "1000"}
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    total: Any = None
    through: Optional[datetime] = None
    event_upper: Optional[datetime] = None
    for page in range(MAX_PAGES):
        if url in seen or not _same_origin(url, reader.base_url):
            raise ValueError("history pagination is cyclic or crosses origins")
        seen.add(url)
        response = session.get(url, params=params, **kwargs)
        if response.status_code == 401 and reader.token_source is not None:
            reader.token_source.invalidate()
        if response.status_code != 200:
            raise ValueError("history read did not succeed")
        landed = str(getattr(response, "url", url))
        if not _same_origin(landed, reader.base_url):
            raise ValueError("history read redirected across origins")
        bundle = response.json()
        if (
            not isinstance(bundle, dict)
            or bundle.get("resourceType") != "Bundle"
            or bundle.get("type") != "history"
        ):
            raise ValueError("history read returned no history Bundle")
        if page == 0:
            total = bundle.get("total")
            if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                raise ValueError("history has no complete event count")
            if total > budget:
                raise ValueError("history exceeds its event budget")
            timestamp = bundle.get("timestamp")
            if timestamp:
                through = instant(timestamp)
                event_upper = through
            else:
                date = getattr(response, "headers", {}).get("Date")
                parsed = parsedate_to_datetime(date) if date else None
                if parsed is not None and parsed.tzinfo is None:
                    raise ValueError("history server date has no timezone")
                through = (
                    parsed.astimezone(timezone.utc) if parsed is not None else None
                )
                # HTTP Date has whole-second precision. Keep that lower
                # coverage frontier; versions within its last fractional
                # second are positive observations, not future coverage.
                event_upper = through + timedelta(seconds=1) if through else None
            if through is None:
                raise ValueError("history has no server coverage time")
        elif bundle.get("total") not in (None, total):
            raise ValueError("history count changed while paging")
        entries = bundle.get("entry", [])
        if not isinstance(entries, list):
            raise ValueError("history entries are malformed")
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("history entry is malformed")
            resource = entry.get("resource") or {}
            request = entry.get("request") or {}
            reply = entry.get("response") or {}
            if not all(isinstance(v, dict) for v in (resource, request, reply)):
                raise ValueError("history entry is malformed")
            method = request.get("method")
            if method == "GET" and isinstance(resource.get("meta"), dict):
                version = resource["meta"].get("versionId")
                expected = (
                    f"{reader.resource_type}/{resource.get('id')}/_history/{version}"
                )
                if target := request.get("url"):
                    if target == expected and version:
                        method = (
                            "PUT"  # a retained version, regardless of original verb
                        )
            if method not in ("POST", "PUT", "PATCH", "DELETE"):
                raise ValueError("history entry has no definite write operation")
            record = resource.get("id")
            target = request.get("url")
            if method == "DELETE":
                pieces = urlsplit(str(target or "")).path.strip("/").split("/")
                if len(pieces) != 2 or pieces[0] != reader.resource_type:
                    raise ValueError("history tombstone names no definite record")
                record = pieces[1]
            elif resource.get("resourceType") != reader.resource_type:
                raise ValueError("history entry names another resource type")
            if not isinstance(record, str) or not _ID.fullmatch(record):
                raise ValueError("history entry names no definite record")
            meta = resource.get("meta") or {}
            at = reply.get("lastModified") or meta.get("lastUpdated")
            if event_upper is None or instant(at) > event_upper:
                raise ValueError("history event is newer than its server response")
            version = meta.get("versionId") or reply.get("etag")
            if not version:
                # R4 tombstones need not carry a version id. Their complete
                # identity includes source, record, operation and server time.
                if method != "DELETE":
                    raise ValueError("history resource has no version")
                version = f"delete:{at}"
            rows.append(
                {
                    "event_id": [record, str(version)],
                    "record_id": record,
                    "operation": method,
                    "author": _path(entry, feed.author_path)
                    if feed.author_path
                    else None,
                    "occurred_at": at,
                    "resource": resource,
                    "record": reader._flatten(resource) if resource else None,
                    "contents_sha256": hashlib.sha256(
                        json.dumps(
                            entry, sort_keys=True, separators=(",", ":")
                        ).encode()
                    ).hexdigest(),
                }
            )
            if len(rows) > budget:
                raise ValueError("history exceeds its event budget")
        links = bundle.get("link", [])
        if not isinstance(links, list) or any(not isinstance(x, dict) for x in links):
            raise ValueError("history links are malformed")
        nexts = [x.get("url") for x in links if x.get("relation") == "next"]
        if len(nexts) > 1 or (nexts and not isinstance(nexts[0], str)):
            raise ValueError("history next link is malformed")
        if not nexts:
            break
        url = urljoin(url, nexts[0])
        params = None
    else:
        raise ValueError("history pagination is incomplete")
    if len(rows) != total:
        raise ValueError("history is truncated")
    assert through is not None
    return rows, instant(since), through


def read_history(
    spec: FrameHistory, since: str, build: Callable[[dict[str, Any]], Any], key: bytes
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Sealed digests plus private in-memory events and version bodies."""
    feeds: dict[str, Any] = {}
    raw: dict[str, dict[str, Any]] = {}
    for name, feed in spec.feeds.items():
        reader = build(feed.read)
        rows, retained, through = (
            _fhir(feed, reader, since, spec.max_events)
            if feed.read["kind"] == "fhir"
            else _sql(feed, reader, spec.max_events)
        )
        if through > datetime.now(timezone.utc):
            raise ValueError("history coverage frontier is in the future")
        if retained > instant(since) or through < instant(since):
            raise ValueError("history does not cover the requested interval")
        events: dict[str, str] = {}
        for row in rows:
            if any(
                row.get(k) in (None, "")
                for k in ("event_id", "record_id", "operation", "occurred_at")
            ):
                raise ValueError("history event has no identity, operation or time")
            when = instant(row["occurred_at"])
            if feed.read["kind"] == "sql" and when > through:
                raise ValueError("history event is newer than its coverage boundary")
            # Event time does not establish when an identity became visible.
            # In particular, SQL transaction-start timestamps may predate
            # this attempt even when the commit appears after its baseline.
            # Keep every returned identity; the existing budget bounds it.
            event = _mac(key, [name, row["event_id"]])
            if event in events:
                raise ValueError("history repeats an event identity")
            events[event] = _mac(key, row)
            raw[event] = {**row, "scope": name, "authoritative": feed.author_server_set}
        feeds[name] = {"through": through.isoformat(), "events": events}
        if feed.read["kind"] == "sql":
            # Older checkpoints omitted pre-since rows. They cannot be
            # silently reinterpreted as complete retained-event inventories.
            feeds[name]["inventory"] = SQL_INVENTORY
    return {"schema_version": HISTORY_SCHEMA, "since": since, "feeds": feeds}, raw


def checkpoint_problem(
    checkpoint: Any, spec: Optional[FrameHistory] = None
) -> Optional[str]:
    if (
        not isinstance(checkpoint, dict)
        or set(checkpoint) != {"schema_version", "since", "feeds"}
        or checkpoint.get("schema_version") != HISTORY_SCHEMA
    ):
        return "the history checkpoint is malformed"
    try:
        instant(checkpoint["since"])
        if not isinstance(checkpoint["feeds"], dict) or not checkpoint["feeds"]:
            raise ValueError()
        if spec is not None and set(checkpoint["feeds"]) != set(spec.feeds):
            raise ValueError()
        for name, feed in checkpoint["feeds"].items():
            keys = {"through", "events"}
            if "inventory" in feed:
                keys.add("inventory")
                if feed["inventory"] != SQL_INVENTORY:
                    raise ValueError()
            if set(feed) != keys or not isinstance(feed["events"], dict):
                raise ValueError()
            if spec is not None:
                kind = spec.feeds[name].read.get("kind")
                if kind not in ("sql", "fhir"):
                    raise ValueError()
                expected = SQL_INVENTORY if kind == "sql" else None
                if feed.get("inventory") != expected:
                    return "the history checkpoint lacks a complete retained-event inventory"
            if instant(feed["through"]) < instant(checkpoint["since"]):
                raise ValueError()
            if any(
                not re.fullmatch(r"[a-f0-9]{64}", value)
                for pair in feed["events"].items()
                for value in pair
            ):
                raise ValueError()
    except (ValueError, TypeError, KeyError, AttributeError):
        return "the history checkpoint is malformed"
    return None


def checkpoint_pair_problem(before: Any, now: Any, spec: FrameHistory) -> Optional[str]:
    """One coverage and attempt-window rule for every history consumer."""
    for checkpoint in (before, now):
        problem = checkpoint_problem(checkpoint, spec)
        if problem is not None:
            return problem
    if instant(before["since"]) != instant(now["since"]):
        return "history checkpoints name different attempt boundaries"
    return None


def judge_history(
    spec: FrameHistory,
    checkpoint: Mapping[str, Any],
    now: Mapping[str, Any],
    raw: Mapping[str, Any],
    params: Mapping[str, object],
    due: str,
    run_actor: str,
) -> Any:
    from openadapt_flow.runtime.effects.frame import FrameChange, FrameVerdict

    problem = checkpoint_pair_problem(checkpoint, now, spec)
    if problem is not None:
        return FrameVerdict("not_checked", problem)
    mine = {str(x.resolve(params) or "") for x in spec.self_actors}
    if "" in mine:
        return FrameVerdict("not_checked", "history self identity is unresolved")
    mine.add(run_actor)
    out = FrameVerdict(
        "held",
        "complete history contains no unexplained late write",
        attribution="history",
    )
    for scope, old in checkpoint["feeds"].items():
        fresh = now["feeds"][scope]
        if instant(fresh["through"]) < max(instant(due), instant(old["through"])):
            return FrameVerdict(
                "not_checked", "history does not cover the quiet window"
            )
        if any(fresh["events"].get(k) != v for k, v in old["events"].items()):
            return FrameVerdict("not_checked", "history disappeared or changed")
        for event in set(fresh["events"]) - set(old["events"]):
            row = raw[event]
            author = row.get("author")
            other = (
                row["authoritative"]
                and isinstance(author, str)
                and bool(author)
                and author not in mine
            )
            kinds: dict[str, Literal["added", "changed", "removed"]] = {
                "POST": "added",
                "INSERT": "added",
                "DELETE": "removed",
                "PUT": "changed",
                "PATCH": "changed",
                "UPDATE": "changed",
            }
            change = kinds.get(str(row["operation"]).upper())
            if change is None:
                return FrameVerdict("not_checked", "history write kind is unknown")
            item = FrameChange(
                scope,
                change,
                "other"
                if other
                else "self"
                if isinstance(author, str) and author in mine
                else "unattributed",
                audit_only=True,
            )
            (out.concurrent if other else out.collateral).append(item)
            out.records_compared += 1
    if out.collateral:
        out.status = "refuted"
        out.reason = "complete history contains unexplained late writes"
    return out


def judge_initial_history(
    spec: FrameHistory,
    before: Mapping[str, Any],
    now: Mapping[str, Any],
    raw: Mapping[str, Any],
    params: Mapping[str, object],
    run_actor: str,
    targets: set[tuple[str, str]],
) -> Any:
    """Every event since begin: one saved version per confirmed target.

    A correct latest snapshot cannot erase an extra intermediate mutation.
    Only the declared record's one write is removed from the late judge;
    additional versions or deletes remain collateral even on that record.
    """
    from openadapt_flow.runtime.effects.frame import FrameVerdict

    problem = checkpoint_pair_problem(before, now, spec)
    if problem is not None:
        return FrameVerdict("not_checked", problem)
    started = instant(before["since"])
    pruned = {
        **now,
        "feeds": {
            name: {**feed, "events": dict(feed["events"])}
            for name, feed in now["feeds"].items()
        },
    }
    used: set[tuple[str, str]] = set()
    remaining = dict(raw)
    for scope, feed in now["feeds"].items():
        old = before["feeds"].get(scope, {}).get("events", {})
        for event in sorted(set(feed["events"]) - set(old)):
            row = raw[event]
            target = (scope, str(row["record_id"]))
            if (
                target in targets
                and target not in used
                # Discovery after a checkpoint does not authorize a write
                # whose own event time predates this attempt's start.
                and instant(row["occurred_at"]) >= started
                and str(row["operation"]).upper()
                in ("POST", "PUT", "PATCH", "INSERT", "UPDATE")
            ):
                used.add(target)
                pruned["feeds"][scope]["events"].pop(event)
            elif target in targets:
                # Before the provisional check, another version of its
                # target is a conflicting write, even by a colleague.
                remaining[event] = {**row, "authoritative": False}
    if targets - used:
        return FrameVerdict(
            "not_checked", "a saved target has no corresponding history event"
        )
    boundary = min(
        instant(feed["through"]) for feed in before["feeds"].values()
    ).isoformat()
    return judge_history(spec, before, pruned, remaining, params, boundary, run_actor)
