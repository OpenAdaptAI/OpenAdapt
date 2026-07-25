from __future__ import annotations

import pytest

from openadapt_flow.backend import StructuralResolutionRefused
from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    Step,
    StructuralLocator,
    Workflow,
)
from openadapt_flow.runtime.replayer import Replayer
from tests.test_replayer import FakeVision, Match, make_png


@pytest.mark.parametrize("mutation", ["target", "row"])
def test_playwright_refuses_mutation_after_fresh_identity(
    mutation,
) -> None:
    sync = pytest.importorskip("playwright.sync_api")
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    with sync.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": 800, "height": 400},
            device_scale_factor=1,
        )
        page.set_content(
            """<!doctype html><html><body>
            <table><tbody>
              <tr data-record="correct">
                <td>MRN-1</td><td>Jane Sample</td>
                <td><button id="target"
                  onclick="window.clicked.push('correct')">Submit</button></td>
              </tr>
              <tr data-record="wrong">
                <td>MRN-2</td><td>Taylor Duplicate</td>
                <td><button id="other-target"
                  onclick="window.clicked.push('wrong')">Submit</button></td>
              </tr>
            </tbody></table>
            <script>window.clicked = [];</script>
            </body></html>"""
        )
        backend = PlaywrightBackend(page)
        locator = StructuralLocator(
            selector="#target",
            role="button",
            name="Submit",
        )
        handle = backend.locate_structural(locator)
        assert handle is not None
        if mutation == "target":
            page.evaluate(
                """() => {
                    const replacement = document.querySelector(
                        '[data-record="correct"] button'
                    ).cloneNode(true);
                    replacement.id = 'target';
                    document.querySelector(
                        '[data-record="correct"] button'
                    ).replaceWith(replacement);
                }"""
            )
        else:
            page.evaluate(
                """() => {
                    document.querySelector(
                        '[data-record="correct"] td'
                    ).textContent = 'MRN-9';
                }"""
            )
        with pytest.raises(StructuralResolutionRefused):
            backend.act_structural(locator, handle)
        clicked = page.evaluate("window.clicked")
        browser.close()

    assert clicked == []


def test_playwright_visual_fallback_uses_identity_bound_dom_click(tmp_path) -> None:
    sync = pytest.importorskip("playwright.sync_api")
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    with sync.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": 800, "height": 400},
            device_scale_factor=1,
        )
        page.set_content(
            """<!doctype html><html><body>
            <table><tbody><tr>
              <td>MRN-1</td><td>Jane Sample</td>
              <td><button id="target"
                onclick="window.clicked += 1">Submit</button></td>
            </tr></tbody></table>
            <script>window.clicked = 0;</script>
            </body></html>"""
        )
        backend = PlaywrightBackend(page)
        box = page.locator("#target").bounding_box()
        assert box is not None
        point = (
            int(round(box["x"] + box["width"] / 2)),
            int(round(box["y"] + box["height"] / 2)),
        )
        region = (
            int(round(box["x"])),
            int(round(box["y"])),
            int(round(box["width"])),
            int(round(box["height"])),
        )
        vision = FakeVision()
        vision.template_results = [
            Match(point=point, region=region, confidence=0.99),
            Match(point=point, region=region, confidence=0.99),
        ]
        bundle = tmp_path / "bundle"
        (bundle / "templates").mkdir(parents=True)
        (bundle / "templates" / "submit.png").write_bytes(make_png((20, 10)))
        step = Step(
            id="submit",
            intent="submit patient update",
            action=ActionKind.CLICK,
            risk="irreversible",
            anchor=Anchor(
                template="templates/submit.png",
                region=region,
                click_point=point,
                ocr_text="Submit",
                structured_identity="MRN-1Jane Sample",
            ),
        )

        report = Replayer(
            backend,
            vision=vision,
            use_structural=False,
        ).run(
            Workflow(name="visual-browser-actuation", steps=[step]),
            bundle_dir=bundle,
            run_dir=tmp_path / "run",
        )
        clicked = page.evaluate("window.clicked")
        browser.close()

    assert report.success is True
    assert report.results[0].actuation == "guarded_coordinate"
    assert report.results[0].delivery_receipt is not None
    assert clicked == 1
