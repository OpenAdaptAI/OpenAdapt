"""Read-only ``POST`` requests no longer use up the write budget (#2389).

Three general classes, none tied to an app:

1. A GraphQL query sent to an operation-style route (``/$graphql``) is a
   read, like one sent to ``/graphql``.
2. A batch envelope (``Bundle`` of type ``batch`` or ``transaction``) whose
   every entry is a ``GET`` or ``HEAD`` is a read.
3. Any other ``POST`` route counts as a read only when it matches a
   ``ReadRoute`` declared as data, which ``probe_read_routes`` learns by
   replaying the route on a sandbox and checking that the whole store did not
   change.

The controls below use a local SQLite-backed server so the store digest is
exact. ``test_tx_post_reads_live.py`` runs the same checks on a real app.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

import pytest

from openadapt_flow.tx.read_probe import probe_read_routes
from openadapt_flow.tx.wire_tap import (
    ReadRoute,
    SessionWireTap,
    WirePolicy,
    is_mutation_request,
    matches_read_route,
)

JSON = [("Content-Type", "application/json")]


def _post(path: str, body: Any) -> tuple[bool, bool]:
    return is_mutation_request("POST", path, json.dumps(body).encode(), JSON)


# ---------------------------------------------------------------------------
# Static classes
# ---------------------------------------------------------------------------


def test_graphql_query_on_operation_route_is_a_read() -> None:
    assert _post("/fhir/R4/$graphql", {"query": "{ PatientList { id } }"}) == (
        False,
        False,
    )
    assert _post("/api/graphql", {"query": "query Q { a }"}) == (False, False)


def test_graphql_mutation_on_operation_route_is_a_mutation() -> None:
    assert _post(
        "/fhir/R4/$graphql", {"query": "mutation { PatientCreate(res: {}) { id } }"}
    ) == (True, True)


def test_graphql_name_elsewhere_in_path_does_not_make_a_read() -> None:
    assert _post("/graphql/upload", {"query": "{ a }"}) == (True, False)
    assert _post("/notgraphql", {"query": "{ a }"}) == (True, False)


@pytest.mark.parametrize("kind", ["batch", "transaction"])
def test_batch_of_reads_is_a_read(kind: str) -> None:
    bundle = {
        "resourceType": "Bundle",
        "type": kind,
        "entry": [
            {"request": {"method": "GET", "url": "Patient?name=a"}},
            {"request": {"method": "HEAD", "url": "Patient/1"}},
        ],
    }
    assert _post("/fhir/R4", bundle) == (False, False)


@pytest.mark.parametrize(
    "entries",
    [
        [
            {"request": {"method": "GET", "url": "Patient"}},
            {"request": {"method": "PUT", "url": "Patient/1"}},
        ],
        [
            {
                "request": {"method": "GET", "url": "Patient"},
                "resource": {"resourceType": "Patient"},
            }
        ],
        [{"request": {"url": "Patient"}}],
        [{"request": {"method": "GET"}}],
        [],
    ],
)
def test_batch_with_any_write_or_odd_entry_is_a_mutation(entries: list) -> None:
    bundle = {"resourceType": "Bundle", "type": "batch", "entry": entries}
    assert _post("/fhir/R4", bundle) == (True, False)


def test_other_bundle_types_are_mutations() -> None:
    bundle = {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [{"request": {"method": "GET", "url": "Patient"}}],
    }
    assert _post("/fhir/R4", bundle) == (True, False)


# ---------------------------------------------------------------------------
# Declared read routes
# ---------------------------------------------------------------------------


def test_read_route_requires_every_declared_body_field() -> None:
    route = ReadRoute(path=r"/rpc", body_equals=(("method", "model.read"),))
    read = json.dumps({"method": "model.read", "params": [1]}).encode()
    write = json.dumps({"method": "model.write", "params": [1]}).encode()
    assert matches_read_route((route,), "POST", "/rpc", read, JSON)
    assert not matches_read_route((route,), "POST", "/rpc", write, JSON)
    assert not matches_read_route((route,), "POST", "/rpc/x", read, JSON)
    assert not matches_read_route((route,), "PUT", "/rpc", read, JSON)
    assert not matches_read_route((route,), "POST", "/rpc", b"not json", JSON)


def test_read_route_matches_form_bodies_and_refuses_repeated_names() -> None:
    form = [("Content-Type", "application/x-www-form-urlencoded")]
    route = ReadRoute(path=r"/fragment\.php", body_equals=(("action", "list"),))
    assert matches_read_route(
        (route,), "POST", "/fragment.php", b"action=list&x=1", form
    )
    assert not matches_read_route(
        (route,), "POST", "/fragment.php", b"action=list&action=save", form
    )


# ---------------------------------------------------------------------------
# A local app with an exact store digest
# ---------------------------------------------------------------------------


class _App:
    """One RPC route that reads or writes by body, one search route, one save."""

    def __init__(self, db: Path) -> None:
        self.db = db
        conn = sqlite3.connect(db)
        conn.execute("create table rec (id integer primary key, note text)")
        conn.execute("insert into rec (id, note) values (1, 'a'), (2, 'b')")
        conn.commit()
        conn.close()
        app = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                rid = self.path.rsplit("/", 1)[-1]
                with sqlite3.connect(app.db) as conn:
                    row = conn.execute(
                        "select id, note from rec where id=?", (rid,)
                    ).fetchone()
                self._json(200, {"id": row[0], "note": row[1]} if row else {})

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                with sqlite3.connect(app.db) as conn:
                    if self.path == "/search":
                        rows = conn.execute("select id from rec").fetchall()
                        self._json(200, {"ids": [r[0] for r in rows]})
                    elif self.path == "/rpc" and body.get("method") == "rec.read":
                        self._json(
                            200,
                            {
                                "n": conn.execute(
                                    "select count(*) from rec"
                                ).fetchone()[0]
                            },
                        )
                    elif self.path == "/rpc" and body.get("method") == "rec.write":
                        conn.execute(
                            "update rec set note=? where id=1", (body["note"],)
                        )
                        self._json(200, {"ok": True})
                    elif self.path == "/rec/1":
                        conn.execute(
                            "update rec set note=? where id=1", (body["note"],)
                        )
                        self._json(200, {"id": 1, "note": body["note"]})
                    else:
                        self._json(404, {})

            def _json(self, status: int, payload: Any) -> None:
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def reset(self) -> None:
        """Restore the state from before any demonstration."""
        with sqlite3.connect(self.db) as conn:
            conn.execute("delete from rec")
            conn.execute("insert into rec (id, note) values (1, 'a'), (2, 'b')")

    def digest(self) -> str:
        with sqlite3.connect(self.db) as conn:
            rows = conn.execute("select id, note from rec order by id").fetchall()
        return hashlib.sha256(repr(rows).encode()).hexdigest()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture()
def app(tmp_path: Path) -> Iterator[_App]:
    a = _App(tmp_path / "store.db")
    try:
        yield a
    finally:
        a.close()


def _call(base: str, method: str, path: str, body: Any = None) -> int:
    parts = urlsplit(base)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    raw = json.dumps(body).encode() if body is not None else None
    conn.request(method, path, body=raw, headers={"Content-Type": "application/json"})
    status = conn.getresponse().status
    conn.close()
    return status


def _demo_har(app: _App) -> dict:
    with SessionWireTap(app.base, WirePolicy(max_mutations=None)) as tap:
        _call(tap.base_url, "POST", "/search", {"q": "a", "page": 1})
        _call(tap.base_url, "POST", "/search", {"q": "b", "page": 1})
        _call(tap.base_url, "POST", "/rpc", {"method": "rec.read", "id": 1})
        _call(tap.base_url, "POST", "/rpc", {"method": "rec.read", "id": 2})
        _call(tap.base_url, "POST", "/rec/1", {"note": "demo"})
        return tap.export_har()


def test_probe_learns_reads_and_refuses_the_save(app: _App) -> None:
    report = probe_read_routes(
        _demo_har(app),
        sandbox_base_url=app.base,
        store_digest=app.digest,
        store_reset=app.reset,
    )
    by_path = {r.path: r for r in report.routes}
    assert report.store_idle
    assert by_path["/search"].read_only and by_path["/search"].body_equals == (
        ("page", "1"),
    )
    assert by_path["/rpc"].read_only
    assert by_path["/rpc"].body_equals == (("method", "rec.read"),)
    assert not by_path["/rec/1"].read_only
    assert by_path["/rec/1"].reason == "replay changed the store"


def test_without_a_reset_a_replayed_save_would_look_like_a_read(app: _App) -> None:
    """Why ``store_reset`` is required: the demo's own write already landed."""
    report = probe_read_routes(
        _demo_har(app),
        sandbox_base_url=app.base,
        store_digest=app.digest,
        store_reset=lambda: None,
    )
    by_path = {r.path: r for r in report.routes}
    assert by_path["/rec/1"].read_only  # the fail-open the reset prevents


