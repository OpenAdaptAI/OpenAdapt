"""API steps for more save shapes, checked against a real browser.

The browser is the reference: Chromium submits each form itself, and the API
step must send the same fields, in the same order, with the same encoding.

- multipart/form-data with no file attached: capture keeps the fields, and
  the API step's encoding is byte for byte the browser's (boundary aside);
- the clicked submit button's own ``name=value``;
- a file input left empty; an attached file is never captured or sent;
- a save the page's script sends to another endpoint than the form's action.

The same shapes on real applications (Redmine, NetBox, Dolibarr, Trac,
WordPress) are in ``test_api_step_shapes_fixtures.py``.
"""

from __future__ import annotations

import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from openadapt_flow.compiler.api_proposal import _success_pattern
from openadapt_flow.ir import ApiBinding, WebFormField, WebFormSpec
from openadapt_flow.network_capture import parse_multipart
from openadapt_flow.runtime.actuators import ActuationStatus
from openadapt_flow.runtime.actuators.web_form import (
    PlaywrightSessionTransport,
    WebFormActuator,
    encode_multipart,
    form_parts,
    submitter_button,
)

playwright = pytest.importorskip("playwright.sync_api")

FORM = """<html><body>
<form action="/save" method="post" enctype="multipart/form-data" id="f">
  <input type="hidden" name="csrf" value="tok-{n}">
  <input name="subject" id="subject">
  <textarea name="notes" id="notes"></textarea>
  <select name="kind"><option value="1">Bug</option><option value="2">Task</option></select>
  <input type="file" name="attachment">
  <input type="submit" name="commit" value="Create">
  <input type="submit" name="continue" value="Create and continue">
</form>{script}</body></html>"""

SCRIPT_SENT = """<html><body>
<form action="/edit" method="post" id="addtag">
  <input type="hidden" name="action" value="add-tag">
  <input type="hidden" name="nonce" value="n-{{n}}">
  <input name="tag-name" id="tag-name">
  <input type="submit" id="submit" value="Add">
</form>
<script>
document.getElementById('submit').addEventListener('click', (e) => {{
  e.preventDefault();
  const body = new URLSearchParams(new FormData(document.getElementById('addtag')));
  {extra}
  fetch('/ajax', {{method: 'POST', body}});
}});
</script></body></html>"""


class _App:
    def __init__(self) -> None:
        self.pages: dict[str, str] = {}
        self.posts: list[tuple[str, str, bytes]] = []
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
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                app.posts.append(
                    (self.path, self.headers.get("Content-Type") or "", raw)
                )
                self.send_response(303)
                self.send_header("Location", "/items/17")
                self.send_header("Content-Length", "0")
                self.end_headers()

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
def app_page(browser):
    app = _App()
    app.pages["/new"] = FORM.replace("{script}", "")
    app.pages["/"] = "<html><body>home</body></html>"
    context = browser.new_context()
    page = context.new_page()
    page.goto(app.base + "/")
    yield app, page
    context.close()
    app.server.shutdown()


def _browser_submit(
    app: _App, page: Any, subject: str, notes: str
) -> tuple[str, bytes]:
    page.goto(app.base + "/new")
    page.fill("#subject", subject)
    page.fill("#notes", notes)
    page.select_option("select[name=kind]", "2")
    page.click("input[name=commit]")
    page.wait_for_load_state()
    _, ctype, raw = app.posts[-1]
    return ctype, raw


def _binding(**spec: Any) -> ApiBinding:
    fields = [
        WebFormField(name="csrf", source="session"),
        WebFormField(name="subject", source="param", param="subject"),
        WebFormField(name="notes", source="param", param="notes"),
        WebFormField(name="kind", source="select_label", param="kind"),
        WebFormField(name="attachment", source="empty_file", value=""),
        WebFormField(name="commit", source="submitter", value="Create"),
    ]
    return ApiBinding(
        kind="web_form",
        method="POST",
        url_template="/save",
        on_unavailable="gui",
        web_form=WebFormSpec(
            form_path="/new",
            fields=fields,
            controls=["csrf", "kind", "notes", "subject"],
            encoding="multipart",
            submitter="input[name=commit]",
            success_pattern=r"/items/\d+",
            replaces_steps=["step_001"],
            folded_checks_digest="sha256:" + "0" * 64,
            **spec,
        ),
    )


