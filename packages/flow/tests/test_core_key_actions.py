"""KEY, HOTKEY and WAIT steps on the real browser.

Characterization tests for the first action executors moved into
``openadapt_flow.runtime.core`` (docs/design/DECOMPOSITION.md, PR 2). They
pin what the replayer does today, through the real ``PlaywrightBackend``, so
the move can be shown to change nothing. The guarded-keyboard path is pinned
by ``test_playwright_atomic_actuation.py`` and the refusal of raw input on a
consequential identity step by ``test_replayer.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from openadapt_flow.ir import ActionKind, Step, Workflow
from openadapt_flow.runtime.replayer import Replayer

sync_api = pytest.importorskip("playwright.sync_api")

_PAGE = """
<!doctype html>
<html><body>
<form id="f" onsubmit="window.events.push('submit'); return false;">
  <input id="field" autofocus>
</form>
<script>
  window.events = [];
  document.addEventListener('keydown', (e) => {
    const mods = ['Control', 'Shift', 'Alt', 'Meta'].filter(
      (m) => e.getModifierState(m));
    window.events.push('keydown:' + [...mods, e.key].join('+'));
  });
</script>
</body></html>
"""


@pytest.fixture(scope="module")
def browser() -> Iterator[object]:
    with sync_api.sync_playwright() as playwright:
        chromium = playwright.chromium.launch(headless=True)
        yield chromium
        chromium.close()


def _run(browser: object, tmp_path: Path, *steps: Step) -> tuple[object, list[str]]:
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    page = browser.new_page(viewport={"width": 640, "height": 320})
    try:
        page.set_content(_PAGE)
        page.locator("#field").focus()
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        report = Replayer(PlaywrightBackend(page), use_structural=False).run(
            Workflow(name="keys", steps=list(steps)),
            bundle_dir=bundle,
            run_dir=tmp_path / "run",
        )
        events = page.evaluate("window.events")
    finally:
        page.close()
    return report, events


def test_key_step_presses_the_key_and_the_form_submits(browser, tmp_path) -> None:
    report, events = _run(
        browser,
        tmp_path,
        Step(id="k1", intent="submit", action=ActionKind.KEY, key="Enter"),
    )

    assert report.success is True
    assert events == ["keydown:Enter", "submit"]
    result = report.results[0]
    assert result.delivery_attempted is True
    assert result.actuation is None  # an unguarded press records no actuation kind
    assert result.delivery_receipt is None


def test_hotkey_step_joins_modifiers_and_key(browser, tmp_path) -> None:
    report, events = _run(
        browser,
        tmp_path,
        Step(
            id="h1",
            intent="select all",
            action=ActionKind.HOTKEY,
            key="a",
            modifiers=["Shift"],
        ),
    )

    assert report.success is True
    # Playwright presses the lowercase key with Shift held.
    assert events == ["keydown:Shift+Shift", "keydown:Shift+a"]


def test_key_step_without_a_key_fails_without_input(browser, tmp_path) -> None:
    report, events = _run(
        browser,
        tmp_path,
        Step(id="k0", intent="press nothing", action=ActionKind.KEY, key=None),
    )

    assert report.success is False
    assert events == []
    result = report.results[0]
    assert result.error == "Step 'k0' (press nothing) is a key step with no key"
    assert result.delivery_attempted is False


def test_wait_step_sends_no_input(browser, tmp_path) -> None:
    report, events = _run(
        browser,
        tmp_path,
        Step(id="w1", intent="let it settle", action=ActionKind.WAIT),
    )

    assert report.success is True
    assert events == []
    assert report.results[0].delivery_attempted is False
