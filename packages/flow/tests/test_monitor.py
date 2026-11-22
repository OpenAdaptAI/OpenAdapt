"""Tests for openadapt_flow.monitor (CDC diffs, fail-closed errors, sinks, SSRF guard)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from openadapt_flow.monitor import (
    CallbackSnapshotSource,
    HttpJsonSnapshotSource,
    JsonlEventSink,
    MemoryMarkdownSink,
    MonitorEngine,
    MonitorSpec,
    SqliteReadOnlySnapshotSource,
    WebhookEventSink,
    sign_webhook_payload,
    validate_http_endpoint,
)


def _fixed_clock() -> datetime:
    return datetime(2026, 11, 22, 19, 0, 0, tzinfo=timezone.utc)


def test_created_updated_deleted_diffs_and_watched_fields() -> None:
    snapshots: list[list[dict[str, Any]]] = [
        [
            {"id": "rec-1", "status": "open", "amount": 100, "noise": "a"},
            {"id": "rec-2", "status": "open", "amount": 250, "noise": "b"},
        ],
        # rec-1 only changes unwatched 'noise' -> no event; rec-2 updates status; rec-3 created
        [
            {"id": "rec-1", "status": "open", "amount": 100, "noise": "changed"},
            {"id": "rec-2", "status": "approved", "amount": 250, "noise": "b"},
            {"id": "rec-3", "status": "open", "amount": 50, "noise": "c"},
        ],
        # rec-1 deleted
        [
            {"id": "rec-2", "status": "approved", "amount": 250, "noise": "b"},
            {"id": "rec-3", "status": "open", "amount": 50, "noise": "c"},
        ],
    ]
    idx = 0

    def read_next() -> list[dict[str, Any]]:
        nonlocal idx
        snap = snapshots[idx]
        idx += 1
        return snap

    engine = MonitorEngine(
        MonitorSpec(
            name="queue-watch",
            primary_key="id",
            watched_fields=("status", "amount"),
        ),
        CallbackSnapshotSource(read_next),
        clock=_fixed_clock,
    )

    t1 = engine.tick()
    assert t1.ok is True
    assert [(e.event_type, e.record_key) for e in t1.events] == [
        ("record.created", "rec-1"),
        ("record.created", "rec-2"),
    ]

    t2 = engine.tick()
    assert t2.ok is True
    assert [(e.event_type, e.record_key, e.changed_fields) for e in t2.events] == [
        ("record.updated", "rec-2", ("status",)),
        ("record.created", "rec-3", ()),
    ]
    assert t2.events[0].before == {"id": "rec-2", "status": "open", "amount": 250}
    assert t2.events[0].after == {"id": "rec-2", "status": "approved", "amount": 250}

    t3 = engine.tick()
    assert t3.ok is True
    assert [(e.event_type, e.record_key) for e in t3.events] == [("record.deleted", "rec-1")]


def test_silent_baseline_and_predicate_filtering() -> None:
    snapshots: list[list[dict[str, Any]]] = [
        [
            {"id": "a", "priority": "low"},
            {"id": "b", "priority": "high"},
        ],
        [
            {"id": "a", "priority": "high"},  # enters predicate -> created
            {"id": "b", "priority": "low"},  # leaves predicate -> deleted
        ],
    ]
    it = iter(snapshots)
    engine = MonitorEngine(
        MonitorSpec(
            name="high-priority-watch",
            primary_key="id",
            predicate=lambda row: row.get("priority") == "high",
            emit_initial_created=False,
        ),
        CallbackSnapshotSource(lambda: next(it)),
        clock=_fixed_clock,
    )

    t1 = engine.tick()
    assert t1.ok is True
    assert t1.events == ()
    assert t1.snapshot_count == 1

    t2 = engine.tick()
    assert t2.ok is True
    assert [(e.event_type, e.record_key) for e in t2.events] == [
        ("record.created", "a"),
        ("record.deleted", "b"),
    ]


def test_fail_closed_on_source_error_preserves_state() -> None:
    call_count = 0

    def flaky_source() -> list[dict[str, Any]]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return [{"id": "k1", "state": "draft"}]
        if call_count == 2:
            raise ConnectionError("backend timeout")
        return [{"id": "k1", "state": "submitted"}]

    engine = MonitorEngine(
        MonitorSpec(name="flaky-watch", primary_key="id"),
        CallbackSnapshotSource(flaky_source),
        clock=_fixed_clock,
    )

    t1 = engine.tick()
    assert t1.ok is True
    assert len(t1.events) == 1

    # Source failure emits monitor.error and NEVER fabricates record.deleted
    t2 = engine.tick()
    assert t2.ok is False
    assert len(t2.events) == 1
    assert t2.events[0].event_type == "monitor.error"
    assert "ConnectionError: backend timeout" in (t2.events[0].error or "")
    assert engine.current_state == {"k1": {"id": "k1", "state": "draft"}}

    # Recovery tick diffs accurately against pre-error snapshot
    t3 = engine.tick()
    assert t3.ok is True
    assert [(e.event_type, e.record_key, e.changed_fields) for e in t3.events] == [
        ("record.updated", "k1", ("state",))
    ]


def test_duplicate_or_missing_primary_key_fails_closed() -> None:
    engine = MonitorEngine(
        MonitorSpec(name="dup-check", primary_key="id"),
        CallbackSnapshotSource(lambda: [{"id": "x"}, {"id": "x"}]),
        clock=_fixed_clock,
    )
    res = engine.tick()
    assert res.ok is False
    assert res.events[0].event_type == "monitor.error"
    assert "Duplicate primary key" in (res.events[0].error or "")


def test_sqlite_read_only_snapshot_source(tmp_path: Path) -> None:
    db_file = tmp_path / "records.sqlite"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE items (id TEXT PRIMARY KEY, status TEXT)")
    conn.execute("INSERT INTO items VALUES ('i1', 'pending'), ('i2', 'done')")
    conn.commit()
    conn.close()

    ro_source = SqliteReadOnlySnapshotSource(db_file, "SELECT id, status FROM items")
    rows = ro_source.read_snapshot()
    assert rows == [{"id": "i1", "status": "pending"}, {"id": "i2", "status": "done"}]

    # Attempting a write query must fail closed because of mode=ro + query_only
    bad_source = SqliteReadOnlySnapshotSource(
        db_file, "INSERT INTO items VALUES ('i3', 'injected')"
    )
    with pytest.raises(sqlite3.OperationalError):
        bad_source.read_snapshot()


def test_http_json_snapshot_source_and_ssrf_guard() -> None:
    source = HttpJsonSnapshotSource(
        "http://127.0.0.1:8080/items",
        items_path=("data", "records"),
        fetcher=lambda _u, _h, _t: {"data": {"records": [{"id": "1", "v": "ok"}]}},
    )
    assert source.read_snapshot() == [{"id": "1", "v": "ok"}]

    with pytest.raises(ValueError, match="Blocked non-public IP"):
        HttpJsonSnapshotSource("https://169.254.169.254/latest/meta-data/")


def test_jsonl_and_memory_markdown_sinks(tmp_path: Path) -> None:
    jsonl_sink = JsonlEventSink(tmp_path / "events.jsonl")
    md_sink = MemoryMarkdownSink(tmp_path / "memory", title="Work Queue")

    snapshots: list[list[dict[str, Any]]] = [
        [{"id": "w1", "status": "queued"}, {"id": "w2", "status": "queued"}],
        [{"id": "w1", "status": "done"}],
    ]
    it = iter(snapshots)
    engine = MonitorEngine(
        MonitorSpec(name="work-queue", primary_key="id"),
        CallbackSnapshotSource(lambda: next(it)),
        sinks=(jsonl_sink, md_sink),
        clock=_fixed_clock,
    )

    engine.tick()
    engine.tick()

    lines = (tmp_path / "events.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 4
    parsed = [json.loads(line) for line in lines]
    assert [p["event_type"] for p in parsed] == [
        "record.created",
        "record.created",
        "record.updated",
        "record.deleted",
    ]

    log_text = (tmp_path / "memory" / "memory_log.md").read_text(encoding="utf-8")
    assert "record.created" in log_text
    assert "record.updated" in log_text
    assert "record.deleted" in log_text

    current_text = (tmp_path / "memory" / "memory_current.md").read_text(encoding="utf-8")
    assert "| w1 |" in current_text
    assert "| done |" in current_text
    assert "| w2 |" not in current_text


def test_webhook_sink_hmac_signing_and_ssrf_guard() -> None:
    sent: list[tuple[str, bytes, dict[str, str], float]] = []
    sink = WebhookEventSink(
        "https://hooks.example.com/openadapt",
        secret="top-secret-key",
        sender=lambda url, body, headers, timeout: sent.append((url, body, headers, timeout)),
        resolver=lambda _host: ["93.184.216.34"],
    )

    engine = MonitorEngine(
        MonitorSpec(name="hook-test", primary_key="id"),
        CallbackSnapshotSource(lambda: [{"id": "r1", "status": "ready"}]),
        sinks=(sink,),
        clock=_fixed_clock,
    )
    engine.tick()

    assert len(sent) == 1
    url, body, headers, _timeout = sent[0]
    assert url == "https://hooks.example.com/openadapt"
    assert headers["X-OpenAdapt-Signature-256"] == sign_webhook_payload(body, "top-secret-key")
    payload = json.loads(body.decode("utf-8"))
    assert payload["schema"] == "openadapt.monitor.webhook.v1"
    assert payload["events"][0]["event_type"] == "record.created"

    # Loopback HTTP is allowed
    assert validate_http_endpoint("http://127.0.0.1:9000/hook") == "http://127.0.0.1:9000/hook"
    assert validate_http_endpoint("http://localhost:9000/hook") == "http://localhost:9000/hook"

    # SSRF / insecure endpoints MUST be rejected
    for bad_url in (
        "http://hooks.example.com/hook",
        "https://169.254.169.254/latest/meta-data/",
        "https://10.0.0.5/internal",
        "https://192.168.1.10/admin",
        "file:///etc/passwd",
    ):
        with pytest.raises(ValueError):
            validate_http_endpoint(bad_url)

    # DNS rebinding to private IP MUST be rejected
    with pytest.raises(ValueError, match="blocked IP"):
        validate_http_endpoint(
            "https://rebind.example.com/hook",
            resolver=lambda _h: ["10.20.30.40"],
        )
