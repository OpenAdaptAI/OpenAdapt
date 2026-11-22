"""Resource-scoped verification for API systems: read-back plus the system's own log.

A real HTTP server on loopback keeps records and an audit log of every write
(who, which record, what). Faults are real writes made through a replaced
``send``: the verifier sees only the record and the log. The lab run against
Medplum and Gitea is ``benchmark/api_scoped``.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

pytest.importorskip("cryptography")

from openadapt_flow.txplan.audit import AuditReader, AuditSource, Cursor, pick  # noqa: E402
from openadapt_flow.txplan.engine import Ctx, TxRefused  # noqa: E402
from openadapt_flow.txplan.scoped import ApiTools, default_send, scoped_write  # noqa: E402
from openadapt_flow.txplan.spec import TxSpec  # noqa: E402
from openadapt_flow.txplan.state import State  # noqa: E402


class AuditedStore:
    """Records, a marker search, and a newest-first audit log per bearer token."""

    def __init__(self) -> None:
        self.records: dict[int, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.next_id = 1
        self.writes = 0
        self.lock = threading.Lock()
        self.hidden_reads = 0  # the log shows a write only after this many log reads
        self.idempotency_keys: list[str] = []
        self.empty_to_null = False

    def log(self, actor: str, rid: int, action: str) -> None:
        self.events.append(
            {
                "seq": len(self.events) + 1,
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "by": {"user": actor},
                "target": f"item/{rid}",
                "action": action,
                "_hide": self.hidden_reads,
            }
        )

    def server(self) -> tuple[ThreadingHTTPServer, str]:
        store = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def _actor(self) -> str:
                return (self.headers.get("Authorization") or "Bearer anon").split()[-1]

            def _send(self, code: int, body: Any = None) -> None:
                data = b"" if body is None else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _body(self) -> dict[str, Any]:
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n)) if n else {}

            def _rid(self) -> Optional[int]:
                parts = urlsplit(self.path).path.strip("/").split("/")
                return (
                    int(parts[2])
                    if len(parts) == 3 and parts[:2] == ["api", "items"]
                    else None
                )

            def do_GET(self) -> None:  # noqa: N802
                url = urlsplit(self.path)
                q = {k: v[0] for k, v in parse_qs(url.query).items()}
                if url.path == "/api/me":
                    return self._send(200, {"login": self._actor()})
                if url.path == "/api/events":
                    with store.lock:
                        shown = []
                        for e in store.events:
                            if e["_hide"] > 0:
                                e["_hide"] -= 1
                                continue
                            shown.append({k: v for k, v in e.items() if k != "_hide"})
                    shown.reverse()
                    page, size = int(q.get("page", 1)), int(q.get("limit", 3))
                    return self._send(200, shown[(page - 1) * size : page * size])
                if url.path == "/api/items" and "marker" in q:
                    found = [
                        r
                        for r in store.records.values()
                        if r.get("ext_ref") == q["marker"]
                    ]
                    return self._send(200, {"items": found})
                rid = self._rid()
                if rid is None or rid not in store.records:
                    return self._send(404, {})
                store.log(self._actor(), rid, "viewed")
                self._send(200, store.records[rid])

            def do_POST(self) -> None:  # noqa: N802
                if urlsplit(self.path).path != "/api/items":
                    return self._send(404, {})
                if self.headers.get("Idempotency-Key"):
                    store.idempotency_keys.append(self.headers["Idempotency-Key"])
                body = self._body()
                if store.empty_to_null:
                    body = {k: (None if v == "" else v) for k, v in body.items()}
                with store.lock:
                    store.writes += 1
                    rid = store.next_id
                    store.next_id += 1
                    rec = {"id": rid, "note": None, **body, "updated": time.time()}
                    store.records[rid] = rec
                    store.log(self._actor(), rid, "created")
                self._send(201, rec)

            def do_PUT(self) -> None:  # noqa: N802
                rid = self._rid()
                if rid is None or rid not in store.records:
                    return self._send(404, {})
                body = self._body()
                with store.lock:
                    store.writes += 1
                    store.records[rid] = {**body, "id": rid, "updated": time.time()}
                    store.log(self._actor(), rid, "updated")
                self._send(200, store.records[rid])

            def do_DELETE(self) -> None:  # noqa: N802
                rid = self._rid()
                if rid is None or rid not in store.records:
                    return self._send(404, {})
                with store.lock:
                    store.writes += 1
                    del store.records[rid]
                    store.log(self._actor(), rid, "deleted")
                self._send(204)

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _spec(
    url: str,
    *,
    search: bool = True,
    idem: bool = False,
    logs: Optional[list[str]] = None,
) -> TxSpec:
    scoped: dict[str, Any] = {
        "audit_ref": "item/{id}",
        "identity": ["title"],
        "volatile": ["updated"],
        "codecs": {"note": "null_empty"},
        "readback_attempts": 2,
        "readback_wait_s": 0,
    }
    if search:
        scoped.update(
            marker={"ext_ref": "{marker}"},
            search="/api/items?marker={marker}",
            search_items="items",
            volatile=["updated", "ext_ref"],
        )
    audit = {
        "pattern": "activity_feed",
        "path": "/api/events",
        "paging": {"kind": "page", "param": "page", "size_param": "limit", "size": 3},
        "fields": {
            "id": "seq",
            "at": "at",
            "actor": "by.user",
            "resource": "target",
            "action": "action",
        },
        "actions": {
            "create": ["created"],
            "update": ["updated"],
            "delete": ["deleted"],
            "read": ["viewed"],
        },
        "actor": {"path": "/api/me", "field": "login"},
        "lag": {"attempts": 4, "wait_s": 0},
        # Each test's store starts with an empty log.
        "may_be_empty": True,
    }
    if logs is not None:
        audit["logs"] = logs
    return TxSpec.model_validate(
        {
            "schema_version": "openadapt.tx/v1",
            "systems": [
                {
                    "name": "app",
                    "kind": "rest",
                    "base_url": url,
                    "auth": {
                        "header": "Authorization",
                        "prefix": "Bearer ",
                        "value_env": "APP_TOKEN",
                    },
                    **({"idempotency_header": "Idempotency-Key"} if idem else {}),
                    "audit": audit,
                    "collections": [
                        {
                            "name": "item",
                            "list": {"path": "/api/items"},
                            "read": {"path": "/api/items/{id}"},
                            "create": {"method": "POST", "path": "/api/items"},
                            "update": {
                                "method": "PUT",
                                "path": "/api/items/{id}",
                                "replace": True,
                            },
                            "delete": {"method": "DELETE", "path": "/api/items/{id}"},
                            "delete_reversible": True,
                            "scoped": scoped,
                        }
                    ],
                }
            ],
        }
    )


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OPENADAPT_SEAL_HOME", str(tmp_path / "seal"))
    monkeypatch.setenv("APP_TOKEN", "agent")


@pytest.fixture
def lab(tmp_path: Path) -> Iterator[tuple[AuditedStore, str, State]]:
    store = AuditedStore()
    srv, url = store.server()
    try:
        yield store, url, State(tmp_path / "state")
    finally:
        srv.shutdown()


def _ctx(url: str, state: State, **kw: Any) -> Ctx:
    return Ctx(spec=_spec(url, **kw), state=state)


def _other(url: str, token: str = "agent") -> httpx.Client:
    return httpx.Client(base_url=url, headers={"Authorization": f"Bearer {token}"})


def _write(ctx: Ctx, **kw: Any) -> dict[str, Any]:
    kw.setdefault("task", "t1")
    return scoped_write(
        ctx, system="app", collection="item", sleep=lambda s: None, **kw
    )


# -- clean writes ----------------------------------------------------------------


def test_clean_create_and_update_verify_with_a_receipt(lab: Any) -> None:
    store, url, state = lab
    ctx = _ctx(url, state)
    out = _write(ctx, op="create", fields={"title": "Synthetic A", "qty": 2})
    assert out["outcome"] == "VERIFIED", out
    assert out["receipt"] is True
    statement = json.loads((Path(out["run_dir"]) / "report.json").read_text())
    assert statement["tx"]["audit"]["own_events"] == 1
    upd = _write(ctx, task="t2", op="update", rid=out["id"], fields={"qty": 3})
    assert upd["outcome"] == "VERIFIED", upd
    assert store.writes == 2


def test_null_and_empty_note_read_back_as_one_value(lab: Any) -> None:
    store, url, state = lab
    store.empty_to_null = True  # the server stores an empty note as null
    out = _write(
        _ctx(url, state), op="create", fields={"title": "Synthetic B", "note": ""}
    )
    assert out["outcome"] == "VERIFIED", out
    assert store.records[int(out["id"])]["note"] is None


def test_delete_verifies_absence(lab: Any) -> None:
    store, url, state = lab
    rec = _write(_ctx(url, state), op="create", fields={"title": "Synthetic C"})
    out = _write(_ctx(url, state), task="t2", op="delete", rid=rec["id"])
    assert out["outcome"] == "VERIFIED", out


# -- planted faults ----------------------------------------------------------------


def test_wrong_value_is_a_hard_failure(lab: Any) -> None:
    _, url, state = lab

    def wrong(adapter: Any, coll: Any, **kw: Any) -> Any:
        return default_send(
            adapter, coll, **{**kw, "fields": {**kw["fields"], "qty": 99}}
        )

    out = _write(
        _ctx(url, state),
        op="create",
        fields={"title": "Synthetic D", "qty": 2},
        send=wrong,
    )
    assert out["outcome"] == "FAILED" and "wrong_value" in out["codes"], out


def test_partial_write_is_a_hard_failure(lab: Any) -> None:
    store, url, state = lab
    rec = store.records[1] = {
        "id": 1,
        "title": "Synthetic E",
        "qty": 1,
        "colour": "red",
        "note": None,
    }
    store.next_id = 2

    def half(adapter: Any, coll: Any, **kw: Any) -> Any:
        return default_send(
            adapter, coll, **{**kw, "fields": {"qty": kw["fields"]["qty"]}}
        )

    out = _write(
        _ctx(url, state),
        op="update",
        rid="1",
        fields={"qty": 5, "colour": "blue"},
        send=half,
    )
    assert out["outcome"] == "FAILED" and "partial_write" in out["codes"], out
    assert rec["colour"] == "red"


def test_clobbered_field_on_a_replace_is_a_hard_failure(lab: Any) -> None:
    store, url, state = lab
    store.records[1] = {
        "id": 1,
        "title": "Synthetic F",
        "qty": 1,
        "colour": "red",
        "note": None,
    }
    store.next_id = 2

    def clobber(adapter: Any, coll: Any, **kw: Any) -> Any:
        base = {k: v for k, v in kw["baseline"].items() if k != "colour"}
        return default_send(adapter, coll, **{**kw, "baseline": base})

    out = _write(
        _ctx(url, state), op="update", rid="1", fields={"qty": 5}, send=clobber
    )
    assert out["outcome"] == "FAILED" and "frame_clobbered" in out["codes"], out


def test_duplicate_create_is_a_hard_failure(lab: Any) -> None:
    store, url, state = lab

    def twice(adapter: Any, coll: Any, **kw: Any) -> Any:
        default_send(
            adapter, coll, **{**kw, "marker": None}
        )  # a retry storm without the marker
        return default_send(adapter, coll, **kw)

    out = _write(
        _ctx(url, state),
        op="create",
        fields={"title": "Synthetic G", "qty": 1},
        send=twice,
    )
    assert out["outcome"] == "FAILED" and "duplicate_write" in out["codes"], out
    assert store.writes == 2


def test_duplicate_update_is_a_hard_failure(lab: Any) -> None:
    store, url, state = lab
    store.records[1] = {"id": 1, "title": "Synthetic H", "qty": 1, "note": None}
    store.next_id = 2

    def twice(adapter: Any, coll: Any, **kw: Any) -> Any:
        default_send(adapter, coll, **kw)
        return default_send(adapter, coll, **kw)

    out = _write(_ctx(url, state), op="update", rid="1", fields={"qty": 2}, send=twice)
    assert out["outcome"] == "FAILED" and "duplicate_write" in out["codes"], out


def test_collateral_edit_by_the_same_actor_is_a_hard_failure(lab: Any) -> None:
    store, url, state = lab
    store.records[7] = {"id": 7, "title": "Synthetic other", "qty": 1, "note": None}
    store.next_id = 8

    def collateral(adapter: Any, coll: Any, **kw: Any) -> Any:
        rid = default_send(adapter, coll, **kw)
        _other(url).put("/api/items/7", json={"title": "Synthetic other", "qty": 0})
        return rid

    out = _write(
        _ctx(url, state), op="create", fields={"title": "Synthetic I"}, send=collateral
    )
    assert out["outcome"] == "FAILED" and "collateral_mutation" in out["codes"], out


def test_another_actors_edit_is_outside_the_scope(lab: Any) -> None:
    store, url, state = lab
    store.records[7] = {"id": 7, "title": "Synthetic other", "qty": 1, "note": None}
    store.next_id = 8

    def busy(adapter: Any, coll: Any, **kw: Any) -> Any:
        rid = default_send(adapter, coll, **kw)
        _other(url, token="someone-else").put(
            "/api/items/7", json={"title": "x", "qty": 0}
        )
        return rid

    out = _write(
        _ctx(url, state), op="create", fields={"title": "Synthetic J"}, send=busy
    )
    assert out["outcome"] == "VERIFIED", out


def test_unknown_event_types_are_logged_not_stopped(lab: Any) -> None:
    store, url, state = lab

    def export(adapter: Any, coll: Any, **kw: Any) -> Any:
        rid = default_send(adapter, coll, **kw)
        store.log("agent", int(rid), "exported")
        return rid

    out = _write(
        _ctx(url, state), op="create", fields={"title": "Synthetic K"}, send=export
    )
    assert out["outcome"] == "VERIFIED", out
    assert "unknown_events_logged" in out["notes"]


# -- timing ---------------------------------------------------------------------------


def test_a_trailing_log_is_retried(lab: Any) -> None:
    store, url, state = lab
    store.hidden_reads = 2
    out = _write(_ctx(url, state), op="create", fields={"title": "Synthetic L"})
    assert out["outcome"] == "VERIFIED", out
    report = json.loads((Path(out["run_dir"]) / "report.json").read_text())
    assert report["tx"]["audit"]["attempts"] > 1


def test_a_log_that_never_shows_the_write_is_not_verified(lab: Any) -> None:
    store, url, state = lab
    store.hidden_reads = 50
    out = _write(_ctx(url, state), op="create", fields={"title": "Synthetic M"})
    assert out["outcome"] == "COMPLETED_UNVERIFIED" and out["codes"] == ["audit_lag"], (
        out
    )


def test_a_log_that_records_no_updates_says_so(lab: Any) -> None:
    store, url, state = lab
    store.records[1] = {"id": 1, "title": "Synthetic N", "qty": 1, "note": None}
    store.next_id = 2
    ctx = _ctx(url, state, logs=["create"])
    out = _write(ctx, op="update", rid="1", fields={"qty": 2})
    assert out["outcome"] == "VERIFIED"
    assert "log_does_not_record_update" in out["notes"]


# -- idempotency at the tool boundary ---------------------------------------------------


def test_a_retry_returns_the_first_result_and_sends_nothing(lab: Any) -> None:
    store, url, state = lab
    first = _write(
        _ctx(url, state), op="create", fields={"title": "Synthetic O", "qty": 1}
    )
    again = _write(
        _ctx(url, state), op="create", fields={"qty": 1, "title": "Synthetic O"}
    )
    assert again["replayed"] is True and again["id"] == first["id"]
    assert store.writes == 1


def test_acrfence_regenerated_payload_is_refused_not_written_twice(lab: Any) -> None:
    """After a rewind the agent regenerates the create with different bytes.

    The key comes from the task and the record's identity (its title), not the
    payload, so the regenerated write lands on the same key and is refused.
    """
    store, url, state = lab
    _write(_ctx(url, state), op="create", fields={"title": "Synthetic P", "qty": 1})
    with pytest.raises(TxRefused) as err:
        _write(
            _ctx(url, state), op="create", fields={"title": " synthetic  p", "qty": 2}
        )
    assert err.value.code == "replay_mismatch"
    assert store.writes == 1


def test_acrfence_control_without_the_guard_writes_twice(lab: Any) -> None:
    store, url, state = lab
    ctx = _ctx(url, state)
    adapter = ctx.adapter("app")
    coll = ctx.spec.system("app").collection("item")
    for qty in (1, 2):
        default_send(
            adapter,
            coll,
            op="create",
            fields={"title": "Synthetic Q", "qty": qty},
            rid=None,
            baseline=None,
            key="k",
            marker=None,
        )
    assert store.writes == 2


class _Crash(BaseException):
    pass


@pytest.mark.parametrize(
    "search", [True, False], ids=["marker-search", "audit-log-only"]
)
def test_a_crash_between_send_and_ledger_is_recovered_without_a_resend(
    lab: Any, search: bool
) -> None:
    store, url, state = lab

    def crash(adapter: Any, coll: Any, **kw: Any) -> Any:
        default_send(adapter, coll, **kw)
        raise _Crash()

    with pytest.raises(_Crash):
        _write(
            _ctx(url, state, search=search),
            op="create",
            fields={"title": "Synthetic R"},
            send=crash,
        )
    out = _write(
        _ctx(url, state, search=search), op="create", fields={"title": "Synthetic R"}
    )
    assert out["outcome"] == "VERIFIED", out
    assert "recovered_without_resend" in out["notes"]
    assert store.writes == 1


def test_a_crash_before_the_send_resends_once_proven_absent(lab: Any) -> None:
    store, url, state = lab

    def crash(adapter: Any, coll: Any, **kw: Any) -> Any:
        raise _Crash()

    with pytest.raises(_Crash):
        _write(
            _ctx(url, state), op="create", fields={"title": "Synthetic S"}, send=crash
        )
    out = _write(_ctx(url, state), op="create", fields={"title": "Synthetic S"})
    assert (
        out["outcome"] == "VERIFIED" and "resent_after_proven_absent" in out["notes"]
    ), out
    assert store.writes == 1


def test_the_key_goes_out_as_idempotency_key_where_supported(lab: Any) -> None:
    store, url, state = lab
    out = _write(
        _ctx(url, state, idem=True), op="create", fields={"title": "Synthetic T"}
    )
    assert out["outcome"] == "VERIFIED"
    assert len(store.idempotency_keys) == 1 and store.idempotency_keys[0].startswith(
        "ik-"
    )


def test_mcp_tools_shape(lab: Any) -> None:
    _, url, state = lab
    tools = ApiTools(_ctx(url, state), task="mcp")
    assert {t["name"] for t in tools.tools()} == {"api_read", "api_write"}
    text, err = tools.call(
        "api_write",
        {
            "system": "app",
            "collection": "item",
            "op": "create",
            "fields": {"title": "Synthetic U"},
        },
    )
    assert not err, text
    rid = json.loads(text)["id"]
    text, err = tools.call(
        "api_read", {"system": "app", "collection": "item", "id": rid}
    )
    assert not err and json.loads(text)["title"] == "Synthetic U"


# -- the same reader over other log shapes ------------------------------------------------


def _reader(source: dict[str, Any], pages: dict[str, Any]) -> AuditReader:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pages[str(request.url)])

    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://x")
    return AuditReader(AuditSource.model_validate(source), http)


def test_fhir_bundle_shape_pages_by_link() -> None:
    def ev(i: int, at: str, who: str, what: str, code: str) -> dict[str, Any]:
        return {
            "resource": {
                "resourceType": "AuditEvent",
                "id": f"a{i}",
                "recorded": at,
                "agent": [{"who": {"reference": who}}],
                "entity": [{"what": {"reference": what}}],
                "subtype": [{"code": code}],
            }
        }

    p1 = {
        "entry": [
            ev(
                3,
                "2026-01-01T00:00:03.000Z",
                "ClientApplication/me",
                "Patient/9",
                "update",
            )
        ],
        "link": [{"relation": "next", "url": "http://x/fhir/AuditEvent?page=2"}],
    }
    p2 = {
        "entry": [
            ev(
                2,
                "2026-01-01T00:00:02.000Z",
                "ClientApplication/me",
                "Patient/8",
                "create",
            ),
            ev(
                1,
                "2026-01-01T00:00:01.000Z",
                "ClientApplication/me",
                "Patient/7",
                "read",
            ),
        ],
        "link": [],
    }
    src = {
        "pattern": "fhir_audit_event",
        "path": "/fhir/AuditEvent",
        "items": "entry.*.resource",
        "paging": {"kind": "link", "path": "link[relation=next].url"},
        "fields": {
            "id": "id",
            "at": "recorded",
            "actor": "agent.*.who.reference",
            "resource": "entity.*.what.reference",
            "action": "subtype.*.code",
        },
        "actions": {"create": ["create"], "update": ["update"], "read": ["read"]},
        "actor": {"any": False, "value_env": "FHIR_ACTOR"},
    }
    import os

    os.environ["FHIR_ACTOR"] = "ClientApplication/me"
    r = _reader(
        src, {"http://x/fhir/AuditEvent": p1, "http://x/fhir/AuditEvent?page=2": p2}
    )
    win = r.after(Cursor(at=1767225601.0, ids=frozenset({"a1"})))
    assert [(e.id, e.kind) for e in win.events] == [("a3", "update"), ("a2", "create")]


def test_event_stream_shape_pages_by_last_id() -> None:
    def ev(
        i: int, created: int, typ: str, obj: str, req: Optional[str]
    ) -> dict[str, Any]:
        return {
            "id": f"evt_{i}",
            "created": created,
            "type": typ,
            "data": {"object": {"id": obj}},
            "request": {"id": req},
        }

    p1 = {
        "data": [
            ev(4, 104, "customer.updated", "cus_1", "req_a"),
            ev(3, 103, "invoice.finalized", "in_1", None),
        ],
        "has_more": True,
    }
    p2 = {
        "data": [
            ev(2, 102, "customer.created", "cus_2", "req_b"),
            ev(1, 101, "customer.created", "cus_0", "req_0"),
        ],
        "has_more": False,
    }
    src = {
        "pattern": "event_stream",
        "path": "/v1/events",
        "items": "data",
        "paging": {"kind": "cursor", "param": "starting_after", "has_more": "has_more"},
        "fields": {
            "id": "id",
            "at": "created",
            "actor": "request.id",
            "resource": "data.object.id",
            "action": "type",
        },
        "actions": {"create": ["customer.created"], "update": ["customer.updated"]},
        "actor": {"any": True},
    }
    r = _reader(
        src, {"http://x/v1/events": p1, "http://x/v1/events?starting_after=evt_3": p2}
    )
    win = r.after(Cursor(at=101.0, ids=frozenset({"evt_1"})))
    # The automatic invoice event names no request, so it isn't this caller's.
    assert [(e.id, e.kind) for e in win.events] == [
        ("evt_4", "update"),
        ("evt_2", "create"),
    ]


def test_a_log_that_comes_back_empty_is_not_read_as_new() -> None:
    """A search that returns nothing for a moment must not place the cursor at
    the start of the log, where every old event would look like this write's."""
    from openadapt_flow.txplan.audit import AuditError

    src = {
        "path": "/feed",
        "fields": {
            "id": "id",
            "at": "at",
            "actor": "by",
            "resource": "r",
            "action": "a",
        },
        "actor": {"any": True},
        "lag": {"attempts": 3, "wait_s": 0},
    }
    r = _reader(src, {"http://x/feed": []})
    with pytest.raises(AuditError):
        r.cursor(sleep=lambda s: None)
    r2 = _reader(
        src,
        {"http://x/feed": [{"id": 9, "at": 50, "by": "u", "r": "x/1", "a": "made"}]},
    )
    with pytest.raises(AuditError):
        r2.after(Cursor(at=40.0, ids=frozenset({"7"})))


