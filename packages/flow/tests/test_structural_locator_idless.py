"""Id-less controls: which get a structural selector, and which never do.

A field with no ``id`` but a unique ``name`` (Trac's ``field_summary``) gets
``input[name=...]``. A button never does: its ``name`` is the form field it
submits (Trac's ``<input type=submit name=submit>``, NetBox's
``<button name=_create>``), which another button can carry at replay, so a
button keeps its role and label. A bracketed name (``items[0][qty]``) is a row
position and never a selector either.

These run in real headless Chromium: the backend's locator on a create form,
then a scripted recording through the real ``Recorder`` compiled by the real
compiler.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import pytest

from openadapt_flow.compiler import compile_recording
from openadapt_flow.recorder import Recorder

sync_api = pytest.importorskip("playwright.sync_api")

pytestmark = pytest.mark.timeout(600)

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>New ticket</title>
</head><body style="font:16px Arial;margin:24px">
<h1>New ticket</h1>
<form method="post" action="/newticket" onsubmit="event.preventDefault();
  window.actions.push(event.submitter.name)">
  <p><label>Summary <input name="field_summary" size="40"></label></p>
  <p><input type="submit" name="preview" value="Preview">
     <input type="submit" name="submit" value="Create ticket"></p>
</form>
<form onsubmit="event.preventDefault(); window.actions.push('netbox')">
  <button type="submit" name="_create">Create</button>
  <button type="submit" name="_addanother">Create &amp; Add Another</button>
</form>
<button type="button" onclick="window.actions.push('a')">Save</button>
<button type="button" onclick="window.actions.push('b')">Save</button>
<script>window.actions = [];</script>
</body></html>"""


@pytest.fixture(scope="module")
def page_url(tmp_path_factory: pytest.TempPathFactory) -> str:
    path = tmp_path_factory.mktemp("idless") / "newticket.html"
    path.write_text(PAGE)
    return path.as_uri()


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    with sync_api.sync_playwright() as playwright:
        launched = playwright.chromium.launch(headless=True)
        try:
            yield launched
        finally:
            launched.close()


def _center(page: Any, selector: str) -> tuple[int, int]:
    box = page.locator(selector).first.bounding_box()
    assert box is not None
    return int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2)


def test_locator_names_idless_buttons_by_their_label(
    browser: Any, page_url: str
) -> None:
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    page = browser.new_page(viewport={"width": 1280, "height": 800})
    try:
        page.goto(page_url)
        backend = PlaywrightBackend(page)

        trac = backend.structural_locator_at(*_center(page, "input[name=submit]"))
        assert trac is not None
        # A button is named by its own label, never by the field name it
        # submits: `name` alone could find another button at replay.
        assert trac.selector is None
        assert (trac.role, trac.name) == ("button", "Create ticket")

        netbox = backend.structural_locator_at(*_center(page, "button[name=_create]"))
        assert netbox is not None
        assert netbox.selector is None
        assert (netbox.role, netbox.name) == ("button", "Create")

        summary = backend.structural_locator_at(
            *_center(page, "input[name=field_summary]")
        )
        assert summary is not None
        assert summary.selector == 'input[name="field_summary"]'

        # Two identical unnamed buttons: no selector that could pick the
        # wrong one, only role and name.
        twin = backend.structural_locator_at(*_center(page, "button:text('Save')"))
        assert twin is not None
        assert twin.selector is None
        assert (twin.role, twin.name) == ("button", "Save")
    finally:
        page.close()


def test_recorded_create_step_carries_its_selector(
    browser: Any, page_url: str, tmp_path: Path
) -> None:
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    page = browser.new_page(viewport={"width": 1280, "height": 800})
    try:
        page.goto(page_url)
        recorder = Recorder(PlaywrightBackend(page), tmp_path / "rec", app_url=page_url)
        recorder.click(*_center(page, "input[name=field_summary]"))
        recorder.type_text("Printer jams on tray 2", param="summary")
        recorder.click(*_center(page, "input[name=submit]"))
        recorder.finish()
        assert page.evaluate("window.actions") == ["submit"]
    finally:
        page.close()

    workflow = compile_recording(tmp_path / "rec", tmp_path / "bundle", name="ticket")
    save = workflow.steps[-1]
    assert save.anchor is not None and save.anchor.structural is not None
    assert save.anchor.structural.selector is None
    assert save.anchor.structural.role == "button"
    assert save.anchor.structural.name == "Create ticket"


SHADOW_PAGE = """<!doctype html><html><head><meta charset="utf-8"></head>
<body style="font:16px Arial;margin:24px">
<p><button data-testid="save" onclick="window.actions.push('light')">Save</button></p>
<save-panel></save-panel>
<script>
window.actions = [];
customElements.define('save-panel', class extends HTMLElement {
  constructor() {
    super();
    const root = this.attachShadow({mode: 'open'});
    root.innerHTML = '<button data-testid="save" style="margin-top:40px">Save</button>';
    root.querySelector('button').addEventListener(
      'click', () => window.actions.push('shadow'));
  }
});
</script>
</body></html>"""


def test_a_lookalike_in_another_root_never_names_the_clicked_button(
    browser: Any, tmp_path: Path
) -> None:
    """A Save inside a shadow root, and a look-alike Save in the page with
    the same name. ``button[data-testid="save"]`` matches exactly one element in the
    document, the look-alike, so a count alone would call it unique for the
    shadow button and a replay would click the wrong Save."""

    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    path = tmp_path / "shadow.html"
    path.write_text(SHADOW_PAGE)
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    try:
        page.goto(path.as_uri())
        backend = PlaywrightBackend(page)
        shadow = page.locator("save-panel").locator("button")
        box = shadow.bounding_box()
        assert box is not None
        x, y = int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2)

        # The page's own hit test stops at the shadow host.
        located = backend.structural_locator_at(x, y)
        assert located is None or located.selector != 'button[data-testid="save"]'

        # A hit test that reaches the button inside the shadow root (as a
        # piercing one would): still no selector, because the only match in
        # the document is the look-alike.
        page.evaluate(
            """() => {
              const inner = document.querySelector('save-panel')
                .shadowRoot.querySelector('button');
              document.elementFromPoint = () => inner;
            }"""
        )
        located = backend.structural_locator_at(x, y)
        assert located is not None
        assert located.selector is None
        assert (located.role, located.name) == ("button", "Save")

        # The light-DOM Save is the one element its selector matches, and it
        # is the clicked one: it keeps the selector.
        page.reload()
        light = page.locator("p > button")
        box = light.bounding_box()
        assert box is not None
        located = backend.structural_locator_at(
            int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2)
        )
        assert located is not None and located.selector == 'button[data-testid="save"]'
    finally:
        page.close()
