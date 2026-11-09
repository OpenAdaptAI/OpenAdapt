"""Nested-frame identity, readback, and one-shot browser input contracts."""

from __future__ import annotations

import pytest

from openadapt_flow.backend import ActionDeliveryUncertain, StructuralResolutionRefused

_INNER_HTML = """<!doctype html><html><body>
<table><tr data-openadapt-identity="record-1">
  <td>Record 1</td><td>
    <input id="field" value="Alpha" aria-label="Record value"
      onkeydown="if(event.key === 'Enter') top.actions.push('enter')">
    <button id="button" onclick="top.actions.push('click')">Submit</button>
  </td>
</tr></table>
</body></html>"""


@pytest.fixture
def nested_context():
    sync = pytest.importorskip("playwright.sync_api")
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    with sync.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": 1000, "height": 700},
            device_scale_factor=1,
        )
        page.set_content(
            f"""<meta name="openadapt-session-identity" content="{"a" * 64}">
            <script>window.actions = []</script>
            <iframe id="shell" style="position:absolute;left:40px;top:30px;
              width:650px;height:360px;border:6px solid black;
              transform:translate(10px,8px) scale(1.1);
              transform-origin:top left"></iframe>"""
        )
        outer = page.frames[-1]
        outer.set_content(
            """<iframe id="app" style="position:absolute;left:25px;top:20px;
              width:500px;height:260px;border:4px solid black;
              transform:translate(7px,5px) scale(.9,1.05);
              transform-origin:top left"></iframe>"""
        )
        frame = page.frames[-1]
        frame.set_content(_INNER_HTML)
        yield PlaywrightBackend(page), page, outer, frame, sync
        browser.close()


def _center(locator) -> tuple[int, int]:
    box = locator.bounding_box()
    assert box is not None
    return (
        int(round(box["x"] + box["width"] / 2)),
        int(round(box["y"] + box["height"] / 2)),
    )


def _has_guard_attribute(page) -> bool:
    for frame in page.frames:
        try:
            if frame.evaluate(
                """() => Array.from(document.querySelectorAll('*')).some(
                    el => Array.from(el.attributes).some(
                        attr => attr.name.startsWith(
                            'data-openadapt-actuation-')))
                """
            ):
                return True
        except Exception:
            continue
    return False


def test_nested_frame_identity_and_value_readback_use_top_level_points(
    nested_context,
) -> None:
    backend, _page, _outer, frame, _sync = nested_context
    field = frame.locator("#field")

    assert backend.structured_text_at(*_center(frame.locator("#button"))) == "record-1"
    assert backend.text_value_at(*_center(field)) == "Alpha"

    field.focus()
    assert backend.focused_text_value() == "Alpha"


def test_nested_frame_guarded_coordinate_and_keyboard_deliver_once(
    nested_context,
) -> None:
    backend, page, _outer, frame, _sync = nested_context
    button_point = _center(frame.locator("#button"))
    field = frame.locator("#field")
    field_point = _center(field)

    backend.arm_guarded_coordinate(*button_point)
    assert backend.session_identity() == "a" * 64
    click_receipt = backend.act_guarded_coordinate(
        *button_point,
        expected_frame_sha256="0" * 64,
    )

    field.focus()
    backend.arm_guarded_keyboard(*field_point)
    type_receipt = backend.type_text_guarded(
        "Z",
        expected_frame_sha256="0" * 64,
    )
    backend.arm_guarded_keyboard(*field_point)
    key_receipt = backend.press_guarded(
        "Enter",
        expected_frame_sha256="0" * 64,
    )

    assert click_receipt.operation == "guarded_coordinate_click"
    assert type_receipt.operation == "guarded_dom_type"
    assert key_receipt.operation == "guarded_dom_key"
    assert page.evaluate("window.actions") == ["click", "enter"]
    assert field.input_value() == "ZAlpha"
    assert not _has_guard_attribute(page)


