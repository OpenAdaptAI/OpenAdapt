"""Real loopback Chromium controls; no sleeps between demonstrated actions."""

from __future__ import annotations

import json
import queue
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO

import pytest
from PIL import Image

from openadapt_flow.interactive_recorder import BrowserAttachError, InteractiveRecorder
from tests.test_browser_attach import _chromium_executable


@pytest.fixture
def palette(tmp_path):
    state = dict(posts=0)
    started, release = threading.Event(), threading.Event()
    markup = b"""<!doctype html><title>Palette board</title>
    <body style="margin:0;background:rgb(245,245,245)">
    <button id="left" onclick="document.body.style.background='rgb(210,20,30)'">Warm panel</button>
    <button id="right" onclick="document.body.style.background='rgb(20,30,210)'">Cool panel</button>
    <a id="next" href="/next.html">Next board</a>
    <button id="save" onclick="fetch('/save',{method:'POST'}).then(()=>document.body.dataset.saved='yes')">Keep board</button>
    </body>"""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = markup
            if self.path == "/next.html":
                body = body.replace(b"rgb(245,245,245)", b"rgb(20,30,210)")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # noqa: N802
            assert self.path == "/save"
            state["posts"] += 1
            started.set()
            if release.wait(5):
                self.send_response(204)
            else:
                self.send_response(503)
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    executable = _chromium_executable()
    assert executable is not None, (
        "cached Chromium is required; no install is permitted"
    )
    profile = tmp_path / "owned-chromium-profile"
    profile.mkdir()
    process = subprocess.Popen(
        [
            str(executable),
            "--headless=new",
            "--no-sandbox",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=0",
            f"--user-data-dir={profile}",
            "--window-size=1280,800",
            "--force-device-scale-factor=1",
            url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    endpoints: queue.Queue[str] = queue.Queue()

    def read_startup():
        assert process.stderr is not None
        for raw in iter(process.stderr.readline, b""):
            found = re.search(
                rb"DevTools listening on (ws://127\.0\.0\.1:[0-9]+/devtools/browser/[A-Za-z0-9-]+)",
                raw,
            )
            if found:
                endpoints.put(found.group(1).decode("ascii"))

    reader = threading.Thread(target=read_startup, daemon=True)
    reader.start()
    try:
        endpoint = endpoints.get(timeout=10)
        yield url, endpoint, state, started, release, process
    finally:
        release.set()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        if process.stderr is not None:
            process.stderr.close()
        reader.join(timeout=1)
        server.shutdown()
        server.server_close()
        worker.join(timeout=1)


@pytest.mark.timeout(90)
@pytest.mark.parametrize(
    "phase",
    [
        "after-acquisition",
        "during-screenshot",
        "navigation-after-acquisition",
        "write-straddle",
        "cancel-during-capture",
    ],
)
def test_real_cdp_pairs_are_exact_or_recording_is_refused(
    palette, tmp_path, monkeypatch, record_property, phase
):
    url, endpoint, state, started, release, process = palette
    out = tmp_path / "recording"
    session = InteractiveRecorder(url, out, cdp_endpoint=endpoint, capture_network=True)
    session.start()
    assert session.page is not None and session.backend is not None
    session.page.wait_for_selector("#left")
    began = time.monotonic()
    pairs = []
    original_persist = session._persist_acquired_event
    original_screenshot = session.backend.screenshot
    injected = False

    def persist(event):
        nonlocal injected
        boundary = session._captured_boundary
        assert boundary is not None
        pairs.append(boundary.png)
        if not injected and phase in {
            "after-acquisition",
            "navigation-after-acquisition",
        }:
            injected = True
            session.page.click("#next" if phase.startswith("navigation") else "#right")
        original_persist(event)

    def screenshot():
        nonlocal injected
        if not injected and phase in {
            "during-screenshot",
            "write-straddle",
            "cancel-during-capture",
        }:
            injected = True
            if phase == "cancel-during-capture":
                raise KeyboardInterrupt()
            if phase == "write-straddle":
                assert started.wait(2), "the owned write must actually be in flight"
            session.page.click("#right")
            release.set()
        return original_screenshot()

    monkeypatch.setattr(session, "_persist_acquired_event", persist)
    monkeypatch.setattr(session.backend, "screenshot", screenshot)
    try:
        session.page.click("#save" if phase == "write-straddle" else "#left")
        if phase == "cancel-during-capture":
            with pytest.raises(KeyboardInterrupt):
                session.pump()
            with pytest.raises(BrowserAttachError, match="interrupted"):
                session.finish()
            assert not out.exists()
        elif phase in {"during-screenshot", "write-straddle"}:
            with pytest.raises(BrowserAttachError, match="more than one logical"):
                session.pump()
            session.abort()
            assert not (out / "meta.json").exists()
            if phase == "write-straddle":
                assert state["posts"] == 1
        else:
            session.pump()
            session.pump()
            result = session.finish()
            events = [
                json.loads(row)
                for row in (result / "events.jsonl").read_text().splitlines()
            ]
            assert [event["kind"] for event in events] == ["click", "click"]
            assert len(pairs) == 2 and pairs[0] != pairs[1]
            colors = []
            for png in pairs:
                with Image.open(BytesIO(png)) as image:
                    colors.append(image.convert("RGB").getpixel((600, 400)))
            assert colors == [(210, 20, 30), (20, 30, 210)]
        assert injected
        assert process.poll() is None, "attached browser remains owned by this fixture"
    finally:
        release.set()
        session.abort()
        record_property(
            "attempted_actions", 1 if phase == "cancel-during-capture" else 2
        )
        record_property("observed_elapsed_s", time.monotonic() - began)
        record_property("case", phase)