def test_a_stale_stamped_event_does_not_end_the_read_before_the_cursor() -> None:
    """One event with an old timestamp on the first page must not hide a
    duplicate on the next page: the read goes on until the cursor is reached."""

    def ev(i: Any, created: int, typ: str, obj: str) -> dict[str, Any]:
        return {
            "id": f"evt_{i}",
            "created": created,
            "type": typ,
            "data": {"object": {"id": obj}},
            "request": {"id": "req_me"},
        }

    p1 = {
        "data": [
            ev(5, 110, "customer.created", "cus_9"),
            ev("stale", 50, "customer.updated", "cus_3"),
        ],
        "has_more": True,
    }
    p2 = {
        "data": [
            ev(4, 109, "customer.created", "cus_9b"),
            ev(1, 101, "customer.created", "cus_0"),
        ],
        "has_more": False,
    }
    src = {
        "pattern": "event_stream",
        "path": "/v1/events",
        "items": "data",
        "paging": {"kind": "cursor", "param": "starting_after", "has_more": "has_more"},
        "fields": {
            "id": "id",
            "at": "created",
            "actor": "request.id",
            "resource": "data.object.id",
            "action": "type",
        },
        "actions": {"create": ["customer.created"], "update": ["customer.updated"]},
        "actor": {"any": True},
    }
    r = _reader(
        src,
        {"http://x/v1/events": p1, "http://x/v1/events?starting_after=evt_stale": p2},
    )
    win = r.after(Cursor(at=101.0, ids=frozenset({"evt_1"})))
    # Placed by id: listed before the cursor, the stale-stamped event counts
    # as new too (never silently dropped).
    assert [e.id for e in win.events] == ["evt_5", "evt_stale", "evt_4"]
    # A page with a stale event and only one of the cursor's ids doesn't end
    # the read: the next page's in-window duplicate is still seen.
    p1b = {
        "data": [
            ev(6, 110, "customer.created", "cus_9"),
            ev(1, 101, "customer.created", "cus_0"),
            ev("old", 40, "customer.updated", "cus_3"),
        ],
        "has_more": True,
    }
    p2b = {
        "data": [
            ev(7, 101, "customer.created", "cus_9c"),
            ev(2, 101, "customer.created", "cus_1"),
        ],
        "has_more": False,
    }
    rb = _reader(
        src,
        {"http://x/v1/events": p1b, "http://x/v1/events?starting_after=evt_old": p2b},
    )
    winb = rb.after(Cursor(at=101.0, ids=frozenset({"evt_1", "evt_2"})))
    assert [e.id for e in winb.events] == ["evt_6", "evt_old", "evt_7"]
    # A log that never reaches the cursor is incomplete, not new.
    from openadapt_flow.txplan.audit import AuditError

    short = _reader(src, {"http://x/v1/events": {**p1, "has_more": False}})
    with pytest.raises(AuditError):
        short.after(Cursor(at=101.0, ids=frozenset({"evt_1"})))


