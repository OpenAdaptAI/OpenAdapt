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
    page_scripts,
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
        self.paths: list[str] = []
        self.loads = 0
        app = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802
                path = self.path.split("?")[0]
                app.paths.append(path)
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


def _evaluate(app: _App, page: Any, fields=GOOD, **kw: Any) -> FormEvaluation:
    return PlaywrightSessionTransport(page).evaluate_form(
        "/new.php", "/save.php", fields, **kw
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
            "valid",
            "",
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

    def evaluate_form(self, form_path, action_path, fields, **kw):
        self.calls.append(fields)
        self.kw = kw
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
        client_scripts=page_scripts(
            FORM.format(form_attrs=VALIDATE, lname_attrs="", script="", n="1"),
            "/save.php",
            ["csrf", "lname", "dob", "state"],
            None,
        ),
        submitter="button[type=submit]",
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
    assert t.kw["submitter"] == "button[type=submit]"
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


# -- review cases: submit-time body, click handlers, blocking ---------------------


def test_an_onsubmit_rewrite_is_in_the_body_compared(app_page):
    app, page = app_page
    app.form(
        form_attrs='onsubmit="this.lname.value = this.lname.value.toUpperCase(); return true"'
    )
    result = _evaluate(app, page)
    assert result.status == "valid" and result.via in (
        "listener",
        "POST",
        "form.submit()",
    )
    assert ("lname", "MARSH") in result.serialized


def test_a_click_handler_on_a_button_needs_the_recorded_control(app_page):
    app, page = app_page
    app.form(
        form_attrs="",
        script="document.querySelector('button').addEventListener('click', "
        "e => { if (!pastDate(document.forms[0].dob.value)) e.preventDefault(); })",
    )
    # No recorded control: refused, the GUI runs.
    assert _evaluate(app, page).status == "unevaluable"
    # The recorded control is clicked, as the person did.
    assert _evaluate(app, page, submitter="button").status == "valid"
    bad = [("lname", "Marsh"), ("dob", "2999-01-01"), ("state", "CA")]
    assert _evaluate(app, page, bad, submitter="button").status == "invalid"
    assert app.writes == []


DIALOG = """<html><body>{body}<script>
function confirmCreate() {{ parent.force_submit = true; parent.check(); }}
</script></body></html>"""


def _dup_flow(app: _App, matches: bool) -> None:
    """The OpenEMR shape: #create validates, opens the duplicate search in a
    dialog frame, and the person confirms there; then the form submits."""

    app.pages["/dup.php"] = DIALOG.format(
        body="<p>Rosa Delgado 1958-03-27</p>"
        if matches
        else '<p>No matches were found.</p><button id="confirmCreate" onclick="confirmCreate()">Confirm</button>'
    )
    app.form(
        form_attrs="",
        script="var force_submit = false;"
        " function check() { const f = document.forms[0];"
        " if (!pastDate(f.dob.value)) return;"
        " if (force_submit) { f.submit(); return; }"
        " const d = document.createElement('iframe'); d.id = 'modalframe'; d.src = '/dup.php?lname=x';"
        " document.body.appendChild(d); }",
    )
    app.pages["/new.php"] = app.pages["/new.php"].replace(
        '<button type="submit">Save</button>',
        '<button type="button" id="create" onclick="check()">Save</button>',
    )


DUP = {
    "submitter": "#create",
    "submitter_requests": ("GET /dup.php",),
    "confirm": "#confirmCreate",
}


def test_the_recorded_read_completes_and_the_page_must_then_submit(app_page):
    app, page = app_page
    _dup_flow(app, matches=False)
    ok = _evaluate(app, page, **DUP)
    assert ok.status == "valid" and ok.via == "form.submit()", ok.reason
    assert ("lname", "Marsh") in ok.serialized
    bad = [("lname", "Marsh"), ("dob", "2999-01-01"), ("state", "CA")]
    refused = _evaluate(app, page, bad, **DUP)
    assert refused.status == "invalid" and "didn't submit" in refused.reason
    assert app.writes == []


def test_a_duplicate_search_that_finds_a_match_is_not_valid(app_page):
    # Review hole 5: the read's answer decides, not that it was made.
    app, page = app_page
    _dup_flow(app, matches=True)
    result = _evaluate(app, page, **DUP)
    assert result.status == "invalid" and "didn't submit" in result.reason
    assert app.writes == []


def test_a_popup_is_routed_too_and_makes_the_run_unevaluable(app_page):
    # Review hole 1: a target=_blank form submitted past every listener.
    app, page = app_page
    app.form(
        form_attrs='target="_blank"',
        script="document.forms[0].addEventListener('submit', e => e.stopPropagation())",
    )
    result = _evaluate(app, page)
    assert result.status == "unevaluable"
    assert app.writes == []


def test_a_websocket_opened_by_the_page_is_closed_and_unevaluable(app_page):
    # Review hole 2.
    app, page = app_page
    port = app.base.rsplit(":", 1)[1]
    app.form(
        script="document.forms[0].dob.addEventListener('change', () => {"
        f" try {{ new WebSocket('ws://127.0.0.1:{port}/ws'); }} catch (e) {{}} }});"
    )
    result = _evaluate(app, page)
    assert result.status == "unevaluable" and "WebSocket" in result.reason
    assert app.writes == [] and "/ws" not in app.paths


def test_key_handlers_run_as_the_person_types(app_page):
    # Review hole 3: an onkeyup uppercaser.
    app, page = app_page
    app.form(lname_attrs='onkeyup="this.value = this.value.toUpperCase()"')
    result = _evaluate(app, page)
    assert result.status == "valid" and ("lname", "MARSH") in result.serialized


def test_the_click_is_trusted(app_page):
    # Review hole 4: a handler that validates only trusted clicks.
    app, page = app_page
    app.form(
        form_attrs="",
        script="document.querySelector('button').addEventListener('click', e => {"
        " if (e.isTrusted && !pastDate(document.forms[0].dob.value)) e.preventDefault(); })",
    )
    bad = [("lname", "Marsh"), ("dob", "2999-01-01"), ("state", "CA")]
    assert _evaluate(app, page, bad, submitter="button").status == "invalid"
    assert _evaluate(app, page, submitter="button").status == "valid"


def test_a_page_that_submits_twice_is_unevaluable(app_page):
    app, page = app_page
    app.form(form_attrs='onsubmit="this.submit(); return true"')
    result = _evaluate(app, page)
    assert result.status == "unevaluable" and "2 times" in result.reason


def test_no_request_leaves_after_the_page_has_loaded(app_page):
    app, page = app_page
    app.pages["/lookup.php"] = "ok"
    app.form(
        script="document.forms[0].dob.addEventListener('change', () => { new Image().src = '/lookup.php?q=1'; });"
    )
    loads = app.loads
    result = _evaluate(app, page)
    assert result.status == "unevaluable" and "GET /lookup.php" in result.reason
    assert app.loads == loads + 1  # the form page only


def test_the_script_fingerprint_covers_handler_source_and_script_urls():
    html = FORM.format(form_attrs=VALIDATE, lname_attrs="", script="", n="1").replace(
        "<script>", '<script src="/js/app.js?v=3"></script><script>'
    )
    found = page_scripts(html, "/save.php", ["dob"], None)
    assert "script /js/app.js?v=3" in found and "inline script" in found
    assert any(f.startswith("form onsubmit ") for f in found)
    edited = html.replace("return pastDate", "return true || pastDate")
    assert page_scripts(edited, "/save.php", ["dob"], None) != found


def test_the_recorded_control_and_the_request_it_made_are_in_the_proposal(tmp_path):
    import json

    from openadapt_flow import network_capture as nc
    from openadapt_flow.compiler.api_proposal import propose_api_step
    from tests.test_api_tier_web_form import _recording, _workflow

    rec = _recording(tmp_path)
    # Without a button on the form, no control is recorded.
    _wf, step, _ = propose_api_step(_workflow(), rec)
    assert step.api_binding.web_form.submitter is None
    # The person clicks the form's #create button, which opens the
    # duplicate search; the save is confirmed in that dialog.
    events = [json.loads(x) for x in (rec / "events.jsonl").read_text().splitlines()]
    events[6]["structural"] = {"selector": "#create", "role": "button"}
    (rec / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    rows = [json.loads(x) for x in (rec / nc.NETWORK_FILE).read_text().splitlines()]
    for row in rows[1:]:
        if "<form" in (row.get("response_text") or ""):
            row["response_text"] = row["response_text"].replace(
                "</form></body>",
                '<button type="button" id="create">Create</button></form></body>',
            )
    (rec / nc.NETWORK_FILE).write_text("".join(json.dumps(r) + "\n" for r in rows))
    _wf, step, reasons = propose_api_step(_workflow(), rec)
    spec = step.api_binding.web_form
    assert reasons == [] and spec.submitter == "#create"
    assert spec.submitter_requests == ["GET /app/dup.php"]


def test_a_field_that_isnt_on_screen_makes_the_run_unevaluable(app_page):
    # Fail closed: only real, trusted key events count.
    app, page = app_page
    app.form()
    app.pages["/new.php"] = (
        app.pages["/new.php"]
        .replace('<input name="dob"', '<div style="display:none"><input name="dob"')
        .replace(
            'onchange="noteChange(this)">', 'onchange="noteChange(this)"></div>', 1
        )
    )
    result = _evaluate(app, page)
    assert result.status == "unevaluable" and "dob isn't on screen" in result.reason
    assert app.writes == []


@pytest.mark.parametrize(
    "tag",
    [
        "const l = document.createElement('link'); l.rel = 'stylesheet'; l.href = '/act.php?delete=1';",
        "const l = document.createElement('script'); l.src = '/act2.php?delete=1';",
    ],
)
def test_code_a_handler_adds_after_load_never_reaches_the_app(app_page, tag):
    # Round 3: a stylesheet or script URL the page didn't load is the page
    # sending something, whatever its resource type.
    app, page = app_page
    app.pages["/act.php"] = app.pages["/act2.php"] = "ok"
    app.form(
        script="document.forms[0].dob.addEventListener('change', () => {"
        f" {tag} document.head.appendChild(l); }});"
    )
    result = _evaluate(app, page)
    assert result.status == "unevaluable" and "act" in result.reason
    assert "/act.php" not in app.paths and "/act2.php" not in app.paths


def test_a_form_submit_in_another_frame_is_seen(app_page):
    # Round 3: an iframe's own form.submit() is recorded in that frame.
    app, page = app_page
    app.pages["/side.php"] = (
        '<html><body><form action="/side-save.php" method="post">'
        '<input name="x" value="1"></form></body></html>'
    )
    app.form(
        script="document.forms[0].dob.addEventListener('change', () => {"
        " const f = document.createElement('iframe'); f.src = '/side.php';"
        " f.onload = () => f.contentDocument.forms[0].submit();"
        " document.body.appendChild(f); });"
    )
    result = _evaluate(app, page, submitter_requests=("GET /side.php",))
    assert result.status == "unevaluable" and "form submit in" in result.reason
    assert app.writes == []
