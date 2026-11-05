"""Declared auth authority and native form POST boundaries; synthetic localhost only."""

from __future__ import annotations

import html
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from openadapt_flow.runtime.effects import Effect, EffectKind, EffectState, Verdict
from openadapt_flow.runtime.effects.ui_readback import (
    PlaywrightReadbackDriver,
    ReadbackSessionError,
    UiReadbackSpec,
    _auth_url_key,
    _PlaywrightSession,
    build_ui_readback_verifier,
    resolve_spec,
)


def _spec(base: str, post_url: str | None) -> UiReadbackSpec:
    return UiReadbackSpec.model_validate(
        {
            "record_url": base + "/record",
            "sign_in": {
                "url": base + "/signin",
                "post_url": post_url,
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
        }
    )


@pytest.mark.parametrize(
    "endpoint",
    [
        "",
        "/login",
        "ftp://app.test/login",
        "http:///login",
        "http://user@app.test/login",
        "http://user:pass@app.test/login",
        "http://app.test/login#x",
        "http://app.test/login#",
        "http://app.test:99999/login",
        "http://app.test\\evil/login",
        "http://app.test/lo gin",
        "http://other.test/login",
    ],
)
def test_invalid_or_other_origin_auth_endpoint_refuses(endpoint: str) -> None:
    with pytest.raises(ValueError, match="post_url"):
        _spec("http://app.test", endpoint)


def test_auth_url_canonicalization_preserves_query_values_duplicates_and_order() -> (
    None
):
    assert _auth_url_key("HTTP://APP.TEST:80/login?a=1&a=2") == _auth_url_key(
        "http://app.test/login?a=1&a=2"
    )
    assert _auth_url_key("http://app.test/login?a=1&a=2") != _auth_url_key(
        "http://app.test/login?a=2&a=1"
    )
    assert _auth_url_key("http://app.test/login?a=%2F") != _auth_url_key(
        "http://app.test/login?a=/"
    )
    assert _auth_url_key("http://app.test:0/login") != _auth_url_key(
        "http://app.test/login"
    )


def test_missing_auth_declaration_refuses_before_browser_launch(monkeypatch) -> None:
    api = pytest.importorskip("playwright.sync_api")
    calls = []

    def forbidden():
        calls.append("launch")
        raise AssertionError("must not launch")

    monkeypatch.setattr(api, "sync_playwright", forbidden)
    session = _PlaywrightSession(
        PlaywrightReadbackDriver(),
        resolve_spec(_spec("http://app.test", None), {}),
        "synthetic",
        "synthetic",
    )
    with pytest.raises(ReadbackSessionError, match="post_url"):
        session.__enter__()
    assert calls == []
    assert session._username == session._password == ""


class _Route:
    def __init__(self):
        self.continued = self.aborted = 0

    def continue_(self):
        self.continued += 1

    def fetch(self, **kwargs):
        assert kwargs["max_redirects"] == kwargs["max_retries"] == 0
        self.continued += 1

        class Response:
            status = 200

            def dispose(self):
                pass

        return Response()

    def fulfill(self, **kwargs):
        assert kwargs["response"].status == 200

    def abort(self, reason):
        self.aborted += 1


class _Request:
    method = "POST"
    resource_type = "document"
    redirected_from = None

    def __init__(self, frame, *, url="http://app.test/login?a=1&a=2", navigation=True):
        self.frame, self.url, self.navigation = frame, url, navigation

    def is_navigation_request(self):
        return self.navigation


@pytest.mark.parametrize("bad", ["frame", "query", "xhr", "redirect"])
def test_auth_allowance_is_frame_endpoint_navigation_bound_and_single_use(bad) -> None:
    spec = _spec("http://app.test", "http://app.test/login?a=1&a=2")
    session = _PlaywrightSession(
        PlaywrightReadbackDriver(), resolve_spec(spec, {}), "synthetic", "synthetic"
    )
    frame = object()
    session._auth_allowance = (
        _auth_url_key(spec.sign_in.post_url),
        frame,
        session._auth_attempt,
    )
    wrong = _Request(
        object() if bad == "frame" else frame,
        navigation=bad != "xhr",
        url="http://app.test/login?a=2&a=1"
        if bad == "query"
        else spec.sign_in.post_url,
    )
    if bad == "redirect":
        wrong.redirected_from = object()
    denied = _Route()
    session._guard(denied, wrong)
    assert (denied.continued, denied.aborted) == (0, 1)
    first = _Route()
    session._guard(first, _Request(frame))
    assert (first.continued, first.aborted) == (1, 0)
    second = _Route()
    session._guard(second, _Request(frame))
    assert (second.continued, second.aborted) == (0, 1)
    assert session._auth_allowance is None


@pytest.fixture()
def auth_app(request):
    case = request.param
    endpoint = "/login?flow=reader&flow=check"
    state = {"posts": [], "post_records": [], "gets": [], "case": case}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, code, text, location=None):
            body = text.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            if location:
                self.send_header("Location", location)
                if code in {301, 302, 303}:
                    self.send_header("Set-Cookie", "auth_test=1; Path=/; HttpOnly")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            state["gets"].append(self.path)
            if self.path == "/signin":
                action = endpoint
                override = ""
                if case == "mismatch":
                    action = "/counter"
                if case == "query_mismatch":
                    action = "/login?flow=check&flow=reader"
                if case == "trusted_override":
                    action = "/counter"
                    override = ' formaction="' + html.escape(endpoint, quote=True) + '"'
                if case == "override_mismatch":
                    override = ' formaction="/counter"'
                if case == "method_override":
                    override = ' formmethod="get"'
                if case == "frame_override":
                    override = ' formtarget="_blank"'
                named = (
                    '<input name="action"><input name="method"><input name="target">'
                    if case == "named_controls"
                    else ""
                )
                script = ""
                if case == "background":
                    script = "<script>setInterval(() => fetch('/counter', {method:'POST'}).catch(()=>{}), 5)</script>"
                if case == "xhr":
                    script = (
                        "<script>document.addEventListener('submit', e => {e.preventDefault(); fetch('"
                        + endpoint
                        + "', {method:'POST'}).catch(()=>{});})</script>"
                    )
                self.send(
                    200,
                    '<form method="post" action="'
                    + html.escape(action, quote=True)
                    + '"><input id=u name=u><input id=p name=p>'
                    + named
                    + "<button id=go"
                    + override
                    + ">go</button></form>"
                    + script,
                )
            elif self.path in {
                "/home",
                "/record",
            } and "auth_test=1" not in self.headers.get("Cookie", ""):
                self.send(401, "session cookie missing")
            elif self.path == "/home" and case in {
                "redirect_get_chain",
                "redirect_get_same",
            }:
                self.send(
                    302, "", "/counter" if case == "redirect_get_chain" else "/home"
                )
            elif self.path == "/home":
                script = (
                    "<script>setInterval(() => fetch('/counter', {method:'POST'}).catch(()=>{}), 5)</script>"
                    if case == "background"
                    else ""
                )
                self.send(200, "<div id=home>home</div>" + script)
            elif self.path == "/record":
                self.send(
                    200,
                    "<title>Record</title><table id=list><tr class=row><td class=value>expected</td></tr></table>",
                )
            else:
                self.send(404, "missing")

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            state["posts"].append(self.path)
            state["post_records"].append(
                {
                    "path": self.path,
                    "time": time.monotonic(),
                    "origin": self.headers.get("Origin"),
                    "referer": self.headers.get("Referer"),
                    "destination": self.headers.get("Sec-Fetch-Dest"),
                }
            )
            if self.path == endpoint:
                if case == "retry" and len(state["posts"]) == 1:
                    self.send(502, "gateway")
                elif case == "redirect_post":
                    self.send(307, "", "/counter")
                elif case == "redirect_same":
                    self.send(307, "", endpoint)
                else:
                    self.send(
                        301
                        if case == "redirect_301"
                        else 302
                        if case == "redirect_302"
                        else 303,
                        "",
                        "/home",
                    )
            else:
                self.send(200, "unexpected write")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    state["base"] = f"http://127.0.0.1:{server.server_address[1]}"
    state["endpoint"] = state["base"] + endpoint
    yield state
    server.shutdown()
    server.server_close()
    worker.join()