def test_capture_keeps_a_multipart_save_with_no_file(app_page):
    app, page = app_page
    ctype, raw = _browser_submit(app, page, "Printer jams", "line one\nline two")
    parsed = parse_multipart(raw, ctype)
    assert parsed is not None
    pairs, files = parsed
    assert pairs[0][0] == "csrf" and pairs[0][1].startswith("tok-")
    assert pairs[1:] == [
        ["subject", "Printer jams"],
        ["notes", "line one\r\nline two"],
        ["kind", "2"],
        ["attachment", ""],
        ["commit", "Create"],
    ]
    assert files == ["attachment"]


def test_capture_never_keeps_an_attached_file(app_page, tmp_path):
    app, page = app_page
    upload = tmp_path / "scan.txt"
    upload.write_text("contents")
    page.goto(app.base + "/new")
    page.set_input_files("input[name=attachment]", str(upload))
    page.click("input[name=commit]")
    page.wait_for_load_state()
    _, ctype, raw = app.posts[-1]
    assert parse_multipart(raw, ctype) is None


def test_multipart_encoding_is_the_browsers_byte_for_byte(app_page):
    app, page = app_page
    ctype, raw = _browser_submit(app, page, "Printer jams", "line one\nline two")
    pairs, files = parse_multipart(raw, ctype)
    ours, our_type = encode_multipart(
        [(k, v.replace("\r\n", "\n")) for k, v in pairs], files
    )
    theirs = re.search(r"boundary=(\S+)", ctype).group(1)
    mine = re.search(r"boundary=(\S+)", our_type).group(1)
    assert ours.encode("utf-8") == raw.replace(theirs.encode(), mine.encode())


def test_api_step_sends_what_the_browser_sends(app_page):
    app, page = app_page
    _, gui_raw = _browser_submit(app, page, "Printer jams", "two\nlines")
    gui_type = app.posts[-1][1]
    actuator = WebFormActuator(PlaywrightSessionTransport(page))
    result = actuator.actuate(
        _binding(), {"subject": "Printer jams", "notes": "two\nlines", "kind": "Task"}
    )
    assert result.status is ActuationStatus.ACTUATED, result.reason
    path, api_type, api_raw = app.posts[-1]
    assert path == "/save" and api_type.startswith("multipart/form-data")
    gui_pairs, gui_files = parse_multipart(gui_raw, gui_type)
    api_pairs, api_files = parse_multipart(api_raw, api_type)
    # The same fields, order and empty file part; only the session's own
    # CSRF value differs (each form read issues a new one).
    assert [p for p in api_pairs if p[0] != "csrf"] == [
        p for p in gui_pairs if p[0] != "csrf"
    ]
    assert api_files == gui_files == ["attachment"]


def test_a_changed_submit_button_keeps_the_save_on_the_gui(app_page):
    app, page = app_page
    app.pages["/new"] = FORM.replace('value="Create">', 'value="Save">').replace(
        "{script}", ""
    )
    actuator = WebFormActuator(PlaywrightSessionTransport(page))
    result = actuator.actuate(
        _binding(), {"subject": "x", "notes": "y", "kind": "Task"}
    )
    assert result.status is ActuationStatus.UNAVAILABLE
    assert "submit button" in result.reason
    assert not app.posts


def test_a_new_file_input_keeps_the_save_on_the_gui(app_page):
    app, page = app_page
    app.pages["/new"] = FORM.replace(
        '<input type="file" name="attachment">',
        '<input type="file" name="attachment"><input type="file" name="photo">',
    ).replace("{script}", "")
    result = WebFormActuator(PlaywrightSessionTransport(page)).actuate(
        _binding(), {"subject": "x", "notes": "y", "kind": "Task"}
    )
    assert result.status is ActuationStatus.UNAVAILABLE
    assert "file inputs changed" in result.reason
    assert not app.posts


