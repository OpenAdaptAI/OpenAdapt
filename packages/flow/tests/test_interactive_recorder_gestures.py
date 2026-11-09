"""Interactive recorder: gesture coverage (double-click, select, checkbox,
contenteditable).

A double-click gesture compiles to ONE ``DOUBLE_CLICK`` step: the in-page
listener marks the second click (``e.detail === 2``) and Python absorbs the
held first click, so replay delivers exactly the two demonstrated clicks. A
native ``<select>`` selection refuses the recording loudly: its option commit
happens in browser-native dropdown UI that produces no recordable action
events, so the choice would otherwise be silently absent from the compiled
workflow. A checkbox records only its click. ``contenteditable`` text entry
records the typed text.
"""

from __future__ import annotations

import http.server
import json
import threading
from pathlib import Path
from typing import Callable, Iterator

import pytest

from openadapt_flow.compiler import compile_recording
from openadapt_flow.interactive_recorder import BrowserAttachError, record_interactive

pytestmark = pytest.mark.timeout(600)

PAGE_HTML = """<!doctype html><title>gesture fixture</title>
<body style="margin:0;background:#fff">
<button id="dbl" ondblclick="this.textContent='Opened Row'"
  style="position:absolute;left:20px;top:20px;width:150px;height:30px">
Patient Row</button>
<button id="single" style="position:absolute;left:20px;top:70px;width:150px;
height:30px">Plain Button</button>
<input type="checkbox" id="chk" style="position:absolute;left:20px;
top:120px;width:20px;height:20px">
<div id="ce" contenteditable="true" style="position:absolute;left:20px;
top:160px;width:300px;height:40px;border:1px solid #999"></div>
<label for="sel" style="position:absolute;left:20px;top:220px">Species</label>
<select id="sel" name="sel" style="position:absolute;left:20px;top:240px;
width:200px">
<option value="">Choose</option>
<option value="v-alpha">Label Alpha</option>
<option value="v-beta">Label Beta</option>
</select>
</body>"""


def _serve() -> tuple[str, Callable[[], None]]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            body = PAGE_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    return url, server.shutdown


@pytest.fixture(scope="module")
def fixture_url() -> Iterator[str]:
    url, stop = _serve()
    yield f"{url}/page.html"
    stop()


def _events(rec_dir: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (rec_dir / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]


def _settle(pump, count: int = 12) -> None:
    # 12 pumps x 60 ms poll outlives the 500 ms double-click hold window.
    for _ in range(count):
        pump()


def test_double_click_compiles_to_one_double_click_step(
    fixture_url: str, tmp_path: Path
) -> None:
    def drive(page, pump) -> None:
        page.wait_for_selector("#dbl", state="visible", timeout=20000)
        page.locator("#single").click()
        _settle(pump)
        page.locator("#dbl").dblclick()
        _settle(pump)

    rec = record_interactive(fixture_url, tmp_path / "rec", headless=True, script=drive)
    events = _events(rec)
    assert [e["kind"] for e in events] == ["click", "double_click"]
    assert events[1]["structural"]["selector"] == "#dbl"

    workflow = compile_recording(rec, tmp_path / "bundle", name="gestures")
    assert [step.action.value for step in workflow.steps] == [
        "click",
        "double_click",
    ]


def test_single_click_survives_the_double_click_hold(
    fixture_url: str, tmp_path: Path
) -> None:
    """A click followed only by idle pumps flushes as a single click."""

    def drive(page, pump) -> None:
        page.wait_for_selector("#single", state="visible", timeout=20000)
        page.locator("#single").click()
        _settle(pump)

    rec = record_interactive(fixture_url, tmp_path / "rec", headless=True, script=drive)
    assert [e["kind"] for e in _events(rec)] == ["click"]


def test_checkbox_records_click_without_phantom_type(
    fixture_url: str, tmp_path: Path
) -> None:
    def drive(page, pump) -> None:
        page.wait_for_selector("#chk", state="visible", timeout=20000)
        page.locator("#chk").click()
        _settle(pump)

    rec = record_interactive(fixture_url, tmp_path / "rec", headless=True, script=drive)
    assert [e["kind"] for e in _events(rec)] == ["click"]


def test_contenteditable_records_typed_text(fixture_url: str, tmp_path: Path) -> None:
    def drive(page, pump) -> None:
        page.wait_for_selector("#ce", state="visible", timeout=20000)
        page.locator("#ce").click()
        _settle(pump)
        page.keyboard.type("hello notes")
        _settle(pump)
        page.locator("#single").click()
        _settle(pump)

    rec = record_interactive(fixture_url, tmp_path / "rec", headless=True, script=drive)
    kinds = [e["kind"] for e in _events(rec)]
    assert kinds == ["click", "type", "click"]
    type_event = _events(rec)[1]
    assert type_event["text"] == "hello notes"


def test_native_select_refuses_the_recording(fixture_url: str, tmp_path: Path) -> None:
    def drive(page, pump) -> None:
        page.wait_for_selector("#sel", state="visible", timeout=20000)
        page.locator("#sel").select_option(label="Label Alpha")
        _settle(pump)

    with pytest.raises(BrowserAttachError, match="select"):
        record_interactive(fixture_url, tmp_path / "rec", headless=True, script=drive)
