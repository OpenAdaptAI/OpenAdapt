"""A one-use API boundary cannot hide redirects, retries or adapter resends."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from openadapt_flow.backend import StructuralResolutionRefused
from openadapt_flow.ir import ApiBinding
from openadapt_flow.runtime.actuators.api import ActuationStatus, ApiActuator


@pytest.mark.parametrize("redirect_status", [307, 308])
def test_a_redirect_cannot_resend_the_post_under_one_permission(redirect_status):
    requests_seen = []

    class Server(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            requests_seen.append(self.path)
            self.send_response(redirect_status if self.path == "/first" else 200)
            if self.path == "/first":
                self.send_header("Location", "/second")
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Server)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    boundaries = []

    def boundary(send):
        boundaries.append("input")
        return send()

    try:
        with requests.Session() as session:
            outcome = ApiActuator(
                f"http://127.0.0.1:{server.server_port}", session=session
            ).actuate(
                ApiBinding(method="POST", url_template="/first"),
                {},
                delivery_boundary=boundary,
                strict_delivery=True,
            )
        assert outcome.status is ActuationStatus.HALT
        assert requests_seen == ["/first"] and boundaries == ["input"]
    finally:
        server.shutdown()
        worker.join(timeout=3)
        server.server_close()


@pytest.mark.parametrize("resend", ["retry", "auth", "hook"])
def test_a_managed_transport_with_hidden_resends_refuses_before_permission(resend):
    with requests.Session() as session:
        if resend == "retry":
            session.mount("https://", HTTPAdapter(max_retries=Retry(total=2)))
        elif resend == "auth":
            session.auth = requests.auth.HTTPDigestAuth("synthetic-user", "synthetic")
        else:
            session.hooks["response"] = [lambda response, **_kwargs: response]
        boundaries = []
        with pytest.raises(StructuralResolutionRefused, match="retry or resend"):
            ApiActuator("https://unused.example", session=session).actuate(
                ApiBinding(method="POST", url_template="/save"),
                {},
                delivery_boundary=lambda call: boundaries.append(call),
                strict_delivery=True,
            )
        assert boundaries == []
