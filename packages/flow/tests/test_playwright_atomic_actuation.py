from __future__ import annotations

import hashlib
from io import BytesIO

import pytest
from PIL import Image

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


def _visual_case(tmp_path, page):
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
    return vision, bundle, step


def _focused_keyboard_case(tmp_path, page, action):
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
        Match(point=point, region=region, confidence=0.99)
        for _ in range(3 if action is ActionKind.TYPE else 2)
    ]
    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True)
    (bundle / "templates" / "field.png").write_bytes(make_png((20, 10)))
    step = Step(
        id="keyboard-write",
        intent="submit or edit the patient record",
        action=action,
        key="Enter" if action is ActionKind.KEY else None,
        text="North" if action is ActionKind.TYPE else None,
        risk="irreversible",
        anchor=Anchor(
            template="templates/field.png",
            structural=StructuralLocator(
                selector="#target",
                role="textbox",
                name="Patient value",
            ),
            region=region,
            click_point=point,
            ocr_text="Patient value",
            structured_identity="MRN-1Jane Sample",
        ),
    )
    return vision, bundle, step


_FOCUSED_ROWS_HTML = """<!doctype html><html><head>
<style>input:focus { outline: none; }</style></head><body>
<table><tbody>
  <tr><td>MRN-1</td><td>Jane Sample</td><td>
    <input id="target" aria-label="Patient value"
      onkeydown="if(event.key === 'Enter') window.actions.push('correct')">
  </td></tr>
  <tr><td>MRN-2</td><td>Taylor Duplicate</td><td>
    <input id="wrong" aria-label="Patient value"
      onkeydown="if(event.key === 'Enter') window.actions.push('wrong')">
  </td></tr>
</tbody></table>
<script>window.actions = [];</script>
</body></html>"""


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


@pytest.mark.parametrize("operation", ["pointer", "keyboard"])
@pytest.mark.parametrize(
    ("source", "marker", "observed", "replacement"),
    [
        (
            "session",
            "openadapt-session-identity",
            "a" * 64,
            "b" * 64,
        ),
        (
            "workflow_state",
            "openadapt-workflow-state",
            "eligibility.review",
            "eligibility.submit",
        ),
    ],
)
def test_playwright_refuses_invisible_context_change_before_delivery(
    operation: str,
    source: str,
    marker: str,
    observed: str,
    replacement: str,
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
            f"""<!doctype html><html><head>
            <meta name="{marker}" content="{observed}">
            </head><body>
            <table><tbody><tr><td>MRN-1</td><td>Jane Sample</td><td>
              <button id="button"
                onclick="window.actions.push('pointer')">Submit</button>
              <input id="input" aria-label="Patient value"
                onkeydown="if(event.key === 'Enter')
                  window.actions.push('keyboard')">
            </td></tr></tbody></table>
            <script>window.actions = [];</script>
            </body></html>"""
        )
        backend = PlaywrightBackend(page)
        if operation == "pointer":
            locator = StructuralLocator(
                selector="#button",
                role="button",
                name="Submit",
            )
            handle = backend.locate_structural(locator)
            assert handle is not None
            assert getattr(backend, f"{source}_identity")() == observed
            page.locator(f'meta[name="{marker}"]').evaluate(
                "(element, value) => element.setAttribute('content', value)",
                replacement,
            )
            with pytest.raises(StructuralResolutionRefused):
                backend.act_structural(locator, handle)
        else:
            target = page.locator("#input")
            target.focus()
            box = target.bounding_box()
            assert box is not None
            backend.arm_guarded_keyboard(
                int(round(box["x"] + box["width"] / 2)),
                int(round(box["y"] + box["height"] / 2)),
            )
            assert getattr(backend, f"{source}_identity")() == observed
            expected = hashlib.sha256(backend.guarded_keyboard_frame()).hexdigest()
            page.locator(f'meta[name="{marker}"]').evaluate(
                "(element, value) => element.setAttribute('content', value)",
                replacement,
            )
            with pytest.raises(StructuralResolutionRefused):
                backend.press_guarded("Enter", expected_frame_sha256=expected)
        actions = page.evaluate("window.actions")
        browser.close()

    assert actions == []


