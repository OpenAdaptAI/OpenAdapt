"""The frame descent asks the browser fewer times and proves the same thing.

``PlaywrightBackend._frame_point`` and ``_focused_element_call`` bind a
point or the focused element to one exact document through the frame chain.
They now read each frame boundary in one call and, when the document has no
shadow root, check the frame selector's uniqueness natively instead of
through three Playwright calls. These tests run a real Chromium and compare
the answers with the previous implementation (kept here as the reference)
on pages that exercise every refusal: nested and transformed frames,
borders, occluded and pointer-transparent frames, duplicate ids, and a
shadow root that makes Playwright's selector count differ from the native
one.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

from openadapt_flow.backends import playwright_backend as pb  # noqa: E402


def _reference_frame_point(backend: Any, x: int, y: int) -> Optional[Any]:
    """``_frame_point`` as it was before the round-trip change."""

    scope: Any = backend.page
    local_x, local_y = float(x), float(y)
    path: list[str] = []
    for _depth in range(9):
        hit = None
        candidate = None
        try:
            hit = scope.evaluate_handle(
                "([px, py]) => document.elementFromPoint(px, py)",
                [local_x, local_y],
            ).as_element()
            if hit is None:
                return None
            tag = hit.evaluate("el => el.localName")
            if tag not in {"iframe", "frame"}:
                return pb._FramePoint(scope, local_x, local_y, tuple(path))
            projection = hit.evaluate(pb._PROJECT_FRAME_POINT_JS, [local_x, local_y])
            if not isinstance(projection, dict):
                return None
            if projection.get("inside") is not True:
                return pb._FramePoint(scope, local_x, local_y, tuple(path))
            if len(path) >= 8:
                return None
            selector = hit.evaluate(pb._FRAME_SELECTOR_JS)
            if not isinstance(selector, str) or not selector or len(selector) > 512:
                return None
            matches = scope.locator(selector)
            if matches.count() != 1:
                return None
            candidate = matches.element_handle()
            if candidate is None or not hit.evaluate(
                "(el, candidate) => el === candidate", candidate
            ):
                return None
            child = hit.content_frame()
            if child is None:
                return None
            width, height = child.evaluate(
                "() => [window.innerWidth, window.innerHeight]"
            )
            next_x, next_y = float(projection["x"]), float(projection["y"])
            if not (0 <= next_x < float(width) and 0 <= next_y < float(height)):
                return None
            path.append(selector)
            scope = child
            local_x, local_y = next_x, next_y
        except Exception:
            return None
        finally:
            if candidate is not None:
                candidate.dispose()
            if hit is not None:
                hit.dispose()
    return None


def _same(a: Optional[Any], b: Optional[Any]) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return (
        a.scope == b.scope
        and a.frame_path == b.frame_path
        and abs(a.x - b.x) < 1e-6
        and abs(a.y - b.y) < 1e-6
    )


def _grid(width: int, height: int, step: int = 23) -> list[tuple[int, int]]:
    return [(x, y) for x in range(1, width, step) for y in range(1, height, step)]


PAGES = {
    "nested-bordered-transformed": (
        """<iframe id="shell" style="position:absolute;left:40px;top:30px;
        width:420px;height:300px;border:8px solid black;
        transform:translate(15px, 10px) scale(1.2, 1.1);
        transform-origin:top left"></iframe>""",
        [
            """<iframe id="app" style="position:absolute;left:25px;top:20px;
            width:300px;height:200px;border:6px solid black;
            transform:translate(9px, 7px) scale(.8, 1.15);
            transform-origin:top left"></iframe>""",
            '<button id="target" style="position:absolute;left:30px;top:30px;'
            'width:120px;height:50px">Submit</button>',
        ],
    ),
    "occluded-transparent-rotated": (
        """<iframe id="transparent" style="position:absolute;left:20px;top:20px;
        width:200px;height:120px;pointer-events:none"></iframe>
        <iframe id="occluded" style="position:absolute;left:260px;top:20px;
        width:200px;height:120px"></iframe>
        <div style="position:absolute;left:260px;top:20px;width:200px;
        height:120px;z-index:3;background:#ccc"></div>
        <iframe id="rotated" style="position:absolute;left:20px;top:200px;
        width:200px;height:120px;transform:rotate(3deg)"></iframe>""",
        None,
    ),
    "duplicate-ids": (
        """<div><iframe id="dup" style="width:220px;height:140px"></iframe></div>
        <div><iframe id="dup" style="width:220px;height:140px"></iframe></div>""",
        None,
    ),
}


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as playwright:
        chromium = playwright.chromium.launch(headless=True)
        yield chromium
        chromium.close()


