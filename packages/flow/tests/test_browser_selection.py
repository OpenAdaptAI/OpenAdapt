"""Browser selection: installed Chrome or Edge first, a small download last."""

from __future__ import annotations

from typing import Any

import pytest

from openadapt_flow import _browser_setup as bs


@pytest.fixture(autouse=True)
def _driver_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bs, "require_browser_support", lambda: None)
    monkeypatch.delenv(bs.BROWSER_ENV, raising=False)
    monkeypatch.delenv(bs.NO_AUTO_INSTALL_ENV, raising=False)
    bs._bundled_ready.clear()


def test_auto_prefers_installed_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bs, "installed_browser_channel", lambda: "chrome")
    monkeypatch.setattr(
        bs, "ensure_bundled_chromium", lambda **_: pytest.fail("no download")
    )
    assert bs.chromium_launch_options(headless=True) == {"channel": "chrome"}


def test_auto_without_installed_browser_gets_headless_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(bs, "installed_browser_channel", lambda: None)
    monkeypatch.setattr(bs, "_bundled_present", lambda *, only_shell: False)
    monkeypatch.setattr(bs, "_install_headless_shell", lambda: calls.append("shell"))
    monkeypatch.setattr(bs, "ensure_chromium_installed", lambda: calls.append("full"))
    assert bs.chromium_launch_options(headless=True) == {}
    assert bs.chromium_launch_options(headless=True) == {}
    assert calls == ["shell"]


