"""Interruption classes end to end: a real browser, the real replayer and OCR.

A local synthetic app saves a note with ``POST /save``. Before the save, it
shows one interruption, chosen by the page address. The server counts every
request it receives, so each case can tell a cleared interruption from a
halted run and a blocked write from one that reached the server.

Pre-registered cases (``docs/INTERRUPTION_RECOVERY.md``):

=============  ===============================================  ============
interruption   what its control does                            expected
=============  ===============================================  ============
session        "Stay signed in" removes the dialog (GET ping)   saved
banner         "Close" removes an "Update available" banner     saved
committing     "Stay signed in" also POSTs /logout              halt, no write
unknown        an unlisted dialog                               halt, no click
=============  ===============================================  ============
"""

from __future__ import annotations

import http.server
import threading
from typing import Any, Iterator

import pytest

pytest.importorskip("playwright.sync_api")

from openadapt_flow.interruptions import interruption_from_data  # noqa: E402
from openadapt_flow.ir import (  # noqa: E402
    ActionKind,
    Anchor,
    Step,
    StructuralLocator,
    Workflow,
)

PAGE = b"""<!doctype html><html><body style="font: 20px sans-serif">
<h1>Encounter note</h1>
<button id="save" style="font-size:20px">Save note</button>
<div id="status"></div>
<script>
document.getElementById('save').onclick = () =>
  fetch('/save', {method: 'POST', body: 'note'}).then(() => {
    document.getElementById('status').textContent = 'Saved';
  });
const kind = location.hash.slice(1);
function dialog(text, label, onclick) {
  const d = document.createElement('div');
  d.setAttribute('role', 'dialog');
  d.style = 'position:fixed;inset:0;background:#fff;padding:40px';
  d.innerHTML = '<p style="font-size:28px">' + text + '</p>';
  const b = document.createElement('button');
  b.textContent = label; b.style = 'font-size:22px';
  b.onclick = () => { onclick(); d.remove(); };
  d.appendChild(b);
  document.body.appendChild(d);
}
if (kind === 'session') {
  dialog('Your session will expire in 5 minutes', 'Stay signed in',
         () => fetch('/ping'));
} else if (kind === 'committing') {
  dialog('Your session will expire in 5 minutes', 'Stay signed in',
         () => fetch('/logout', {method: 'POST'}));
} else if (kind === 'unknown') {
  dialog('Unexpected error 42 occurred', 'Close', () => {});
} else if (kind === 'banner') {
  const b = document.createElement('div');
  b.style = 'position:fixed;top:0;left:0;right:0;height:120px;background:#ffd;'
          + 'font-size:28px;padding:20px';
  b.innerHTML = 'Update available <button aria-label="Close">x</button>';
  b.querySelector('button').onclick = () => b.remove();
  document.body.appendChild(b);
}
</script></body></html>"""


class _Server(http.server.BaseHTTPRequestHandler):
    seen: list[str] = []

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        type(self).seen.append(f"GET {self.path}")
        self.send_response(200)
        self.send_header("content-type", "text/html")
        self.end_headers()
        self.wfile.write(PAGE)

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        type(self).seen.append(f"POST {self.path}")
        self.send_response(204)
        self.end_headers()

    def log_message(self, *_: Any) -> None:
        pass


CLASSES = [
    interruption_from_data(
        {
            "name": "session expiry warning",
            "texts": ["Your session will expire"],
            "dismiss": {"control": {"role": "button", "name": "Stay signed in"}},
        }
    ),
    interruption_from_data(
        {
            "name": "update banner",
            "texts": ["Update available"],
            "dismiss": {"control": {"role": "button", "name": "Close"}},
        }
    ),
]


@pytest.fixture()
def server() -> Iterator[str]:
    _Server.seen = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Server)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}/"
    finally:
        srv.shutdown()


def _run(url: str, kind: str, tmp_path: Any) -> Any:
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    from openadapt_flow.runtime.replayer import Replayer

    try:
        backend, close = PlaywrightBackend.launch(url + "#" + kind, headless=True)
    except Exception as exc:  # pragma: no cover - no browser installed
        pytest.skip(f"no Playwright browser: {exc}")
    workflow = Workflow(
        name="save note",
        steps=[
            Step(
                id="save",
                intent="click 'Save note'",
                action=ActionKind.CLICK,
                anchor=Anchor(
                    template="",
                    structural=StructuralLocator(selector="#save"),
                    region=(8, 80, 120, 30),
                    click_point=(60, 95),
                    ocr_text="Save note",
                ),
                risk="reversible",
            )
        ],
        interstitials=CLASSES,
    )
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    try:
        report = Replayer(backend).run(
            workflow, bundle_dir=bundle, run_dir=tmp_path / "run"
        )
        backend.page.wait_for_timeout(300)
    finally:
        close()
    return report


@pytest.mark.parametrize("kind", ["session", "banner"])
def test_a_benign_interruption_is_cleared_and_the_save_lands(
    server: str, kind: str, tmp_path: Any
) -> None:
    report = _run(server, kind, tmp_path)
    assert report.success is True, report.results[0].error
    event = report.results[0].interstitial_actions[0]
    assert event.ok and event.effect_watchers == ["network"]
    assert event.write_attempts == []
    assert "POST /save" in _Server.seen


def test_a_dismissal_that_commits_halts_and_nothing_is_written(
    server: str, tmp_path: Any
) -> None:
    report = _run(server, "committing", tmp_path)
    assert report.success is False
    result = report.results[0]
    assert result.safety_halt is True
    assert any("/logout" in a for a in result.interstitial_actions[0].write_attempts)
    # The fence stopped the logout in the browser, and the save never ran.
    assert not any(seen.startswith("POST") for seen in _Server.seen)


def test_an_unknown_dialog_halts_without_a_click(server: str, tmp_path: Any) -> None:
    report = _run(server, "unknown", tmp_path)
    assert report.success is False
    assert report.results[0].interstitial_actions == []
    assert not any(seen.startswith("POST") for seen in _Server.seen)