def _page(browser, top: str, nested: Optional[list[str]]):
    page = browser.new_page(viewport={"width": 640, "height": 460})
    page.set_content(top)
    for child in page.frames[1:]:
        child.set_content(
            "<body style='margin:0'><input id='field' value='x' "
            "style='width:180px;height:30px'></body>"
        )
    if nested:
        outer = page.frames[1]
        outer.set_content(nested[0])
        page.frames[-1].set_content(nested[1])
    return page


@pytest.mark.parametrize("name", sorted(PAGES))
def test_frame_point_matches_the_previous_descent_everywhere(browser, name):
    top, nested = PAGES[name]
    page = _page(browser, top, nested)
    try:
        backend = pb.PlaywrightBackend(page)
        compared = 0
        descended = 0
        for x, y in _grid(640, 460):
            new = backend._frame_point(x, y)
            old = _reference_frame_point(backend, x, y)
            assert _same(new, old), (name, x, y, new, old)
            compared += 1
            if new is not None and new.frame_path:
                descended += 1
        assert compared > 100
        if name == "nested-bordered-transformed":
            assert descended > 0
    finally:
        page.close()


def test_shadow_root_falls_back_to_playwrights_selector_count(browser):
    # The light-DOM frame is the only "#app" a native query sees, but
    # Playwright's engine pierces the open shadow root and finds two, so the
    # frame path is ambiguous and both implementations must refuse it.
    page = browser.new_page(viewport={"width": 640, "height": 460})
    page.set_content(
        """<iframe id="app" style="position:absolute;left:10px;top:10px;
        width:300px;height:200px"></iframe>
        <div id="host" style="position:absolute;left:10px;top:260px"></div>
        <script>
        const root = document.getElementById('host').attachShadow({mode: 'open'});
        root.innerHTML = '<iframe id="app" style="width:200px;height:100px"></iframe>';
        </script>"""
    )
    try:
        page.frames[1].set_content("<input id='field' value='x'>")
        backend = pb.PlaywrightBackend(page)
        assert page.locator("#app").count() == 2
        assert page.evaluate("document.querySelectorAll('#app').length") == 1
        assert _reference_frame_point(backend, 100, 100) is None
        assert backend._frame_point(100, 100) is None

        page.frames[1].locator("#field").focus()
        assert backend.focused_text_value() is None
    finally:
        page.close()


def test_focused_value_reads_through_nested_frames_and_refuses_ambiguity(browser):
    top, nested = PAGES["nested-bordered-transformed"]
    page = _page(browser, top, None)
    try:
        page.frames[1].set_content(
            """<iframe id="app" style="position:absolute;left:25px;top:20px;
            width:300px;height:200px"></iframe>"""
        )
        inner = page.frames[-1]
        inner.set_content("<input id='field' value='typed value'>")
        backend = pb.PlaywrightBackend(page)
        inner.locator("#field").focus()
        assert backend.focused_text_value() == "typed value"

        # A second frame with the same id in the same document: the
        # selector the descent derives must still name exactly one frame.
        page.frames[1].evaluate(
            "() => document.body.insertAdjacentHTML('beforeend', "
            '\'<iframe id="app" style="width:50px;height:50px"></iframe>\')'
        )
        inner.locator("#field").focus()
        path: list[str] = []
        backend._focused_element_call("el => el.value", frame_path_out=path)
        assert path and path[-1] != "#app"
    finally:
        page.close()
