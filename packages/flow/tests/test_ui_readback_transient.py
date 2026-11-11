"""The separate-session read on a flaky network, in a real headless browser.

A small local server stands in for an application behind a bad VPN: the
first sign-in and the first record load answer 502, and the landing page
keeps posting a counter after sign-in. A transient gateway error is retried;
a post from a page the session already left is still refused but doesn't spoil
the record read; a post from the record page itself still does.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator

import pytest

pytest.importorskip("playwright.sync_api")

from openadapt_flow.runtime.effects import (  # noqa: E402
    Effect,
    EffectKind,
    EffectState,
    ValueExpr,
    Verdict,
)
from openadapt_flow.runtime.effects.ui_readback import (  # noqa: E402
    UiReadbackSpec,
    build_ui_readback_verifier,
)

SIGN_IN = """<form method=post action=/login><input id=u name=u><input id=p name=p>
<button id=go>go</button></form>"""
LANDING = """<div id=home>home</div><script>
setInterval(() => fetch('/counter', {method: 'POST'}).catch(() => {}), 50);
</script>"""
RECORD = """<title>Record</title><table id=list><tr class=row><td class=name>Ada</td>
<td class=phone>555-0101</td></tr></table>{extra}"""
POSTS_ITSELF = "<script>fetch('/track', {method: 'POST'}).catch(() => {});</script>"


@pytest.fixture()
def app() -> Iterator[dict[str, Any]]:
    state: dict[str, Any] = {"fail_login": 1, "fail_record": 1, "record_posts": False}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a: object) -> None:
            pass

        def _send(self, code: int, body: str, extra: dict[str, str] = {}) -> None:
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(data)))
            for k, v in extra.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/signin"):
                self._send(200, SIGN_IN)
            elif self.path.startswith("/home"):
                self._send(200, LANDING)
            elif self.path.startswith("/record"):
                if state["fail_record"]:
                    state["fail_record"] -= 1
                    self._send(502, "bad gateway")
                    return
                extra = POSTS_ITSELF if state["record_posts"] else ""
                self._send(200, RECORD.format(extra=extra))
            else:
                self._send(404, "no")

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            if self.path == "/login":
                if state["fail_login"]:
                    state["fail_login"] -= 1
                    self._send(502, "bad gateway")
                    return
                self._send(303, "", {"Location": "/home"})
            else:
                state.setdefault("writes", []).append(self.path)
                self._send(200, "ok")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state["base"] = f"http://127.0.0.1:{server.server_address[1]}"
    yield state
    server.shutdown()


def _verify(app: dict[str, Any]) -> Any:
    base = app["base"]
    spec = UiReadbackSpec.model_validate(
        {
            "record_url": base + "/record",
            "sign_in": {
                "url": base + "/signin",
                "post_url": base + "/login",
                "username_selector": "#u",
                "password_selector": "#p",
                "submit_selector": "#go",
                "signed_in_selector": "#home",
                "username_env": "R_USER",
                "password_env": "R_PASS",
            },
            "ready_selector": "#list",
            "row_selector": "tr.row",
            "fields": {"name": {"selector": ".name"}, "phone": {"selector": ".phone"}},
            "identity": {"reader": {"selector": "title"}, "value": "Record"},
            "navigation_timeout_s": 5.0,
        }
    )
    verifier = build_ui_readback_verifier(
        spec, {}, timeout_s=1.0, environ={"R_USER": "r", "R_PASS": "p"}
    )
    effect = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={
            "name": ValueExpr(literal="Ada"),
            "phone": ValueExpr(literal="555-0101"),
        },
        forbid_collateral_loss=False,
        timeout_s=0.0,
    )
    return verifier.verify(effect, EffectState(substrate="ui-readback", reachable=True))


def test_gateway_errors_and_a_chatty_landing_page_still_verify(app) -> None:
    verdict = _verify(app)
    assert verdict.verdict is Verdict.CONFIRMED, verdict.reason
    assert app["fail_login"] == 0 and app["fail_record"] == 0
    # The landing page's posts were all refused: nothing reached the server.
    assert not app.get("writes")


def test_a_record_page_that_posts_still_stops(app) -> None:
    app["record_posts"] = True
    verdict = _verify(app)
    assert verdict.verdict is Verdict.INDETERMINATE
    assert "blocked" in verdict.reason
    assert not app.get("writes")
