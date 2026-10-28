"""A taught write sends only its taught requests, and sign-in values stay put.

From the #1589 review, in real headless Chromium against a small app with a
sign-in, a GET search box, a POST order form and a POST "Close account"
button. The server counts what it receives.

* Teaching: a write the contract doesn't list (Close account) is stopped
  before it is sent, and run 1 ends.
* Teaching: a sign-in value can't be typed into the search box (it would go
  into an address) or into the order form; a request whose address carries
  one, in any encoding, never leaves.
* Calling the tool: a page that adds a field to the order, or a second write,
  is stopped before it is sent, and the result is never done.
* No form of the password (raw, percent, form, JSON, base64) appears in any
  result. The password has @, a space and &.
* A verified result carries a Seal of its receipt and says the values were
  declared by the agent.
"""

from __future__ import annotations

import http.server
import json
import re
import threading
import urllib.parse
from pathlib import Path
from typing import Iterator

import pytest

pytest.importorskip("playwright")

from openadapt_flow.agent.bridge import BridgeError  # noqa: E402
from openadapt_flow.agent.browser_teach import BrowserTeachBridge  # noqa: E402
from openadapt_flow.verify import WriteContract  # noqa: E402
from openadapt_flow.wire_check import secret_forms  # noqa: E402

PASSWORD = "p@ss w0rd&x"


class _App:
    def __init__(self) -> None:
        self.orders: list[dict[str, str]] = []
        self.closed = 0
        self.mode = ""
        app = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_a: object) -> None:
                pass

            def _send(
                self, body: bytes, ctype: str = "text/html", code: int = 200
            ) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _page(self, title: str, body: str) -> None:
                extra = ""
                if app.mode == "extra_field":
                    extra = """<script>document.querySelector('form[action="/orders"]')
                      .addEventListener('submit', (e) => { const h = document.createElement('input');
                      h.type='hidden'; h.name='discount'; h.value='100'; e.target.appendChild(h); });
                      </script>"""
                if app.mode == "second_write":
                    extra = """<script>document.querySelector('form[action="/orders"]')
                      .addEventListener('submit', () => { fetch('/orders', {method: 'POST',
                      headers: {'Content-Type': 'application/json'},
                      body: JSON.stringify({customer: 'ghost', note: 'x'})}); });</script>"""
                self._send(
                    f"<!doctype html><title>{title}</title><body>{body}{extra}</body>".encode()
                )

            def do_GET(self) -> None:  # noqa: N802
                path = urllib.parse.urlsplit(self.path).path
                if path == "/login":
                    self._page(
                        "Sign in",
                        '<form method="post" action="/login"><label>Username <input name="u">'
                        '</label><label>Password <input type="password" name="p"></label>'
                        "<button>Sign in</button></form>",
                    )
                elif path in ("/app", "/search"):
                    self._page(
                        "App",
                        '<form method="get" action="/search"><label>Search <input name="q">'
                        "</label><button>Find</button></form>"
                        '<form method="post" action="/orders"><label>Customer <input '
                        'name="customer"></label><button>Save order</button></form>'
                        '<form method="post" action="/account/close"><button>Close account'
                        "</button></form>",
                    )
                elif path == "/api/orders":
                    self._send(
                        json.dumps({"records": app.orders}).encode(), "application/json"
                    )
                else:
                    self._send(b"nf", code=404)

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode()
                if "json" in (self.headers.get("Content-Type") or ""):
                    form = json.loads(raw)
                else:
                    form = dict(urllib.parse.parse_qsl(raw))
                if self.path == "/orders":
                    app.orders.append({"customer": form.get("customer", "")})
                elif self.path == "/account/close":
                    app.closed += 1
                self.send_response(303)
                self.send_header("Location", "/app")
                self.end_headers()

        self.http = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.http.server_address[1]}"


@pytest.fixture
def app() -> Iterator[_App]:
    server = _App()
    yield server
    server.http.shutdown()


def _contract(base: str) -> WriteContract:
    return WriteContract.from_dict(
        {
            "schema_version": "openadapt.verify/v1",
            "writes": ["POST /login", "POST /orders"],
            "verifier": {
                "kind": "rest",
                "base_url": base,
                "records_path": "/api/orders",
                "records_key": "records",
            },
            "effects": [
                {
                    "kind": "record_written",
                    "match": {"customer": {"param": "customer"}},
                    "expected_count": 1,
                    "count_new_only": True,
                    "timeout_s": 1.0,
                }
            ],
        },
        name="order",
    )


def _ref(listing: str, label: str) -> str:
    for line in listing.splitlines():
        found = re.match(r"\[(e\d+)\] \S+ \"" + re.escape(label) + '"', line)
        if found:
            return found.group(1)
    raise AssertionError(f"no {label!r}:\n{listing}")