def test_the_audit_read_fails_closed_on_evidence_it_cannot_place() -> None:
    from openadapt_flow.txplan.audit import AuditError, check_window

    src = {
        "path": "/feed",
        "fields": {
            "id": "id",
            "at": "at",
            "actor": "by",
            "resource": "r",
            "action": "a",
        },
        "actor": {"any": True},
        "actions": {"create": ["made"], "update": ["set"]},
    }
    # An event with no time can't be placed.
    r = _reader(src, {"http://x/feed": [{"id": 9, "by": "u", "r": "x/1", "a": "set"}]})
    with pytest.raises(AuditError):
        r.after(Cursor(at=None))
    # A page that isn't a list is not an empty page.
    r = _reader(src, {"http://x/feed": {"error": "busy"}})
    with pytest.raises(AuditError):
        r.after(Cursor(at=None))
    # A next link off the log's origin never gets the credential.
    linked = {**src, "paging": {"kind": "link", "path": "next"}, "items": "items"}
    page = {
        "items": [{"id": 1, "at": 10, "by": "u", "r": "x/1", "a": "set"}],
        "next": "https://evil.example/feed",
    }
    r = _reader(linked, {"http://x/feed": page})
    with pytest.raises(AuditError, match="origin"):
        r.after(Cursor(at=None))
    # One event naming the target and another record changed both.
    both = [{"id": 1, "at": 10, "by": "u", "r": ["x/1", "x/2"], "a": "set"}]
    src_list = {**src, "fields": {**src["fields"], "resource": "r.*"}}
    audit = check_window(
        _reader(src_list, {"http://x/feed": both}),
        Cursor(at=None),
        op="update",
        target="x/1",
        collection_prefix="x/",
        logged=True,
        sleep=lambda s: None,
    )
    assert audit.collateral and audit.own


