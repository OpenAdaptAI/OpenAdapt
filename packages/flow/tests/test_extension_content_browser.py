"""The extension content script against real Chromium event dispatch.

The Node harness models shadow retargeting; this runs ``content.js`` in a
headless Chromium page so real ``click`` and ``input`` events (with real
retargeting and ``composedPath()``) reach its document listeners. Only the
``chrome.runtime`` transport is stubbed: it records messages in the page.
This does not qualify an installed MV3 extension or native messaging.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

CONTENT = Path(__file__).resolve().parents[1] / "chrome_extension" / "content.js"

LIVE_SECRET = "hunter2-live-value-must-not-be-sent"

_PAGE = """<!doctype html>
<div id="wrap"></div>
<div id="locked"></div>
<script>
window.__sent = [];
window.chrome = {runtime: {
  sendMessage(msg, cb) {
    window.__sent.push(JSON.parse(JSON.stringify(msg)));
    if (typeof cb === 'function') cb({active: false});
  },
  onMessage: {addListener() {}},
}};
const open = document.getElementById('wrap').attachShadow({mode: 'open'});
open.innerHTML = '<input id="password" name="note">'
  + '<input id="memo" name="memo">'
  + '<button id="go">Go</button>';
const closed = document.getElementById('locked').attachShadow({mode: 'closed'});
closed.innerHTML = '<input id="ssn" name="ssn">';
window.__closedInput = closed.getElementById('ssn');
</script>"""


@pytest.fixture
def page():
    sync = pytest.importorskip("playwright.sync_api")
    with sync.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001 - no browser on this machine
            pytest.skip(f"chromium unavailable: {exc}")
        try:
            page = browser.new_page()
            page.set_content(_PAGE)
            page.add_script_tag(content=CONTENT.read_text(encoding="utf-8"))
            page.evaluate("window.__openadaptExtensionContent.setRecording(true)")
            page.evaluate("window.__sent.length = 0")
            yield page
        finally:
            browser.close()


def _evidence(page) -> list[dict]:
    return [
        m["evidence"]
        for m in page.evaluate("window.__sent")
        if m.get("type") == "EXTENSION_EVIDENCE"
    ]


def test_click_inside_open_shadow_reports_inner_button(page):
    page.click("#wrap >> #go")
    clicks = [e for e in _evidence(page) if e["event"] == "click"]
    assert clicks, "no click evidence"
    assert clicks[-1]["tag"] == "button"
    assert clicks[-1]["id"] == "go"
    assert clicks[-1]["in_shadow"] is True
    assert clicks[-1]["path"].endswith(">>> button#go")


def test_typing_into_secret_id_with_benign_name_never_sends_value(page):
    page.type("#wrap >> #password", LIVE_SECRET)
    inputs = [e for e in _evidence(page) if e["event"] == "input"]
    assert len(inputs) == len(LIVE_SECRET)
    assert all(e["secret_field"] is True for e in inputs)
    assert all(e["value"] == "<secret>" for e in inputs)
    assert all(e["in_shadow"] is True and e["id"] == "password" for e in inputs)
    wire = json.dumps(page.evaluate("window.__sent"))
    for i in range(3, len(LIVE_SECRET) + 1):
        assert LIVE_SECRET[:i] not in wire


def test_typing_into_benign_shadow_field_keeps_inner_target(page):
    page.type("#wrap >> #memo", "ok")
    inputs = [e for e in _evidence(page) if e["event"] == "input"]
    assert inputs[-1]["id"] == "memo"
    assert inputs[-1]["secret_field"] is False
    assert inputs[-1]["value"] == "ok"


def test_closed_shadow_input_is_reported_at_host_only(page):
    page.evaluate(
        "() => { const el = window.__closedInput; el.focus();"
        " el.value = '123-45-6789';"
        " el.dispatchEvent(new InputEvent('input', {bubbles: true, composed: true})); }"
    )
    inputs = [e for e in _evidence(page) if e["event"] == "input"]
    assert inputs[-1]["id"] == "locked"
    assert inputs[-1]["in_shadow"] is False
    assert "123-45-6789" not in json.dumps(page.evaluate("window.__sent"))


def test_nothing_is_sent_when_not_recording(page):
    page.evaluate("window.__openadaptExtensionContent.setRecording(false)")
    page.evaluate("window.__sent.length = 0")
    page.click("#wrap >> #go")
    page.type("#wrap >> #password", LIVE_SECRET)
    assert _evidence(page) == []