@pytest.mark.parametrize(
    "operation",
    ["structural", "coordinate", "keyboard", "structural_drag", "coordinate_drag"],
)
def test_external_environment_guard_runs_at_each_final_browser_input_edge(
    operation: str,
) -> None:
    sync = pytest.importorskip("playwright.sync_api")
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    with sync.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 800, "height": 400})
        page.set_content(
            """<!doctype html><html><body><table><tbody>
            <tr data-openadapt-identity="record-1">
            <td>MRN-1</td><td>Jane Sample</td><td>
              <button id="button" onclick="window.actions.push('click')">Go</button>
              <input id="input" aria-label="Value"
                onkeydown="window.actions.push('key')">
              <div id="source" draggable="true">Source</div>
              <div id="destination">Destination</div>
            </td></tr></tbody></table>
            <script>window.actions = [];</script></body></html>"""
        )
        backend = PlaywrightBackend(page)

        if operation == "structural":
            locator = StructuralLocator(selector="#button", role="button", name="Go")
            handle = backend.locate_structural(locator)
            assert handle is not None

            def invoke():
                return backend.act_structural(locator, handle)
        elif operation == "coordinate":
            box = page.locator("#button").bounding_box()
            assert box is not None
            x, y = (
                int(round(box["x"] + box["width"] / 2)),
                int(round(box["y"] + box["height"] / 2)),
            )
            backend.arm_guarded_coordinate(x, y)

            def invoke():
                return backend.act_guarded_coordinate(
                    x,
                    y,
                    expected_frame_sha256="0" * 64,
                )
        elif operation == "keyboard":
            target = page.locator("#input")
            target.focus()
            box = target.bounding_box()
            assert box is not None
            x, y = (
                int(round(box["x"] + box["width"] / 2)),
                int(round(box["y"] + box["height"] / 2)),
            )
            backend.arm_guarded_keyboard(x, y)
            expected = hashlib.sha256(backend.guarded_keyboard_frame()).hexdigest()

            def invoke():
                return backend.press_guarded("Enter", expected_frame_sha256=expected)
        elif operation == "structural_drag":
            source_locator = StructuralLocator(selector="#source")
            destination_locator = StructuralLocator(selector="#destination")
            source = backend.locate_structural(source_locator)
            destination = backend.locate_structural(destination_locator)
            assert source is not None and destination is not None

            def invoke():
                return backend.drag_structural_guarded(
                    source_locator,
                    source,
                    destination_locator,
                    destination,
                )
        else:
            source_box = page.locator("#button").bounding_box()
            destination_box = page.locator("#destination").bounding_box()
            assert source_box is not None and destination_box is not None
            x, y = (
                int(round(source_box["x"] + source_box["width"] / 2)),
                int(round(source_box["y"] + source_box["height"] / 2)),
            )
            end_x, end_y = (
                int(destination_box["x"] + 2),
                int(destination_box["y"] + 2),
            )
            expected = hashlib.sha256(backend.screenshot()).hexdigest()
            backend.arm_guarded_coordinate(x, y)

            def invoke():
                return backend.drag_guarded(
                    x,
                    y,
                    end_x,
                    end_y,
                    expected_frame_sha256=expected,
                )

        guard_calls = 0

        def refuse_changed_environment() -> None:
            nonlocal guard_calls
            guard_calls += 1
            raise StructuralResolutionRefused("external environment changed")

        backend.set_qualification_input_guard(refuse_changed_environment)
        with pytest.raises(StructuralResolutionRefused):
            invoke()
        assert guard_calls == 1
        assert page.evaluate("window.actions") == []
        browser.close()


