"""Generic cross-frame structural resolution and one-shot delivery contracts."""

from __future__ import annotations

import pytest

from openadapt_flow.backend import StructuralResolutionRefused
from openadapt_flow.ir import StructuralLocator


@pytest.fixture
def framed_backend():
    sync = pytest.importorskip("playwright.sync_api")
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    with sync.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": 800, "height": 500},
            device_scale_factor=1,
        )
        page.set_content(
            '<iframe id="shell" style="width:650px;height:360px"></iframe>'
        )
        outer = page.frames[-1]
        outer.set_content('<iframe id="app" style="width:500px;height:260px"></iframe>')
        frame = page.frames[-1]
        frame.set_content(
            """<!doctype html><html><body>
            <table><tr data-openadapt-identity="record-1">
              <td>Record 1</td><td><button id="target"
                onclick="window.clicks += 1">Submit</button></td>
            </tr></table><script>window.clicks = 0</script>
            </body></html>"""
        )
        yield PlaywrightBackend(page), page, frame
        browser.close()


def _target_locator(backend, frame):
    box = frame.locator("#target").bounding_box()
    assert box is not None
    x = int(round(box["x"] + box["width"] / 2))
    y = int(round(box["y"] + box["height"] / 2))
    locator = backend.structural_locator_at(x, y)
    assert locator is not None
    return locator, box


def _has_guard_attribute(frame) -> bool:
    return bool(
        frame.evaluate(
            """() => Array.from(document.querySelectorAll('*')).some(
                el => Array.from(el.attributes).some(
                    attr => attr.name.startsWith('data-openadapt-actuation-'))
            )"""
        )
    )


def test_nested_frame_locator_delivers_once_in_top_level_coordinates(
    framed_backend,
) -> None:
    backend, _page, frame = framed_backend
    locator, box = _target_locator(backend, frame)

    assert locator.frame_path == ["#shell", "#app"]
    handle = backend.locate_structural(locator)
    assert handle is not None
    assert handle.region == tuple(
        int(round(box[key])) for key in ("x", "y", "width", "height")
    )

    receipt = backend.act_structural(locator, handle)

    assert receipt.target_fingerprint == handle.target_fingerprint
    assert frame.evaluate("window.clicks") == 1
    with pytest.raises(StructuralResolutionRefused, match="stale|consumed"):
        backend.act_structural(locator, handle)


def test_frame_path_and_target_ambiguity_refuse_before_arming() -> None:
    sync = pytest.importorskip("playwright.sync_api")
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    with sync.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 800, "height": 500})
        page.set_content("<iframe></iframe><iframe></iframe>")
        backend = PlaywrightBackend(page)
        locator = StructuralLocator(selector="#target", frame_path=["iframe"])

        with pytest.raises(
            StructuralResolutionRefused, match="frame path is ambiguous"
        ):
            backend.locate_structural(locator)
        assert all(not _has_guard_attribute(frame) for frame in page.frames[1:])
        browser.close()


def test_navigation_invalidates_child_guard_and_token_reuse(framed_backend) -> None:
    backend, _page, frame = framed_backend
    locator, _box = _target_locator(backend, frame)
    handle = backend.locate_structural(locator)
    assert handle is not None

    frame.set_content(
        '<button id="target" onclick="window.clicks += 1">Submit</button>'
        "<script>window.clicks = 0</script>"
    )
    with pytest.raises(StructuralResolutionRefused):
        backend.act_structural(locator, handle)
    assert frame.evaluate("window.clicks") == 0
    with pytest.raises(StructuralResolutionRefused, match="stale|consumed"):
        backend.act_structural(locator, handle)


def test_same_selector_frame_replacement_refuses_delivery(framed_backend) -> None:
    backend, _page, frame = framed_backend
    outer = frame.parent_frame
    assert outer is not None
    locator, _box = _target_locator(backend, frame)
    handle = backend.locate_structural(locator)
    assert handle is not None

    outer.locator("#app").evaluate(
        "old => { const next = document.createElement('iframe'); "
        "next.id = old.id; old.replaceWith(next); }"
    )
    replacement = next(child for child in outer.child_frames if child != frame)
    replacement.set_content(
        '<button id="target" onclick="window.clicks += 1">Submit</button>'
        "<script>window.clicks = 0</script>"
    )

    with pytest.raises(StructuralResolutionRefused, match="frame context changed"):
        backend.act_structural(locator, handle)
    assert replacement.evaluate("window.clicks") == 0


def test_top_level_context_change_refuses_child_frame_delivery(framed_backend) -> None:
    backend, page, frame = framed_backend
    page.locator("head").evaluate(
        "el => el.insertAdjacentHTML('beforeend', "
        '\'<meta name="openadapt-session-identity" content="\' + '
        "'a'.repeat(64) + '\">')"
    )
    locator, _box = _target_locator(backend, frame)
    handle = backend.locate_structural(locator)
    assert handle is not None
    assert backend.session_identity() == "a" * 64

    page.locator('meta[name="openadapt-session-identity"]').evaluate(
        "el => el.content = 'b'.repeat(64)"
    )
    with pytest.raises(StructuralResolutionRefused, match="execution context"):
        backend.act_structural(locator, handle)
    assert frame.evaluate("window.clicks") == 0
    assert not _has_guard_attribute(frame)


