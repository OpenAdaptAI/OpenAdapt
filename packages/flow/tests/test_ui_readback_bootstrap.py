"""Synthetic sibling documents: immediate writes before any later init registration."""

import html
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

from openadapt_flow.runtime.effects import Effect, EffectKind, EffectState, Verdict
from openadapt_flow.runtime.effects import ui_readback as ui


@pytest.mark.parametrize(
    "delivery,transport",
    [
        ("redirect", "fetch"),
        ("body", "fetch"),
        ("redirect", "native"),
        ("body", "native"),
        ("body", "submit"),
        ("body", "spoof"),
        ("body", "override-get"),
        ("body", "override-read"),
        ("body", "broken-bootstrap"),
    ],
)
def test_first_authenticated_document_is_read_only(delivery, transport, monkeypatch):
    pytest.importorskip("playwright.sync_api")
    endpoint = "/login?flow=reader&flow=check"
    state = {"posts": [], "guards": []}
    original = ui._PlaywrightSession._guard

    def traced(self, route, request):
        state["guards"].append(
            (request.method, urlsplit(request.url).path, self._phase)
        )
        return original(self, route, request)

    monkeypatch.setattr(ui._PlaywrightSession, "_guard", traced)
    form_method = "get" if transport == "override-get" else "post"
    form_action = "/read" if transport == "override-read" else endpoint
    override = (
        ' formmethod="post" formaction="/counter"'
        if transport in {"override-get", "override-read"}
        else ""
    )
    form = (
        f'<form id=background method="{form_method}" action="'
        + html.escape(form_action, quote=True)
        + '"><button id=again'
        + override
        + ">again</button></form>"
    )
    if transport == "broken-bootstrap":
        original_policy = ui._no_writes_script

        def broken_policy(target):
            # A failed transport-hook installation must leave arming disabled.
            return (
                "Object.defineProperty(Navigator.prototype,'sendBeacon',"
                "{value:()=>false,configurable:false,writable:false});"
                + original_policy(target)
            )

        monkeypatch.setattr(ui, "_no_writes_script", broken_policy)
    if transport == "fetch":
        script = "fetch('/counter',{method:'POST'}).catch(()=>{}); const x=new XMLHttpRequest();x.open('POST','/counter');try{x.send()}catch{};navigator.sendBeacon('/counter','x');"
    else:
        action = "f.submit()" if transport == "submit" else "f.requestSubmit(b)"
        arm = "window.__openadaptReadbackAuth?.arm(b);" if transport == "spoof" else ""
        script = (
            "const f=document.querySelector('#background'),b=document.querySelector('#again');"
            + arm
            + "try{"
            + action
            + "}catch{};setInterval(()=>{try{"
            + action
            + "}catch{}},5);"
        )
    home = "<div id=home>home</div>" + form + "<script>" + script + "</script>"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, code, body, location=None, cookie=False):
            raw = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(raw)))
            if location:
                self.send_header("Location", location)
            if cookie:
                self.send_header("Set-Cookie", "auth_test=1; Path=/; HttpOnly")
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.path == "/signin":
                self.send(
                    200,
                    '<form method=post action="'
                    + html.escape(endpoint, quote=True)
                    + '"><input id=u><input id=p><button id=go>go</button></form>',
                )
            elif self.path == "/home":
                self.send(200, home)
            elif self.path == "/record" and "auth_test=1" in self.headers.get(
                "Cookie", ""
            ):
                self.send(
                    200,
                    "<title>Record</title><table id=list><tr class=row><td class=value>expected</td></tr></table>",
                )
            else:
                self.send(404, "missing")

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            state["posts"].append(self.path)
            if self.path == endpoint:
                self.send(
                    303 if delivery == "redirect" else 200,
                    "" if delivery == "redirect" else home,
                    "/home" if delivery == "redirect" else None,
                    True,
                )
            else:
                self.send(200, "unexpected write")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        spec = ui.UiReadbackSpec.model_validate(
            {
                "record_url": base + "/record",
                "sign_in": {
                    "url": base + "/signin",
                    "post_url": base + endpoint,
                    "username_selector": "#u",
                    "password_selector": "#p",
                    "submit_selector": "#go",
                    "signed_in_selector": "#home",
                    "username_env": "AUTH_TEST_USER",
                    "password_env": "AUTH_TEST_PASS",
                },
                "ready_selector": "#list",
                "row_selector": ".row",
                "fields": {"value": {"selector": ".value"}},
                "identity": {"reader": {"selector": "title"}, "value": "Record"},
                "navigation_timeout_s": 1.0,
                "read_post_paths": ["/read"],
            }
        )
        verifier = ui.build_ui_readback_verifier(
            spec,
            {},
            timeout_s=4.0,
            environ={"AUTH_TEST_USER": "synthetic", "AUTH_TEST_PASS": "synthetic"},
        )
        result = verifier.verify(
            Effect(
                kind=EffectKind.RECORD_WRITTEN,
                match={"value": "expected"},
                forbid_collateral_loss=False,
                timeout_s=0.0,
            ),
            EffectState(substrate="ui-readback", reachable=True),
        )
        print(
            json.dumps(
                {
                    "delivery": delivery,
                    "transport": transport,
                    "verdict": result.verdict.value,
                    "reason": result.reason,
                    **state,
                }
            )
        )
        refused = transport in {"spoof", "broken-bootstrap"}
        assert state["posts"] == ([] if transport == "broken-bootstrap" else [endpoint])
        assert result.verdict is (
            Verdict.INDETERMINATE if refused else Verdict.CONFIRMED
        )
        if not refused:
            assert [(m, p) for m, p, _ in state["guards"] if m == "POST"] == [
                ("POST", "/login")
            ]
    finally:
        server.shutdown()
        server.server_close()
        worker.join()