@pytest.mark.parametrize("mutation", ["clone_handler", "hidden_attribute"])
def test_visual_fallback_refuses_pixel_identical_post_identity_mutation(
    tmp_path,
    mutation,
) -> None:
    sync = pytest.importorskip("playwright.sync_api")
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    class MutatingBackend(PlaywrightBackend):
        identity_reads = 0

        def structured_text_at(self, x, y):
            observed = super().structured_text_at(x, y)
            self.identity_reads += 1
            if self.identity_reads == 2:
                if mutation == "clone_handler":
                    self.page.evaluate(
                        """() => {
                            const target = document.querySelector('#target');
                            const replacement = target.cloneNode(true);
                            replacement.setAttribute(
                                'onclick', "window.clicked.push('wrong')"
                            );
                            target.replaceWith(replacement);
                        }"""
                    )
                else:
                    self.page.evaluate(
                        """() => {
                            document.querySelector('#target').dataset.destination =
                                'wrong';
                        }"""
                    )
            return observed

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
              <td><button id="target" data-destination="correct"
                onclick="window.clicked.push(this.dataset.destination)">
                Submit</button></td>
            </tr></tbody></table>
            <script>window.clicked = [];</script>
            </body></html>"""
        )
        backend = MutatingBackend(page)
        vision, bundle, step = _visual_case(tmp_path, page)
        report = Replayer(backend, vision=vision, use_structural=False).run(
            Workflow(name="visual-browser-race", steps=[step]),
            bundle_dir=bundle,
            run_dir=tmp_path / "run",
        )
        clicked = page.evaluate("window.clicked")
        browser.close()

    assert backend.identity_reads == 2
    assert report.success is False
    assert report.results[0].safety_halt is True
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
        vision, bundle, step = _visual_case(tmp_path, page)

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


def test_playwright_guarded_right_click_emits_typed_receipt() -> None:
    """The browser emits the operation that qualification accepts."""

    sync = pytest.importorskip("playwright.sync_api")
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    with sync.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 800, "height": 400})
        page.set_content(
            """<!doctype html><html><body>
            <table><tbody><tr data-openadapt-identity="record-1">
              <td>MRN-1</td><td>Jane Sample</td><td>
                <button id="target"
                  oncontextmenu="event.preventDefault(); window.opened += 1">
                  Review
                </button>
              </td>
            </tr></tbody></table>
            <script>window.opened = 0;</script>
            </body></html>"""
        )
        backend = PlaywrightBackend(page)
        box = page.locator("#target").bounding_box()
        assert box is not None
        point = (
            int(round(box["x"] + box["width"] / 2)),
            int(round(box["y"] + box["height"] / 2)),
        )
        backend.arm_guarded_coordinate(*point)
        expected_frame_sha256 = hashlib.sha256(backend.screenshot()).hexdigest()
        receipt = backend.act_guarded_coordinate(
            *point,
            expected_frame_sha256=expected_frame_sha256,
            button="right",
        )
        opened = page.evaluate("window.opened")
        browser.close()

    assert receipt.operation == "guarded_coordinate_right_click"
    assert receipt.native is False
    assert receipt.target_fingerprint is not None
    assert opened == 1


def test_structural_revalidation_replaces_stale_guard(tmp_path) -> None:
    """A fresh pre-delivery structural guard must not invalidate itself."""

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
        vision, bundle, step = _visual_case(tmp_path, page)
        assert step.anchor is not None
        step.anchor.structural = StructuralLocator(
            selector="#target",
            role="button",
            name="Submit",
        )

        report = Replayer(backend, vision=vision, use_structural=True).run(
            Workflow(name="structural-fresh-guard", steps=[step]),
            bundle_dir=bundle,
            run_dir=tmp_path / "run",
        )
        clicked = page.evaluate("window.clicked")
        browser.close()

    assert report.success is True
    assert report.results[0].actuation == "dom"
    assert report.results[0].delivery_receipt is not None
    assert clicked == 1


def test_structural_drag_binds_both_freshly_resolved_endpoints(tmp_path) -> None:
    sync = pytest.importorskip("playwright.sync_api")
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    with sync.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": 800, "height": 400},
            device_scale_factor=1,
        )
        page.set_content(
            """<!doctype html><html><body><table><tbody><tr>
            <td>MRN-1</td><td>Jane Sample</td>
            <td><button id="source">Move</button></td>
            <td><button id="destination"
              onmouseup="window.drops += 1">Destination</button></td>
            </tr></tbody></table><script>window.drops = 0;</script>
            </body></html>"""
        )
        backend = PlaywrightBackend(page)
        source = backend.locate_structural(StructuralLocator(selector="#source"))
        destination = backend.locate_structural(
            StructuralLocator(selector="#destination")
        )
        assert source is not None and destination is not None
        identity = backend.structured_text_at(*source.point)
        assert identity
        bundle = tmp_path / "bundle"
        (bundle / "templates").mkdir(parents=True)
        (bundle / "templates" / "source.png").write_bytes(make_png((20, 10)))
        (bundle / "templates" / "destination.png").write_bytes(make_png((20, 10)))
        step = Step(
            id="move",
            intent="move patient item",
            action=ActionKind.DRAG,
            risk="irreversible",
            anchor=Anchor(
                template="templates/source.png",
                structural=StructuralLocator(selector="#source"),
                region=source.region,
                click_point=source.point,
                structured_identity=identity,
            ),
            drag_end_anchor=Anchor(
                template="templates/destination.png",
                structural=StructuralLocator(selector="#destination"),
                region=destination.region,
                click_point=destination.point,
            ),
        )

        report = Replayer(backend).run(
            Workflow(name="structural-drag", steps=[step]),
            bundle_dir=bundle,
            run_dir=tmp_path / "run",
        )
        drops = page.evaluate("window.drops")
        browser.close()

    assert report.success is True, report.results[0].error
    assert report.results[0].drag_end_resolution is not None
    assert report.results[0].drag_end_resolution.rung == "structural"
    assert drops == 1


def test_guarded_button_ignores_screenshot_caret_side_effects(tmp_path) -> None:
    """A focused field in the same record must not invalidate a stable button."""

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
            <section role="listitem">
              <div>MRN-1Jane Sample</div>
              <textarea id="field"></textarea>
              <button id="target"
                onclick="window.clicked += 1">Submit</button>
            </section>
            <script>window.clicked = 0;</script>
            </body></html>"""
        )
        page.locator("#field").fill("synthetic note")
        page.locator("#field").focus()
        backend = PlaywrightBackend(page)
        vision, bundle, step = _visual_case(tmp_path, page)

        report = Replayer(backend, vision=vision, use_structural=False).run(
            Workflow(name="guarded-button-focused-field", steps=[step]),
            bundle_dir=bundle,
            run_dir=tmp_path / "run",
        )
        clicked = page.evaluate("window.clicked")
        browser.close()

    assert report.success is True
    assert report.results[0].actuation == "guarded_coordinate"
    assert clicked == 1