def test_hit_testing_ignores_occluded_and_pointer_transparent_frames() -> None:
    sync = pytest.importorskip("playwright.sync_api")
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    with sync.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 800, "height": 500})
        page.set_content(
            """<button id="under" style="position:absolute;left:20px;top:20px;
            width:220px;height:120px">Under</button>
            <iframe id="transparent" style="position:absolute;left:20px;top:20px;
            width:220px;height:120px;pointer-events:none;z-index:2"></iframe>
            <iframe id="occluded" style="position:absolute;left:300px;top:20px;
            width:220px;height:120px;z-index:2"></iframe>
            <button id="cover" style="position:absolute;left:300px;top:20px;
            width:220px;height:120px;z-index:3">Cover</button>"""
        )
        transparent, occluded = page.frames[1:]
        for child in (transparent, occluded):
            child.set_content('<button id="hidden">Hidden target</button>')
        backend = PlaywrightBackend(page)

        transparent_locator = backend.structural_locator_at(130, 80)
        occluded_locator = backend.structural_locator_at(410, 80)

        assert transparent_locator is not None
        assert transparent_locator.selector == "#under"
        assert transparent_locator.frame_path is None
        assert occluded_locator is not None
        assert occluded_locator.selector == "#cover"
        assert occluded_locator.frame_path is None
        browser.close()


def test_scaled_bordered_nested_frames_map_the_hit_target() -> None:
    sync = pytest.importorskip("playwright.sync_api")
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    with sync.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1000, "height": 700})
        page.set_content(
            """<iframe id="shell" style="position:absolute;left:40px;top:30px;
            width:600px;height:400px;border:8px solid black;
            transform:translate(15px, 10px) scale(1.2, 1.1);
            transform-origin:top left"></iframe>"""
        )
        outer = page.frames[-1]
        outer.set_content(
            """<iframe id="app" style="position:absolute;left:25px;top:20px;
            width:420px;height:260px;border:6px solid black;
            transform:translate(9px, 7px) scale(.8, 1.15);
            transform-origin:top left"></iframe>"""
        )
        frame = page.frames[-1]
        frame.set_content(
            '<button id="target" style="position:absolute;left:80px;top:70px;'
            'width:120px;height:50px" onclick="window.clicks += 1">Submit</button>'
            "<script>window.clicks = 0</script>"
        )
        backend = PlaywrightBackend(page)

        locator, _box = _target_locator(backend, frame)
        assert locator.frame_path == ["#shell", "#app"]
        handle = backend.locate_structural(locator)
        assert handle is not None
        backend.act_structural(locator, handle)
        assert frame.evaluate("window.clicks") == 1
        browser.close()


def _mutate_ancestor_frame(page, mutation: str) -> None:
    if mutation == "covered":
        page.locator("body").evaluate(
            "body => body.insertAdjacentHTML('beforeend', "
            "'<div id=cover style=\"position:fixed;inset:0;z-index:99\"></div>')"
        )
    elif mutation == "pointer-disabled":
        page.locator("#shell").evaluate("el => el.style.pointerEvents = 'none'")
    elif mutation == "rotated":
        page.locator("#shell").evaluate("el => el.style.transform = 'rotate(2deg)'")
    else:  # pragma: no cover - closed test vocabulary
        raise AssertionError(mutation)


@pytest.mark.parametrize("mutation", ["covered", "pointer-disabled", "rotated"])
def test_ancestor_frame_change_before_locate_refuses_handle(
    framed_backend, mutation: str
) -> None:
    backend, page, frame = framed_backend
    locator, _box = _target_locator(backend, frame)

    _mutate_ancestor_frame(page, mutation)

    assert backend.locate_structural(locator) is None
    assert not _has_guard_attribute(frame)


@pytest.mark.parametrize("mutation", ["covered", "pointer-disabled", "rotated"])
def test_ancestor_frame_change_before_act_refuses_delivery(
    framed_backend, mutation: str
) -> None:
    backend, page, frame = framed_backend
    locator, _box = _target_locator(backend, frame)
    handle = backend.locate_structural(locator)
    assert handle is not None

    _mutate_ancestor_frame(page, mutation)

    with pytest.raises(StructuralResolutionRefused, match="frame chain changed"):
        backend.act_structural(locator, handle)
    assert frame.evaluate("window.clicks") == 0
    assert not _has_guard_attribute(frame)


@pytest.mark.parametrize(
    "transform",
    ["rotate(2deg)", "skewX(3deg)", "scaleX(-1)", "translateZ(1px)"],
)
def test_unsupported_frame_transforms_refuse_structural_observation(
    transform: str,
) -> None:
    sync = pytest.importorskip("playwright.sync_api")
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    with sync.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 800, "height": 500})
        page.set_content(
            f'<iframe id="app" style="position:absolute;left:180px;top:100px;'
            f"width:300px;height:220px;transform:{transform};"
            'transform-origin:center center"></iframe>'
        )
        frame = page.frames[-1]
        frame.set_content(
            '<button id="target" style="position:absolute;left:100px;top:80px;'
            'width:80px;height:40px">Submit</button>'
        )
        box = frame.locator("#target").bounding_box()
        assert box is not None
        x = int(round(box["x"] + box["width"] / 2))
        y = int(round(box["y"] + box["height"] / 2))

        assert PlaywrightBackend(page).structural_locator_at(x, y) is None
        browser.close()


def test_cancel_and_actuation_exception_always_clean_child_guard(
    framed_backend,
) -> None:
    backend, _page, frame = framed_backend
    locator, _box = _target_locator(backend, frame)
    cancelled = backend.locate_structural(locator)
    assert cancelled is not None and _has_guard_attribute(frame)
    backend.cancel_pending_structural_guards()
    assert not _has_guard_attribute(frame)

    handle = backend.locate_structural(locator)
    assert handle is not None
    frame.locator("body").evaluate(
        """body => body.insertAdjacentHTML('beforeend',
        '<div style="position:fixed;inset:0;z-index:99"></div>')"""
    )
    with pytest.raises(StructuralResolutionRefused, match="unactionable"):
        backend.act_structural(locator, handle)
    assert frame.evaluate("window.clicks") == 0
    assert not _has_guard_attribute(frame)
