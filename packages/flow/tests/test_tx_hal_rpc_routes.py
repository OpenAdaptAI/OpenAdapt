"""Regression tests for tx engine issues #2442, #2443, #2444, and #2445."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from openadapt_flow.tx.induce import (
    _HarExchange,
    _infer_effect_routes,
    _shows_item_route,
)
from openadapt_flow.tx.judge import FrameAndIntentJudge
from openadapt_flow.tx.read_probe import probe_read_routes
from openadapt_flow.tx.wire_tap import ReadRoute, matches_read_route


def test_judge_allows_hal_embedded_and_links_mirror_of_intended_field() -> None:
    """Issue #2442: HAL copies of an intended field in _embedded/_links are not clobbered."""
    judge = FrameAndIntentJudge()
    r0 = {
        "id": 7,
        "subject": "Old title",
        "priority": "normal",
        "_embedded": {"self_copy": {"subject": "Old title"}},
        "_links": {"self": {"title": "Old title"}},
    }
    r1 = {
        "id": 7,
        "subject": "New title",
        "priority": "normal",
        "_embedded": {"self_copy": {"subject": "New title"}},
        "_links": {"self": {"title": "New title"}},
    }
    verdict = judge.evaluate(r0, r1, {"subject": "New title"})
    assert verdict.passed is True
    assert verdict.clobbered_fields == ()

    # A real clobber inside _embedded (unrelated to the intended change) still fails.
    r1_clobbered = {
        "id": 7,
        "subject": "New title",
        "priority": "normal",
        "_embedded": {"self_copy": {"subject": "Unexpected unrelated value"}},
        "_links": {"self": {"title": "New title"}},
    }
    bad = judge.evaluate(r0, r1_clobbered, {"subject": "New title"})
    assert bad.passed is False
    assert "_embedded.self_copy.subject" in bad.clobbered_fields


def test_probe_read_routes_learns_rpc_fetch_without_constant_body_field() -> None:
    """Issues #2443 and #2444: POST .../<id>/fetch with varying body is learned as path_only read route."""
    writes = 0

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

        def do_POST(self) -> None:  # noqa: N802
            nonlocal writes
            length = int(self.headers.get("Content-Length", "0") or 0)
            _ = self.rfile.read(length) if length else b""
            if self.path.endswith("/fetch") or self.path == "/ws/meta/view":
                body = json.dumps({"status": 0, "data": [{"id": 42}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            writes += 1
            body = json.dumps({"status": 0, "written": writes}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        har = {
            "log": {
                "entries": [
                    {
                        "request": {
                            "method": "POST",
                            "url": f"{base}/ws/rest/Partner/42/fetch",
                            "headers": [
                                {"name": "Content-Type", "value": "application/json"}
                            ],
                            "postData": {"text": json.dumps({"fields": ["name"]})},
                        },
                        "response": {"status": 200},
                    },
                    {
                        "request": {
                            "method": "POST",
                            "url": f"{base}/ws/rest/Partner/42/fetch",
                            "headers": [
                                {"name": "Content-Type", "value": "application/json"}
                            ],
                            "postData": {"text": json.dumps({"fields": ["email"]})},
                        },
                        "response": {"status": 200},
                    },
                    {
                        "request": {
                            "method": "POST",
                            "url": f"{base}/ws/meta/view",
                            "headers": [
                                {"name": "Content-Type", "value": "application/json"}
                            ],
                            "postData": {"text": json.dumps({"name": "partner-form"})},
                        },
                        "response": {"status": 200},
                    },
                    {
                        "request": {
                            "method": "POST",
                            "url": f"{base}/ws/meta/view",
                            "headers": [
                                {"name": "Content-Type", "value": "application/json"}
                            ],
                            "postData": {"text": json.dumps({"name": "order-form"})},
                        },
                        "response": {"status": 200},
                    },
                ]
            }
        }
        report = probe_read_routes(
            har,
            sandbox_base_url=base,
            store_digest=lambda: str(writes),
            store_reset=lambda: None,
        )
        learned = report.read_routes
        assert len(learned) == 2
        assert all(r.path_only for r in learned)
        # Generalized /{id}/fetch matches another record ID without consuming mutation budget.
        assert matches_read_route(
            learned,
            "POST",
            "/ws/rest/Partner/99/fetch",
            b'{"fields":["phone"]}',
            [("Content-Type", "application/json")],
        )
        assert matches_read_route(
            learned,
            "POST",
            "/ws/meta/view",
            b'{"name":"other-view"}',
            [("Content-Type", "application/json")],
        )
        # Non-read write route is NOT matched.
        assert not matches_read_route(
            learned,
            "POST",
            "/ws/rest/Partner/99",
            b'{"data":{"name":"Edited"}}',
            [("Content-Type", "application/json")],
        )
    finally:
        server.shutdown()
        server.server_close()


def test_read_route_still_requires_body_equals_when_not_path_only() -> None:
    with pytest.raises(ValueError, match="body_equals"):
        ReadRoute(method="POST", path="/rpc", body_equals=())


def test_induce_recognizes_item_routes_with_verb_suffix() -> None:
    """Issue #2445: item routes of the form /{id}/<verb> (e.g. /{id}/fetch) are supported."""
    ex_fetch = _HarExchange(
        seq=0,
        method="POST",
        origin="http://app.local",
        path="/ws/rest/Partner/42/fetch",
        segments=("ws", "rest", "Partner", "42", "fetch"),
        query_params={},
        req_data={"fields": ["name"]},
        resp_status=200,
        resp_json={"id": 42, "name": "Before"},
        etag_header=None,
    )
    ex_save = _HarExchange(
        seq=1,
        method="POST",
        origin="http://app.local",
        path="/ws/rest/Partner/42",
        segments=("ws", "rest", "Partner", "42"),
        query_params={},
        req_data={"name": "After"},
        resp_status=200,
        resp_json={"id": 42, "name": "After"},
        etag_header=None,
    )
    assert _shows_item_route([ex_fetch]) is True
    routes = _infer_effect_routes([ex_fetch, ex_save])
    assert routes is not None
    assert routes.parent == ("ws", "rest", "Partner")
    assert routes.id_field == "id"
