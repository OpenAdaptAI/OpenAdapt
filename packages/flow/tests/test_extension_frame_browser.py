"""Cross-origin iframe evidence from the extension content script, in Chromium.

A parent page on one origin embeds a child frame on another. The content
script runs in both (``all_frames``), as Chrome injects it; here Playwright's
init script stands in for injection and only ``chrome.runtime`` is stubbed.
Real focus, click and typing go through headless Chromium. This does not
qualify an installed MV3 extension or native messaging.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openadapt_flow.extension_evidence import (
    ExtensionEvidenceBuffer,
    cross_origin_frame_targets,
    link_frame_targets,
    load_extension_evidence,
)

CONTENT = Path(__file__).resolve().parents[1] / "chrome_extension" / "content.js"

PARENT = "http://parent.test/app?session=PARENT-TOKEN-123#frag"
CHILD = "http://child.test/pay/frame?token=CHILD-TOKEN-456"
LIVE_SECRET = "hunter2-in-cross-origin-frame"

_PARENT_HTML = f"""<!doctype html>
<h1>Parent</h1>
<div id="shell"></div>
<script>
const root = document.getElementById('shell').attachShadow({{mode: 'open'}});
root.innerHTML = '<iframe id="pay" title="Payment" src="{CHILD}"></iframe>';
</script>"""

_CHILD_HTML = """<!doctype html>
<input id="card_name" name="card_name">
<input id="cvc" name="note" type="password">
<button id="submit">Pay</button>"""

_STUB = """
window.__sent = [];
window.chrome = {runtime: {
  sendMessage(msg, cb) {
    window.__sent.push(JSON.parse(JSON.stringify(msg)));
    if (typeof cb === 'function') cb({active: false});
  },
  onMessage: {addListener() {}},
}};
"""


@pytest.fixture
def frames():
    sync = pytest.importorskip("playwright.sync_api")
    init = (
        _STUB
        + CONTENT.read_text(encoding="utf-8")
        + "\nwindow.__openadaptExtensionContent.setRecording(true);"
        + "\nwindow.__sent.length = 0;"
    )
    with sync.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001 - no browser on this machine
            pytest.skip(f"chromium unavailable: {exc}")
        context = browser.new_context()

        def handle(route):
            url = route.request.url
            if url.startswith("http://parent.test/"):
                route.fulfill(content_type="text/html", body=_PARENT_HTML)
            elif url.startswith("http://child.test/"):
                route.fulfill(content_type="text/html", body=_CHILD_HTML)
            else:
                route.abort()

        context.route("**/*", handle)
        context.add_init_script(init)
        page = context.new_page()
        page.goto(PARENT)
        child = page.frame_locator("#shell >> #pay")
        child.locator("#card_name").wait_for()
        child_frame = next(f for f in page.frames if f.url.startswith("http://child"))
        yield page, child, child_frame
        browser.close()


def _sent(frame) -> list[dict]:
    return [
        m["evidence"]
        for m in frame.evaluate("window.__sent")
        if m.get("type") == "EXTENSION_EVIDENCE"
    ]


def test_child_frame_targets_carry_cross_origin_context_without_tokens(frames):
    page, child, child_frame = frames
    child.locator("#card_name").click()
    child.locator("#card_name").type("Ada")
    evidence = _sent(child_frame)
    clicks = [e for e in evidence if e["event"] == "click"]
    assert clicks and clicks[-1]["id"] == "card_name"
    for e in evidence:
        assert e["is_iframe"] is True
        assert e["cross_origin_parent"] is True
        assert e["frame_origin"] == "http://child.test"
        assert e["frame_url"] == "http://child.test/pay/frame"
    wire = json.dumps(child_frame.evaluate("window.__sent"))
    assert "CHILD-TOKEN-456" not in wire
    assert "token=" not in wire


def test_secret_in_cross_origin_frame_is_withheld_before_send(frames):
    _page, child, child_frame = frames
    child.locator("#cvc").type(LIVE_SECRET)
    inputs = [e for e in _sent(child_frame) if e["event"] == "input"]
    assert len(inputs) == len(LIVE_SECRET)
    assert all(e["secret_field"] is True and e["value"] == "<secret>" for e in inputs)
    wire = json.dumps(child_frame.evaluate("window.__sent"))
    for i in range(4, len(LIVE_SECRET) + 1):
        assert LIVE_SECRET[:i] not in wire


def test_parent_links_focused_iframe_without_tokens(frames):
    page, child, _child_frame = frames
    child.locator("#card_name").click()
    focus = [e for e in _sent(page.main_frame) if e["kind"] == "frame_focus"]
    assert focus, "parent emitted no frame_focus"
    f = focus[-1]
    assert f["tag"] == "iframe"
    assert f["id"] == "pay"
    assert f["in_shadow"] is True
    assert f["path"].endswith(">>> iframe#pay")
    assert f["frame_src"] == "http://child.test/pay/frame"
    assert f["child_origin"] == "http://child.test"
    assert f["cross_origin_child"] is True
    assert f["is_iframe"] is False
    assert f["frame_url"] == "http://parent.test/app"
    wire = json.dumps(page.main_frame.evaluate("window.__sent"))
    assert "PARENT-TOKEN-123" not in wire
    assert "CHILD-TOKEN-456" not in wire


def test_ingest_links_child_targets_to_parent_iframe_path(frames, tmp_path):
    page, child, child_frame = frames
    child.locator("#card_name").click()
    child.locator("#cvc").type(LIVE_SECRET)
    parent_lines = _sent(page.main_frame)
    child_lines = _sent(child_frame)
    # Each frame's evidence arrives on its own channel; the parent's
    # frame_focus (fired as focus enters the frame) precedes the child's lines.
    ordered = parent_lines + child_lines

    buf = ExtensionEvidenceBuffer()
    for line in ordered:
        buf.append(line)
    buf.write(tmp_path)
    on_disk = (tmp_path / "extension_evidence.jsonl").read_text(encoding="utf-8")
    assert LIVE_SECRET not in on_disk
    assert "TOKEN" not in on_disk

    loaded = load_extension_evidence(tmp_path)
    cross = cross_origin_frame_targets(loaded)
    assert cross and all(e["frame_origin"] == "http://child.test" for e in cross)
    linked = [
        e
        for e in link_frame_targets(loaded)
        if e.get("kind") == "target" and e["is_iframe"]
    ]
    assert linked
    assert all(e["parent_frame_path"].endswith(">>> iframe#pay") for e in linked)
    assert all(e["parent_frame_url"] == "http://parent.test/app" for e in linked)


def test_no_frame_evidence_when_not_recording(frames):
    page, child, child_frame = frames
    for frame in (page.main_frame, child_frame):
        frame.evaluate("window.__openadaptExtensionContent.setRecording(false)")
        frame.evaluate("window.__sent.length = 0")
    child.locator("#card_name").click()
    child.locator("#cvc").type(LIVE_SECRET)
    assert _sent(page.main_frame) == []
    assert _sent(child_frame) == []
