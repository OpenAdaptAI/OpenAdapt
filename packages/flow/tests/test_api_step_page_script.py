"""The API step runs a form's own script before it sends the save.

A form whose fields have handlers, whose ``onsubmit`` validates, or whose
validate.js rules include a custom kind (OpenEMR's ``pastDate``) used to keep
the save on the GUI. Now the runtime runs that script in a scratch copy of
the session, on the run's values, with every write blocked, and sends only
when the page would submit the same body. These tests use a real headless
Chromium against a local server. Synthetic values only.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from openadapt_flow.ir import WebFormField, WebFormSpec
from openadapt_flow.runtime.actuators.form_eval import FormEvaluation
from openadapt_flow.runtime.actuators.web_form import (
    PlaywrightSessionTransport,
    WebFormActuator,
)

playwright = pytest.importorskip("playwright.sync_api")

FORM = """<!doctype html><html><body>
<form action="/save.php" method="post" {form_attrs}>
  <input type="hidden" name="csrf" value="tok-{n}">
  <input name="lname" {lname_attrs}>
  <input name="dob" onchange="noteChange(this)">
  <select name="state"><option value="">-</option><option value="CA">California</option></select>
  <button type="submit">Save</button>
</form>
<script>
function noteChange(el) {{ el.dataset.seen = '1'; }}
function pastDate(value) {{ return value !== '' && new Date(value) < new Date(); }}
{script}
</script></body></html>"""

VALIDATE = 'onsubmit="return pastDate(this.dob.value)"'


class _App:
    """A local app: serves pages by path and counts every write."""

    def __init__(self) -> None:
        self.pages: dict[str, str] = {"/": "<html><body>home</body></html>"}
        self.writes: list[str] = []
        self.loads = 0
        app = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802
                path = self.path.split("?")[0]
                if path in app.pages:
                    app.loads += 1
                    body = app.pages[path].replace("{n}", str(app.loads)).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                else:
                    body = b"no"
                    self.send_response(404)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:  # noqa: N802
                app.writes.append(self.path)
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def form(
        self, *, form_attrs: str = VALIDATE, lname_attrs: str = "", script: str = ""
    ) -> None:
        self.pages["/new.php"] = FORM.format(
            form_attrs=form_attrs, lname_attrs=lname_attrs, script=script, n="{n}"
        )


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture
def app_page(browser):
    app = _App()
    context = browser.new_context()
    context.add_cookies([{"name": "sid", "value": "live", "url": app.base}])
    page = context.new_page()
    page.goto(app.base + "/")
    yield app, page
    context.close()
    app.server.shutdown()


GOOD = [("lname", "Marsh"), ("dob", "1979-06-14"), ("state", "CA")]


def _evaluate(app: _App, page: Any, fields=GOOD) -> FormEvaluation:
    return PlaywrightSessionTransport(page).evaluate_form(
        "/new.php", "/save.php", fields
    )


def test_values_the_page_accepts_are_valid_and_nothing_is_written(app_page):
    app, page = app_page
    app.form()
    result = _evaluate(app, page)
    assert result.status == "valid", result.reason
    assert ("lname", "Marsh") in result.serialized and (
        "state",
        "CA",
    ) in result.serialized
    assert app.writes == []
    # The run's own tab is untouched: same page, same URL, one tab.
    assert page.url == app.base + "/" and len(page.context.pages) == 1


def test_a_custom_rule_that_refuses_the_value_makes_it_invalid(app_page):
    app, page = app_page
    app.form()
    result = _evaluate(
        app, page, [("lname", "Marsh"), ("dob", "2999-01-01"), ("state", "CA")]
    )
    assert result.status == "invalid" and "refused the submit" in result.reason
    assert app.writes == []


def test_a_handler_that_rewrites_a_value_shows_in_the_body_it_would_send(app_page):
    app, page = app_page
    app.form(lname_attrs='onchange="this.value = this.value.toUpperCase()"')
    result = _evaluate(app, page)
    assert result.status == "valid"
    assert ("lname", "MARSH") in result.serialized


def test_script_that_tries_to_write_is_blocked_and_unevaluable(app_page):
    app, page = app_page
    app.form(
        form_attrs="onsubmit=\"fetch('/audit.php', {method: 'POST', body: 'x'}); return true\""
    )
    result = _evaluate(app, page)
    assert result.status == "unevaluable" and "POST /audit.php" in result.reason
    assert app.writes == []


def test_a_programmatic_submit_after_validation_counts_and_never_navigates(app_page):
    app, page = app_page
    app.form(
        form_attrs='onsubmit="if (pastDate(this.dob.value)) this.submit(); return false"'
    )
    result = _evaluate(app, page)
    assert result.status == "valid", result.reason
    assert app.writes == []


@pytest.mark.parametrize(
    "kw, status, reason",
    [
        (
            {"script": "undefinedThing();"},
            "unevaluable",
            "raised an error as it loaded",
        ),
        (
            {
                "form_attrs": "onsubmit=\"alert('Date of birth is required'); return false\""
            },
            "invalid",
            "showed a dialog",
        ),
        (
            {
                "form_attrs": "",
                "script": "document.forms[0].addEventListener('submit', e => e.stopPropagation())",
            },
            "unevaluable",
            "tried to send POST /save.php",
        ),
        ({"form_attrs": "", "lname_attrs": "required"}, "valid", ""),
        (
            {
                "form_attrs": "",
                "script": "var constraints = {dob: {pastDate: true}};"
                "function validate(values, c) { return pastDate(values.dob) ? undefined : {dob: ['must be past']}; }"
                "validate.collectFormValues = f => Object.fromEntries(new FormData(f));",
            },
            "valid",
            "",
        ),
    ],
)
def test_what_the_page_does_decides(app_page, kw, status, reason):
    app, page = app_page
    app.form(**kw)
    result = _evaluate(app, page)
    assert result.status == status and reason in result.reason, result.reason
    assert app.writes == []


def test_native_constraints_and_validate_js_rules_refuse(app_page):
    app, page = app_page
    app.form(form_attrs="", lname_attrs="required")
    assert (
        _evaluate(
            app, page, [("lname", ""), ("dob", "1979-06-14"), ("state", "CA")]
        ).status
        == "invalid"
    )
    app.form(
        form_attrs="",
        script="var constraints = {dob: {pastDate: true}};"
        "function validate(values, c) { return pastDate(values.dob) ? undefined : {dob: ['must be past']}; }",
    )
    result = _evaluate(
        app, page, [("lname", "Marsh"), ("dob", "2999-01-01"), ("state", "CA")]
    )
    assert result.status == "invalid" and "dob" in result.reason


def test_an_unknown_option_or_a_missing_form_is_unevaluable(app_page):
    app, page = app_page
    app.form()
    assert _evaluate(app, page, [("state", "ZZ")]).status == "unevaluable"
    assert (
        PlaywrightSessionTransport(page)
        .evaluate_form("/new.php", "/other.php", GOOD)
        .status
        == "unevaluable"
    )


# -- the actuator: what it does with the page's verdict ------------------------


class _Transport:
    base_url = "http://app"

    def __init__(self, evaluation: FormEvaluation) -> None:
        self.evaluation = evaluation
        self.calls: list[Any] = []
        self.posts: list[str] = []

    def get(self, path):
        from openadapt_flow.runtime.actuators.web_form import _Resp

        return _Resp(
            200, FORM.format(form_attrs=VALIDATE, lname_attrs="", script="", n="1"), {}
        )

    def post_form(self, path, body, *, referer):
        self.posts.append(body)
        from openadapt_flow.runtime.actuators.web_form import _Resp

        return _Resp(302, "", {"location": "/view.php?id=7"})

    def evaluate_form(self, form_path, action_path, fields):
        self.calls.append(fields)
        return self.evaluation


def _binding():
    from openadapt_flow.ir import ApiBinding

    spec = WebFormSpec(
        form_path="/new.php",
        fields=[
            WebFormField(name="csrf", source="session"),
            WebFormField(name="lname", source="param", param="lname"),
            WebFormField(name="dob", source="param", param="dob"),
            WebFormField(name="state", source="select_label", param="state_label"),
        ],
        controls=["csrf", "dob", "lname", "state"],
        client_scripts=["dob onchange", "form onsubmit"],
        replaces_steps=["s0"],
        folded_checks_digest="sha256:" + "0" * 64,
    )
    return ApiBinding(
        kind="web_form", method="POST", url_template="/save.php", web_form=spec
    )


RUN = {"lname": "Marsh", "dob": "1979-06-14", "state_label": "California"}


def _evaluation(status="valid", serialized=None, reason=""):
    return FormEvaluation(
        status=status,
        reason=reason,
        serialized=serialized
        if serialized is not None
        else [
            ("csrf", "tok-9"),
            ("lname", "Marsh"),
            ("dob", "1979-06-14"),
            ("state", "CA"),
        ],
    )


def test_the_actuator_sends_only_what_the_page_would_send():
    t = _Transport(_evaluation())
    out = WebFormActuator(t).actuate(_binding(), RUN)
    assert out.status.value == "actuated", out.reason
    # Only the typed and picked fields are put into the scratch form.
    assert t.calls == [[("lname", "Marsh"), ("dob", "1979-06-14"), ("state", "CA")]]
    assert len(t.posts) == 1 and "csrf=tok-1" in t.posts[0]


@pytest.mark.parametrize(
    "evaluation, reason",
    [
        (
            _evaluation("invalid", reason="the form's script refused the submit"),
            "refused the submit",
        ),
        (
            _evaluation("unevaluable", reason="the scratch page failed"),
            "scratch page failed",
        ),
        (
            _evaluation(
                serialized=[
                    ("csrf", "x"),
                    ("lname", "MARSH"),
                    ("dob", "1979-06-14"),
                    ("state", "CA"),
                ]
            ),
            "its script changes them",
        ),
        (
            _evaluation(
                serialized=[
                    ("csrf", "x"),
                    ("lname", "Marsh"),
                    ("dob", "1979-06-14"),
                    ("state", "CA"),
                    ("extra", "1"),
                ]
            ),
            "extra",
        ),
    ],
)
def test_the_actuator_leaves_the_save_to_the_gui_when_the_page_disagrees(
    evaluation, reason
):
    t = _Transport(evaluation)
    out = WebFormActuator(t).actuate(_binding(), RUN)
    assert out.status.value == "unavailable" and reason in out.reason
    assert t.posts == []