def test_learned_routes_let_the_one_save_through_and_still_gate_rpc_writes(
    app: _App,
) -> None:
    report = probe_read_routes(
        _demo_har(app),
        sandbox_base_url=app.base,
        store_digest=app.digest,
        store_reset=app.reset,
    )
    policy = WirePolicy(max_mutations=1, read_routes=report.read_routes)
    with SessionWireTap(app.base, policy) as tap:
        assert _call(tap.base_url, "POST", "/search", {"q": "z", "page": 1}) == 200
        assert (
            _call(tap.base_url, "POST", "/rpc", {"method": "rec.read", "id": 1}) == 200
        )
        assert _call(tap.base_url, "POST", "/rec/1", {"note": "saved"}) == 200
        # A write on the RPC route is still a mutation, so the budget stops it.
        assert (
            _call(tap.base_url, "POST", "/rpc", {"method": "rec.write", "note": "x"})
            == 403
        )
        summary = tap.summary()
    assert summary.mutations_allowed == 1 and summary.mutations_blocked == 1
    assert summary.read_requests == 2


def test_probe_refuses_everything_when_the_store_is_not_idle(app: _App) -> None:
    ticks = iter(range(1000))
    report = probe_read_routes(
        _demo_har(app),
        sandbox_base_url=app.base,
        store_digest=lambda: str(next(ticks)),
        store_reset=app.reset,
    )
    assert not report.store_idle and report.read_routes == ()


def test_probe_refuses_a_non_loopback_sandbox(app: _App) -> None:
    from openadapt_flow.tx.induce import SandboxSafetyError

    with pytest.raises(SandboxSafetyError):
        probe_read_routes(
            _demo_har(app),
            sandbox_base_url="http://192.0.2.10",
            store_digest=app.digest,
            store_reset=app.reset,
        )


def test_learned_routes_never_store_credential_fields(app: _App) -> None:
    with SessionWireTap(app.base, WirePolicy(max_mutations=None)) as tap:
        for _ in range(2):
            _call(tap.base_url, "POST", "/search", {"csrf_token": "t0k", "page": 1})
        har = tap.export_har()
    report = probe_read_routes(
        har, sandbox_base_url=app.base, store_digest=app.digest, store_reset=app.reset
    )
    assert report.read_routes[0].body_equals == (("page", "1"),)
