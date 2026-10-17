"""Post-dispatch uncertainty must not hide or blindly repeat a GUI write."""

from __future__ import annotations

import pytest

from openadapt_flow.backend import ActionDeliveryUncertain, StructuralResolutionRefused


class _RaiseAfterRealClick:
    """Delegate the real browser action, then model a detached-frame API error."""

    def __init__(self, locator, playwright_error) -> None:
        self._locator = locator
        self._playwright_error = playwright_error

    def count(self):
        return self._locator.count()

    def click(self, **kwargs):
        self._locator.click(**kwargs)
        if not kwargs.get("trial"):
            raise self._playwright_error("Frame was detached after input dispatch")

    def dblclick(self, **kwargs):
        self._locator.dblclick(**kwargs)
        if not kwargs.get("trial"):
            raise self._playwright_error("Frame was detached after input dispatch")


class _FailActionabilityTrial:
    def __init__(self, locator, playwright_error) -> None:
        self._locator = locator
        self._playwright_error = playwright_error
        self.real_attempts = 0

    def count(self):
        return self._locator.count()

    def click(self, **kwargs):
        if kwargs.get("trial"):
            raise self._playwright_error("element is not actionable")
        self.real_attempts += 1
        self._locator.click(**kwargs)

    def dblclick(self, **kwargs):
        if kwargs.get("trial"):
            raise self._playwright_error("element is not actionable")
        self.real_attempts += 1
        self._locator.dblclick(**kwargs)


def test_real_click_then_frame_detach_is_typed_delivery_uncertainty() -> None:
    sync = pytest.importorskip("playwright.sync_api")
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    with sync.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 800, "height": 500})
        page.set_content(
            "<script>window.__sor = []</script>"
            '<iframe id="app" style="width:500px;height:260px"></iframe>'
        )
        frame = page.frames[-1]
        frame.set_content(
            """<button id="target" onmousedown="
            parent.__sor.push({id: 'record-1'});
            window.frameElement.remove();
            ">Submit</button>"""
        )
        backend = PlaywrightBackend(page)
        box = frame.locator("#target").bounding_box()
        assert box is not None
        locator = backend.structural_locator_at(
            int(round(box["x"] + box["width"] / 2)),
            int(round(box["y"] + box["height"] / 2)),
        )
        assert locator is not None
        handle = backend.locate_structural(locator)
        assert handle is not None

        real_token_locator = backend._token_locator

        def _raising_token_locator(token, scope):
            return _RaiseAfterRealClick(
                real_token_locator(token, scope),
                sync.Error,
            )

        backend._token_locator = _raising_token_locator
        with pytest.raises(ActionDeliveryUncertain) as raised:
            backend.act_structural(locator, handle)
        assert page.evaluate("window.__sor") == [{"id": "record-1"}]
        assert raised.value.operation == "dom_click"
        assert raised.value.target_fingerprint == handle.target_fingerprint
        browser.close()


def test_replacement_before_real_click_remains_structural_refusal() -> None:
    sync = pytest.importorskip("playwright.sync_api")
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    with sync.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 800, "height": 500})
        page.set_content(
            "<script>window.__sor = []</script>"
            '<button id="target" onclick="__sor.push({id: \'record-1\'})">'
            "Submit</button>"
        )
        backend = PlaywrightBackend(page)
        box = page.locator("#target").bounding_box()
        assert box is not None
        locator = backend.structural_locator_at(
            int(round(box["x"] + box["width"] / 2)),
            int(round(box["y"] + box["height"] / 2)),
        )
        assert locator is not None
        handle = backend.locate_structural(locator)
        assert handle is not None
        page.locator("#target").evaluate("el => el.replaceWith(el.cloneNode(true))")

        with pytest.raises(StructuralResolutionRefused):
            backend.act_structural(locator, handle)
        assert page.evaluate("window.__sor") == []
        browser.close()


def test_actionability_trial_failure_remains_pre_dispatch_refusal() -> None:
    sync = pytest.importorskip("playwright.sync_api")
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    with sync.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 800, "height": 500})
        page.set_content(
            "<script>window.__sor = []</script>"
            '<button id="target" onclick="__sor.push({id: \'record-1\'})">'
            "Submit</button>"
        )
        backend = PlaywrightBackend(page)
        box = page.locator("#target").bounding_box()
        assert box is not None
        locator = backend.structural_locator_at(
            int(round(box["x"] + box["width"] / 2)),
            int(round(box["y"] + box["height"] / 2)),
        )
        assert locator is not None
        handle = backend.locate_structural(locator)
        assert handle is not None
        real_token_locator = backend._token_locator
        failing = None

        def _failing_token_locator(token, scope):
            nonlocal failing
            failing = _FailActionabilityTrial(
                real_token_locator(token, scope),
                sync.TimeoutError,
            )
            return failing

        backend._token_locator = _failing_token_locator
        with pytest.raises(StructuralResolutionRefused):
            backend.act_structural(locator, handle)
        assert failing is not None
        assert failing.real_attempts == 0
        assert page.evaluate("window.__sor") == []
        browser.close()