def test_a_marker_search_without_its_results_is_not_empty() -> None:
    from openadapt_flow.txplan.adapters import AdapterError
    from openadapt_flow.txplan.audit import ScopedCollection
    from openadapt_flow.txplan.scoped import _search

    base = {
        "audit_ref": "item/{id}",
        "identity": ["title"],
        "marker": {"ext": "{marker}"},
        "search": "/s?m={marker}",
    }
    scoped = ScopedCollection.model_validate({**base, "search_items": "items"})
    # Pack-shaped FHIR: entry.*.resource, with the declared empty shape.
    fhir = ScopedCollection.model_validate(
        {
            **base,
            "search_items": "entry.*.resource",
            "search_empty": {
                "equals": {"resourceType": "Bundle", "type": "searchset"},
                "absent": ["link[relation=next]"],
            },
        }
    )
    undeclared = fhir.model_copy(update={"search_empty": None})

    class Adapter:
        def __init__(self, body):
            self.http = httpx.Client(
                transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)),
                base_url="http://x",
            )

    def run(body, pack):
        return _search(Adapter(body), pack, "m1", "id")

    def refused(body, pack):
        with pytest.raises(AdapterError):
            run(body, pack)

    bundle = {"resourceType": "Bundle", "type": "searchset"}
    patient = {"resource": {"resourceType": "Patient", "id": "p1"}}
    # Empty: only the declared shape, or an explicit total 0.
    assert run(bundle, fhir) == []
    assert run({**bundle, "total": 0}, fhir) == []
    assert run({"total": 0}, scoped) == []
    refused(bundle, undeclared)
    refused({"error": "busy"}, scoped)
    # The declared shape never hides a partial answer.
    refused({**bundle, "total": 1}, fhir)  # reports a match it doesn't return
    refused({**bundle, "total": 1, "entry": []}, fhir)
    refused({**bundle, "total": "1"}, fhir)  # a total that isn't a count
    refused({**bundle, "total": 1.0}, fhir)
    refused({**bundle, "type": "history"}, fhir)  # not a search answer
    refused({**bundle, "type": "batch-response"}, fhir)
    refused(
        {**bundle, "link": [{"relation": "next", "url": "http://x/s?page=2"}]}, fhir
    )
    refused({**bundle, "entry": None}, fhir)
    refused({"resourceType": "OperationOutcome"}, fhir)
    # Every returned item must name its record, and each counts.
    refused({**bundle, "entry": [{"resource": {"resourceType": "Patient"}}]}, fhir)
    refused(
        {**bundle, "entry": [patient, {"resource": {"resourceType": "Patient"}}]}, fhir
    )
    refused({"items": [{"title": "no id"}]}, scoped)
    assert run({**bundle, "entry": [patient]}, fhir) == ["p1"]
    assert run({"items": [{"id": 3}]}, scoped) == ["3"]
    # search_empty without search_items is refused when the pack loads.
    with pytest.raises(ValueError):
        ScopedCollection.model_validate(
            {**base, "search_empty": {"equals": {"resourceType": "Bundle"}}}
        )