def test_guarded_visual_type_refuses_replacement_before_focus_click(tmp_path) -> None:
    sync = pytest.importorskip("playwright.sync_api")
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    class ReplacingBackend(PlaywrightBackend):
        identity_reads = 0
        replacement_frames: tuple[bytes, bytes] | None = None

        def structured_text_at(self, x, y):
            observed = super().structured_text_at(x, y)
            self.identity_reads += 1
            if self.identity_reads == 2:
                before = self.page.screenshot(type="png", full_page=False)
                self.page.evaluate(
                    """() => {
                        const target = document.querySelector('#target');
                        const replacement = document.createElement('button');
                        for (const attribute of target.attributes) {
                            replacement.setAttribute(
                                attribute.name, attribute.value
                            );
                        }
                        replacement.textContent = '';
                        replacement.onclick = () => {
                            window.actions.push('wrong-focus-click');
                        };
                        target.replaceWith(replacement);
                    }"""
                )
                after = self.page.screenshot(type="png", full_page=False)
                self.replacement_frames = (before, after)
            return observed

    with sync.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": 800, "height": 400},
            device_scale_factor=1,
        )
        page.set_content(
            """<!doctype html><html><body>
            <table><tbody><tr>
                <td>MRN-1</td><td>Jane Sample</td><td>
                  <input id="target" aria-label="Patient value"
                  style="all:unset;appearance:none;box-sizing:border-box;
                    display:inline-block;vertical-align:top;width:180px;
                    height:28px;background:#eee;color:transparent">
              </td>
            </tr></tbody></table>
            <script>window.actions = [];</script>
            </body></html>"""
        )
        backend = ReplacingBackend(page)
        vision, bundle, step = _focused_keyboard_case(
            tmp_path,
            page,
            ActionKind.TYPE,
        )
        report = Replayer(backend, vision=vision, use_structural=False).run(
            Workflow(name="visual-type-focus-race", steps=[step]),
            bundle_dir=bundle,
            run_dir=tmp_path / "run",
        )
        actions = page.evaluate("window.actions")
        target_value = page.locator("#target").evaluate(
            "(element) => element.value ?? null"
        )
        browser.close()

    assert backend.identity_reads == 2
    assert backend.replacement_frames is not None
    before_image = Image.open(BytesIO(backend.replacement_frames[0]))
    after_image = Image.open(BytesIO(backend.replacement_frames[1]))
    assert before_image.size == after_image.size
    assert before_image.mode == after_image.mode
    assert before_image.tobytes() == after_image.tobytes()
    assert report.success is False
    assert report.results[0].safety_halt is True
    assert actions == []
    assert target_value == ""