def test_the_button_pair_needs_one_button_with_that_value():
    form = form_parts(FORM.replace("{script}", ""), "/save")
    assert submitter_button(form, "commit", "Create") is not None
    assert submitter_button(form, "commit", "Create and continue") is None
    no_value = form_parts(
        '<form action="/save"><input type="submit" name="go"></form>', "/save"
    )
    # An <input type=submit> without a value sends the browser's own label.
    assert submitter_button(no_value, "go", "Submit") is None


def test_a_form_without_an_action_posts_to_its_page():
    html = '<form method="post"><input name="a"></form>'
    assert form_parts(html, "/ipam/add/") is None
    assert form_parts(html, "/ipam/add/", "/ipam/add/")["controls"].keys() == {"a"}


def _script_binding(extra_fields: list[WebFormField] = ()) -> ApiBinding:
    return ApiBinding(
        kind="web_form",
        method="POST",
        url_template="/ajax",
        on_unavailable="gui",
        web_form=WebFormSpec(
            form_path="/tags",
            form_action="/edit",
            fields=[
                WebFormField(name="action", source="form_default", value="add-tag"),
                WebFormField(name="nonce", source="session"),
                WebFormField(name="tag-name", source="param", param="name"),
                *extra_fields,
            ],
            controls=["action", "nonce", "tag-name"],
            client_scripts=["inline script"],
            submitter="#submit",
            replaces_steps=["step_001"],
            folded_checks_digest="sha256:" + "0" * 64,
        ),
    )


def test_a_script_sent_save_runs_the_script_first(app_page):
    app, page = app_page
    app.pages["/tags"] = SCRIPT_SENT.format(extra="")
    actuator = WebFormActuator(PlaywrightSessionTransport(page))
    spec = _script_binding().web_form
    # The live page's own script list, so the run checks it the same way.
    from openadapt_flow.runtime.actuators.web_form import client_rules, page_scripts

    html = app.pages["/tags"].replace("{n}", "1")
    scripts = page_scripts(
        html, "/edit", ["action", "nonce", "tag-name"], client_rules(html), "/tags"
    )
    binding = _script_binding()
    binding.web_form.client_scripts = scripts
    assert spec.form_action == "/edit"
    result = actuator.actuate(binding, {"name": "Community"})
    assert result.status is ActuationStatus.ACTUATED, result.reason
    page_script = [e for e in actuator.log if e["stage"] == "page_script"]
    assert page_script and page_script[0]["status"] == "valid"
    assert [(p, t.split(";")[0]) for p, t, _ in app.posts] == [
        ("/ajax", "application/x-www-form-urlencoded")
    ]


def test_a_script_that_sends_another_field_keeps_the_save_on_the_gui(app_page):
    app, page = app_page
    app.pages["/tags"] = SCRIPT_SENT.format(extra="body.append('screen', 'edit');")
    from openadapt_flow.runtime.actuators.web_form import client_rules, page_scripts

    html = app.pages["/tags"].replace("{n}", "1")
    binding = _script_binding()
    binding.web_form.client_scripts = page_scripts(
        html, "/edit", ["action", "nonce", "tag-name"], client_rules(html), "/tags"
    )
    result = WebFormActuator(PlaywrightSessionTransport(page)).actuate(
        binding, {"name": "Community"}
    )
    assert result.status is ActuationStatus.UNAVAILABLE
    assert "screen" in result.reason
    assert not app.posts


@pytest.mark.parametrize(
    "location, good, bad",
    [
        ("http://h/ticket/1", "http://h/ticket/42", "http://h/newticket?x=1"),
        ("/ipam/ip-addresses/3/", "/ipam/ip-addresses/9/", "/ipam/ip-addresses/add/"),
        (
            "/?p=1&unapproved=2&moderation-hash=e995d4ec71c477832a7f6b9e",
            "/?p=1&unapproved=7&moderation-hash=0123abcdef4567890abc",
            "/?p=1",
        ),
        (
            "/contact/card.php?id=1",
            "/contact/card.php?id=8",
            "/contact/card.php?action=create",
        ),
    ],
)
def test_success_patterns_generalize_record_numbers_and_hashes(location, good, bad):
    pattern = _success_pattern({"location": location})
    assert re.search(pattern, good)
    assert not re.search(pattern, bad)