def test_activity_feed_resource_from_two_parts() -> None:
    item = {
        "id": 5,
        "created": "2026-01-01T00:00:00Z",
        "act_user": {"login": "me"},
        "op_type": "comment_issue",
        "repo": {"full_name": "oa/lab"},
        "content": "12|Synthetic",
    }
    src = AuditSource.model_validate(
        {
            "path": "/feeds",
            "fields": {
                "id": "id",
                "at": "created",
                "actor": "act_user.login",
                "resource": {
                    "parts": [
                        {"path": "repo.full_name"},
                        {"path": "content", "regex": "^(\\d+)\\|"},
                    ],
                    "sep": "#",
                },
                "action": "op_type",
            },
            "actor": {"value_env": "X"},
        }
    )
    assert src.fields.resource.values(item) == ["oa/lab#12"]
    assert pick({"a": [{"b": 1}, {"b": 2}]}, "a.*.b") == [1, 2]


# -- one proxy for SQL and APIs ------------------------------------------------------------


def test_one_mcp_endpoint_serves_sql_and_api_tools(lab: Any, tmp_path: Path) -> None:
    import sqlite3

    from openadapt_flow.txplan.proxy import (
        McpServer,
        Proxy,
        child_environment,
        sql_spec,
    )

    store, url, state = lab
    db = tmp_path / "app.db"
    sqlite3.connect(db).executescript(
        "CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT);"
    )
    proxy = Proxy(
        Ctx(spec=sql_spec(sqlite=str(db)), state=state),
        task="both",
        extra=ApiTools(_ctx(url, state), task="both"),
    )
    server = McpServer(proxy, port=0)
    server.start()
    try:

        def rpc(method: str, params: dict[str, Any]) -> Any:
            got = httpx.post(
                server.url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                headers={
                    "Authorization": f"Bearer {server.token}",
                    "Accept": "application/json, text/event-stream",
                },
            )
            return got.json()["result"]

        names = {t["name"] for t in rpc("tools/list", {})["tools"]}
        assert {"sql_query", "sql_preview", "api_read", "api_write"} <= names
        out = rpc(
            "tools/call",
            {
                "name": "api_write",
                "arguments": {
                    "system": "app",
                    "collection": "item",
                    "op": "create",
                    "fields": {"title": "Synthetic V"},
                },
            },
        )
        assert (
            out["isError"] is False
            and json.loads(out["content"][0]["text"])["outcome"] == "VERIFIED"
        )
        assert store.writes == 1
    finally:
        server.stop()
    env = child_environment(
        {"APP_TOKEN": "agent", "PATH": "/bin", "COPY": "x-agent-x"}, ["agent"]
    )
    assert env == {"PATH": "/bin"}