def test_guarded_visual_type_focuses_and_types_once(tmp_path) -> None:
    sync = pytest.importorskip("playwright.sync_api")
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    with sync.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": 800, "height": 400},
            device_scale_factor=1,
        )
        page.set_content(_FOCUSED_ROWS_HTML)
        backend = PlaywrightBackend(page)
        vision, bundle, step = _focused_keyboard_case(
            tmp_path,
            page,
            ActionKind.TYPE,
        )
        report = Replayer(backend, vision=vision, use_structural=False).run(
            Workflow(name="guarded-visual-type", steps=[step]),
            bundle_dir=bundle,
            run_dir=tmp_path / "run",
        )
        values = page.locator("input").evaluate_all(
            "(inputs) => inputs.map((input) => input.value)"
        )
        browser.close()

    assert report.success is True
    assert report.results[0].actuation == "guarded_keyboard"
    assert report.results[0].delivery_receipt is not None
    assert values == ["North", ""]


@pytest.mark.parametrize("action", [ActionKind.KEY, ActionKind.TYPE])
def test_guarded_keyboard_refuses_post_identity_focus_theft(
    tmp_path,
    action,
) -> None:
    sync = pytest.importorskip("playwright.sync_api")
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    class FocusStealingBackend(PlaywrightBackend):
        identity_reads = 0

        def structured_text_at(self, x, y):
            observed = super().structured_text_at(x, y)
            self.identity_reads += 1
            final_read = 3 if action is ActionKind.TYPE else 2
            if self.identity_reads == final_read:
                self.page.locator("#wrong").focus()
            return observed

    with sync.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": 800, "height": 400},
            device_scale_factor=1,
        )
        page.set_content(_FOCUSED_ROWS_HTML)
        page.locator("#target").focus()
        backend = FocusStealingBackend(page)
        vision, bundle, step = _focused_keyboard_case(tmp_path, page, action)
        report = Replayer(backend, vision=vision, use_structural=True).run(
            Workflow(name="guarded-keyboard-race", steps=[step]),
            bundle_dir=bundle,
            run_dir=tmp_path / "run",
        )
        actions = page.evaluate("window.actions")
        values = page.locator("input").evaluate_all(
            "(inputs) => inputs.map((input) => input.value)"
        )
        browser.close()

    assert report.success is False
    assert report.results[0].safety_halt is True
    assert actions == []
    assert values == ["", ""]


@pytest.mark.parametrize("action", [ActionKind.KEY, ActionKind.TYPE])
def test_guarded_keyboard_stable_delivery_once(tmp_path, action) -> None:
    sync = pytest.importorskip("playwright.sync_api")
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    with sync.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": 800, "height": 400},
            device_scale_factor=1,
        )
        page.set_content(_FOCUSED_ROWS_HTML)
        page.locator("#target").focus()
        backend = PlaywrightBackend(page)
        vision, bundle, step = _focused_keyboard_case(tmp_path, page, action)
        report = Replayer(backend, vision=vision, use_structural=True).run(
            Workflow(name="guarded-keyboard-stable", steps=[step]),
            bundle_dir=bundle,
            run_dir=tmp_path / "run",
        )
        actions = page.evaluate("window.actions")
        values = page.locator("input").evaluate_all(
            "(inputs) => inputs.map((input) => input.value)"
        )
        browser.close()

    assert report.success is True
    assert report.results[0].actuation == "guarded_keyboard"
    assert report.results[0].delivery_receipt is not None
    if action is ActionKind.KEY:
        assert actions == ["correct"]
        assert values == ["", ""]
    else:
        assert actions == []
        assert values == ["North", ""]
