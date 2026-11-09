"""The interactive recorder binds the icon-only Save a person clicked.

``record_interactive`` drives its own Playwright session, so this test lives
apart from the module-scoped browser of ``test_submit_binding_record_replay``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

import pytest

from openadapt_flow.compiler import compile_recording
from openadapt_flow.ir import Workflow
from openadapt_flow.runtime import Replayer
from tests.test_submit_binding_record_replay import _page

sync_api = pytest.importorskip("playwright.sync_api")

pytestmark = pytest.mark.timeout(600)


@pytest.fixture(scope="module")
def served_claims(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """The claims page over loopback HTTP, as the interactive recorder needs."""

    import functools
    import http.server
    import threading

    root = tmp_path_factory.mktemp("served-claims")
    (root / "index.html").write_text(_page())

    class _Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            return None

    handler = functools.partial(_Quiet, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/index.html"
    finally:
        server.shutdown()
        server.server_close()


def test_interactive_recording_binds_the_save_a_person_clicked(
    served_claims: str, tmp_path: Path
) -> None:
    """A person's own demonstration (the interactive recorder) yields the same
    binding, from in-page evidence captured before the click took effect."""

    from openadapt_flow.interactive_recorder import record_interactive

    def drive(page: Any, pump: Any) -> None:
        for selector, value in (
            ("#ins", "610420233"),
            ("#cno", "VD263957"),
            ("#expl", "Red skin"),
        ):
            page.click(selector)
            pump()
            page.keyboard.type(value)
            pump()
        page.click(".savefab")
        for _ in range(4):
            pump()

    recording = record_interactive(
        served_claims,
        tmp_path / "recording",
        param_fields=("ins", "cno", "expl"),
        headless=True,
        script=drive,
    )
    events = [
        json.loads(line)
        for line in (recording / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    save_event = events[-1]
    assert save_event["kind"] == "click"
    observed = save_event["submit_binding"]
    assert observed["fields"] == 3
    assert observed["form_owner"] == "fields_only"
    assert observed["candidates"] == 1
    assert "icon" not in observed and len(observed["icon_sha256"]) == 64
    for value in ("610420233", "VD263957", "Red skin"):
        assert value not in json.dumps(observed)

    workflow = compile_recording(recording, tmp_path / "bundle", name="claims")
    save = workflow.steps[-1]
    assert save.submit_binding is not None, save.submit_binding_unbound_reason
    assert save.submit_binding.filled_fields == 3

    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(
                viewport={"width": 1280, "height": 800}, device_scale_factor=1
            )
            page.goto(served_claims)
            report = Replayer(PlaywrightBackend(page)).run(
                Workflow.load(tmp_path / "bundle"),
                params={"ins": "610420118", "cno": "VD264752", "expl": "Fever"},
                bundle_dir=tmp_path / "bundle",
                run_dir=tmp_path / "run",
            )
            actions = page.evaluate("window.actions")
        finally:
            browser.close()

    assert report.success is True, report.results[-1].error
    assert report.results[-1].submit_binding_verified is True
    assert actions == ["save"]