# -- receipts and one task's several writes -------------------------------------------------


def test_every_outcome_writes_a_receipt_that_verifies(lab: Any) -> None:
    import os

    from openadapt_flow.receipt_record import local_registry, verify_run_receipt

    store, url, state = lab
    registry = local_registry(Path(os.environ["OPENADAPT_SEAL_HOME"]))
    ok = _write(_ctx(url, state), op="create", fields={"title": "Synthetic W"})

    def wrong(adapter: Any, coll: Any, **kw: Any) -> Any:
        return default_send(
            adapter, coll, **{**kw, "fields": {**kw["fields"], "title": "x"}}
        )

    bad = _write(
        _ctx(url, state),
        task="t2",
        op="create",
        fields={"title": "Synthetic X"},
        send=wrong,
    )
    store.hidden_reads = 50
    slow = _write(
        _ctx(url, state), task="t3", op="create", fields={"title": "Synthetic Y"}
    )
    store.hidden_reads = 0
    with pytest.raises(TxRefused) as refused:
        _write(
            _ctx(url, state),
            task="t1",
            op="create",
            fields={"title": "Synthetic W", "qty": 9},
        )
    runs = {
        "VERIFIED": Path(ok["run_dir"]),
        "FAILED": Path(bad["run_dir"]),
        "UNKNOWN": Path(slow["run_dir"]),
        "STOPPED": Path(refused.value.report["run_dir"]),
    }
    for outcome, run in runs.items():
        got = verify_run_receipt(run, registry)
        assert got.ok, (outcome, got.reason, got.detail)
        assert got.outcome == outcome
        for line in got.statement["predicate"]["lines"]:
            assert "approved_unverified" not in line["not_checked"]