def test_headed_needs_full_build(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(bs, "installed_browser_channel", lambda: None)
    monkeypatch.setattr(bs, "ensure_chromium_installed", lambda: calls.append("full"))
    assert bs.chromium_launch_options(headless=False) == {}
    assert calls == ["full"]


def test_bundled_env_skips_installed_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(bs.BROWSER_ENV, "bundled")
    monkeypatch.setattr(bs, "installed_browser_channel", lambda: "chrome")
    monkeypatch.setattr(bs, "ensure_bundled_chromium", lambda **_: None)
    assert bs.chromium_launch_options(headless=True) == {}


def test_unknown_choice_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(bs.BROWSER_ENV, "firefox")
    with pytest.raises(RuntimeError, match="not one of"):
        bs.chromium_launch_options(headless=True)


class _Chromium:
    def __init__(self, fail_channel: bool) -> None:
        self.fail_channel = fail_channel
        self.calls: list[dict[str, Any]] = []

    def launch(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        if "channel" in kwargs and self.fail_channel:
            raise RuntimeError("policy blocked")
        return "browser"


def test_broken_channel_falls_back_to_bundled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bs, "installed_browser_channel", lambda: "msedge")
    monkeypatch.setattr(bs, "ensure_bundled_chromium", lambda **_: None)
    chromium = _Chromium(fail_channel=True)
    assert bs.launch_chromium(chromium, headless=True) == "browser"
    assert chromium.calls == [
        {"headless": True, "channel": "msedge"},
        {"headless": True},
    ]


def test_explicit_channel_failure_is_not_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(bs.BROWSER_ENV, "chrome")
    with pytest.raises(RuntimeError, match="policy blocked"):
        bs.launch_chromium(_Chromium(fail_channel=True), headless=True)


def test_installed_channel_probe_uses_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    app = tmp_path / "Edge"
    app.mkdir()
    monkeypatch.setattr(
        bs,
        "_INSTALLED_BROWSER_PATHS",
        {
            "chrome": {"darwin": (), "linux": (), "win32": ()},
            "msedge": {
                "darwin": (str(app),),
                "linux": (str(app),),
                "win32": (str(app),),
            },
        },
    )
    assert bs.installed_browser_channel() == "msedge"


# These tests use real browser documents and events. Backend calls are never
# mocked; only one explicit transport-failure test interrupts evaluation.
_NATIVE_FORM = """
<label for="choice">Route</label>
<select id="choice">
  <option value="base">Choose</option>
  <option value="code-a">Alpha</option>
  <option value="code-b">Alpine</option>
</select>
<input id="other">
<script>
window.seen = [];
for (const kind of ['input', 'change', 'keydown']) {
  choice.addEventListener(kind, event => seen.push([kind, event.target.value]));
}
</script>
"""


@pytest.fixture(scope="module")
def native_browser() -> Any:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture
def native_page(native_browser: Any) -> Any:
    page = native_browser.new_page(viewport={"width": 1280, "height": 800})
    page.set_content(_NATIVE_FORM)
    page.locator("#choice").focus()
    yield page
    page.close()


def _native_contract() -> Any:
    from openadapt_flow.ir import NativeSelectContract

    return NativeSelectContract(
        value="code-b",
        label="Alpine",
        options=(
            {"value": "base", "label": "Choose", "disabled": False},
            {"value": "code-a", "label": "Alpha", "disabled": False},
            {"value": "code-b", "label": "Alpine", "disabled": False},
        ),
    )


def _native_binding(page: Any, *, frame_path: Any = None) -> Any:
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    from openadapt_flow.ir import StructuralLocator

    backend = PlaywrightBackend(page)
    locator = StructuralLocator(selector="#choice", frame_path=frame_path)
    handle = backend.locate_structural(locator)
    assert handle is not None
    return backend, locator, handle


def test_native_select_exact_value_and_label_with_shared_prefix(
    native_page: Any,
) -> None:
    backend, locator, handle = _native_binding(native_page)
    receipt = backend.select_native_option_guarded(
        _native_contract(), locator=locator, handle=handle
    )
    assert receipt.operation == "guarded_dom_select"
    assert receipt.native is False
    assert native_page.locator("#choice").input_value() == "code-b"
    assert native_page.evaluate("seen") == [["input", "code-b"], ["change", "code-b"]]
    observed = backend.native_selected_option_at(*handle.point)
    assert observed["value"] == "code-b"
    assert observed["label"] == "Alpine"
    assert backend.native_selected_option_at(*handle.point) is None


def test_native_capture_retains_focused_control_and_exact_census(
    native_page: Any,
) -> None:
    backend, _, _ = _native_binding(native_page)
    token = backend.begin_native_select()
    assert token is not None
    native_page.keyboard.type("Alpine")
    native_page.keyboard.press("Enter")
    observed = backend.finish_native_select(token)
    assert observed is not None
    assert observed["selector"] == "#choice"
    assert observed["frame_path"] == []
    assert observed["value"] == "code-b"
    assert observed["label"] == "Alpine"
    assert len(observed["options"]) == 3
    assert backend.finish_native_select(token) is None


@pytest.mark.parametrize(
    "mutation",
    [
        "choice.options[2].value = 'wrong-code'",
        "choice.options[2].label = 'Other label'",
        "choice.options[2].disabled = true",
        "choice.add(new Option('Alpine', 'another-code'))",
        "choice.add(new Option('Other', 'code-b'))",
        "choice.multiple = true",
        "choice.size = 3",
        "choice.disabled = true",
        "choice.style.opacity = '0'",
        "other.focus()",
        "choice.replaceWith(choice.cloneNode(true)); document.querySelector('#choice').focus()",
        "document.body.insertAdjacentHTML('beforeend', '<div style=\"position:fixed;inset:0;background:white\"></div>')",
    ],
)
def test_native_select_refuses_changed_live_control(
    native_page: Any, mutation: str
) -> None:
    from openadapt_flow.backend import StructuralResolutionRefused

    backend, locator, handle = _native_binding(native_page)
    native_page.evaluate(mutation)
    with pytest.raises(StructuralResolutionRefused):
        backend.select_native_option_guarded(
            _native_contract(), locator=locator, handle=handle
        )
    assert native_page.evaluate("seen") == []
    assert native_page.locator("#choice").input_value() == "base"


def test_native_select_rejects_stale_census_even_with_fresh_handle(
    native_page: Any,
) -> None:
    from openadapt_flow.backend import StructuralResolutionRefused

    native_page.evaluate("choice.options[2].value = 'changed-before-resolution'")
    backend, locator, handle = _native_binding(native_page)
    with pytest.raises(StructuralResolutionRefused, match="atomic guard"):
        backend.select_native_option_guarded(
            _native_contract(), locator=locator, handle=handle
        )
    assert native_page.evaluate("seen") == []


@pytest.mark.parametrize(
    "mutation",
    [
        "choice.options[2].disabled = true",
        "choice.add(new Option('Alpine', 'duplicate-label'))",
        "choice.add(new Option('Other label', 'code-b'))",
        "choice.remove()",
        "other.focus()",
    ],
)
def test_native_capture_refuses_changed_or_ambiguous_control(
    native_page: Any, mutation: str
) -> None:
    backend, _, _ = _native_binding(native_page)
    token = backend.begin_native_select()
    assert token
    native_page.evaluate(mutation)
    assert backend.finish_native_select(token) is None


def test_native_capture_cancel_discards_token(native_page: Any) -> None:
    backend, _, _ = _native_binding(native_page)
    token = backend.begin_native_select()
    assert token
    backend.cancel_native_select(token)
    assert backend.finish_native_select(token) is None
    assert native_page.evaluate("seen") == []


@pytest.mark.parametrize(
    "handler",
    [
        "other.focus()",
        "other.focus(); choice.focus()",
        "choice.remove()",
        "choice.replaceWith(choice.cloneNode(true))",
        "choice.options[2].value = 'changed-by-handler'",
        "choice.value = 'code-a'",
    ],
)
def test_native_select_stops_after_input_if_handler_changes_target(
    native_page: Any, handler: str
) -> None:
    from openadapt_flow.backend import (
        ActionDeliveryUncertain,
        StructuralResolutionRefused,
    )

    backend, locator, handle = _native_binding(native_page)
    native_page.evaluate(
        "handler => choice.addEventListener('input', () => {" + handler + "})", None
    )
    with pytest.raises(ActionDeliveryUncertain):
        backend.select_native_option_guarded(
            _native_contract(), locator=locator, handle=handle
        )
    assert native_page.evaluate("seen") == [["input", "code-b"]]
    assert backend.native_selected_option_at(*handle.point) is None
    with pytest.raises(StructuralResolutionRefused):
        backend.select_native_option_guarded(
            _native_contract(), locator=locator, handle=handle
        )


def test_native_select_change_handler_replacement_cannot_pass_readback(
    native_page: Any,
) -> None:
    from openadapt_flow.backend import ActionDeliveryUncertain

    backend, locator, handle = _native_binding(native_page)
    native_page.evaluate("""() => choice.addEventListener('change', () => {
        const replacement = choice.cloneNode(true);
        replacement.value = 'code-b';
        choice.replaceWith(replacement);
        replacement.focus();
    })""")
    with pytest.raises(ActionDeliveryUncertain):
        backend.select_native_option_guarded(
            _native_contract(), locator=locator, handle=handle
        )
    assert native_page.locator("#choice").input_value() == "code-b"
    assert backend.native_selected_option_at(*handle.point) is None


def test_native_readback_rejects_replacement_after_successful_delivery(
    native_page: Any,
) -> None:
    backend, locator, handle = _native_binding(native_page)
    backend.select_native_option_guarded(
        _native_contract(), locator=locator, handle=handle
    )
    native_page.evaluate("""() => {
        const replacement = choice.cloneNode(true);
        replacement.value = 'code-b';
        choice.replaceWith(replacement);
        replacement.focus();
    }""")
    fresh_handle = backend.locate_structural(locator)
    assert fresh_handle is not None
    assert backend.native_selected_option_at(*fresh_handle.point) is None


def test_native_readback_survives_fresh_resolution_tokens(native_page: Any) -> None:
    backend, locator, handle = _native_binding(native_page)
    backend.select_native_option_guarded(
        _native_contract(), locator=locator, handle=handle
    )
    fresh_handle = backend.locate_structural(locator)
    assert fresh_handle is not None
    backend.cancel_pending_structural_guards()
    assert backend.native_selected_option_at(*fresh_handle.point)["value"] == "code-b"


def test_native_delivery_exception_is_uncertain_without_retry(
    native_page: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from playwright.sync_api import Locator
    from openadapt_flow.backend import ActionDeliveryUncertain
    from openadapt_flow.backends import playwright_backend as implementation

    backend, locator, handle = _native_binding(native_page)
    original = Locator.evaluate
    attempts = []

    def interrupted(self: Any, expression: str, arg: Any = None, **kwargs: Any) -> Any:
        result = original(self, expression, arg, **kwargs)
        if expression == implementation._NATIVE_SELECT_DELIVER_JS:
            attempts.append(result)
            raise RuntimeError("synthetic response lost after dispatch")
        return result

    monkeypatch.setattr(Locator, "evaluate", interrupted)
    with pytest.raises(ActionDeliveryUncertain):
        backend.select_native_option_guarded(
            _native_contract(), locator=locator, handle=handle
        )
    assert len(attempts) == 1
    assert native_page.evaluate("seen") == [["input", "code-b"], ["change", "code-b"]]
    assert backend.native_selected_option_at(*handle.point)["value"] == "code-b"


def test_native_select_in_same_origin_frame(native_page: Any) -> None:
    native_page.set_content(
        '<iframe id="frame" style="width:600px;height:300px"></iframe>'
    )
    frame = native_page.locator("#frame").element_handle().content_frame()
    frame.set_content(_NATIVE_FORM)
    frame.locator("#choice").focus()
    backend, locator, handle = _native_binding(native_page, frame_path=["#frame"])
    backend.select_native_option_guarded(
        _native_contract(), locator=locator, handle=handle
    )
    observed = backend.native_selected_option_at(*handle.point)
    assert observed["value"] == "code-b"
    assert observed["frame_path"] == ["#frame"]


def test_native_select_refuses_replaced_frame(native_page: Any) -> None:
    from openadapt_flow.backend import StructuralResolutionRefused

    native_page.set_content(
        '<iframe id="frame" style="width:600px;height:300px"></iframe>'
    )
    frame = native_page.locator("#frame").element_handle().content_frame()
    frame.set_content(_NATIVE_FORM)
    frame.locator("#choice").focus()
    backend, locator, handle = _native_binding(native_page, frame_path=["#frame"])
    native_page.evaluate("frame.replaceWith(frame.cloneNode(true))")
    replacement = native_page.locator("#frame").element_handle().content_frame()
    replacement.set_content(_NATIVE_FORM)
    replacement.locator("#choice").focus()
    with pytest.raises(StructuralResolutionRefused):
        backend.select_native_option_guarded(
            _native_contract(), locator=locator, handle=handle
        )
    assert replacement.evaluate("seen") == []


@pytest.mark.parametrize(
    "mutation",
    [
        "other.focus()",
        "choice.options[2].value = 'boundary-change'",
        "document.body.insertAdjacentHTML('beforeend', '<div style=\"position:fixed;inset:0;background:white\"></div>')",
    ],
)
def test_native_select_rechecks_at_mutation_boundary(
    native_page: Any, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    from openadapt_flow.backend import StructuralResolutionRefused

    backend, locator, handle = _native_binding(native_page)
    monkeypatch.setattr(
        backend,
        "_assert_qualification_environment_current",
        lambda: native_page.evaluate(mutation),
    )
    with pytest.raises(StructuralResolutionRefused, match="atomic guard"):
        backend.select_native_option_guarded(
            _native_contract(), locator=locator, handle=handle
        )
    assert native_page.evaluate("seen") == []


def test_native_select_rechecks_context_at_mutation_boundary(
    native_page: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openadapt_flow.backend import StructuralResolutionRefused

    native_page.evaluate("""() => {
        const meta = document.createElement('meta');
        meta.name = 'openadapt-workflow-state';
        meta.content = 'edit';
        document.head.append(meta);
    }""")
    backend, locator, handle = _native_binding(native_page)
    assert backend.workflow_state_identity() == "edit"
    monkeypatch.setattr(
        backend,
        "_assert_qualification_environment_current",
        lambda: native_page.evaluate(
            "document.querySelector('meta').content = 'different-record'"
        ),
    )
    with pytest.raises(StructuralResolutionRefused, match="atomic guard"):
        backend.select_native_option_guarded(
            _native_contract(), locator=locator, handle=handle
        )
    assert native_page.evaluate("seen") == []


def test_native_select_receipt_binds_value_not_display_label(native_page: Any) -> None:
    import hashlib

    backend, locator, handle = _native_binding(native_page)
    receipt = backend.select_native_option_guarded(
        _native_contract(), locator=locator, handle=handle
    )
    assert receipt.selection_value_sha256 == hashlib.sha256(b"code-b").hexdigest()
    assert receipt.selection_commit_key is None


def _native_tracked_value(page: Any, backend: Any) -> Any:
    return page.evaluate(
        """args => ({
            touched: (window[args.fillKey] || []).includes(choice),
            snapshot: window[args.valueKey]?.get(choice) || null,
        })""",
        {"fillKey": backend._fill_store_key, "valueKey": backend._fill_value_slot},
    )


def test_native_selection_tracks_committed_value_for_form_identity(
    native_page: Any,
) -> None:
    backend, locator, handle = _native_binding(native_page)
    backend.select_native_option_guarded(
        _native_contract(), locator=locator, handle=handle
    )
    assert _native_tracked_value(native_page, backend) == {
        "touched": True,
        "snapshot": {"value": "code-b", "typed": True},
    }


def test_native_capture_replaces_prefix_snapshot_only_after_verified_commit(
    native_page: Any,
) -> None:
    backend, _, _ = _native_binding(native_page)
    token = backend.begin_native_select()
    assert token
    backend._snapshot_focused_field("Al")
    assert _native_tracked_value(native_page, backend)["snapshot"]["typed"] is False
    native_page.locator("#choice").select_option("code-b")
    observed = backend.finish_native_select(token)
    assert observed["label"] == "Alpine"
    assert _native_tracked_value(native_page, backend) == {
        "touched": True,
        "snapshot": {"value": "code-b", "typed": True},
    }


def test_native_label_attribute_tracks_visible_label_without_changing_type_rules(
    native_page: Any,
) -> None:
    from openadapt_flow.ir import NativeSelectContract

    native_page.evaluate("choice.options[2].text = 'Different inner text'")
    native_page.evaluate("choice.options[2].label = 'Alpine'")
    backend, locator, handle = _native_binding(native_page)
    backend.select_native_option_guarded(
        NativeSelectContract.model_validate(_native_contract().model_dump()),
        locator=locator,
        handle=handle,
    )
    assert _native_tracked_value(native_page, backend)["snapshot"]["typed"] is True
    backend._snapshot_focused_field("Alpine")
    assert _native_tracked_value(native_page, backend)["snapshot"]["typed"] is False


def test_native_interrupted_selection_does_not_record_committed_form_value(
    native_page: Any,
) -> None:
    from openadapt_flow.backend import ActionDeliveryUncertain

    backend, locator, handle = _native_binding(native_page)
    native_page.evaluate(
        "choice.addEventListener('input', () => {other.focus(); choice.focus()})"
    )
    with pytest.raises(ActionDeliveryUncertain):
        backend.select_native_option_guarded(
            _native_contract(), locator=locator, handle=handle
        )
    assert _native_tracked_value(native_page, backend) == {
        "touched": True,
        "snapshot": None,
    }


def test_native_select_acquires_stable_focus_without_opening_popup(
    native_page: Any,
) -> None:
    native_page.locator("#other").focus()
    native_page.evaluate("""() => {
        for (const kind of ['click', 'mousedown', 'keydown']) {
            choice.addEventListener(kind, () => seen.push([kind, choice.value]));
        }
    }""")
    backend, locator, handle = _native_binding(native_page)
    backend.select_native_option_guarded(
        _native_contract(), locator=locator, handle=handle
    )
    assert native_page.evaluate("document.activeElement === choice")
    assert native_page.evaluate("seen") == [["input", "code-b"], ["change", "code-b"]]
    assert backend.native_selected_option_at(*handle.point)["value"] == "code-b"


def test_native_select_refuses_transient_focus_theft_before_delivery(
    native_page: Any,
) -> None:
    from openadapt_flow.backend import StructuralResolutionRefused

    backend, locator, handle = _native_binding(native_page)
    native_page.evaluate("other.focus(); choice.focus()")
    with pytest.raises(StructuralResolutionRefused):
        backend.select_native_option_guarded(
            _native_contract(), locator=locator, handle=handle
        )
    assert native_page.evaluate("seen") == []


@pytest.mark.parametrize(
    "handler",
    [
        "other.focus()",
        "choice.replaceWith(choice.cloneNode(true))",
        "choice.options[2].value = 'focus-handler-change'",
        "choice.value = 'code-a'",
        "document.body.insertAdjacentHTML('beforeend', '<div style=\"position:fixed;inset:0;background:white\"></div>')",
    ],
)
def test_native_focus_handler_change_prevents_value_delivery(
    native_page: Any, handler: str
) -> None:
    from openadapt_flow.backend import ActionDeliveryUncertain

    native_page.locator("#other").focus()
    native_page.evaluate(
        "() => choice.addEventListener('focus', () => {" + handler + "})"
    )
    backend, locator, handle = _native_binding(native_page)
    with pytest.raises(ActionDeliveryUncertain):
        backend.select_native_option_guarded(
            _native_contract(), locator=locator, handle=handle
        )
    assert native_page.evaluate("seen") == []
    assert native_page.locator("#choice").input_value() != "code-b"
    assert backend.native_selected_option_at(*handle.point) is None


def test_native_capture_retains_control_from_before_click_through_enter(
    native_page: Any,
) -> None:
    native_page.locator("#other").focus()
    backend, _, handle = _native_binding(native_page)
    token = backend.begin_native_select_at(*handle.point)
    assert token
    assert not backend.continue_native_select(token)
    native_page.locator("#choice").focus()
    assert backend.continue_native_select(token)
    assert backend.continue_native_select(token)
    native_page.locator("#choice").select_option("code-b")
    assert not backend.continue_native_select(token)
    observed = backend.finish_native_select(token)
    assert observed["value"] == "code-b"


def test_native_capture_rejects_click_that_changes_selected_value(
    native_page: Any,
) -> None:
    native_page.locator("#other").focus()
    backend, _, handle = _native_binding(native_page)
    token = backend.begin_native_select_at(*handle.point)
    assert token
    native_page.locator("#choice").focus()
    native_page.locator("#choice").select_option("code-a")
    assert not backend.continue_native_select(token)
    backend.cancel_native_select(token)
    assert backend.finish_native_select(token) is None


def test_native_capture_permanently_invalidates_blur_after_click_focus(
    native_page: Any,
) -> None:
    native_page.locator("#other").focus()
    backend, _, handle = _native_binding(native_page)
    token = backend.begin_native_select_at(*handle.point)
    assert token
    native_page.locator("#choice").focus()
    assert backend.continue_native_select(token)
    native_page.evaluate("other.focus(); choice.focus()")
    assert not backend.continue_native_select(token)
    assert backend.finish_native_select(token) is None


def test_native_frame_focus_theft_and_restore_during_input_is_uncertain(
    native_page: Any,
) -> None:
    from openadapt_flow.backend import ActionDeliveryUncertain

    native_page.set_content(
        '<input id="outside"><iframe id="frame" style="width:600px;height:300px"></iframe>'
    )
    frame = native_page.locator("#frame").element_handle().content_frame()
    frame.set_content(_NATIVE_FORM)
    frame.locator("#choice").focus()
    frame.evaluate("""() => choice.addEventListener('input', () => {
        parent.document.getElementById('outside').focus();
        window.frameElement.focus();
        choice.focus();
    })""")
    backend, locator, handle = _native_binding(native_page, frame_path=["#frame"])
    with pytest.raises(ActionDeliveryUncertain):
        backend.select_native_option_guarded(
            _native_contract(), locator=locator, handle=handle
        )
    assert native_page.evaluate(
        "document.activeElement === document.getElementById('frame')"
    )
    assert frame.evaluate("document.activeElement === choice")
    assert frame.evaluate("seen") == [["input", "code-b"]]
    assert backend.native_selected_option_at(*handle.point) is None