def _mutate_guarded_context(page, outer, frame, operation: str, mutation: str) -> None:
    target = "#button" if operation == "coordinate" else "#field"
    if mutation == "target-replacement":
        frame.locator(target).evaluate("el => el.replaceWith(el.cloneNode(true))")
    elif mutation == "frame-replacement":
        outer.locator("#app").evaluate(
            "old => { const next = document.createElement('iframe'); "
            "next.id = old.id; old.replaceWith(next); }"
        )
        outer.child_frames[-1].set_content(_INNER_HTML)
    elif mutation == "occlusion":
        page.locator("body").evaluate(
            "body => body.insertAdjacentHTML('beforeend', "
            "'<div style=\"position:fixed;inset:0;z-index:999\"></div>')"
        )
    elif mutation == "context-change":
        page.locator('meta[name="openadapt-session-identity"]').evaluate(
            "el => el.content = 'b'.repeat(64)"
        )
    else:  # pragma: no cover - closed test vocabulary
        raise AssertionError(mutation)


@pytest.mark.parametrize("operation", ["coordinate", "keyboard"])
@pytest.mark.parametrize(
    "mutation",
    ["target-replacement", "frame-replacement", "occlusion", "context-change"],
)
def test_nested_frame_guards_refuse_stale_or_unproven_context(
    nested_context,
    operation: str,
    mutation: str,
) -> None:
    backend, page, outer, frame, _sync = nested_context
    point = _center(frame.locator("#button" if operation == "coordinate" else "#field"))
    if operation == "coordinate":
        backend.arm_guarded_coordinate(*point)
    else:
        frame.locator("#field").focus()
        backend.arm_guarded_keyboard(*point)
    assert backend.session_identity() == "a" * 64

    _mutate_guarded_context(page, outer, frame, operation, mutation)

    with pytest.raises(StructuralResolutionRefused):
        if operation == "coordinate":
            backend.act_guarded_coordinate(
                *point,
                expected_frame_sha256="0" * 64,
            )
        else:
            backend.type_text_guarded(
                "Z",
                expected_frame_sha256="0" * 64,
            )
    assert page.evaluate("window.actions") == []
    assert not _has_guard_attribute(page)


class _RaiseAfterDelivery:
    def __init__(self, locator, error_type) -> None:
        self._locator = locator
        self._error_type = error_type

    def count(self):
        return self._locator.count()

    def evaluate(self, *args, **kwargs):
        return self._locator.evaluate(*args, **kwargs)

    def click(self, **kwargs):
        self._locator.click(**kwargs)
        if not kwargs.get("trial"):
            raise self._error_type("frame detached after pointer dispatch")

    def press_sequentially(self, *args, **kwargs):
        self._locator.press_sequentially(*args, **kwargs)
        raise self._error_type("frame detached after keyboard dispatch")


@pytest.mark.parametrize("operation", ["coordinate", "keyboard"])
def test_nested_frame_post_dispatch_error_is_typed_uncertainty(
    nested_context,
    operation: str,
) -> None:
    backend, page, _outer, frame, sync = nested_context
    field = frame.locator("#field")
    point = _center(frame.locator("#button") if operation == "coordinate" else field)
    if operation == "coordinate":
        backend.arm_guarded_coordinate(*point)
    else:
        field.focus()
        backend.arm_guarded_keyboard(*point)
    real_token_locator = backend._token_locator
    backend._token_locator = lambda token, scope=None: _RaiseAfterDelivery(
        real_token_locator(token, scope), sync.Error
    )

    with pytest.raises(ActionDeliveryUncertain) as raised:
        if operation == "coordinate":
            backend.act_guarded_coordinate(
                *point,
                expected_frame_sha256="0" * 64,
            )
        else:
            backend.type_text_guarded(
                "Z",
                expected_frame_sha256="0" * 64,
            )

    assert raised.value.operation == (
        "guarded_coordinate_click" if operation == "coordinate" else "guarded_dom_type"
    )
    assert page.evaluate("window.actions") == (
        ["click"] if operation == "coordinate" else []
    )
    assert field.input_value() == ("Alpha" if operation == "coordinate" else "ZAlpha")
    assert not _has_guard_attribute(page)