@pytest.fixture
def bridge(
    app: _App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> BrowserTeachBridge:
    monkeypatch.setenv("P_USER", "clerk")
    monkeypatch.setenv("P_PW", PASSWORD)
    monkeypatch.setenv("OPENADAPT_SEAL_HOME", str(tmp_path / "seal"))
    return BrowserTeachBridge(
        tmp_path / "tools",
        tmp_path / "files",
        contracts={"order": _contract(app.base)},
        secrets={"username": "P_USER", "password": "P_PW"},
        verify_dir=tmp_path / "verify",
    )


def _start(bridge: BrowserTeachBridge, app: _App) -> tuple[str, list[str]]:
    seen: list[str] = []
    started = bridge.dispatch(
        "teach_start",
        {
            "name": "add_order",
            "url": f"{app.base}/login",
            "task": "Add an order",
            "contract": "order",
            "inputs": {"customer": "Acme"},
        },
    )
    seen.append(json.dumps(started))
    page = started["page"]
    for args in (
        {"action": "type", "ref": _ref(page, "Username"), "input": "username"},
        {"action": "type", "ref": _ref(page, "Password"), "input": "password"},
    ):
        result = bridge.dispatch("teach_act", args)
        seen.append(json.dumps(result))
        page = result["page"]
    result = bridge.dispatch(
        "teach_act", {"action": "click", "ref": _ref(page, "Sign in")}
    )
    seen.append(json.dumps(result))
    return result["page"], seen


def _assert_no_password(texts: list[str]) -> None:
    for text in texts:
        for form in secret_forms(PASSWORD):
            assert form not in text, form


def test_an_undeclared_write_is_never_sent(
    bridge: BrowserTeachBridge, app: _App
) -> None:
    page, seen = _start(bridge, app)
    page = bridge.dispatch(
        "teach_act",
        {"action": "type", "ref": _ref(page, "Customer"), "input": "customer"},
    )["page"]
    page = bridge.dispatch(
        "teach_act", {"action": "click", "ref": _ref(page, "Save order")}
    )["page"]
    with pytest.raises(
        BridgeError, match="POST /account/close, which the write contract"
    ):
        bridge.dispatch(
            "teach_act", {"action": "click", "ref": _ref(page, "Close account")}
        )
    assert app.closed == 0
    _assert_no_password(seen)


def test_a_sign_in_value_stays_out_of_addresses_and_records(
    bridge: BrowserTeachBridge, app: _App
) -> None:
    page, seen = _start(bridge, app)
    for label in ("Search", "Customer"):
        with pytest.raises(
            BridgeError, match="sign-in value goes only into"
        ) as refused:
            bridge.dispatch(
                "teach_act",
                {"action": "type", "ref": _ref(page, label), "input": "password"},
            )
        seen.append(str(refused.value))
    # Even typed as plain text, a request carrying it never leaves.
    page = bridge.dispatch(
        "teach_act", {"action": "type", "ref": _ref(page, "Search"), "text": PASSWORD}
    )["page"]
    with pytest.raises(BridgeError, match="sign-in value in its address") as ended:
        bridge.dispatch("teach_act", {"action": "click", "ref": _ref(page, "Find")})
    seen.append(str(ended.value))
    _assert_no_password(seen)


@pytest.mark.parametrize("mode", ["extra_field", "second_write"])
def test_a_taught_write_sends_only_what_it_was_taught(
    bridge: BrowserTeachBridge, app: _App, mode: str
) -> None:
    page, seen = _start(bridge, app)
    page = bridge.dispatch(
        "teach_act",
        {"action": "type", "ref": _ref(page, "Customer"), "input": "customer"},
    )["page"]
    bridge.dispatch("teach_act", {"action": "click", "ref": _ref(page, "Save order")})
    finished = bridge.dispatch("teach_finish", {})
    seen.append(json.dumps(finished))
    assert finished["status"] == "ready", finished
    program = json.loads(Path(finished["program"]).read_text())
    assert [w["write"] for w in program["writes"]] == ["POST /login", "POST /orders"]

    good = bridge.dispatch("taught_add_order", {"inputs": {"customer": "Birch"}})
    seen.append(json.dumps(good))
    assert good["outcome"] == "done", good
    assert good["values_declared_by"].startswith("agent")
    assert good["authorization"] == "unbound"
    assert good["seal_path"] and Path(good["seal_path"]).exists()

    app.mode = mode
    bad = bridge.dispatch("taught_add_order", {"inputs": {"customer": "Cedar"}})
    seen.append(json.dumps(bad))
    assert bad["outcome"] == "needs_review", bad
    assert bad["verified"] is False
    assert "it was not sent" in bad["what_happened"]
    customers = [o["customer"] for o in app.orders]
    assert "ghost" not in customers  # the extra write never left the browser
    if mode == "extra_field":
        assert "Cedar" not in customers  # the changed save was stopped
    _assert_no_password(seen)