@pytest.mark.parametrize(
    "auth_app",
    [
        "native",
        "redirect_301",
        "redirect_302",
        "retry",
        "named_controls",
        "trusted_override",
        "background",
        "mismatch",
        "query_mismatch",
        "override_mismatch",
        "method_override",
        "frame_override",
        "xhr",
        "redirect_post",
        "redirect_same",
        "redirect_get_chain",
        "redirect_get_same",
    ],
    indirect=True,
)
def test_real_native_form_auth_only_sends_declared_single_use_post(
    auth_app, monkeypatch
) -> None:
    pytest.importorskip("playwright.sync_api")
    app = auth_app
    observed = []
    original_guard = _PlaywrightSession._guard

    def traced(session, route, request):
        from urllib.parse import urlsplit

        observed.append((request.method, urlsplit(request.url).path, session._phase))
        return original_guard(session, route, request)

    monkeypatch.setattr(_PlaywrightSession, "_guard", traced)
    verifier = build_ui_readback_verifier(
        _spec(app["base"], app["endpoint"]),
        {},
        timeout_s=4.0,
        environ={"AUTH_TEST_USER": "synthetic", "AUTH_TEST_PASS": "synthetic"},
    )
    effect = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"value": "expected"},
        forbid_collateral_loss=False,
        timeout_s=0.0,
    )
    result = verifier.verify(
        effect, EffectState(substrate="ui-readback", reachable=True)
    )
    import json

    print(
        json.dumps(
            {
                "case": app["case"],
                "posts": app["posts"],
                "post_records": app["post_records"],
                "gets": app["gets"],
                "verdict": result.verdict.value,
                "reason": result.reason,
                "observed_guard": observed,
            },
            sort_keys=True,
        )
    )
    positive = app["case"] in {
        "native",
        "redirect_301",
        "redirect_302",
        "retry",
        "named_controls",
        "trusted_override",
        "background",
    }
    assert result.verdict is (Verdict.CONFIRMED if positive else Verdict.INDETERMINATE)
    assert all(path == "/login?flow=reader&flow=check" for path in app["posts"])
    if positive:
        assert ("GET", "/home", "read") in observed
        assert len(app["posts"]) == (2 if app["case"] == "retry" else 1)
    elif app["case"].startswith("redirect"):
        assert len(app["posts"]) == 1
        assert app["gets"].count("/home") == (1 if "redirect_get" in app["case"] else 0)
        assert "/counter" not in app["gets"]
    else:
        assert app["posts"] == []