def test_two_deliberate_updates_of_one_record_in_one_task(lab: Any) -> None:
    store, url, state = lab
    tools = ApiTools(_ctx(url, state), task="session")
    text, err = tools.call(
        "api_write",
        {
            "system": "app",
            "collection": "item",
            "op": "create",
            "fields": {"title": "Synthetic Z", "qty": 1},
        },
    )
    rid = json.loads(text)["id"]
    text, err = tools.call(
        "api_write",
        {
            "system": "app",
            "collection": "item",
            "op": "update",
            "id": rid,
            "fields": {"qty": 2},
        },
    )
    assert not err, text
    # The same record again with another value is a replay unless the caller names a new step.
    text, err = tools.call(
        "api_write",
        {
            "system": "app",
            "collection": "item",
            "op": "update",
            "id": rid,
            "fields": {"qty": 3},
        },
    )
    assert err and json.loads(text)["refused"] == "replay_mismatch"
    text, err = tools.call(
        "api_write",
        {
            "system": "app",
            "collection": "item",
            "op": "update",
            "id": rid,
            "slot": "step-3",
            "fields": {"qty": 3},
        },
    )
    assert not err, text
    assert store.records[int(rid)]["qty"] == 3 and store.writes == 3


def test_proxy_cli_api_only_gets_a_stable_default_task(
    lab: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import yaml

    from openadapt_flow.txplan import proxy, proxy_cli

    _, url, _ = lab
    tx = tmp_path / "systems.tx.yaml"
    tx.write_text(yaml.safe_dump(_spec(url).model_dump(mode="json", exclude_none=True)))
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(proxy, "run", lambda command, **kw: seen.append(kw) or 0)
    args = proxy_cli._parser().parse_args(
        ["--api", str(tx), "--state", str(tmp_path / "s")]
    )
    assert proxy_cli._run(args, []) == 0
    assert proxy_cli._run(args, []) == 0
    first, second = seen
    assert first["task"] == second["task"] and first["task"].startswith("proxy-")
    assert first["api_spec"] is not None and "agent" in first["secrets_"]
