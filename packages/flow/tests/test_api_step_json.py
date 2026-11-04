"""API steps for saves a page's script sends as JSON, GraphQL mutations included.

- The compiler binds each typed value to the one JSON leaf that carries it
  and refuses what it can't send again: a session or secret value, an id or
  time the page makes on each save, a typed value inside GraphQL operation
  text, an ambiguous or missing value.
- At run time (headless Chromium, a local single-page app that signs its API
  calls with a bearer header) the API step sends what the page's own save
  sends: the same body and the same header, whose value it takes from the
  app's own requests in the session. A header the app hasn't sent keeps the
  save on the GUI; a GraphQL answer with errors halts it.

The same on real applications is in ``test_api_step_shapes_fixtures.py``.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from openadapt_flow import network_capture as nc
from openadapt_flow.compiler.api_proposal import admit_api_step, propose_api_step
from openadapt_flow.ir import ActionKind, Step, Workflow
from openadapt_flow.runtime.actuators import ActuationStatus, ApiHaltKind
from openadapt_flow.runtime.actuators import session_headers
from openadapt_flow.runtime.actuators.web_form import (
    PlaywrightSessionTransport,
    WebFormActuator,
)
from openadapt_flow.runtime.effects import Effect, EffectKind

PARAMS = {"first": "Ada", "last": "Park", "age": "41"}
BODY = {
    "firstName": "Ada",
    "lastName": "Park",
    "age": 41,
    "assignedUserId": "u1",
    "teams": ["sales"],
}
MUTATION = "mutation ($n: String!) { groups { create(name: $n) { id } } }"


def _recording(tmp_path: Path, body: Any, *, headers=("authorization",)) -> Path:
    rec = tmp_path / "rec"
    rec.mkdir()
    (rec / "meta.json").write_text(json.dumps({"params": PARAMS}))
    events = [
        {"i": i, "kind": k, "t": float(i + 1)}
        for i, k in enumerate(
            ["click", "type", "click", "type", "click", "type", "click"]
        )
    ]
    (rec / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    save: dict[str, Any] = {
        "t": 7.2,
        "method": "POST",
        "url": "http://app/api/v1/Contact",
        "status": 200,
        "resource_type": "fetch",
        "frame_url": "http://app/app#Contact/create",
        "request_type": "application/json",
        "body": json.dumps(body),
        "response_text": '{"id": "c9"}',
    }
    if headers is not None:
        save["request_headers"] = list(headers)
    lines = [
        {
            "t": -1.0,
            "method": "GET",
            "url": "http://app/app",
            "status": 200,
            "resource_type": "document",
            "response_text": "<div id=app></div>",
        },
        save,
    ]
    (rec / nc.NETWORK_FILE).write_text(
        json.dumps({"schema": nc.SCHEMA, "exchanges": len(lines)})
        + "\n"
        + "".join(json.dumps(ln) + "\n" for ln in lines)
    )
    return rec


def _workflow() -> Workflow:
    steps = [
        Step(
            id=f"step_{i:03d}",
            intent=f"s{i}",
            action=ActionKind.TYPE if i in (1, 3, 5) else ActionKind.CLICK,
            param={1: "first", 3: "last", 5: "age"}.get(i),
        )
        for i in range(7)
    ]
    steps[6] = steps[6].model_copy(
        update={
            "effects": [
                Effect(
                    kind=EffectKind.RECORD_WRITTEN,
                    match={"last_name": "{last}"},
                    expected_count=1,
                    timeout_s=1.0,
                )
            ],
            "risk": "irreversible",
        }
    )
    return Workflow(name="contact", steps=steps, params=dict(PARAMS))


def test_a_json_save_binds_each_typed_value_by_pointer(tmp_path):
    wf, step, reasons = propose_api_step(_workflow(), _recording(tmp_path, BODY))
    assert step is not None, reasons
    spec = step.api_binding.web_form
    assert spec.encoding == "json" and not spec.graphql
    assert {(f.name, f.param) for f in spec.fields} == {
        ("firstName", "first"),
        ("lastName", "last"),
        ("age", "age"),
    }
    # The typed values are not in the bundle; the constants are.
    assert spec.json_body == {
        "firstName": "",
        "lastName": "",
        "age": 0,
        "assignedUserId": "u1",
        "teams": ["sales"],
    }
    assert spec.session_headers == ["authorization"]
    assert (
        spec.form_path == "/app" and step.api_binding.url_template == "/api/v1/Contact"
    )
    admitted, _ = admit_api_step(wf, admitted_by="a person")
    assert admitted.steps[-1].api_binding.admitted()


def test_a_graphql_mutation_binds_its_variables_only(tmp_path):
    PARAMS_GQL = {"first": "Ada", "last": "Park", "age": "41"}
    body = {"query": MUTATION, "variables": {"n": "Park", "a": "Ada", "b": 41}}
    _, step, reasons = propose_api_step(_workflow(), _recording(tmp_path, body))
    assert step is not None, reasons
    spec = step.api_binding.web_form
    assert spec.graphql and spec.json_body["query"] == MUTATION
    assert {f.name for f in spec.fields} == {
        "variables/n",
        "variables/a",
        "variables/b",
    }
    assert PARAMS_GQL == PARAMS


@pytest.mark.parametrize(
    "body, headers, reason",
    [
        (
            BODY | {"id": "3f2b6c1e-9d4a-4c5e-8f00-1a2b3c4d5e6f"},
            ("authorization",),
            "makes on each save",
        ),
        (
            BODY | {"createdAt": "2026-11-04T10:00:00Z"},
            ("authorization",),
            "makes on each save",
        ),
        (BODY | {"csrf": "<session:csrf>"}, ("authorization",), "session or secret"),
        (BODY | {"nick": "Park"}, ("authorization",), "ambiguous"),
        (
            {k: v for k, v in BODY.items() if k != "age"},
            ("authorization",),
            "doesn't send it",
        ),
        (
            {
                "query": 'mutation { create(name: "Park") { id } }',
                "variables": {"a": "Ada", "b": 41},
            },
            ("authorization",),
            "operation text",
        ),
        ([BODY], ("authorization",), "not an object"),
        (BODY, None, "which headers"),
    ],
)
def test_a_json_save_it_cant_send_again_stays_on_the_gui(
    tmp_path, body, headers, reason
):
    _, step, reasons = propose_api_step(
        _workflow(), _recording(tmp_path, body, headers=headers)
    )
    assert step is None
    assert reason in " ".join(reasons), reasons


# -- against a real browser ------------------------------------------------------

playwright = pytest.importorskip("playwright.sync_api")

SPA = """<html><body>
<input id="first"><input id="last"><input id="age"><button id="save">Save</button>
<script>
const token = 'Bearer s3ss10n-' + Math.random().toString(36).slice(2);
fetch('/api/v1/App/user', {headers: {Authorization: token}});
document.getElementById('save').onclick = () => fetch('/api/v1/Contact', {
  method: 'POST',
  headers: {Authorization: token, 'Content-Type': 'application/json'},
  body: JSON.stringify({firstName: first.value, lastName: last.value,
                        age: Number(age.value), assignedUserId: 'u1', teams: ['sales']}),
});
</script></body></html>"""


class _App:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, str], bytes]] = []
        self.answer = b'{"id": "c9"}'
        app = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def _send(self, code: int, body: bytes, kind: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                if self.path.startswith("/app"):
                    self._send(200, SPA.encode(), "text/html")
                else:
                    self._send(200, b"{}", "application/json")

            def do_POST(self) -> None:  # noqa: N802
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                app.posts.append(
                    (self.path, {k.lower(): v for k, v in self.headers.items()}, raw)
                )
                self._send(200, app.answer, "application/json")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture
def spa(browser):
    app = _App()
    context = browser.new_context()
    session_headers.arm(context)
    page = context.new_page()
    page.goto(app.base + "/app")
    page.wait_for_load_state("networkidle")
    yield app, page
    context.close()
    app.server.shutdown()


def _binding(tmp_path, body=BODY):
    _, step, reasons = propose_api_step(_workflow(), _recording(tmp_path, body))
    assert step is not None, reasons
    return step.api_binding


def test_the_api_step_sends_what_the_page_sends(spa, tmp_path):
    app, page = spa
    page.fill("#first", "Ada")
    page.fill("#last", "Park")
    page.fill("#age", "41")
    page.click("#save")
    page.wait_for_load_state("networkidle")
    _, gui_headers, gui_raw = app.posts[-1]

    result = WebFormActuator(PlaywrightSessionTransport(page)).actuate(
        _binding(tmp_path), {"first": "Ada", "last": "Park", "age": "41"}
    )
    assert result.status is ActuationStatus.ACTUATED, result.reason
    path, api_headers, api_raw = app.posts[-1]
    assert path == "/api/v1/Contact"
    assert json.loads(api_raw) == json.loads(gui_raw)
    assert api_headers["authorization"] == gui_headers["authorization"]
    assert api_headers["content-type"] == "application/json"


def test_without_the_apps_header_the_save_stays_on_the_gui(browser, tmp_path):
    app = _App()
    context = browser.new_context()  # not watched: no header value is known
    page = context.new_page()
    page.goto(app.base + "/app")
    try:
        result = WebFormActuator(PlaywrightSessionTransport(page)).actuate(
            _binding(tmp_path), {"first": "Ada", "last": "Park", "age": "41"}
        )
        assert result.status is ActuationStatus.UNAVAILABLE
        assert "authorization" in result.reason
        assert not app.posts
    finally:
        context.close()
        app.server.shutdown()


def test_a_number_field_takes_only_a_number(spa, tmp_path):
    app, page = spa
    result = WebFormActuator(PlaywrightSessionTransport(page)).actuate(
        _binding(tmp_path), {"first": "Ada", "last": "Park", "age": "forty"}
    )
    assert result.status is ActuationStatus.UNAVAILABLE
    assert "not a number" in result.reason
    assert not app.posts


def test_a_graphql_error_halts_and_is_not_retried(spa, tmp_path):
    app, page = spa
    app.answer = b'{"errors": [{"message": "name taken"}], "data": {"groups": null}}'
    body = {"query": MUTATION, "variables": {"n": "Park", "a": "Ada", "b": 41}}
    binding = _binding(tmp_path, body)
    result = WebFormActuator(PlaywrightSessionTransport(page)).actuate(
        binding, {"first": "Ada", "last": "Park", "age": "41"}
    )
    assert result.status is ActuationStatus.HALT
    assert result.halt_kind is ApiHaltKind.RESPONSE_REJECTED
    assert len(app.posts) == 1
    app.answer = b'{"data": {"groups": {"create": {"id": 4}}}}'
    ok = WebFormActuator(PlaywrightSessionTransport(page)).actuate(
        binding, {"first": "Ada", "last": "Park", "age": "41"}
    )
    assert ok.status is ActuationStatus.ACTUATED, ok.reason
