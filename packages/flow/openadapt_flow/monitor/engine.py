"""Domain-agnostic read-only CDC monitor engine.

Turns any pollable snapshot source (read-only SQLite query, HTTP JSON endpoint,
or DOM/accessibility extractor callback) into a deterministic, typed stream of
`record.created`, `record.updated`, `record.deleted`, and fail-closed
`monitor.error` events.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Protocol, Sequence

from openadapt_flow.monitor.sinks import EventSink, validate_http_endpoint

EventType = Literal["record.created", "record.updated", "record.deleted", "monitor.error"]


@dataclass(frozen=True)
class ChangeEvent:
    """Single typed state-change or fail-closed error event."""

    event_id: str
    monitor_name: str
    event_type: EventType
    timestamp: str
    record_key: str | None = None
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    changed_fields: tuple[str, ...] = ()
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "monitor_name": self.monitor_name,
            "event_type": self.event_type,
            "timestamp": self.timestamp,
            "record_key": self.record_key,
            "before": self.before,
            "after": self.after,
            "changed_fields": list(self.changed_fields),
            "error": self.error,
        }


@dataclass(frozen=True)
class MonitorTickResult:
    """Outcome of a single MonitorEngine.tick() poll."""

    ok: bool
    snapshot_count: int
    events: tuple[ChangeEvent, ...]
    error: str | None = None


class SnapshotSource(Protocol):
    """Strictly read-only source returning the current list of record mappings."""

    def read_snapshot(self) -> Sequence[Mapping[str, Any]]: ...


class CallbackSnapshotSource:
    """Adapter for DOM, accessibility tree, or in-memory extractor callbacks."""

    def __init__(self, callback: Callable[[], Sequence[Mapping[str, Any]]]) -> None:
        self._callback = callback

    def read_snapshot(self) -> Sequence[Mapping[str, Any]]:
        return list(self._callback())


class SqliteReadOnlySnapshotSource:
    """Strictly read-only SQLite snapshot source (`mode=ro` + `PRAGMA query_only`)."""

    def __init__(
        self,
        db_path: Path | str,
        query: str,
        params: Sequence[Any] = (),
    ) -> None:
        self.db_path = Path(db_path)
        self.query = query
        self.params = tuple(params)

    def read_snapshot(self) -> Sequence[Mapping[str, Any]]:
        uri = f"{self.db_path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            conn.execute("PRAGMA query_only = ON")
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(self.query, self.params)
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()


class HttpJsonSnapshotSource:
    """Strictly read-only HTTP GET JSON snapshot source with SSRF guard."""

    def __init__(
        self,
        url: str,
        *,
        items_path: Sequence[str] = (),
        headers: Mapping[str, str] | None = None,
        timeout_s: float = 10.0,
        fetcher: Callable[[str, dict[str, str], float], Any] | None = None,
        resolver: Callable[[str], Sequence[str]] | None = None,
    ) -> None:
        self._resolver = resolver
        self.url = validate_http_endpoint(url, resolver=resolver)
        self.items_path = tuple(items_path)
        self.headers = dict(headers or {})
        self.timeout_s = timeout_s
        self._fetcher = fetcher

    def read_snapshot(self) -> Sequence[Mapping[str, Any]]:
        validate_http_endpoint(self.url, resolver=self._resolver)
        if self._fetcher is not None:
            data = self._fetcher(self.url, self.headers, self.timeout_s)
        else:
            req = urllib.request.Request(self.url, headers=self.headers, method="GET")
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                data = json.loads(resp.read().decode("utf-8"))

        node: Any = data
        for segment in self.items_path:
            if not isinstance(node, Mapping) or segment not in node:
                raise ValueError(f"Missing items_path segment {segment!r} in JSON response")
            node = node[segment]

        if not isinstance(node, list):
            raise ValueError("HTTP JSON snapshot payload must resolve to a list of records")
        records: list[Mapping[str, Any]] = []
        for idx, item in enumerate(node):
            if not isinstance(item, Mapping):
                raise ValueError(f"Record at index {idx} is not a JSON object")
            records.append(dict(item))
        return records


@dataclass(frozen=True)
class MonitorSpec:
    """Declarative specification for monitoring a read-only record set."""

    name: str
    primary_key: str
    watched_fields: tuple[str, ...] | None = None
    predicate: Callable[[Mapping[str, Any]], bool] | None = None
    emit_initial_created: bool = True


class MonitorEngine:
    """Computes deterministic structural diffs across consecutive snapshots.

    Fail-closed guarantee: if reading or indexing a snapshot fails, prior state
    is preserved untouched, no empty/delete diffs are fabricated, and a single
    `monitor.error` event is emitted to all sinks.
    """

    def __init__(
        self,
        spec: MonitorSpec,
        source: SnapshotSource,
        sinks: Sequence[EventSink] = (),
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.spec = spec
        self.source = source
        self.sinks = tuple(sinks)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._previous: dict[str, dict[str, Any]] | None = None

    @property
    def current_state(self) -> dict[str, dict[str, Any]]:
        """Return a shallow copy of the last valid keyed snapshot."""
        if self._previous is None:
            return {}
        return {k: dict(v) for k, v in self._previous.items()}

    def _now_iso(self) -> str:
        return self._clock().astimezone(timezone.utc).isoformat()

    def _new_event(
        self,
        event_type: EventType,
        *,
        record_key: str | None = None,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        changed_fields: tuple[str, ...] = (),
        error: str | None = None,
    ) -> ChangeEvent:
        return ChangeEvent(
            event_id=uuid.uuid4().hex,
            monitor_name=self.spec.name,
            event_type=event_type,
            timestamp=self._now_iso(),
            record_key=record_key,
            before=before,
            after=after,
            changed_fields=changed_fields,
            error=error,
        )

    def _project_and_index(
        self, raw_records: Sequence[Mapping[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        pk = self.spec.primary_key
        watched = self.spec.watched_fields
        indexed: dict[str, dict[str, Any]] = {}

        for idx, raw in enumerate(raw_records):
            if pk not in raw or raw[pk] is None or str(raw[pk]).strip() == "":
                raise ValueError(f"Record at index {idx} is missing primary key {pk!r}")
            key = str(raw[pk])
            if key in indexed:
                raise ValueError(f"Duplicate primary key {key!r} in snapshot")

            if watched is None:
                projected = dict(raw)
            else:
                projected = {pk: raw[pk]}
                for field in watched:
                    if field in raw:
                        projected[field] = raw[field]

            if self.spec.predicate is not None and not self.spec.predicate(projected):
                continue
            indexed[key] = projected

        return indexed

    def _emit_all(self, events: Sequence[ChangeEvent]) -> None:
        if not events:
            return
        for sink in self.sinks:
            sink.emit(events)

    def tick(self) -> MonitorTickResult:
        """Poll the source once, diff against prior state, and emit events."""
        try:
            raw_records = self.source.read_snapshot()
            current = self._project_and_index(raw_records)
        except Exception as exc:  # noqa: BLE001
            err_msg = f"{type(exc).__name__}: {exc}"
            err_event = self._new_event("monitor.error", error=err_msg)
            self._emit_all((err_event,))
            return MonitorTickResult(
                ok=False,
                snapshot_count=0,
                events=(err_event,),
                error=err_msg,
            )

        events: list[ChangeEvent] = []
        if self._previous is None:
            if self.spec.emit_initial_created:
                for key in sorted(current):
                    events.append(
                        self._new_event(
                            "record.created",
                            record_key=key,
                            after=dict(current[key]),
                        )
                    )
        else:
            prev = self._previous
            all_keys = sorted(set(prev) | set(current))
            for key in all_keys:
                in_prev = key in prev
                in_curr = key in current
                if not in_prev and in_curr:
                    events.append(
                        self._new_event(
                            "record.created",
                            record_key=key,
                            after=dict(current[key]),
                        )
                    )
                elif in_prev and not in_curr:
                    events.append(
                        self._new_event(
                            "record.deleted",
                            record_key=key,
                            before=dict(prev[key]),
                        )
                    )
                else:
                    old_row = prev[key]
                    new_row = current[key]
                    field_names = sorted(set(old_row) | set(new_row))
                    changed = tuple(
                        f for f in field_names if old_row.get(f) != new_row.get(f)
                    )
                    if changed:
                        events.append(
                            self._new_event(
                                "record.updated",
                                record_key=key,
                                before=dict(old_row),
                                after=dict(new_row),
                                changed_fields=changed,
                            )
                        )

        self._previous = current
        emitted = tuple(events)
        self._emit_all(emitted)
        return MonitorTickResult(
            ok=True,
            snapshot_count=len(current),
            events=emitted,
        )
