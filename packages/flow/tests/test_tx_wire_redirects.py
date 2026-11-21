"""Redirect relay through ``SessionWireTap`` (OpenAdapt #2388).

Server-rendered form apps answer a login or save ``POST`` with ``302`` and a
``Location`` on the same origin (Post/Redirect/Get). The tap used to answer
every upstream redirect with ``502``, so no such app could be fronted. It now
hands a same-origin redirect back to the client, never follows one itself,
and still refuses a redirect that would leave the tapped origin.

The live test runs against a real application when
``OAFLOW_TX_LIVE_REDIRECT_URL`` names a page on a local copy that redirects
(for example a login page root). It is skipped otherwise.
"""

from __future__ import annotations

import http.client
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator, Optional
from urllib.parse import urljoin, urlsplit

import pytest

from openadapt_flow.tx.wire_tap import (
    SessionWireTap,
    WirePolicy,
    relay_redirect_location,
)


class _Upstream:
    def __init__(self, location_for: dict[str, str]) -> None:
        self.hits: list[tuple[str, str]] = []
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                upstream.hits.append((self.command, self.path))
                location = location_for.get(self.path)
                if location is not None:
                    self.send_response(302)
                    self.send_header(
                        "Location", location.replace("{self}", upstream.origin)
                    )
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = _serve
            do_POST = _serve

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture()
def upstream() -> Iterator[_Upstream]:
    up = _Upstream(
        {
            "/login": "/home?tab=1",
            "/save": "{self}/record/7?saved=1",
            "/dotdot": "../elsewhere",
            "/foreign": "http://127.0.0.1:1/steal",
            "/protocol-relative": "//127.0.0.1:1/steal",
            "/scheme-change": "https://127.0.0.1/steal",
        }
    )
    try:
        yield up
    finally:
        up.close()


