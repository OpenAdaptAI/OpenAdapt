"""The exception watch under browser faults, in a real headless Chromium.

* A save the page aborts while the watch holds it paused: the browser refuses
  the watch's decision. The watch logs it and keeps going, and the next save
  is still blocked while the card is open.
* The watch's own DevTools session is dropped while a card is open: the
  watch says so, logs ``watch_lost`` and ends with exit code 4.

Synthetic page on 127.0.0.1; nothing is sent anywhere else.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("playwright")

from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey,
)

from openadapt_flow.approval_mode.approver_roster import (  # noqa: E402
    ROOT_ENV,
    file_key_signer,
    init_root,
)
from openadapt_flow.exceptions.registry import (  # noqa: E402
    ExceptionRegistry,
    roster_verifier,
)
from openadapt_flow.exceptions.watch import (  # noqa: E402
    _PAUSED_TYPES,
    WATCH_EVENTS,
    ExceptionWatch,
    run_loop,
)
from openadapt_flow.jit.authority import public_hex  # noqa: E402

pytestmark = pytest.mark.timeout(300)

PAGE = b"<!doctype html><html><body><h1>New note</h1></body></html>"
CODE_SCREEN = {
    "text": "Verification code",
    "elements": [
        {
            "tag": "input",
            "autocomplete": "one-time-code",
            "visible": True,
            "bbox": [1, 1, 50, 10],
        }
    ],
}
#: A save the page gives up on after ``ms``, recorded in window.saveResult.
SAVE = """(ms) => {
  window.saveResult = null;
  const c = new AbortController();
  fetch('/save', {method: 'POST', body: 'x', signal: c.signal})
    .then(r => { window.saveResult = r.status; })
    .catch(() => { window.saveResult = 'aborted'; });
  if (ms > 0) { setTimeout(() => c.abort(), ms); }
}"""


@pytest.fixture
def site():
    hits: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a: Any) -> None:
            return None

        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(PAGE)))
            self.end_headers()
            self.wfile.write(PAGE)

        def do_POST(self) -> None:  # noqa: N802
            hits.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", hits
    server.shutdown()
    server.server_close()


@pytest.fixture
def tab(site):
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    try:
        browser = pw.chromium.launch(headless=True)
    except Exception as exc:  # noqa: BLE001
        pw.stop()
        pytest.skip(f"chromium unavailable: {exc}")
    page = browser.new_page()
    page.goto(site[0] + "/")
    cdp = page.context.new_cdp_session(page)
    paused: list[Any] = []
    cdp.on("Fetch.requestPaused", lambda params: paused.append(params))
    cdp.send(
        "Fetch.enable",
        {
            "patterns": [
                {"urlPattern": "*", "resourceType": t, "requestStage": "Request"}
                for t in _PAUSED_TYPES
            ]
        },
    )
    yield page, cdp, paused
    browser.close()
    pw.stop()


@pytest.fixture
def watch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, site) -> ExceptionWatch:
    key_dir = tmp_path / "keys"
    key = Ed25519PrivateKey.generate()
    init_root(
        key_dir,
        principal="front-desk",
        public_key=public_hex(key),
        sign=file_key_signer(key),
    )
    monkeypatch.setenv(ROOT_ENV, public_hex(key))
    return ExceptionWatch(
        ExceptionRegistry(tmp_path / "registry"),
        run_ref="faults",
        verify=roster_verifier(key_dir, public_hex(key)),
        app_origin=site[0],
        events_path=tmp_path / "run" / WATCH_EVENTS,
    )


def _events(tmp_path: Path) -> list[dict]:
    path = tmp_path / "run" / WATCH_EVENTS
    return [json.loads(line) for line in path.read_text().splitlines()]


def _until(page: Any, check: Any, what: str) -> None:
    for _ in range(100):
        if check():
            return
        page.wait_for_timeout(50)
    raise AssertionError(f"timed out waiting for {what}")


def _ticks(n: int) -> Any:
    count = [0]

    def stop() -> bool:
        count[0] += 1
        return count[0] >= n

    return stop


def test_an_aborted_paused_save_does_not_end_the_watch(
    tmp_path: Path, site, tab, watch: ExceptionWatch
) -> None:
    _base, hits = site
    page, cdp, paused = tab
    assert watch.observe(CODE_SCREEN) == "second_factor"

    # The page gives up on a save while the watch holds it paused.
    page.evaluate(SAVE, 100)
    _until(page, lambda: bool(paused), "the save to be paused")
    _until(
        page,
        lambda: page.evaluate("() => window.saveResult") == "aborted",
        "the page to abort",
    )
    # The watch decides it late: the browser refuses, the loop goes on.
    run_loop(watch, page, cdp, paused, poll_s=0.05, stop=_ticks(5))
    assert watch.lost is None
    assert "request_decision_failed" in [e["event"] for e in _events(tmp_path)]

    # The next save is still blocked while the card is open.
    page.evaluate(SAVE, 0)
    _until(page, lambda: bool(paused), "the second save to be paused")
    run_loop(watch, page, cdp, paused, poll_s=0.05, stop=_ticks(5))
    _until(
        page,
        lambda: page.evaluate("() => window.saveResult") == 409,
        "the block",
    )
    assert hits == []
    assert watch.lost is None
    assert watch.stop_code() == 6  # it would end with the card unanswered


def test_a_dropped_session_while_a_card_is_open_exits_loudly(
    tmp_path: Path, tab, watch: ExceptionWatch, capsys: pytest.CaptureFixture
) -> None:
    page, cdp, paused = tab
    assert watch.observe(CODE_SCREEN) == "second_factor"
    dropped = [False]

    def drop_then_go_on() -> bool:
        if not dropped[0]:
            cdp.detach()  # the watch's own DevTools session goes away
            dropped[0] = True
        return False

    run_loop(watch, page, cdp, paused, poll_s=0.05, max_s=30, stop=drop_then_go_on)
    assert watch.lost == "session_lost"
    assert watch.stop_code() == 4
    [lost] = [e for e in _events(tmp_path) if e["event"] == "watch_lost"]
    assert lost["card_open"] is True
    assert "WATCH LOST" in capsys.readouterr().err
