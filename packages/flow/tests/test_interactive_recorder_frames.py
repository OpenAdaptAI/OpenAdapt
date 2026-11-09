"""Interactive recorder: iframe events are page-space or refused.

A DOM event inside an iframe delivers frame-local ``clientX``/``clientY``.
Every consumer of a recording (frame crops, ``_FramePoint`` replay descent,
secret redaction rects) treats recorded points as top-document page space, so
the in-page listeners compose the frame offset at capture time and name the
frame chain (``structural.frame_path``). A frame whose page-space position
cannot be proven (cross-origin) refuses the recording instead of emitting a
point in an undeclared coordinate space.
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

TOP_HTML = """<!doctype html><title>frame fixture</title>
<body style="margin:0;background:#fff">
<button id="top" style="position:absolute;left:20px;top:20px;width:120px;
height:30px">Top Button</button>
<iframe id="outer" src="/outer.html" style="position:absolute;left:300px;
top:300px;width:520px;height:320px;border:3px solid #333;padding:5px"></iframe>
</body>"""

OUTER_HTML = """<!doctype html><title>outer</title>
<body style="margin:0;background:#eee">
<iframe id="inner" src="/inner.html" style="position:absolute;left:40px;
top:40px;width:420px;height:220px;border:0"></iframe>
</body>"""

INNER_HTML = """<!doctype html><title>inner</title>
<body style="margin:0;background:#fff">
<button id="deep" style="position:absolute;left:20px;top:20px;width:130px;
height:30px">Deep Button</button>
<label for="ifield" style="position:absolute;left:20px;top:70px">Notes</label>
<input id="ifield" name="ifield" style="position:absolute;left:20px;
top:90px;width:200px;height:24px">
</body>"""

CROSS_TOP_HTML = """<!doctype html><title>cross fixture</title>
<body style="margin:0;background:#fff">
<button id="top" style="position:absolute;left:20px;top:20px;width:120px;
height:30px">Top Button</button>
<iframe id="foreign" src="__FOREIGN__/inner.html" style="position:absolute;
left:300px;top:300px;width:420px;height:220px;border:0"></iframe>
</body>"""


def _serve(pages: dict[str, str]) -> tuple[str, Callable[[], None]]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            body = pages.get(self.path.lstrip("/"), pages["top.html"]).encode()
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
def framed_url() -> Iterator[str]:
    url, stop = _serve(
        {"top.html": TOP_HTML, "outer.html": OUTER_HTML, "inner.html": INNER_HTML}
    )
    yield f"{url}/top.html"
    stop()


def _events(rec_dir: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (rec_dir / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]


def _center(box: dict) -> tuple[int, int]:
    return (
        round(box["x"] + box["width"] / 2),
        round(box["y"] + box["height"] / 2),
    )


@pytest.fixture(scope="module")
def framed_recording(
    framed_url: str, tmp_path_factory: pytest.TempPathFactory
) -> tuple[Path, dict[str, tuple[int, int]]]:
    truth: dict[str, tuple[int, int]] = {}

    def drive(page, pump) -> None:
        def settle(count: int = 6) -> None:
            for _ in range(count):
                pump()

        page.wait_for_selector("#outer", state="visible", timeout=20000)
        inner = page.frame_locator("#outer").frame_locator("#inner")
        inner.locator("#deep").wait_for(state="visible", timeout=20000)
        top_button = page.locator("#top")
        deep_button = inner.locator("#deep")
        field = inner.locator("#ifield")
        # bounding_box is relative to the main frame: the page-space truth.
        truth["top"] = _center(top_button.bounding_box())
        truth["deep"] = _center(deep_button.bounding_box())
        box = field.bounding_box()
        truth["field"] = (round(box["x"]), round(box["y"]))
        truth["field_center"] = _center(box)
        top_button.click()
        settle()
        deep_button.click()
        settle()
        field.click()
        settle()
        page.keyboard.type("hello")
        settle()
        # A boundary event flushes the pending type run.
        top_button.click()
        settle()

    out = tmp_path_factory.mktemp("framed_rec") / "rec"
    rec = record_interactive(framed_url, out, headless=True, script=drive)
    return rec, truth


def test_frame_events_record_page_space_points(framed_recording) -> None:
    rec, truth = framed_recording
    meta = json.loads((rec / "meta.json").read_text())
    assert meta["coordinate_space"] == "page"

    clicks = [e for e in _events(rec) if e["kind"] == "click"]
    assert len(clicks) == 4
    top_click, deep_click, field_click, _ = clicks
    assert (top_click["x"], top_click["y"]) == truth["top"]
    assert abs(deep_click["x"] - truth["deep"][0]) <= 1
    assert abs(deep_click["y"] - truth["deep"][1]) <= 1
    # The two targets are 300+ px apart in page space; frame-local capture
    # collapses them onto the same point.
    assert (deep_click["x"], deep_click["y"]) != (top_click["x"], top_click["y"])
    assert abs(field_click["x"] - truth["field_center"][0]) <= 2
    assert abs(field_click["y"] - truth["field_center"][1]) <= 2


def test_frame_events_name_their_frame_chain(framed_recording) -> None:
    rec, _truth = framed_recording
    clicks = [e for e in _events(rec) if e["kind"] == "click"]
    top_click, deep_click, field_click, _ = clicks
    assert top_click["structural"]["selector"] == "#top"
    assert "frame_path" not in top_click["structural"]
    assert deep_click["structural"]["selector"] == "#deep"
    assert deep_click["structural"]["frame_path"] == ["#outer", "#inner"]
    assert field_click["structural"]["frame_path"] == ["#outer", "#inner"]


def test_frame_field_rect_is_page_space(framed_recording) -> None:
    rec, truth = framed_recording
    type_events = [e for e in _events(rec) if e["kind"] == "type"]
    assert len(type_events) == 1
    event = type_events[0]
    assert event["text"] == "hello"
    rect = event["field_rect"]
    assert abs(rect[0] - truth["field"][0]) <= 2
    assert abs(rect[1] - truth["field"][1]) <= 2


def test_framed_recording_compiles_with_frame_path(
    framed_recording, tmp_path: Path
) -> None:
    rec, _truth = framed_recording
    workflow = compile_recording(rec, tmp_path / "bundle", name="framed")
    framed_steps = [
        s
        for s in workflow.steps
        if s.anchor is not None
        and s.anchor.structural is not None
        and s.anchor.structural.frame_path
    ]
    assert framed_steps
    assert framed_steps[0].anchor.structural.frame_path == ["#outer", "#inner"]


def test_cross_origin_frame_refuses_the_recording(
    tmp_path: Path,
) -> None:
    foreign_url, stop_foreign = _serve({"top.html": INNER_HTML})
    pages = {"top.html": CROSS_TOP_HTML.replace("__FOREIGN__", foreign_url)}
    url, stop = _serve(pages)
    try:

        def drive(page, pump) -> None:
            page.wait_for_selector("#foreign", state="visible", timeout=20000)
            frame = page.frame_locator("#foreign")
            frame.locator("#deep").wait_for(state="visible", timeout=20000)
            frame.locator("#deep").click()
            for _ in range(6):
                pump()

        with pytest.raises(BrowserAttachError, match="iframe"):
            record_interactive(
                f"{url}/top.html", tmp_path / "rec", headless=True, script=drive
            )
    finally:
        stop()
        stop_foreign()