def _call(
    base_url: str, method: str, path: str, body: Optional[bytes] = None
) -> tuple[int, dict[str, str], bytes]:
    parts = urlsplit(base_url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    try:
        conn.request(method, path, body=body, headers={"Content-Type": "text/plain"})
        resp = conn.getresponse()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
    finally:
        conn.close()


def test_relative_redirect_is_relayed_unchanged_and_not_followed(upstream):
    with SessionWireTap(upstream.origin) as tap:
        status, headers, _ = _call(tap.base_url, "POST", "/login", b"user=synthetic")
    assert status == 302
    assert headers["location"] == "/home?tab=1"
    assert upstream.hits == [("POST", "/login")]


def test_absolute_same_origin_redirect_is_rewritten_onto_the_tap(upstream):
    with SessionWireTap(upstream.origin) as tap:
        status, headers, _ = _call(tap.base_url, "POST", "/save", b"x=1")
        assert status == 302
        assert headers["location"] == f"{tap.base_url}/record/7?saved=1"
        # The client's next request goes back through the tap and is recorded.
        follow = urlsplit(headers["location"])
        status2, _, body2 = _call(tap.base_url, "GET", f"{follow.path}?{follow.query}")
        summary = tap.summary()
    assert status2 == 200 and body2 == b'{"ok":true}'
    assert upstream.hits == [("POST", "/save"), ("GET", "/record/7?saved=1")]
    assert summary.total_requests == 2


def test_dot_segment_redirect_stays_relative(upstream):
    with SessionWireTap(upstream.origin) as tap:
        status, headers, _ = _call(tap.base_url, "GET", "/dotdot")
    assert status == 302 and headers["location"] == "../elsewhere"


@pytest.mark.parametrize("path", ["/foreign", "/protocol-relative", "/scheme-change"])
def test_redirect_off_the_tapped_origin_is_still_refused(upstream, path):
    with SessionWireTap(upstream.origin) as tap:
        status, headers, body = _call(tap.base_url, "GET", path)
    assert status == 502
    assert "location" not in headers
    assert body == b'{"error":"redirect_refused"}'
    assert upstream.hits == [("GET", path)]


def test_a_redirected_mutation_still_counts_against_the_budget(upstream):
    policy = WirePolicy(max_mutations=1)
    with SessionWireTap(upstream.origin, policy) as tap:
        first, _, _ = _call(tap.base_url, "POST", "/login", b"a=1")
        second, _, _ = _call(tap.base_url, "POST", "/login", b"a=2")
        summary = tap.summary()
    assert first == 302 and second == 403
    assert summary.mutations_allowed == 1 and summary.mutations_blocked == 1
    assert upstream.hits == [("POST", "/login")]


@pytest.mark.parametrize(
    "location",
    ["", "   ", "/ok\r\nSet-Cookie: x=1", "javascript:alert(1)", "data:text/html,x"],
)
def test_relay_helper_refuses_malformed_or_non_http_targets(location):
    origin = "http://127.0.0.1:8000"
    assert (
        relay_redirect_location(
            location,
            request_url=f"{origin}/form",
            allowed_origins=frozenset({origin}),
        )
        is None
    )


def test_browser_route_relays_same_origin_redirect_and_aborts_foreign():
    origin = "http://127.0.0.1:8000"

    class Response:
        def __init__(self, location: str) -> None:
            self.status = 302
            self.headers = {"location": location}
            self.body = b""

    class Request:
        def __init__(self, url: str) -> None:
            self.url = url
            self.method = "POST"
            self.headers = {"content-type": "text/plain"}
            self.post_data_buffer = b"x=1"

    class Route:
        def __init__(self, url: str, location: str) -> None:
            self.request = Request(url)
            self._location = location
            self.fulfilled: Optional[Response] = None
            self.aborted = False

        def fetch(self, *, max_redirects: int) -> Response:
            assert max_redirects == 0
            return Response(self._location)

        def fulfill(self, *, response: Response) -> None:
            self.fulfilled = response

        def abort(self) -> None:
            self.aborted = True

        def continue_(self) -> None:  # pragma: no cover - must never be called
            raise AssertionError("the tap must not continue a route unobserved")

    class Page:
        def route(self, pattern: str, handler: Any) -> None:
            self.handler = handler

    page = Page()
    tap = SessionWireTap(origin, WirePolicy(max_mutations=None))
    tap.attach_playwright_route(page)
    same = Route(f"{origin}/save", "/record/7")
    foreign = Route(f"{origin}/save", "http://127.0.0.1:1/steal")
    page.handler(same)
    page.handler(foreign)
    assert same.fulfilled is not None and not same.aborted
    assert foreign.aborted and foreign.fulfilled is None
    statuses = [m.response_status for m in tap.summary().mutations]
    assert statuses == [302, 302]


LIVE_URL = os.environ.get("OAFLOW_TX_LIVE_REDIRECT_URL", "").strip()


@pytest.mark.skipif(not LIVE_URL, reason="set OAFLOW_TX_LIVE_REDIRECT_URL")
def test_live_application_redirect_is_relayed_and_followable():
    """A real app's own redirect passes through the tap and lands on a page."""
    parts = urlsplit(LIVE_URL)
    origin = f"{parts.scheme}://{parts.netloc}"
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    direct_status, direct_headers, _ = _call(origin, "GET", path)
    assert 300 <= direct_status < 400, "the live URL must redirect"
    with SessionWireTap(origin) as tap:
        status, headers, _ = _call(tap.base_url, "GET", path)
        assert status == direct_status
        location = headers["location"]
        target = urlsplit(urljoin(f"{tap.base_url}{parts.path or '/'}", location))
        assert f"{target.scheme}://{target.netloc}" == tap.base_url
        follow_path = (target.path or "/") + (
            f"?{target.query}" if target.query else ""
        )
        follow_status, _, follow_body = _call(tap.base_url, "GET", follow_path)
        summary = tap.summary()
    assert follow_status == 200 and follow_body
    assert summary.total_requests == 2
