"""Classification and forwarding must agree on the complete request target."""

from __future__ import annotations

import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import urlencode, urlsplit

import pytest

from openadapt_flow.tx.wire_tap import SessionWireTap, WirePolicy

READ = "query Read { item { id } }"
WRITE = "mutation Write { changeItem { id } }"
MIXED = READ + " " + WRITE


@pytest.fixture
def upstream():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def dispatch(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            seen.append((self.command, self.path, body))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(b"{}")

        do_GET = do_HEAD = do_OPTIONS = do_POST = dispatch

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def send(tap, method, target, body=None):
    parts = urlsplit(tap.base_url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=5)
    try:
        conn.request(method, target, body, {"Content-Type": "application/json"})
        response = conn.getresponse()
        response.read()
        return response.status
    finally:
        conn.close()


def targets(tap, origin, path, query):
    suffix = f"{path}?{query}"
    return (suffix, f"{origin}/prefix{suffix}", f"{tap.base_url}{suffix}")


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
@pytest.mark.parametrize("path", ["/graphql", "/api/gql"])
def test_zero_budget_blocks_query_mutations_before_any_upstream_call(
    upstream, method, path
):
    origin, seen = upstream
    queries = [
        urlencode({"query": WRITE}),
        urlencode({"query": WRITE}).replace("query=", "%71uery="),
        urlencode({"query": MIXED, "operationName": "Write"}),
        urlencode([("query", READ), ("query", WRITE)]),
        urlencode([("query", WRITE), ("query", READ)]),
        urlencode(
            [("query", MIXED), ("operationName", "Read"), ("operationName", "Write")]
        ),
    ]
    with SessionWireTap(origin + "/prefix", WirePolicy(max_mutations=0)) as tap:
        for query in queries:
            for target in targets(tap, origin, path, query):
                assert send(tap, method, target) == 403
                assert seen == []
        summary = tap.summary()
        assert summary.mutations_allowed == summary.read_requests == 0
        assert summary.mutations_blocked == 3 * len(queries)
        assert all(
            item.block_reason == "max_mutations_exceeded" for item in summary.mutations
        )


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
@pytest.mark.parametrize("path", ["/graphql", "/api/gql"])
def test_read_queries_keep_exact_routing_and_do_not_consume_write_budget(
    upstream, method, path
):
    origin, seen = upstream
    query = urlencode(
        [("query", MIXED), ("operationName", "Read"), ("tag", "a+b"), ("tag", "c d")]
    )
    with SessionWireTap(origin + "/prefix", WirePolicy(max_mutations=0)) as tap:
        for target in targets(tap, origin, path, query):
            assert send(tap, method, target) == 200
            assert seen[-1] == (method, f"/prefix{path}?{query}", b"")
        assert len(seen) == 3
        assert tap.summary().read_requests == 3
        assert tap.summary().mutations_allowed == tap.summary().mutations_blocked == 0


def test_ordinary_query_decoding_json_posts_and_policy_boundaries(upstream):
    origin, seen = upstream
    with SessionWireTap(
        origin, WirePolicy(max_mutations=0, secrets=("private-marker",))
    ) as tap:
        # Decode URL parameters once, as the classifier's query protocol requires.
        for query in (
            "%2571uery=" + urlencode({"v": WRITE})[2:],
            "query=ordinary+search",
        ):
            assert send(tap, "GET", "/api/gql?" + query) == 200
        assert len(seen) == 2
        for path in ("/graphql", "/api/gql"):
            assert send(tap, "POST", path, json.dumps({"query": READ}).encode()) == 200
            before = len(seen)
            assert send(tap, "POST", path, json.dumps({"query": WRITE}).encode()) == 403
            assert len(seen) == before
        before = len(seen)
        assert send(tap, "GET", "/api/gql?query=private-marker") == 403
        assert (
            send(
                tap,
                "GET",
                "http://example.invalid/api/gql?" + urlencode({"query": READ}),
            )
            == 403
        )
        assert len(seen) == before
        assert "private-marker" not in json.dumps(tap.export_har())


class Route:
    def __init__(self, method, url):
        self.request = SimpleNamespace(
            url=url, method=method, headers={}, post_data_buffer=None
        )
        self.events = []

    def abort(self):
        self.events.append("abort")

    def fetch(self, *, max_redirects):
        assert max_redirects == 0
        self.events.append("fetch")
        return SimpleNamespace(status=200, headers={}, body=lambda: b"{}")

    def fulfill(self, *, response):
        assert response.status == 200
        self.events.append("fulfill")


@pytest.mark.parametrize("entrypoint", ["record", "har", "route"])
@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_all_entrypoints_share_stateful_budget_and_write_route_policy(
    entrypoint, method
):
    origin = "http://127.0.0.1:9"
    tap = SessionWireTap(
        origin,
        WirePolicy(max_mutations=1, allowed_write_routes=((method, "/api/gql"),)),
    )
    page = SimpleNamespace(route=lambda _pattern, _handler: None)
    handler = tap.attach_playwright_route(page)
    expected = [(READ, False), (WRITE, False), (WRITE, True), (READ, False)]
    for document, blocked in expected:
        url = origin + "/api/gql?" + urlencode({"query": document})
        if entrypoint == "route":
            route = Route(method, url)
            handler(route)
            assert route.events == (["abort"] if blocked else ["fetch", "fulfill"])
        elif entrypoint == "har":
            tap.import_har(
                {
                    "log": {
                        "entries": [
                            {
                                "request": {"method": method, "url": url},
                                "response": {"status": 200},
                            }
                        ]
                    }
                }
            )
        else:
            record = tap.record_exchange(method, url, response_status=200)
            assert (record is not None) == (document == WRITE)
            if record is not None:
                assert record.blocked is blocked
    summary = tap.summary()
    assert summary.read_requests == 2
    assert summary.mutations_allowed == summary.mutations_blocked == 1
    assert all(item.is_graphql_mutation for item in summary.mutations)
    assert summary.mutations[-1].block_reason == "max_mutations_exceeded"
    # Reset is an explicit new run scope; route policy still applies before budget.
    tap.reset_session()
    route = Route(method, origin + "/other?" + urlencode({"query": WRITE}))
    handler(route)
    assert route.events == ["abort"]
    assert tap.summary().mutations[0].block_reason == "undeclared_write_route"
    assert tap.summary().mutations_allowed == 0


@pytest.mark.parametrize("suffix", [":notaport/rows", ":99999/rows", ":-1/rows"])
def test_absolute_target_with_an_unparseable_origin_is_refused(upstream, suffix):
    origin, seen = upstream
    host = urlsplit(origin).hostname
    with SessionWireTap(origin, WirePolicy(max_mutations=None)) as tap:
        status = send(tap, "GET", f"http://{host}{suffix}")
    assert status >= 400
    assert seen == []
