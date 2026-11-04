"""#1378 on a real OpenEMR: under latency a navigation click is judged on the
screen it led to, not on the screen before it.

A small proxy in this test sits in front of the local OpenEMR fixture and
holds every PHP page (not static assets) for ``DELAY_S`` before forwarding it,
the way a slow server keeps the old screen up. The test signs in through the
proxy and records the click on "Login", a navigation to the tab shell:

- with the recorder's pending-change wait turned off (the behaviour before
  the fix), the after frame is the login page again: the race #1378 hit;
- with the default wait, the after frame is the signed-in shell, the compiled
  step carries visual checks, and the risk inference does not ask for a
  reviewed override for an unproven navigation.

Needs the running local fixture (``benchmark/openemr_local``; this test never
starts or stops containers) and Chromium. It does not reset the database.
From ``packages/flow``::

    OPENEMR_LIVE_UI=1 \\
    OPENEMR_UI_URL=http://127.0.0.1:9301 \\
    OPENEMR_API_URL=https://127.0.0.1:9300 \\
    OPENEMR_FIXTURE_STATE=benchmark/openemr_local/state \\
    PYTHONPATH=../types:. python -m pytest \\
        tests/test_latency_settle_live_openemr.py -v -p no:cacheprovider

``OPENEMR_UI_URL`` must be plain ``http``. ``OPENEMR_FIXTURE_STATE`` and
``OPENEMR_FIXTURE_LOCK`` are optional (see
``benchmark/openemr_local/qualify_hooks.py``).
"""

from __future__ import annotations

import http.client
import json
import os
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

pytestmark = pytest.mark.skipif(
    not (os.environ.get("OPENEMR_LIVE_UI") == "1" and os.environ.get("OPENEMR_UI_URL")),
    reason=(
        "live OpenEMR UI: set OPENEMR_LIVE_UI=1 and OPENEMR_UI_URL (+ "
        "OPENEMR_API_URL, OPENEMR_FIXTURE_STATE) with the local fixture running"
    ),
)

#: How long the proxy holds each PHP page, as in the silent-saves latency block.
DELAY_S = 2.0

_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
}


def _delaying_proxy(upstream: str, delay_s: float) -> tuple[str, Callable[[], None]]:
    """Serve ``upstream`` (plain http) on a loopback port, holding PHP pages."""

    target = urllib.parse.urlsplit(upstream)
    assert target.scheme == "http", "the proxy forwards plain http only"
    host, port = target.hostname or "127.0.0.1", target.port or 80

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:  # quiet
            return

        def _forward(self) -> None:
            path = urllib.parse.urlsplit(self.path).path
            if path.endswith(".php") or path.endswith("/"):
                time.sleep(delay_s)
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None
            headers = {
                k: v for k, v in self.headers.items() if k.lower() not in _HOP_BY_HOP
            }
            headers["Host"] = f"{host}:{port}"
            conn = http.client.HTTPConnection(host, port, timeout=120)
            try:
                conn.request(self.command, self.path, body=body, headers=headers)
                resp = conn.getresponse()
                data = resp.read()
            finally:
                conn.close()
            self.send_response(resp.status, resp.reason)
            here = f"http://{self.headers.get('Host')}"
            for k, v in resp.getheaders():
                if k.lower() in _HOP_BY_HOP:
                    continue
                if k.lower() == "location":
                    v = v.replace(f"http://{host}:{port}", here)
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        do_GET = do_POST = do_HEAD = do_PUT = do_DELETE = do_OPTIONS = _forward

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def stop() -> None:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    return f"http://127.0.0.1:{server.server_address[1]}", stop


@pytest.fixture()
def proxied() -> Iterator[tuple[str, dict[str, str]]]:
    from benchmark.openemr_local.qualify_hooks import fixture

    fx = fixture()
    url, stop = _delaying_proxy(fx.ui_base_url, DELAY_S)
    try:
        yield url, fx._runtime_values()
    finally:
        stop()


def _record_login(
    url: str, values: dict[str, str], out: Path, **recorder_kw: Any
) -> dict[str, Any]:
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    from openadapt_flow.recorder import Recorder

    backend, close = PlaywrightBackend.launch(url)
    try:
        page = backend.page
        page.locator("#authUser").wait_for(state="visible", timeout=60_000)
        # Unrecorded: the test credentials never enter the recording.
        page.locator("#authUser").fill(values["OPENEMR_ACTOR_USER"])
        page.locator("#clearPass").fill(values["OPENEMR_ACTOR_PASSWORD"])
        # Let the login page finish its own loading before the recorded click.
        page.wait_for_load_state("networkidle", timeout=60_000)
        box = page.locator("#login-button").bounding_box()
        assert box is not None
        recorder = Recorder(backend, out, settle_timeout_s=1.0, **recorder_kw)
        recorder.click(
            int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2)
        )
        recorder.finish()
    finally:
        close()
    return json.loads((out / "events.jsonl").read_text().splitlines()[0])


def _frames_differ(out: Path) -> bool:
    from openadapt_flow.recorder import _pixels

    before = (out / "frames" / "0000_before.png").read_bytes()
    after = (out / "frames" / "0000_after.png").read_bytes()
    return _pixels(before) != _pixels(after)


def test_the_login_click_is_judged_on_the_screen_it_led_to(proxied, tmp_path):
    """Under a 2 s delay the Login click is still judged as navigation.

    Measured on the pinned OpenEMR (2026-10-28): with the wait turned off, this
    click compiled to the same checks, because OpenEMR paints a loading screen
    at once. So OpenEMR does not show the #1378 refusal; this test guards that
    the longer wait does not break a real app's navigation step. The refusal
    itself is reproduced in a real browser on MockMed ``slow``
    (tests/test_ui_dismiss_and_latency.py).
    """
    from openadapt_flow.compiler.compile import compile_recording
    from openadapt_flow.ir import PostconditionKind
    from openadapt_flow.risk import infer_step_risk

    url, values = proxied
    out = tmp_path / "rec"
    event = _record_login(url, values, out)
    assert "main.php" in str(event.get("url_after")), event.get("url_after")
    assert _frames_differ(out)

    wf = compile_recording(out, tmp_path / "bundle", name="openemr-login")
    (step,) = wf.steps
    kinds = {p.kind for p in step.expect}
    assert kinds - {PostconditionKind.URL_CHANGED, PostconditionKind.TITLE_CHANGED}
    inference = infer_step_risk(step)
    assert not any(e.startswith("transition:") for e in inference.evidence), inference