def test_missing_auth_declaration_precedes_credential_environment_reads():
    class NoCredentialReads(dict):
        def get(self, key, default=None):
            raise AssertionError("must not read credential environment")

    with pytest.raises(ValueError, match="post_url"):
        build_ui_readback_verifier(
            _spec("http://app.test", None), {}, environ=NoCredentialReads()
        )


def test_legacy_read_only_protocol_can_omit_native_auth_declaration():
    from contextlib import contextmanager

    from openadapt_flow.runtime.effects.ui_readback import RawPage, SessionFacts

    class Driver:
        @contextmanager
        def open(self, target, username, password):
            class Session:
                facts = SessionFacts(
                    own_browser=True, fresh_context=True, signed_in=True
                )

                def read(self):
                    return RawPage(
                        ok=True, identity="Record", rows=[{"value": "expected"}]
                    )

            yield Session()

    verifier = build_ui_readback_verifier(
        _spec("http://app.test", None),
        {},
        driver=Driver(),
        environ={"AUTH_TEST_USER": "synthetic", "AUTH_TEST_PASS": "synthetic"},
    )
    assert verifier.capture_pre_state().reachable


@pytest.mark.parametrize("bad", ["frame", "attempt", "phase", "refused"])
def test_captured_auth_read_token_is_frame_attempt_bound_and_consumed(bad):
    session = _PlaywrightSession(
        PlaywrightReadbackDriver(),
        resolve_spec(_spec("http://app.test", "http://app.test/login"), {}),
        "synthetic",
        "synthetic",
    )
    frame = object()
    session._auth_frame = frame
    session._auth_refused = bad == "refused"
    session._auth_attempt = 2
    session._phase = "sign_in" if bad == "phase" else "read"
    session._auth_read_redirect = (
        object() if bad == "frame" else frame,
        1 if bad == "attempt" else 2,
        "http://app.test/home",
    )
    with pytest.raises(ReadbackSessionError, match="not current"):
        session._consume_auth_redirect()
    assert session._auth_read_redirect is None


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        (
            False,
            "sha256:e2080f9d4b910fecc7e3de473ea04fedd4fe853c70b600f67c237f1d875a2e32",
        ),
        (
            True,
            "sha256:26d9d354d5817ff5ae2bddf636e839c54f9e2a4813a9570e5d29fc3ed06d18f0",
        ),
    ],
)
def test_optional_auth_endpoint_preserves_legacy_identity_and_binds_declaration(
    candidate, expected
):
    from types import SimpleNamespace

    from openadapt_flow.deployment import (
        EffectsConfig,
        _effect_verifier_config_identity,
    )

    verifier = SimpleNamespace(substrate="ui-readback", adapter_version="1")

    def identity(endpoint):
        direct = EffectsConfig(
            kind="ui_readback", ui_readback=_spec("http://app.test", endpoint)
        )
        config = (
            EffectsConfig(kind="none", candidates=[direct]) if candidate else direct
        )
        return _effect_verifier_config_identity(config, verifier)

    # Fixed identities computed from the unchanged parent before this field existed.
    assert identity(None) == expected
    first = identity("http://app.test/login?flow=one")
    second = identity("http://app.test/login?flow=two")
    assert len({expected, first, second}) == 3
