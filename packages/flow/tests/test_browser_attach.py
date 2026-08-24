"""Safe attachment of the Playwright recorder to an existing Chromium tab."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.request import urlopen

import pytest
from PIL import Image

import openadapt_flow.interactive_recorder as interactive_recorder_module
from openadapt_flow.__main__ import main
from openadapt_flow.backends.playwright_backend import (
    PlaywrightBackend,
    ScreenshotMaskStabilityError,
)
from openadapt_flow.compiler import compile_recording
from openadapt_flow.interactive_recorder import (
    BrowserAttachError,
    InteractiveRecorder,
    _secret_screenshot_selectors,
    record_interactive,
    select_attached_page,
    validate_browser_cdp_endpoint,
)


class _Page:
    def __init__(self, url: str) -> None:
        self.url = url


class _FakePrivacyCdp:
    """Empty-page CDP stand-in: the closed-shadow scan finds no secret node."""

    def send(self, method: str, params: dict | None = None) -> dict:
        if method == "DOM.performSearch":
            return {"searchId": "fake-search", "resultCount": 0}
        return {}

    def detach(self) -> None:
        return None


def _browser(*urls: str):
    return SimpleNamespace(
        contexts=[SimpleNamespace(pages=[_Page(url) for url in urls])]
    )


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://localhost:9222",
        "http://127.0.0.1:9222",
        "http://127.9.8.7:9222",
        "ws://[::1]:9222/devtools/browser/abc",
    ],
)
def test_cdp_endpoint_accepts_only_explicit_loopback(endpoint: str) -> None:
    assert validate_browser_cdp_endpoint(endpoint) == endpoint


@pytest.mark.parametrize(
    ("endpoint", "message"),
    [
        ("http://example.com:9222", "loopback"),
        ("http://localhost", "include a port"),
        ("ftp://localhost:9222", "must use http"),
        ("http://user:pass@localhost:9222", "must not contain credentials"),
        ("http://localhost:9222?token=secret", "must not contain credentials"),
    ],
)
def test_cdp_endpoint_refuses_unsafe_boundaries(endpoint: str, message: str) -> None:
    with pytest.raises(BrowserAttachError, match=message):
        validate_browser_cdp_endpoint(endpoint)


def test_attach_selects_the_only_tab_on_the_declared_origin() -> None:
    wanted = _Page("https://app.example.test/work/12?record=private")
    browser = SimpleNamespace(
        contexts=[
            SimpleNamespace(
                pages=[
                    _Page("chrome://settings/"),
                    _Page("https://unrelated.example.test/"),
                    wanted,
                ]
            )
        ]
    )
    assert select_attached_page(browser, app_url="https://app.example.test/") is wanted


def test_attach_requires_exact_url_when_origin_has_multiple_tabs() -> None:
    browser = _browser(
        "https://app.example.test/one?patient=SECRET_ONE#private",
        "https://app.example.test/two?patient=SECRET_TWO",
    )
    with pytest.raises(BrowserAttachError) as caught:
        select_attached_page(browser, app_url="https://app.example.test/")
    message = str(caught.value)
    assert "--browser-page-url" in message
    assert "/one" in message and "/two" in message
    assert "SECRET_ONE" not in message and "SECRET_TWO" not in message


def test_attach_exact_url_selects_one_tab_without_navigation() -> None:
    selected = "https://app.example.test/two?record=42"
    browser = _browser("https://app.example.test/one", selected)
    page = select_attached_page(
        browser,
        app_url="https://app.example.test/",
        page_url=selected,
    )
    assert page.url == selected


def test_attach_refuses_cross_origin_page_selector_without_echoing_it() -> None:
    private_url = "https://other.example.test/path?token=DO_NOT_PRINT"
    with pytest.raises(BrowserAttachError) as caught:
        select_attached_page(
            _browser("https://app.example.test/"),
            app_url="https://app.example.test/",
            page_url=private_url,
        )
    assert "same origin" in str(caught.value)
    assert private_url not in str(caught.value)
    assert "DO_NOT_PRINT" not in str(caught.value)


def test_attached_backend_uses_live_css_viewport_and_css_screenshot() -> None:
    class Page:
        viewport_size = None

        def __init__(self) -> None:
            self.screenshot_options = None

        def evaluate(self, _script):
            return {"width": 1440, "height": 900}

        def screenshot(self, **kwargs):
            self.screenshot_options = kwargs
            return b"png"

    page = Page()
    backend = PlaywrightBackend(page, screenshot_scale="css")  # type: ignore[arg-type]
    assert backend.viewport == (1440, 900)
    assert backend.screenshot() == b"png"
    assert page.screenshot_options["scale"] == "css"


def test_attached_backend_uses_source_sanitized_structural_state() -> None:
    page = SimpleNamespace(
        url="https://app.example.test/?token=RAW-SECRET",
        title=lambda: "RAW-SECRET",
    )
    backend = PlaywrightBackend(  # type: ignore[arg-type]
        page,
        structural_state_reader=lambda: {
            "url": "https://app.example.test/?token=[secret]",
            "title": "[secret]",
        },
    )

    assert backend.url == "https://app.example.test/?token=[secret]"
    assert backend.page_title == "[secret]"


def test_backend_runs_privacy_guard_before_screenshot_bytes_exist() -> None:
    calls: list[str] = []

    class Page:
        def screenshot(self, **_kwargs):
            calls.append("screenshot")
            return b"png"

    backend = PlaywrightBackend(  # type: ignore[arg-type]
        Page(),
        screenshot_guard=lambda: calls.append("guard"),
    )
    assert backend.screenshot() == b"png"
    assert calls == ["guard", "screenshot"]

    def refuse() -> None:
        calls.append("refuse")
        raise BrowserAttachError("unsafe closed shadow boundary")

    refusing = PlaywrightBackend(Page(), screenshot_guard=refuse)  # type: ignore[arg-type]
    with pytest.raises(BrowserAttachError, match="closed shadow"):
        refusing.screenshot()
    assert calls == ["guard", "screenshot", "refuse"]


def test_backend_masks_password_and_declared_secret_fields_on_every_frame() -> None:
    class Frame:
        def __init__(self, name: str) -> None:
            self.name = name

        def locator(self, selector):
            return f"locator:{self.name}:{selector}"

    class Page:
        viewport_size = {"width": 1280, "height": 800}

        def __init__(self) -> None:
            self.screenshot_options: list[dict] = []
            self.frames = [Frame("main"), Frame("child")]
            self.listeners: dict[str, list] = {}
            self.attach_on_next_screenshot = True

        def on(self, event, listener):
            self.listeners.setdefault(event, []).append(listener)

        def remove_listener(self, event, listener):
            self.listeners[event].remove(listener)

        def evaluate(self, _script):
            return None

        def screenshot(self, **kwargs):
            self.screenshot_options.append(kwargs)
            if self.attach_on_next_screenshot:
                self.attach_on_next_screenshot = False
                frame = Frame("late-child")
                self.frames.append(frame)
                for listener in self.listeners.get("frameattached", []):
                    listener(frame)
            return b"png"

    selectors = (
        "input[type='password']",
        '[name="token"], [id="token"]',
    )
    page = Page()
    backend = PlaywrightBackend(  # type: ignore[arg-type]
        page,
        screenshot_mask_selectors=selectors,
    )

    assert backend.screenshot() == b"png"
    assert backend.screenshot() == b"png"
    assert len(page.screenshot_options) == 3
    assert page.screenshot_options[0]["mask"] == [
        f"locator:{frame}:{selector}"
        for frame in ("main", "child")
        for selector in selectors
    ]
    assert page.screenshot_options[1]["mask"] == [
        f"locator:{frame}:{selector}"
        for frame in ("main", "child", "late-child")
        for selector in selectors
    ]
    assert page.screenshot_options[2]["mask"] == page.screenshot_options[1]["mask"]
    for options in page.screenshot_options:
        assert options["mask_color"] == "#000000"
    backend.stop_screenshot_mask_tracking()
    assert not any(page.listeners.values())


def test_declared_secret_selectors_use_css_string_escaping() -> None:
    selectors = _secret_screenshot_selectors({"päss", 'quote"\\line\nend'})

    assert '[name="päss"], [id="päss"]' in selectors
    assert (
        '[name="quote\\"\\\\line\\a end"], [id="quote\\"\\\\line\\a end"]' in selectors
    )
    with pytest.raises(BrowserAttachError, match="null character"):
        _secret_screenshot_selectors({"unsafe\x00field"})
    with pytest.raises(BrowserAttachError, match="Unicode surrogate"):
        _secret_screenshot_selectors({"unsafe\ud800field"})


def test_attached_recorder_api_refuses_incompatible_options(tmp_path: Path) -> None:
    with pytest.raises(BrowserAttachError, match="requires a browser CDP"):
        InteractiveRecorder(
            "https://app.example.test/",
            tmp_path / "recording",
            browser_page_url="https://app.example.test/work",
        )
    with pytest.raises(BrowserAttachError, match="headless"):
        InteractiveRecorder(
            "https://app.example.test/",
            tmp_path / "recording",
            cdp_endpoint="http://127.0.0.1:9222",
            headless=True,
        )


def test_attached_recorder_reads_geometry_and_refuses_origin_drift(
    tmp_path: Path,
) -> None:
    session = InteractiveRecorder(
        "https://app.example.test/",
        tmp_path / "recording",
        cdp_endpoint="http://127.0.0.1:9222",
    )
    origin = {"value": "https://app.example.test"}
    session.page = SimpleNamespace(
        evaluate=lambda _script: {
            "origin": origin["value"],
            "width": 1280,
            "height": 800,
            "dpr": 2,
        },
    )
    assert session._read_attached_geometry() == (1280, 800, 2.0)

    origin["value"] = "https://other.example.test"
    with pytest.raises(BrowserAttachError) as caught:
        session._read_attached_geometry()
    assert "left the declared application origin" in str(caught.value)
    assert "DO_NOT_PRINT" not in str(caught.value)


def test_attached_recorder_retains_main_frame_origin_violation(
    tmp_path: Path,
) -> None:
    out = tmp_path / "recording"
    session = InteractiveRecorder(
        "https://app.example.test/",
        out,
        cdp_endpoint="http://127.0.0.1:9222",
    )
    session._prepare_recording_dir()
    origin = {"value": "https://other.example.test"}
    frame = SimpleNamespace(evaluate=lambda _script: origin["value"])
    session.page = SimpleNamespace(main_frame=frame)

    session._handle_frame_navigation(frame)
    origin["value"] = "https://app.example.test"
    session._handle_frame_navigation(frame)

    assert session.done is True
    assert session._listener_error is not None
    assert "left the declared application origin" in str(session._listener_error)
    with pytest.raises(BrowserAttachError, match="left the declared"):
        session.finish()
    assert not out.exists()
    assert not list(tmp_path.glob(".openadapt-recording-partial-*"))


def test_attached_recorder_refuses_existing_output_without_changing_it(
    tmp_path: Path,
) -> None:
    out = tmp_path / "recording"
    frames = out / "frames"
    frames.mkdir(parents=True)
    (out / "meta.json").write_text('{"id":"complete-existing"}\n')
    (out / "events.jsonl").write_text('{"i":0,"kind":"key"}\n')
    (frames / "0000_before.png").write_bytes(b"SENSITIVE-FRAME")
    before = {
        path.relative_to(out): path.read_bytes()
        for path in out.rglob("*")
        if path.is_file()
    }
    session = InteractiveRecorder(
        "https://app.example.test/",
        out,
        cdp_endpoint="http://127.0.0.1:9222",
    )

    with pytest.raises(BrowserAttachError, match="output already exists"):
        session._prepare_recording_dir()
    session.abort()

    after = {
        path.relative_to(out): path.read_bytes()
        for path in out.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert not list(tmp_path.glob(".openadapt-recording-partial-*"))


def test_attached_recorder_refuses_output_created_during_promotion(
    tmp_path: Path,
) -> None:
    out = tmp_path / "recording"
    session = InteractiveRecorder(
        "https://app.example.test/",
        out,
        cdp_endpoint="http://127.0.0.1:9222",
    )
    session._prepare_recording_dir()
    assert session._recording_dir is not None
    (session._recording_dir / "complete.txt").write_text("new recording")

    out.mkdir()
    original_identity = out.stat()
    with pytest.raises(BrowserAttachError, match="appeared during capture"):
        session._promote_recording()

    current_identity = out.stat()
    assert (current_identity.st_dev, current_identity.st_ino) == (
        original_identity.st_dev,
        original_identity.st_ino,
    )
    session.abort()
    assert out.is_dir()
    assert not list(tmp_path.glob(".openadapt-recording-partial-*"))


def test_attached_tab_close_discards_partial_output(tmp_path: Path) -> None:
    out = tmp_path / "recording"
    session = InteractiveRecorder(
        "https://app.example.test/",
        out,
        cdp_endpoint="http://127.0.0.1:9222",
    )
    session._prepare_recording_dir()
    assert session._recording_dir is not None
    (session._recording_dir / "meta.json").write_text('{"id":"not-final"}\n')

    session._handle_page_close()

    with pytest.raises(BrowserAttachError, match="selected browser tab closed"):
        session.finish()
    assert not out.exists()
    assert not list(tmp_path.glob(".openadapt-recording-partial-*"))


def test_attached_tab_close_during_finalization_discards_metadata(
    tmp_path: Path,
) -> None:
    out = tmp_path / "recording"
    session = InteractiveRecorder(
        "https://app.example.test/",
        out,
        cdp_endpoint="http://127.0.0.1:9222",
    )
    session._prepare_recording_dir()

    class ClosingPage:
        frames: list = []

        def __init__(self) -> None:
            self.origin = "https://app.example.test"
            self.main_frame = SimpleNamespace(evaluate=lambda _script: self.origin)

        def evaluate(self, _script):
            return {
                "origin": self.origin,
                "width": 1280,
                "height": 800,
                "dpr": 1,
            }

    session.page = ClosingPage()
    session._page_lifecycle_listeners_installed = True
    session._privacy_cdp = _FakePrivacyCdp()
    session._attached_geometry = (1280, 800, 1.0)
    session._initial_attached_viewport = (1280, 800)

    class ClosingRecorder:
        def finish(self):
            assert session._recording_dir is not None
            (session._recording_dir / "meta.json").write_text(
                json.dumps({"viewport": [1280, 800]})
            )
            return session._recording_dir

    session.recorder = ClosingRecorder()  # type: ignore[assignment]
    session._pw = SimpleNamespace(stop=lambda: session._handle_page_close())

    with pytest.raises(BrowserAttachError, match="selected browser tab closed"):
        session.finish()
    assert not out.exists()
    assert not list(tmp_path.glob(".openadapt-recording-partial-*"))


def test_attached_popup_during_finalization_discards_metadata(
    tmp_path: Path,
) -> None:
    out = tmp_path / "recording"
    session = InteractiveRecorder(
        "https://app.example.test/",
        out,
        cdp_endpoint="http://127.0.0.1:9222",
    )
    session._prepare_recording_dir()

    class PopupPage:
        frames: list = []

        def __init__(self) -> None:
            self.origin = "https://app.example.test"
            self.main_frame = SimpleNamespace(evaluate=lambda _script: self.origin)

        def evaluate(self, _script):
            return {
                "origin": self.origin,
                "width": 1280,
                "height": 800,
                "dpr": 1,
            }

    session.page = PopupPage()
    session._page_lifecycle_listeners_installed = True
    session._privacy_cdp = _FakePrivacyCdp()
    session._attached_geometry = (1280, 800, 1.0)
    session._initial_attached_viewport = (1280, 800)

    class FinalizingRecorder:
        def finish(self):
            assert session._recording_dir is not None
            (session._recording_dir / "meta.json").write_text(
                json.dumps({"viewport": [1280, 800]})
            )
            return session._recording_dir

    session.recorder = FinalizingRecorder()  # type: ignore[assignment]
    session._pw = SimpleNamespace(
        stop=lambda: session._handle_popup(SimpleNamespace(url="about:blank"))
    )

    with pytest.raises(BrowserAttachError, match="popup or new tab"):
        session.finish()
    assert not out.exists()
    assert not list(tmp_path.glob(".openadapt-recording-partial-*"))


@pytest.mark.parametrize("late_event", ["context_page", "origin", "frame"])
def test_attached_late_lifecycle_event_discards_metadata(
    tmp_path: Path,
    late_event: str,
) -> None:
    out = tmp_path / f"recording-{late_event}"
    session = InteractiveRecorder(
        "https://app.example.test/",
        out,
        cdp_endpoint="http://127.0.0.1:9222",
    )
    session._prepare_recording_dir()

    class LifecyclePage:
        frames: list = []

        def __init__(self) -> None:
            self.origin = "https://app.example.test"
            self.main_frame = SimpleNamespace(evaluate=lambda _script: self.origin)

        def evaluate(self, _script):
            return {
                "origin": self.origin,
                "width": 1280,
                "height": 800,
                "dpr": 1,
            }

    session.page = LifecyclePage()
    session._page_lifecycle_listeners_installed = True
    session._privacy_cdp = _FakePrivacyCdp()
    session._attached_geometry = (1280, 800, 1.0)
    session._initial_attached_viewport = (1280, 800)

    class FinalizingRecorder:
        def finish(self):
            assert session._recording_dir is not None
            (session._recording_dir / "meta.json").write_text(
                json.dumps({"viewport": [1280, 800]})
            )
            return session._recording_dir

    session.recorder = FinalizingRecorder()  # type: ignore[assignment]

    def emit_late_event() -> None:
        if late_event == "context_page":
            session._handle_context_page(SimpleNamespace(url="about:blank"))
        elif late_event == "origin":
            session.page.origin = "https://other.example.test"
            session._handle_frame_navigation(session.page.main_frame)
        else:
            session._handle_frame_tree_change(SimpleNamespace())

    session._pw = SimpleNamespace(stop=emit_late_event)

    with pytest.raises(BrowserAttachError):
        session.finish()
    assert not out.exists()
    assert not list(tmp_path.glob(".openadapt-recording-partial-*"))


def test_attached_recorder_refuses_a_new_context_page(tmp_path: Path) -> None:
    session = InteractiveRecorder(
        "https://app.example.test/",
        tmp_path / "recording",
        cdp_endpoint="http://127.0.0.1:9222",
    )
    selected = SimpleNamespace()
    context = SimpleNamespace(pages=[selected])
    selected.context = context
    session.page = selected
    session._context_pages_at_start = (selected,)

    context.pages.append(SimpleNamespace(context=context))

    with pytest.raises(BrowserAttachError, match="popup or new tab"):
        session._assert_no_new_pages()
    assert session.done is True


def test_attached_recorder_refuses_uncomposed_iframe_events(tmp_path: Path) -> None:
    """A subframe event is accepted ONLY with the page-space composition
    marker the in-page closure sets after proving the frame chain. Anything
    else -- no marker, or a declared secret inside a frame -- refuses."""

    session = InteractiveRecorder(
        "https://app.example.test/",
        tmp_path / "recording",
        cdp_endpoint="http://127.0.0.1:9222",
    )
    session._enqueue_browser_event(
        {
            "__oaflow_session": session._session_id,
            "__oaflow_top_level": False,
            "kind": "click",
            "x": 10,
            "y": 20,
        }
    )
    assert session.done is True
    assert session._pyq == []
    assert session._listener_error is not None
    assert "page-space contract" in str(session._listener_error)

    session.done = False
    session._listener_error = None
    selected_frame = object()
    session.page = SimpleNamespace(main_frame=selected_frame)
    session._enqueue_browser_event(
        {
            "__oaflow_session": session._session_id,
            "__oaflow_top_level": True,
            "kind": "click",
            "x": 10,
            "y": 20,
        },
        source={"page": session.page, "frame": object()},
    )
    assert session.done is True
    assert session._listener_error is not None
    assert "page-space contract" in str(session._listener_error)

    session.done = False
    session._listener_error = None
    session._enqueue_browser_event(
        {
            "__oaflow_session": session._session_id,
            "__oaflow_top_level": False,
            "__oaflow_frame_composed": True,
            "__oaflow_viewport": [1280, 800],
            "__oaflow_dpr": 1,
            "__oaflow_origin": "https://app.example.test",
            "kind": "click",
            "x": 310,
            "y": 320,
        },
        source={"page": session.page, "frame": object()},
    )
    assert session._listener_error is None
    assert len(session._pyq) == 1
    assert session._pyq[0]["x"] == 310

    session._pyq.clear()
    session._enqueue_browser_event(
        {
            "__oaflow_session": session._session_id,
            "__oaflow_top_level": False,
            "__oaflow_frame_composed": True,
            "__oaflow_secret_mask_bound": True,
            "__oaflow_input_session": f"{session._session_id}:input:1",
            "__oaflow_viewport": [1280, 800],
            "__oaflow_dpr": 1,
            "__oaflow_origin": "https://app.example.test",
            "kind": "input",
            "secret": True,
            "field": "password",
        },
        source={"page": session.page, "frame": object()},
    )
    assert session.done is True
    assert session._pyq == []
    assert session._listener_error is not None
    assert "secret field inside an iframe" in str(session._listener_error)


def test_cross_origin_frame_refusal_event_stops_recording(tmp_path: Path) -> None:
    """The in-page closure emits `frame_refusal` when it cannot prove a
    frame's page-space position (cross-origin or too-deep chain)."""

    session = InteractiveRecorder(
        "https://app.example.test/",
        tmp_path / "recording",
        cdp_endpoint="http://127.0.0.1:9222",
    )
    session._enqueue_browser_event(
        {
            "__oaflow_session": session._session_id,
            "kind": "frame_refusal",
        }
    )
    assert session.done is True
    assert session._pyq == []
    assert session._listener_error is not None
    assert "cross-origin or too-deep" in str(session._listener_error)


def test_attached_recorder_refuses_invalid_viewport_evidence(tmp_path: Path) -> None:
    session = InteractiveRecorder(
        "https://app.example.test/",
        tmp_path / "recording",
        cdp_endpoint="http://127.0.0.1:9222",
    )
    selected_frame = object()
    session.page = SimpleNamespace(main_frame=selected_frame)
    session._enqueue_browser_event(
        {
            "__oaflow_session": session._session_id,
            "__oaflow_top_level": True,
            "__oaflow_viewport": [0, 800],
            "__oaflow_dpr": 2,
            "__oaflow_origin": "https://app.example.test",
            "kind": "click",
            "url": "https://app.example.test/work",
            "x": 10,
            "y": 20,
        },
        source={"page": session.page, "frame": selected_frame},
    )
    assert session.done is True
    assert session._pyq == []
    assert session._listener_error is not None
    assert "invalid viewport evidence" in str(session._listener_error)


@pytest.mark.parametrize(
    "batch",
    [
        [{"kind": "click"}, {"kind": "click"}],
        [
            {"kind": "input", "field": "note", "_oaflow_input_session": "a"},
            {"kind": "click"},
        ],
        [
            {"kind": "input", "field": "note", "_oaflow_input_session": "a"},
            {"kind": "key", "key": "Enter"},
        ],
        [{"kind": "scroll", "dy": 10}, {"kind": "click"}],
        [
            {"kind": "input", "field": "note", "_oaflow_input_session": "a"},
            {"kind": "input", "field": "note", "_oaflow_input_session": "b"},
        ],
    ],
)
def test_browser_event_batch_refuses_multiple_logical_actions(
    batch: list[dict],
) -> None:
    with pytest.raises(BrowserAttachError, match="more than one logical"):
        InteractiveRecorder._validate_event_batch(batch)


@pytest.mark.parametrize(
    "batch",
    [
        [
            {"kind": "input", "field": "note", "_oaflow_input_session": "a"},
            {"kind": "input", "field": "note", "_oaflow_input_session": "a"},
        ],
        [{"kind": "scroll", "dy": 10}, {"kind": "scroll", "dy": 20}],
    ],
)
def test_browser_event_batch_preserves_one_coalescible_action(
    batch: list[dict],
) -> None:
    InteractiveRecorder._validate_event_batch(batch)


def test_cli_threads_attach_contract_to_the_recorder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}

    def fake_record(url, out_dir, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        out_dir.mkdir(parents=True)
        (out_dir / "meta.json").write_text(json.dumps({"source": "fake"}))
        return out_dir

    monkeypatch.setattr(
        "openadapt_flow.interactive_recorder.record_interactive", fake_record
    )
    selected = "https://app.example.test/work?record=42"
    rc = main(
        [
            "record",
            "--backend",
            "web",
            "--url",
            "https://app.example.test/",
            "--browser-cdp-endpoint",
            "http://127.0.0.1:9222",
            "--browser-page-url",
            selected,
            "--out",
            str(tmp_path / "recording"),
        ]
    )
    assert rc == 0
    assert captured["url"] == "https://app.example.test/"
    assert captured["cdp_endpoint"] == "http://127.0.0.1:9222"
    assert captured["browser_page_url"] == selected


def test_cli_refuses_attach_flags_on_the_wrong_surface(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="apply only to --backend web"):
        main(
            [
                "record",
                "--backend",
                "windows",
                "--browser-cdp-endpoint",
                "http://127.0.0.1:9222",
                "--out",
                str(tmp_path / "recording"),
            ]
        )


def test_cli_refuses_page_selector_without_endpoint(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="requires --browser-cdp-endpoint"):
        main(
            [
                "record",
                "--backend",
                "web",
                "--url",
                "https://app.example.test/",
                "--browser-page-url",
                "https://app.example.test/work",
                "--out",
                str(tmp_path / "recording"),
            ]
        )


def test_cli_refuses_headless_attachment(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="--headless cannot be combined"):
        main(
            [
                "record",
                "--backend",
                "web",
                "--url",
                "https://app.example.test/",
                "--browser-cdp-endpoint",
                "http://127.0.0.1:9222",
                "--headless",
                "--out",
                str(tmp_path / "recording"),
            ]
        )


_ATTACH_HTML = b"""<!doctype html>
<html><head><title>Attach recorder test</title></head>
<body>
  <label for="note">Note</label><input id="note" name="note">
  <label for="password">Password</label>
  <input id="password" name="password" type="text">
  <label for="p&#228;ss">International secret</label>
  <input id="p&#228;ss" name="p&#228;ss" type="text">
  <label for="pre-focus-secret">Pre-focus secret</label>
  <input id="pre-focus-secret" name="pre-focus-secret" type="text">
  <label for="sticky-secret">Sticky secret</label>
  <input id="sticky-secret" name="sticky-secret" type="text"
         onkeydown="this.removeAttribute('name'); this.removeAttribute('id')">
  <label for="replacement-secret">Replacement secret</label>
  <input id="replacement-secret" name="replacement-secret" type="text"
         oninput="if (!this.dataset.replaced) {
           const replacement = this.cloneNode(true);
           replacement.removeAttribute('name');
           replacement.removeAttribute('id');
           replacement.dataset.replaced = 'yes';
           this.replaceWith(replacement);
           replacement.focus();
         }">
  <button id="save" onclick="document.body.dataset.saved='yes'">Save</button>
  <button id="open-popup" onclick="window.open('about:blank', '_blank')">
    Open popup
  </button>
  <iframe id="child" srcdoc="
    <input id='frame-password' type='password' value='FRAME-SECRET-NEVER-PERSIST'
           style='width:160px;height:30px;border:0'>
    <button id='inside'>Inside frame</button>
  "></iframe>
</body></html>"""

_CLOSED_SHADOW_HTML = b"""<!doctype html>
<html><head><title>Closed shadow test</title></head><body>
<x-closed-secret id="undeclared-closed-host"></x-closed-secret>
<script>
  const host = document.querySelector('x-closed-secret');
  const root = host.attachShadow({mode: 'closed'});
  const field = document.createElement('input');
  field.type = 'password';
  field.value = 'STATIC-LAUNCHED-CLOSED-SECRET';
  root.appendChild(field);
</script>
</body></html>"""


_GET_FORM_HTML = b"""<!doctype html>
<html><head><title>Token form</title></head><body>
  <form id="token-form" method="get" action="/">
    <label for="token">Token</label>
    <input id="token" name="token" type="text">
    <button id="submit-token" type="submit">Submit</button>
  </form>
</body></html>"""


@pytest.fixture(scope="module")
def attach_app_url() -> str:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib callback name
            if self.path.startswith("/closed-shadow"):
                payload = _CLOSED_SHADOW_HTML
            elif self.path.startswith("/get-form"):
                payload = _GET_FORM_HTML
            else:
                payload = _ATTACH_HTML
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _chromium_executable() -> Path | None:
    configured = os.environ.get("OPENADAPT_TEST_CHROMIUM_EXECUTABLE")
    candidates = [Path(configured)] if configured else []
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            candidates.append(Path(playwright.chromium.executable_path))
    except Exception:
        pass
    for command in ("google-chrome", "google-chrome-stable", "chromium"):
        found = shutil.which(command)
        if found:
            candidates.append(Path(found))
    candidates.append(
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    )
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def _activate_app_tab(endpoint: str, app_url: str) -> None:
    """Bring the app tab back to the front through the DevTools HTTP API.

    A refusal trial can leave the popup or new tab it created as the active
    tab. Chromium throttles rendering for the now-background app tab, so the
    next attach trial's first evidence screenshot can stall on a slow runner.
    A real operator records in a visible tab; restore that precondition.
    """

    with urlopen(f"{endpoint}/json/list", timeout=5) as response:
        targets = json.load(response)
    for target in targets:
        if target.get("type") == "page" and str(target.get("url", "")).startswith(
            app_url
        ):
            with urlopen(
                f"{endpoint}/json/activate/{target['id']}", timeout=5
            ) as response:
                assert response.status == 200
            return
    raise AssertionError("the app tab was not found for activation")


@pytest.mark.timeout(60)
def test_launched_browser_refuses_static_unbound_closed_shadow_password(
    attach_app_url: str,
    tmp_path: Path,
) -> None:
    """Owned launch inventories closed roots before its first screenshot."""

    if _chromium_executable() is None:
        pytest.skip("no Chromium executable is installed")
    output = tmp_path / "launched-static-closed-shadow"
    with pytest.raises(BrowserAttachError, match="closed shadow root"):
        record_interactive(
            f"{attach_app_url}closed-shadow",
            output,
            headless=True,
            script=lambda _page, _pump: None,
        )
    assert not output.exists()


@pytest.mark.timeout(30)
def test_page_closure_scrubs_replaced_prefilled_and_reflected_secrets() -> None:
    """Real Chromium proves the page-local guard before screenshot handling."""

    executable = _chromium_executable()
    if executable is None:
        pytest.skip("no Chromium executable is installed")
    session_id = "page-closure-privacy-test"
    binding_name = "__oaflow_emit_page_closure_test"
    secret_fields = (
        "prefilled-secret",
        "reordered-secret",
        "ambiguous-secret",
        "reflected-secret",
        "contenteditable-secret",
        "label-equals-secret",
        "altgr-secret",
    )
    init_js = (
        interactive_recorder_module._INIT_JS.replace(
            "__SESSION_ID__", json.dumps(session_id)
        )
        .replace("__BINDING_NAME__", json.dumps(binding_name))
        .replace("__SECRET_NAMES__", json.dumps(secret_fields))
        .replace("__SECRET_MARKER__", json.dumps("data-oaflow-secret-test"))
        .replace("__IDENT_NAMES__", "[]")
        .replace("__SPECIAL_KEYS__", "[]")
    )
    events: list[dict] = []
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(executable),
            headless=True,
            args=["--no-sandbox"],
        )
        try:
            page = browser.new_page()
            prefilled = "PREFILLED SECRET MUST NOT CROSS"
            page.route(
                "http://privacy.test/**",
                lambda route: route.fulfill(
                    content_type="text/html",
                    body=(
                        "<input name='prefilled-secret'>"
                        "<div id='rewrite'></div>"
                        "<div id='ambiguous-rewrite'></div><button>save</button>"
                    ),
                ),
            )
            page.goto("http://privacy.test/")
            page.locator("[name='prefilled-secret']").evaluate(
                "(element, secret) => { element.value = secret; document.title = secret; }",
                prefilled,
            )
            page.expose_binding(
                binding_name,
                lambda _source, detail: events.append(detail),
            )
            page.evaluate(init_js)
            state = page.evaluate("() => window.__oaflowRecorder.structuralState()")
            assert prefilled not in json.dumps(state)

            reordered = "REORDERED SECRET MUST NOT CROSS"
            page.evaluate(
                """secret => {
                  const parent = document.querySelector('#rewrite');
                  const declared = document.createElement('input');
                  declared.name = 'reordered-secret';
                  parent.appendChild(declared);
                  parent.removeChild(declared);
                  declared.removeAttribute('name');
                  const replacement = document.createElement('input');
                  parent.appendChild(replacement);
                  replacement.value = secret;
                  replacement.dispatchEvent(new Event('input', {bubbles: true}));
                }""",
                reordered,
            )
            page.evaluate(
                """() => {
                  const parent = document.querySelector('#ambiguous-rewrite');
                  const declared = document.createElement('input');
                  declared.name = 'ambiguous-secret';
                  parent.append(declared, document.createElement('input'));
                  const possible = document.createElement('input');
                  parent.replaceChildren(document.createElement('input'), possible);
                  possible.value = 'AMBIGUOUS VALUE MUST NOT CROSS';
                  possible.dispatchEvent(new Event('input', {bubbles: true}));
                }"""
            )

            reflected = "REFLECTED SECRET MUST NOT CROSS"
            page.evaluate(
                """secret => {
                  const field = document.createElement('input');
                  field.name = 'reflected-secret';
                  document.body.appendChild(field);
                  field.value = secret;
                  field.dispatchEvent(new Event('input', {bubbles: true}));
                  history.replaceState({}, '', '/?token=' + encodeURIComponent(secret));
                  document.title = secret;
                  const button = document.querySelector('button');
                  button.id = secret;
                  button.setAttribute('role', secret);
                  button.setAttribute('aria-label', secret);
                  button.click();
                }""",
                reflected,
            )
            contenteditable = "CONTENTEDITABLE SECRET MUST NOT CROSS"
            page.evaluate(
                """secret => {
                  const field = document.createElement('div');
                  field.contentEditable = 'true';
                  field.setAttribute('name', 'contenteditable-secret');
                  field.innerText = secret;
                  document.body.appendChild(field);
                  field.dispatchEvent(new Event('input', {bubbles: true}));
                  field.click();
                }""",
                contenteditable,
            )
            page.evaluate(
                """async () => {
                  const label = document.createElement('label');
                  label.htmlFor = 'label-equals-secret';
                  label.textContent = 'Password';
                  const field = document.createElement('input');
                  field.id = 'label-equals-secret';
                  field.name = 'label-equals-secret';
                  document.body.append(label, field);
                  await Promise.resolve();
                  field.value = 'Password';
                  field.dispatchEvent(new Event('input', {bubbles: true}));

                  const altGr = document.createElement('input');
                  altGr.name = 'altgr-secret';
                  document.body.appendChild(altGr);
                  altGr.dispatchEvent(new KeyboardEvent('keydown', {
                    key: '@', ctrlKey: true, altKey: true, bubbles: true,
                  }));

                  const editable = document.createElement('div');
                  editable.contentEditable = 'true';
                  editable.innerText = 'VISIBLE CONTENTEDITABLE';
                  document.body.appendChild(editable);
                  editable.dispatchEvent(new Event('input', {bubbles: true}));

                  const aria = document.createElement('div');
                  aria.setAttribute('role', 'textbox');
                  aria.textContent = 'VISIBLE ARIA TEXTBOX';
                  document.body.appendChild(aria);
                  aria.dispatchEvent(new Event('input', {bubbles: true}));

                  const checkbox = document.createElement('input');
                  checkbox.type = 'checkbox';
                  document.body.appendChild(checkbox);
                  checkbox.dispatchEvent(new Event('input', {bubbles: true}));
                  const select = document.createElement('select');
                  select.innerHTML = '<option value="one">One</option>';
                  document.body.appendChild(select);
                  select.dispatchEvent(new Event('input', {bubbles: true}));
                }"""
            )
            page.wait_for_timeout(50)
        finally:
            browser.close()

    payload = json.dumps(events)
    assert prefilled not in payload
    assert reordered not in payload
    assert "AMBIGUOUS VALUE MUST NOT CROSS" not in payload
    assert reflected not in payload
    assert contenteditable not in payload
    input_events = [event for event in events if event.get("kind") == "input"]
    assert len(input_events) == 6
    assert sum(event.get("secret") is True for event in input_events) == 4
    label_event = next(
        event for event in input_events if event.get("field") == "label-equals-secret"
    )
    # Flow does not rewrite captured text. A label that holds another declared
    # field's value is WITHHELD, not replaced by a placeholder: a placeholder
    # would propose a parameter name the page never showed.
    assert label_event["label"] is None
    assert {
        event.get("value") for event in input_events if not event.get("secret")
    } == {
        "VISIBLE CONTENTEDITABLE",
        "VISIBLE ARIA TEXTBOX",
    }
    assert sum(event.get("kind") == "privacy_refusal" for event in events) == 1
    assert not any(
        event.get("kind") == "hotkey" and event.get("key") == "@" for event in events
    )
    click = next(event for event in events if event.get("kind") == "click")
    assert click["structural"]["selector"] is None
    # Identity evidence is EXACT or WITHHELD. Replay compares the role and the
    # accessible name against the live page, so a placeholder here would make
    # replay compare against characters the page never held.
    assert click["structural"]["role"] is None
    assert click["structural"]["name"] is None
    # A refused DOM identity says WHY. It is never a bare null selector that
    # reads as "this element simply had no stable identity".
    assert click["structural"]["identity_withheld"] == "secret-value-in-identity"


@pytest.mark.timeout(300)
def test_live_cdp_attach_records_compiles_and_leaves_browser_running_three_trials(
    attach_app_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Three real Chromium trials cover attach, secrets, frames, and detach."""

    executable = _chromium_executable()
    if executable is None:
        pytest.skip("no Chromium executable is installed")
    from playwright.sync_api import sync_playwright

    profile = tmp_path / "chrome-profile"
    profile.mkdir()
    process = subprocess.Popen(
        [
            str(executable),
            "--headless=new",
            "--no-sandbox",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=0",
            f"--user-data-dir={profile}",
            "--window-size=1280,800",
            attach_app_url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        port_file = profile / "DevToolsActivePort"
        deadline = time.monotonic() + 20
        while not port_file.is_file() and time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail("Chromium exited before its CDP endpoint was ready")
            time.sleep(0.05)
        assert port_file.is_file(), "Chromium CDP endpoint did not become ready"
        port = int(port_file.read_text().splitlines()[0])
        endpoint = f"http://127.0.0.1:{port}"

        for trial in range(3):
            # Trial 1 types a LOWERCASE secret whose characters occur in the
            # attach URL http://127.0.0.1:<port>/. An uppercase phrase shares
            # no character with that URL, and that blind spot hid a redaction
            # defect that rewrote the URL of every event and refused the whole
            # recording on the first keystroke.
            secret = (
                "hunter2-attach-secret"
                if trial == 1
                else f"ATTACH-SECRET-{trial}-NEVER-PERSIST"
            )
            secret_rect: dict[str, int] = {}

            def drive(page, pump, *, secret_value=secret, trial_number=trial):
                page.evaluate(
                    """() => {
                      document.querySelector('#note').value = '';
                      document.querySelector('#password').value = '';
                      delete document.body.dataset.saved;
                    }"""
                )
                page.click("#note")
                pump()
                page.keyboard.type(f"trial-{trial_number}")
                pump()
                pump()
                page.click("#password")
                box = page.locator("#password").bounding_box()
                assert box is not None
                secret_rect.update(
                    x=round(box["x"]),
                    y=round(box["y"]),
                    width=round(box["width"]),
                    height=round(box["height"]),
                )
                pump()
                page.keyboard.type(secret_value)
                pump()
                pump()
                page.click("#save")
                pump()
                pump()
                assert page.get_attribute("body", "data-saved") == "yes"

            recording = record_interactive(
                attach_app_url,
                tmp_path / f"recording-{trial}",
                secret_fields=("password",),
                param_fields=("note",),
                cdp_endpoint=endpoint,
                script=drive,
            )
            meta = json.loads((recording / "meta.json").read_text())
            events_text = (recording / "events.jsonl").read_text()
            assert meta["source"] == "openadapt-flow-playwright-cdp"
            assert meta["secret_params"] == ["password"]
            assert secret not in json.dumps(meta)
            assert secret not in events_text
            assert meta["viewport"][0] > 0 and meta["viewport"][1] > 0
            events = [json.loads(line) for line in events_text.splitlines()]
            secret_event = next(event for event in events if event.get("secret"))
            next_before = Image.open(
                recording / "frames" / f"{int(secret_event['i']) + 1:04d}_before.png"
            ).convert("RGB")
            crop = next_before.crop(
                (
                    secret_rect["x"],
                    secret_rect["y"],
                    secret_rect["x"] + secret_rect["width"],
                    secret_rect["y"] + secret_rect["height"],
                )
            )
            assert all(extrema == (0, 0) for extrema in crop.getextrema())

            bundle = tmp_path / f"bundle-{trial}"
            workflow = compile_recording(
                recording,
                bundle,
                name=f"attached-browser-{trial}",
            )
            assert workflow.steps
            assert workflow.secret_params == ["password"]

            # Finishing a recording detaches Playwright. It does not close the
            # operator's external browser or its selected tab.
            assert process.poll() is None
            with urlopen(f"{endpoint}/json/version", timeout=2) as response:
                assert response.status == 200

        unicode_secret = "INTERNATIONAL-SECRET-NEVER-PERSIST"
        unicode_secret_rect: dict[str, int] = {}

        def record_unicode_secret(page, pump):
            field = page.locator('[name="päss"]')
            field.fill("")
            box = field.bounding_box()
            assert box is not None
            unicode_secret_rect.update(
                x=round(box["x"]),
                y=round(box["y"]),
                width=round(box["width"]),
                height=round(box["height"]),
            )
            field.click()
            pump()
            page.keyboard.type(unicode_secret)
            pump()
            pump()
            page.click("#save")
            pump()
            pump()

        unicode_recording = record_interactive(
            attach_app_url,
            tmp_path / "recording-unicode-secret",
            secret_fields=("päss",),
            cdp_endpoint=endpoint,
            script=record_unicode_secret,
        )
        unicode_events_text = (unicode_recording / "events.jsonl").read_text()
        unicode_meta_text = (unicode_recording / "meta.json").read_text()
        assert unicode_secret not in unicode_events_text
        assert unicode_secret not in unicode_meta_text
        unicode_events = [json.loads(line) for line in unicode_events_text.splitlines()]
        unicode_event = next(event for event in unicode_events if event.get("secret"))
        unicode_next_before = Image.open(
            unicode_recording
            / "frames"
            / f"{int(unicode_event['i']) + 1:04d}_before.png"
        ).convert("RGB")
        unicode_crop = unicode_next_before.crop(
            (
                unicode_secret_rect["x"],
                unicode_secret_rect["y"],
                unicode_secret_rect["x"] + unicode_secret_rect["width"],
                unicode_secret_rect["y"] + unicode_secret_rect["height"],
            )
        )
        assert all(extrema == (0, 0) for extrema in unicode_crop.getextrema())
        assert process.poll() is None

        def assert_mutating_secret_stays_private(
            *,
            field_selector: str,
            declared_field: str,
            secret: str,
            output_name: str,
            remove_identity_before_focus: bool = False,
        ) -> None:
            secret_rect: dict[str, int] = {}

            def type_through_mutation(page, pump):
                field = page.query_selector(field_selector)
                assert field is not None
                if remove_identity_before_focus:
                    field.evaluate(
                        """element => {
                          element.removeAttribute('name');
                          element.removeAttribute('id');
                        }"""
                    )
                box = field.bounding_box()
                assert box is not None
                secret_rect.update(
                    x=round(box["x"]),
                    y=round(box["y"]),
                    width=round(box["width"]),
                    height=round(box["height"]),
                )
                field.click()
                pump()
                page.keyboard.type(secret)
                pump()
                pump()
                page.click("#save")
                pump()
                pump()

            recording = record_interactive(
                attach_app_url,
                tmp_path / output_name,
                secret_fields=(declared_field,),
                cdp_endpoint=endpoint,
                script=type_through_mutation,
            )
            for artifact in recording.rglob("*"):
                if artifact.is_file():
                    assert secret.encode() not in artifact.read_bytes()
            events = [
                json.loads(line)
                for line in (recording / "events.jsonl").read_text().splitlines()
            ]
            secret_events = [event for event in events if event.get("secret")]
            assert secret_events
            assert all(event.get("text") is None for event in secret_events)
            for frame_path in (recording / "frames").glob("*.png"):
                frame = Image.open(frame_path).convert("RGB")
                crop = frame.crop(
                    (
                        secret_rect["x"],
                        secret_rect["y"],
                        secret_rect["x"] + secret_rect["width"],
                        secret_rect["y"] + secret_rect["height"],
                    )
                )
                assert all(extrema == (0, 0) for extrema in crop.getextrema())

        pre_focus_secret = "PREINPUT-SECRET-NEVER-PERSIST"
        assert_mutating_secret_stays_private(
            field_selector="#pre-focus-secret",
            declared_field="pre-focus-secret",
            secret=pre_focus_secret,
            output_name="recording-pre-focus-secret",
            remove_identity_before_focus=True,
        )
        attribute_secret = "ATTRIBUTE-MUTATION-SECRET-NEVER-PERSIST"
        assert_mutating_secret_stays_private(
            field_selector="#sticky-secret",
            declared_field="sticky-secret",
            secret=attribute_secret,
            output_name="recording-sticky-secret",
        )
        replacement_secret = "REPLACEMENT-SECRET-NEVER-PERSIST"
        assert_mutating_secret_stays_private(
            field_selector="#replacement-secret",
            declared_field="replacement-secret",
            secret=replacement_secret,
            output_name="recording-replacement-secret",
        )
        captured_output = capsys.readouterr()
        for secret in (pre_focus_secret, attribute_secret, replacement_secret):
            assert secret not in captured_output.out
            assert secret not in captured_output.err
        assert process.poll() is None

        dynamic_secret = "DYNAMIC-SECRET-LITERAL-NEVER-PERSIST"
        dynamic_secret_rect: dict[str, int] = {}

        def add_remove_and_type_dynamic_secret(page, pump):
            rect = page.evaluate(
                """secret => {
                  const field = document.createElement('input');
                  field.id = 'dynamic-secret';
                  field.name = 'dynamic-secret';
                  field.style.cssText = 'width:220px;height:40px;border:0';
                  document.body.appendChild(field);
                  field.removeAttribute('name');
                  field.removeAttribute('id');
                  field.value = secret;
                  field.dispatchEvent(new Event('input', {bubbles: true}));
                  const box = field.getBoundingClientRect();
                  return {
                    x: Math.round(box.left), y: Math.round(box.top),
                    width: Math.round(box.width), height: Math.round(box.height),
                  };
                }""",
                dynamic_secret,
            )
            dynamic_secret_rect.update(rect)
            pump()
            pump()

        dynamic_recording = record_interactive(
            attach_app_url,
            tmp_path / "recording-dynamic-secret",
            secret_fields=("dynamic-secret",),
            cdp_endpoint=endpoint,
            script=add_remove_and_type_dynamic_secret,
        )
        for artifact in dynamic_recording.rglob("*"):
            if artifact.is_file():
                assert dynamic_secret.encode() not in artifact.read_bytes()
        dynamic_events = [
            json.loads(line)
            for line in (dynamic_recording / "events.jsonl").read_text().splitlines()
        ]
        assert len(dynamic_events) == 1
        assert dynamic_events[0].get("secret") is True
        dynamic_after = Image.open(
            dynamic_recording / "frames" / "0000_after.png"
        ).convert("RGB")
        dynamic_crop = dynamic_after.crop(
            (
                dynamic_secret_rect["x"],
                dynamic_secret_rect["y"],
                dynamic_secret_rect["x"] + dynamic_secret_rect["width"],
                dynamic_secret_rect["y"] + dynamic_secret_rect["height"],
            )
        )
        assert all(extrema == (0, 0) for extrema in dynamic_crop.getextrema())
        assert process.poll() is None

        pre_input_replacement_secret = (
            "PRE-INPUT-REPLACEMENT-SECRET-LITERAL-NEVER-PERSIST"
        )
        pre_input_replacement_rect: dict[str, int] = {}

        def replace_dynamic_secret_before_first_input(page, pump):
            rect = page.evaluate(
                """secret => {
                  const declared = document.createElement('input');
                  declared.id = 'pre-input-replacement-secret';
                  declared.name = 'pre-input-replacement-secret';
                  declared.style.cssText = 'width:220px;height:40px;border:0';
                  document.body.appendChild(declared);
                  const replacement = declared.cloneNode(true);
                  replacement.removeAttribute('name');
                  replacement.removeAttribute('id');
                  declared.replaceWith(replacement);
                  replacement.value = secret;
                  replacement.dispatchEvent(new Event('input', {bubbles: true}));
                  const box = replacement.getBoundingClientRect();
                  return {
                    x: Math.round(box.left), y: Math.round(box.top),
                    width: Math.round(box.width), height: Math.round(box.height),
                  };
                }""",
                pre_input_replacement_secret,
            )
            pre_input_replacement_rect.update(rect)
            pump()
            pump()

        pre_input_replacement_recording = record_interactive(
            attach_app_url,
            tmp_path / "recording-pre-input-replacement-secret",
            secret_fields=("pre-input-replacement-secret",),
            cdp_endpoint=endpoint,
            script=replace_dynamic_secret_before_first_input,
        )
        for artifact in pre_input_replacement_recording.rglob("*"):
            if artifact.is_file():
                assert (
                    pre_input_replacement_secret.encode() not in artifact.read_bytes()
                )
        pre_input_replacement_events = [
            json.loads(line)
            for line in (pre_input_replacement_recording / "events.jsonl")
            .read_text()
            .splitlines()
        ]
        assert len(pre_input_replacement_events) == 1
        assert pre_input_replacement_events[0].get("secret") is True
        pre_input_replacement_after = Image.open(
            pre_input_replacement_recording / "frames" / "0000_after.png"
        ).convert("RGB")
        pre_input_replacement_crop = pre_input_replacement_after.crop(
            (
                pre_input_replacement_rect["x"],
                pre_input_replacement_rect["y"],
                pre_input_replacement_rect["x"] + pre_input_replacement_rect["width"],
                pre_input_replacement_rect["y"] + pre_input_replacement_rect["height"],
            )
        )
        assert all(
            extrema == (0, 0) for extrema in pre_input_replacement_crop.getextrema()
        )
        assert process.poll() is None

        open_shadow_secret = "OPEN-SHADOW-SECRET-LITERAL-NEVER-PERSIST"

        def type_open_shadow_secret_after_identity_removal(page, pump):
            page.evaluate(
                """secret => {
                  const host = document.createElement('x-open-secret');
                  host.id = 'open-shadow-secret';
                  document.body.appendChild(host);
                  const root = host.attachShadow({mode: 'open'});
                  const field = document.createElement('input');
                  field.name = 'open-shadow-secret';
                  field.style.cssText = 'width:220px;height:40px;border:0';
                  root.appendChild(field);
                  field.removeAttribute('name');
                  field.value = secret;
                  field.dispatchEvent(new Event('input', {
                    bubbles: true, composed: true,
                  }));
                }""",
                open_shadow_secret,
            )
            pump()
            pump()
            page.evaluate("() => document.querySelector('x-open-secret').remove()")

        open_shadow_recording = record_interactive(
            attach_app_url,
            tmp_path / "recording-open-shadow-secret",
            secret_fields=("open-shadow-secret",),
            cdp_endpoint=endpoint,
            script=type_open_shadow_secret_after_identity_removal,
        )
        for artifact in open_shadow_recording.rglob("*"):
            if artifact.is_file():
                assert open_shadow_secret.encode() not in artifact.read_bytes()
        open_shadow_events = [
            json.loads(line)
            for line in (open_shadow_recording / "events.jsonl")
            .read_text()
            .splitlines()
        ]
        assert len(open_shadow_events) == 1
        assert open_shadow_events[0].get("secret") is True

        future_closed_secret = "FUTURE-CLOSED-SECRET-LITERAL-NEVER-PERSIST"
        future_closed_rect: dict[str, int] = {}

        def type_future_closed_shadow_secret(page, pump):
            rect = page.evaluate(
                """secret => {
                  const host = document.createElement('x-future-closed-secret');
                  host.id = 'future-closed-secret';
                  host.style.cssText = 'display:block;width:240px;height:50px';
                  document.body.appendChild(host);
                  const root = host.attachShadow({mode: 'closed'});
                  const field = document.createElement('input');
                  field.style.cssText = 'width:220px;height:40px;border:0';
                  root.appendChild(field);
                  field.value = secret;
                  field.dispatchEvent(new Event('input', {
                    bubbles: true, composed: true,
                  }));
                  const box = host.getBoundingClientRect();
                  return {
                    x: Math.round(box.left), y: Math.round(box.top),
                    width: Math.round(box.width), height: Math.round(box.height),
                  };
                }""",
                future_closed_secret,
            )
            future_closed_rect.update(rect)
            pump()
            pump()
            page.evaluate(
                "() => document.querySelector('x-future-closed-secret').remove()"
            )

        future_closed_recording = record_interactive(
            attach_app_url,
            tmp_path / "recording-future-closed-secret",
            secret_fields=("future-closed-secret",),
            cdp_endpoint=endpoint,
            script=type_future_closed_shadow_secret,
        )
        for artifact in future_closed_recording.rglob("*"):
            if artifact.is_file():
                assert future_closed_secret.encode() not in artifact.read_bytes()
        future_closed_events = [
            json.loads(line)
            for line in (future_closed_recording / "events.jsonl")
            .read_text()
            .splitlines()
        ]
        assert len(future_closed_events) == 1
        assert future_closed_events[0].get("secret") is True
        future_closed_after = Image.open(
            future_closed_recording / "frames" / "0000_after.png"
        ).convert("RGB")
        future_closed_crop = future_closed_after.crop(
            (
                future_closed_rect["x"],
                future_closed_rect["y"],
                future_closed_rect["x"] + future_closed_rect["width"],
                future_closed_rect["y"] + future_closed_rect["height"],
            )
        )
        assert all(extrema == (0, 0) for extrema in future_closed_crop.getextrema())

        late_closed_secret = "LATE-CLOSED-SECRET-LITERAL-NEVER-PERSIST"
        late_closed_output = tmp_path / "recording-late-unbound-closed-secret"

        def expose_late_unbound_closed_secret(page, pump):
            page.evaluate(
                """secret => {
                  const host = document.createElement('x-late-closed-secret');
                  host.id = 'different-late-closed-host';
                  document.body.appendChild(host);
                  const root = host.attachShadow({mode: 'closed'});
                  const field = document.createElement('input');
                  field.name = 'late-closed-secret';
                  field.value = secret;
                  root.appendChild(field);
                  document.title = secret;
                }""",
                late_closed_secret,
            )
            page.click("#save")
            pump()

        with pytest.raises(BrowserAttachError, match="closed shadow root"):
            record_interactive(
                attach_app_url,
                late_closed_output,
                secret_fields=("late-closed-secret",),
                cdp_endpoint=endpoint,
                script=expose_late_unbound_closed_secret,
            )
        assert not late_closed_output.exists()
        with sync_playwright() as late_cleanup_playwright:
            late_cleanup_browser = late_cleanup_playwright.chromium.connect_over_cdp(
                endpoint
            )
            late_cleanup_page = select_attached_page(
                late_cleanup_browser,
                app_url=attach_app_url,
            )
            late_cleanup_page.evaluate(
                """() => {
                  document.querySelector('#different-late-closed-host').remove();
                  document.title = 'Attach recorder test';
                }"""
            )

        contenteditable_secret = "CONTENTEDITABLE-SECRET-LITERAL-NEVER-PERSIST"

        def type_and_click_secret_contenteditable(page, pump):
            page.evaluate(
                """secret => {
                  const field = document.createElement('div');
                  field.contentEditable = 'true';
                  field.setAttribute('name', 'contenteditable-secret');
                  field.setAttribute('role', 'textbox');
                  field.style.cssText = 'width:260px;height:40px;border:0';
                  document.body.appendChild(field);
                  field.innerText = secret;
                  field.dispatchEvent(new Event('input', {
                    bubbles: true, composed: true,
                  }));
                }""",
                contenteditable_secret,
            )
            pump()
            pump()
            page.locator('[name="contenteditable-secret"]').click()
            pump()
            pump()
            page.locator('[name="contenteditable-secret"]').evaluate(
                "element => element.remove()"
            )

        contenteditable_recording = record_interactive(
            attach_app_url,
            tmp_path / "recording-contenteditable-secret",
            secret_fields=("contenteditable-secret",),
            cdp_endpoint=endpoint,
            script=type_and_click_secret_contenteditable,
        )
        for artifact in contenteditable_recording.rglob("*"):
            if artifact.is_file():
                assert contenteditable_secret.encode() not in artifact.read_bytes()
        contenteditable_events = [
            json.loads(line)
            for line in (contenteditable_recording / "events.jsonl")
            .read_text()
            .splitlines()
        ]
        assert any(event.get("secret") is True for event in contenteditable_events)
        click_event = next(
            event for event in contenteditable_events if event.get("kind") == "click"
        )
        assert contenteditable_secret not in json.dumps(click_event)

        reflected_secret = "URL TITLE SECRET LITERAL NEVER PERSIST"

        def reflect_secret_into_url_title_and_target(page, pump):
            page.evaluate(
                """secret => {
                  const field = document.createElement('input');
                  field.name = 'reflected-secret';
                  document.body.appendChild(field);
                  field.addEventListener('input', () => {
                    history.replaceState({}, '', '/?token=' + encodeURIComponent(secret));
                    document.title = 'Result ' + secret;
                    document.querySelector('#save').setAttribute(
                      'aria-label', 'Save ' + secret
                    );
                  });
                  field.value = secret;
                  field.dispatchEvent(new Event('input', {
                    bubbles: true, composed: true,
                  }));
                }""",
                reflected_secret,
            )
            pump()
            pump()
            page.click("#save")
            pump()
            pump()
            page.evaluate(
                """() => {
                  history.replaceState({}, '', '/');
                  document.title = 'Attach recorder';
                  document.querySelector('#save').removeAttribute('aria-label');
                  document.querySelector('[name="reflected-secret"]').remove();
                }"""
            )

        reflected_recording = record_interactive(
            attach_app_url,
            tmp_path / "recording-reflected-secret",
            secret_fields=("reflected-secret",),
            cdp_endpoint=endpoint,
            script=reflect_secret_into_url_title_and_target,
        )
        encoded_reflected_secret = reflected_secret.replace(" ", "%20")
        for artifact in reflected_recording.rglob("*"):
            if artifact.is_file():
                payload = artifact.read_bytes()
                assert reflected_secret.encode() not in payload
                assert encoded_reflected_secret.encode() not in payload

        replaced_guard_output = tmp_path / "recording-replaced-privacy-guard"

        def replace_page_privacy_guard(page, pump):
            page.evaluate(
                """() => {
                  const host = document.createElement('x-guard-secret');
                  document.body.appendChild(host);
                  const root = host.attachShadow({mode: 'open'});
                  const field = document.createElement('input');
                  field.name = 'guard-secret';
                  field.value = 'GUARD-SECRET-NEVER-PERSIST';
                  root.appendChild(field);
                  field.dispatchEvent(new Event('input', {
                    bubbles: true, composed: true,
                  }));
                }"""
            )
            pump()
            page.evaluate(
                "() => { window.__oaflowRecorder = {sessionId: 'replaced'}; }"
            )

        with pytest.raises(BrowserAttachError, match="privacy guard is unavailable"):
            record_interactive(
                attach_app_url,
                replaced_guard_output,
                secret_fields=("guard-secret",),
                cdp_endpoint=endpoint,
                script=replace_page_privacy_guard,
            )
        assert not replaced_guard_output.exists()
        with sync_playwright() as guard_cleanup_playwright:
            guard_cleanup_browser = guard_cleanup_playwright.chromium.connect_over_cdp(
                endpoint
            )
            guard_cleanup_page = select_attached_page(
                guard_cleanup_browser,
                app_url=attach_app_url,
            )
            remaining_markers = guard_cleanup_page.evaluate(
                """() => {
                  const host = document.querySelector('x-guard-secret');
                  const field = host.shadowRoot.querySelector('input');
                  const markers = Array.from(field.attributes).filter(
                    (attribute) => attribute.name.startsWith('data-oaflow-secret-')
                  );
                  host.remove();
                  delete window.__oaflowRecorder;
                  return markers.length;
                }"""
            )
            assert remaining_markers == 0

        existing_closed_secret = "EXISTING-CLOSED-SECRET-LITERAL-NEVER-PERSIST"
        with sync_playwright() as setup_playwright:
            setup_browser = setup_playwright.chromium.connect_over_cdp(endpoint)
            setup_page = select_attached_page(setup_browser, app_url=attach_app_url)
            setup_page.evaluate(
                """secret => {
                  const host = document.createElement('x-existing-closed-secret');
                  host.id = 'existing-closed-secret';
                  host.style.cssText = 'display:block;width:240px;height:50px';
                  document.body.appendChild(host);
                  const root = host.attachShadow({mode: 'closed'});
                  const field = document.createElement('input');
                  field.name = 'existing-closed-secret';
                  field.value = secret;
                  field.style.cssText = 'width:220px;height:40px;border:0';
                  root.appendChild(field);
                  document.title = secret;
                  host.writeSecret = (secret) => {
                    field.value = secret;
                    field.dispatchEvent(new Event('input', {
                      bubbles: true, composed: true,
                    }));
                  };
                }""",
                existing_closed_secret,
            )

        def type_existing_closed_shadow_secret(page, pump):
            page.evaluate(
                """secret => document.querySelector(
                  '#existing-closed-secret'
                ).writeSecret(secret)""",
                existing_closed_secret,
            )
            pump()
            pump()

        existing_closed_recording = record_interactive(
            attach_app_url,
            tmp_path / "recording-existing-closed-secret",
            secret_fields=("existing-closed-secret",),
            cdp_endpoint=endpoint,
            script=type_existing_closed_shadow_secret,
        )
        for artifact in existing_closed_recording.rglob("*"):
            if artifact.is_file():
                assert existing_closed_secret.encode() not in artifact.read_bytes()

        with sync_playwright() as refusal_playwright:
            refusal_browser = refusal_playwright.chromium.connect_over_cdp(endpoint)
            refusal_page = select_attached_page(refusal_browser, app_url=attach_app_url)
            refusal_page.evaluate(
                """() => {
                  document.querySelector('#existing-closed-secret').remove();
                  document.title = 'Attach recorder test';
                  const host = document.createElement('x-undeclared-closed-secret');
                  host.id = 'different-closed-host';
                  document.body.appendChild(host);
                  const root = host.attachShadow({mode: 'closed'});
                  const field = document.createElement('input');
                  field.name = 'undeclared-closed-secret';
                  root.appendChild(field);
                }"""
            )

        refused_closed_output = tmp_path / "recording-refused-existing-closed-secret"
        with pytest.raises(
            BrowserAttachError,
            match="pre-existing or newly added closed shadow",
        ):
            record_interactive(
                attach_app_url,
                refused_closed_output,
                secret_fields=("undeclared-closed-secret",),
                cdp_endpoint=endpoint,
                script=lambda _page, _pump: None,
            )
        assert not refused_closed_output.exists()
        with sync_playwright() as refusal_cleanup_playwright:
            refusal_cleanup_browser = (
                refusal_cleanup_playwright.chromium.connect_over_cdp(endpoint)
            )
            refusal_cleanup_page = select_attached_page(
                refusal_cleanup_browser,
                app_url=attach_app_url,
            )
            refusal_cleanup_page.evaluate(
                """() => document.querySelector(
                  '#different-closed-host'
                ).remove()"""
            )
        assert process.poll() is None

        moved_secret = "MOVED-FINAL-SECRET-NEVER-PERSIST"
        moved_secret_rect: dict[str, int] = {}

        def move_secret_and_finish_without_pump(page, _pump):
            rect = page.evaluate(
                """secret => {
                  const field = document.createElement('input');
                  field.id = 'moved-final-secret';
                  field.name = 'moved-final-secret';
                  field.dataset.oaMovedFinalSecret = 'yes';
                  field.style.cssText = [
                    'position:fixed', 'left:20px', 'top:180px',
                    'width:220px', 'height:40px', 'border:0', 'z-index:1000',
                  ].join(';');
                  document.body.appendChild(field);
                  field.removeAttribute('name');
                  field.removeAttribute('id');
                  field.value = secret;
                  field.dispatchEvent(new Event('input', {bubbles: true}));
                  field.style.left = '700px';
                  field.style.top = '300px';
                  const box = field.getBoundingClientRect();
                  return {
                    x: Math.round(box.left), y: Math.round(box.top),
                    width: Math.round(box.width), height: Math.round(box.height),
                  };
                }""",
                moved_secret,
            )
            moved_secret_rect.update(rect)

        moved_recording = record_interactive(
            attach_app_url,
            tmp_path / "recording-moved-final-secret",
            secret_fields=("moved-final-secret",),
            cdp_endpoint=endpoint,
            script=move_secret_and_finish_without_pump,
        )
        for artifact in moved_recording.rglob("*"):
            if artifact.is_file():
                assert moved_secret.encode() not in artifact.read_bytes()
        moved_events = [
            json.loads(line)
            for line in (moved_recording / "events.jsonl").read_text().splitlines()
        ]
        assert len(moved_events) == 1
        assert moved_events[0].get("secret") is True
        moved_after = Image.open(moved_recording / "frames" / "0000_after.png").convert(
            "RGB"
        )
        moved_crop = moved_after.crop(
            (
                moved_secret_rect["x"],
                moved_secret_rect["y"],
                moved_secret_rect["x"] + moved_secret_rect["width"],
                moved_secret_rect["y"] + moved_secret_rect["height"],
            )
        )
        assert all(extrema == (0, 0) for extrema in moved_crop.getextrema())
        from playwright.sync_api import sync_playwright

        with sync_playwright() as cleanup_playwright:
            cleanup_browser = cleanup_playwright.chromium.connect_over_cdp(endpoint)
            cleanup_page = select_attached_page(
                cleanup_browser,
                app_url=attach_app_url,
            )
            cleanup_page.evaluate(
                """() => document.querySelector(
                  '[data-oa-moved-final-secret="yes"]'
                ).remove()"""
            )
        assert process.poll() is None

        detached_marker_session = InteractiveRecorder(
            attach_app_url,
            tmp_path / "recording-detached-secret-marker",
            secret_fields=("detached-cleanup-secret",),
            cdp_endpoint=endpoint,
        )
        detached_marker_session.start()
        assert detached_marker_session.page is not None
        detached_marker_session.page.evaluate(
            """() => {
              const field = document.createElement('input');
              field.name = 'detached-cleanup-secret';
              document.body.appendChild(field);
              window.__detachedOaSecret = field;
            }"""
        )
        detached_marker_session.page.wait_for_timeout(0)
        assert detached_marker_session.page.evaluate(
            """() => Array.from(window.__detachedOaSecret.attributes)
              .some((attribute) => attribute.name.startsWith('data-oaflow-secret-'))"""
        )
        detached_marker_session.page.evaluate(
            "() => window.__detachedOaSecret.remove()"
        )
        detached_marker_session.finish()

        detached_marker_probe = InteractiveRecorder(
            attach_app_url,
            tmp_path / "recording-detached-secret-marker-probe",
            cdp_endpoint=endpoint,
        )
        detached_marker_probe.start()
        assert detached_marker_probe.page is not None
        assert not detached_marker_probe.page.evaluate(
            """() => Array.from(window.__detachedOaSecret.attributes)
              .some((attribute) => attribute.name.startsWith('data-oaflow-secret-'))"""
        )
        detached_marker_probe.page.evaluate("() => delete window.__detachedOaSecret")
        detached_marker_probe.abort()
        assert process.poll() is None

        child_secret_rect: dict[str, int] = {}

        def retain_top_level_action_with_child_secret(page, pump):
            child_secret = page.frame_locator("#child").locator("#frame-password")
            box = child_secret.bounding_box()
            assert box is not None
            child_secret_rect.update(
                x=round(box["x"]),
                y=round(box["y"]),
                width=round(box["width"]),
                height=round(box["height"]),
            )
            page.click("#note")
            pump()
            pump()

        child_secret_recording = record_interactive(
            attach_app_url,
            tmp_path / "recording-child-frame-secret",
            cdp_endpoint=endpoint,
            script=retain_top_level_action_with_child_secret,
        )
        child_before = Image.open(
            child_secret_recording / "frames" / "0000_before.png"
        ).convert("RGB")
        child_crop = child_before.crop(
            (
                child_secret_rect["x"],
                child_secret_rect["y"],
                child_secret_rect["x"] + child_secret_rect["width"],
                child_secret_rect["y"] + child_secret_rect["height"],
            )
        )
        assert all(extrema == (0, 0) for extrema in child_crop.getextrema())
        assert process.poll() is None

        frame_race_session = InteractiveRecorder(
            attach_app_url,
            tmp_path / "recording-frame-race-probe",
            cdp_endpoint=endpoint,
        )
        frame_race_session.start()
        try:
            race_page = frame_race_session.page
            race_backend = frame_race_session.backend
            assert race_page is not None and race_backend is not None
            original_screenshot = race_page.screenshot
            masked_races = 0
            for trial in range(30):
                race_page.evaluate(
                    """() => {
                      const previous = document.querySelector('#race-frame');
                      if (previous) previous.remove();
                    }"""
                )
                race_page.wait_for_timeout(0)
                state = {"attach": True}

                def attach_after_frame_snapshot(**kwargs):
                    if state["attach"]:
                        state["attach"] = False
                        race_page.evaluate(
                            """trial => {
                              const frame = document.createElement('iframe');
                              frame.id = 'race-frame';
                              frame.style.cssText = [
                                'position:fixed', 'left:20px', 'top:160px',
                                'width:220px', 'height:80px', 'border:0',
                              ].join(';');
                              frame.srcdoc = `<input id="race-password"
                                type="password" value="RACE-${trial}"
                                style="width:180px;height:40px;border:0">`;
                              document.body.appendChild(frame);
                            }""",
                            trial,
                        )
                    return original_screenshot(**kwargs)

                with monkeypatch.context() as patch_context:
                    patch_context.setattr(
                        race_page,
                        "screenshot",
                        attach_after_frame_snapshot,
                    )
                    png = race_backend.screenshot()
                password = race_page.frame_locator("#race-frame").locator(
                    "#race-password"
                )
                box = password.bounding_box()
                assert box is not None
                image = Image.open(BytesIO(png)).convert("RGB")
                crop = image.crop(
                    (
                        round(box["x"]),
                        round(box["y"]),
                        round(box["x"] + box["width"]),
                        round(box["y"] + box["height"]),
                    )
                )
                assert all(extrema == (0, 0) for extrema in crop.getextrema())
                masked_races += 1
            assert masked_races == 30

            churn_attempts = 0

            def attach_and_detach_during_every_capture(**kwargs):
                nonlocal churn_attempts
                churn_attempts += 1
                race_page.evaluate(
                    """attempt => {
                      const frame = document.createElement('iframe');
                      frame.id = `churn-${attempt}`;
                      frame.srcdoc = '<input type="password" value="churn">';
                      document.body.appendChild(frame);
                      frame.remove();
                    }""",
                    churn_attempts,
                )
                return original_screenshot(**kwargs)

            with monkeypatch.context() as patch_context:
                patch_context.setattr(
                    race_page,
                    "screenshot",
                    attach_and_detach_during_every_capture,
                )
                with pytest.raises(ScreenshotMaskStabilityError, match="frame tree"):
                    race_backend.screenshot()
                assert churn_attempts == 3
        finally:
            if frame_race_session.page is not None:
                frame_race_session.page.evaluate(
                    """() => {
                      const frame = document.querySelector('#race-frame');
                      if (frame) frame.remove();
                    }"""
                )
            frame_race_session.abort()
        assert not (tmp_path / "recording-frame-race-probe").exists()
        assert process.poll() is None

        interleaved_recording = tmp_path / "recording-interleaved-action-refusal"
        interleaved_session = InteractiveRecorder(
            attach_app_url,
            interleaved_recording,
            cdp_endpoint=endpoint,
        )
        interleaved_session.start()
        assert interleaved_session.page is not None
        assert interleaved_session.backend is not None
        interleaved_session.page.click("#note")
        original_backend_screenshot = interleaved_session.backend.screenshot
        interleaved_action_injected = False

        def screenshot_after_second_action() -> bytes:
            nonlocal interleaved_action_injected
            if not interleaved_action_injected:
                interleaved_action_injected = True
                interleaved_session.page.click("#save")
                interleaved_session.page.wait_for_timeout(0)
            return original_backend_screenshot()

        monkeypatch.setattr(
            interleaved_session.backend,
            "screenshot",
            screenshot_after_second_action,
        )
        try:
            with pytest.raises(BrowserAttachError, match="more than one logical"):
                interleaved_session.pump()
        finally:
            interleaved_session.abort()
        assert interleaved_action_injected
        assert not (interleaved_recording / "meta.json").exists()
        assert process.poll() is None

        def rapid_pointer_pointer(page, pump):
            page.click("#note")
            page.click("#save")
            pump()

        def rapid_input_submit(page, pump):
            page.evaluate("document.querySelector('#note').value = ''")
            page.click("#note")
            pump()
            page.keyboard.type("rapid-input-submit")
            page.click("#save")
            pump()

        def rapid_input_enter(page, pump):
            page.evaluate("document.querySelector('#note').value = ''")
            page.click("#note")
            pump()
            page.keyboard.type("rapid-input-enter")
            page.keyboard.press("Enter")
            pump()

        def rapid_scroll_click(page, pump):
            page.mouse.wheel(0, 40)
            page.click("#note")
            pump()

        for case_name, drive_rapid_actions in (
            ("pointer-pointer", rapid_pointer_pointer),
            ("input-submit", rapid_input_submit),
            ("input-enter", rapid_input_enter),
            ("scroll-click", rapid_scroll_click),
        ):
            rapid_recording = tmp_path / f"recording-rapid-{case_name}"
            with pytest.raises(BrowserAttachError, match="more than one logical"):
                record_interactive(
                    attach_app_url,
                    rapid_recording,
                    cdp_endpoint=endpoint,
                    script=drive_rapid_actions,
                )
            assert not (rapid_recording / "meta.json").exists()
            assert process.poll() is None

        def coalesce_one_field(page, pump):
            page.evaluate("document.querySelector('#note').value = ''")
            page.click("#note")
            pump()
            page.keyboard.type("same-field-input-coalesces")
            pump()
            pump()

        coalesced_recording = record_interactive(
            attach_app_url,
            tmp_path / "recording-coalesced-input",
            cdp_endpoint=endpoint,
            script=coalesce_one_field,
        )
        coalesced_events = [
            json.loads(line)
            for line in (coalesced_recording / "events.jsonl").read_text().splitlines()
        ]
        assert (
            len([event for event in coalesced_events if event["kind"] == "type"]) == 1
        )
        assert process.poll() is None

        popup_recording = tmp_path / "recording-popup-refusal"

        def open_popup(page, pump):
            with page.expect_popup() as popup_info:
                page.click("#open-popup")
            popup_info.value.wait_for_load_state()
            pump()

        with pytest.raises(BrowserAttachError, match="popup or new tab"):
            record_interactive(
                attach_app_url,
                popup_recording,
                cdp_endpoint=endpoint,
                script=open_popup,
            )
        assert not (popup_recording / "meta.json").exists()
        assert process.poll() is None
        with urlopen(f"{endpoint}/json/list", timeout=2) as response:
            popup_targets = json.load(response)
        assert any(target.get("url") == "about:blank" for target in popup_targets)

        _activate_app_tab(endpoint, attach_app_url)
        popup_activity_recording = tmp_path / "recording-popup-activity-refusal"

        def act_inside_popup(page, pump):
            with page.expect_popup() as popup_info:
                page.click("#open-popup")
            popup = popup_info.value
            popup.set_content(
                "<input id='popup-note'><button id='popup-save'>Save</button>"
            )
            popup.fill("#popup-note", "activity-that-must-not-disappear")
            popup.click("#popup-save")
            pump()

        with pytest.raises(BrowserAttachError, match="popup or new tab"):
            record_interactive(
                attach_app_url,
                popup_activity_recording,
                cdp_endpoint=endpoint,
                script=act_inside_popup,
            )
        assert not (popup_activity_recording / "meta.json").exists()
        assert process.poll() is None
        with urlopen(f"{endpoint}/json/version", timeout=2) as response:
            assert response.status == 200

        _activate_app_tab(endpoint, attach_app_url)
        short_page_recording = tmp_path / "recording-short-page-refusal"

        def act_in_short_lived_context_page(page, pump):
            temporary = page.context.new_page()
            temporary.set_content(
                "<input id='short-note'><button id='short-save'>Save</button>"
            )
            temporary.fill("#short-note", "activity-that-must-not-disappear")
            temporary.click("#short-save")
            temporary.close()
            assert len(page.context.pages) >= 1
            pump()

        with pytest.raises(BrowserAttachError, match="popup or new tab"):
            record_interactive(
                attach_app_url,
                short_page_recording,
                cdp_endpoint=endpoint,
                script=act_in_short_lived_context_page,
            )
        assert not (short_page_recording / "meta.json").exists()
        assert process.poll() is None
        with urlopen(f"{endpoint}/json/version", timeout=2) as response:
            assert response.status == 200

        _activate_app_tab(endpoint, attach_app_url)
        prebaseline_recording = tmp_path / "recording-prebaseline-page-refusal"
        original_select_attached_page = interactive_recorder_module.select_attached_page
        prebaseline_page_acted = False

        def select_after_short_lived_page(browser, *, app_url, page_url=None):
            nonlocal prebaseline_page_acted
            selected = original_select_attached_page(
                browser,
                app_url=app_url,
                page_url=page_url,
            )
            temporary = selected.context.new_page()
            temporary.set_content(
                "<input id='pre-note'><button id='pre-save'>Save</button>"
            )
            temporary.fill("#pre-note", "prebaseline-activity-must-not-disappear")
            temporary.click("#pre-save")
            prebaseline_page_acted = True
            temporary.close()
            return selected

        with monkeypatch.context() as patch_context:
            patch_context.setattr(
                interactive_recorder_module,
                "select_attached_page",
                select_after_short_lived_page,
            )
            with pytest.raises(BrowserAttachError, match="popup or new tab"):
                record_interactive(
                    attach_app_url,
                    prebaseline_recording,
                    cdp_endpoint=endpoint,
                    script=lambda _page, _pump: None,
                )
        assert prebaseline_page_acted
        assert not (prebaseline_recording / "meta.json").exists()
        assert process.poll() is None

        _activate_app_tab(endpoint, attach_app_url)
        baseline_getter_recording = tmp_path / "recording-baseline-getter-refusal"
        from playwright.sync_api import BrowserContext

        original_pages_getter = BrowserContext.pages.fget
        assert original_pages_getter is not None
        baseline_getter_page_acted = False

        def pages_with_short_lived_action(context):
            nonlocal baseline_getter_page_acted
            existing = original_pages_getter(context)
            if not baseline_getter_page_acted:
                baseline_getter_page_acted = True
                temporary = context.new_page()
                temporary.set_content(
                    "<input id='gap-note'><button id='gap-save'>Save</button>"
                )
                temporary.fill("#gap-note", "baseline-gap-action-must-not-disappear")
                temporary.click("#gap-save")
                temporary.close()
            return existing

        with monkeypatch.context() as patch_context:
            patch_context.setattr(
                BrowserContext,
                "pages",
                property(pages_with_short_lived_action),
            )
            with pytest.raises(BrowserAttachError, match="popup or new tab"):
                record_interactive(
                    attach_app_url,
                    baseline_getter_recording,
                    cdp_endpoint=endpoint,
                    script=lambda _page, _pump: None,
                )
        assert baseline_getter_page_acted
        assert not (baseline_getter_recording / "meta.json").exists()
        assert process.poll() is None

        _activate_app_tab(endpoint, attach_app_url)
        late_page_recording = tmp_path / "recording-late-page-refusal"
        late_page_session = InteractiveRecorder(
            attach_app_url,
            late_page_recording,
            cdp_endpoint=endpoint,
        )
        late_page_session.start()
        assert late_page_session.page is not None
        assert late_page_session._pw is not None
        original_playwright_stop = late_page_session._pw.stop

        def stop_after_short_lived_page() -> None:
            temporary = late_page_session.page.context.new_page()
            temporary.set_content(
                "<input id='late-note'><button id='late-save'>Save</button>"
            )
            temporary.fill("#late-note", "late-activity-must-not-disappear")
            temporary.click("#late-save")
            temporary.close()
            original_playwright_stop()

        monkeypatch.setattr(late_page_session._pw, "stop", stop_after_short_lived_page)
        with pytest.raises(BrowserAttachError, match="popup or new tab"):
            late_page_session.finish()
        assert not (late_page_recording / "meta.json").exists()
        assert process.poll() is None
        with urlopen(f"{endpoint}/json/version", timeout=2) as response:
            assert response.status == 200

        _activate_app_tab(endpoint, attach_app_url)
        cleanup_frame_recording = tmp_path / "recording-cleanup-frame-refusal"
        cleanup_frame_session = InteractiveRecorder(
            attach_app_url,
            cleanup_frame_recording,
            cdp_endpoint=endpoint,
        )
        cleanup_frame_session.start()
        assert cleanup_frame_session.page is not None
        original_cleanup = cleanup_frame_session._cleanup_page_listeners
        cleanup_race_injected = False

        def cleanup_after_frame_race() -> None:
            nonlocal cleanup_race_injected
            if not cleanup_race_injected:
                cleanup_race_injected = True
                cleanup_frame_session.page.evaluate(
                    """() => {
                      const frame = document.createElement('iframe');
                      frame.srcdoc = '<input type="password" value="late">';
                      document.body.appendChild(frame);
                      frame.remove();
                    }"""
                )
                cleanup_frame_session.page.wait_for_timeout(0)
            original_cleanup()

        monkeypatch.setattr(
            cleanup_frame_session,
            "_cleanup_page_listeners",
            cleanup_after_frame_race,
        )
        with pytest.raises(BrowserAttachError, match="changed frame state"):
            cleanup_frame_session.finish()
        assert cleanup_race_injected
        assert not (cleanup_frame_recording / "meta.json").exists()
        assert process.poll() is None

        iframe_recording = tmp_path / "recording-iframe-click"

        def click_inside_existing_iframe(page, pump):
            page.frame_locator("#child").locator("#inside").click()
            pump()
            pump()

        iframe_out = record_interactive(
            attach_app_url,
            iframe_recording,
            cdp_endpoint=endpoint,
            script=click_inside_existing_iframe,
        )
        iframe_events = [
            json.loads(line)
            for line in (iframe_out / "events.jsonl").read_text().splitlines()
            if line.strip()
        ]
        assert [event["kind"] for event in iframe_events] == ["click"]
        # The same-origin frame's click is composed into page space and names
        # the frame chain replay re-enters.
        assert iframe_events[0]["structural"]["frame_path"] == ["#child"]
        # The frame document's own pre-filled secret never persists.
        frame_blob = (iframe_out / "events.jsonl").read_text() + (
            iframe_out / "meta.json"
        ).read_text()
        assert "FRAME-SECRET-NEVER-PERSIST" not in frame_blob
        assert process.poll() is None

        origin_bounce_recording = tmp_path / "recording-origin-bounce-refusal"
        other_origin = attach_app_url.replace("127.0.0.1", "localhost")

        def leave_origin_and_return(page, pump):
            page.goto(other_origin)
            page.goto(attach_app_url)
            pump()

        with pytest.raises(BrowserAttachError, match="left the declared"):
            record_interactive(
                attach_app_url,
                origin_bounce_recording,
                cdp_endpoint=endpoint,
                script=leave_origin_and_return,
            )
        assert not origin_bounce_recording.exists()
        assert process.poll() is None

        overlap_recording = tmp_path / "recording-resize-overlap-refusal"

        def resize_during_action(page, pump):
            cdp = page.context.new_cdp_session(page)
            cdp.send(
                "Emulation.setDeviceMetricsOverride",
                {
                    "width": 1000,
                    "height": 650,
                    "deviceScaleFactor": 2,
                    "mobile": False,
                },
            )
            page.click("#note")
            pump()

        with pytest.raises(BrowserAttachError, match="overlapped"):
            record_interactive(
                attach_app_url,
                overlap_recording,
                cdp_endpoint=endpoint,
                script=resize_during_action,
            )
        assert not (overlap_recording / "meta.json").exists()
        assert process.poll() is None

        resized_recording = tmp_path / "recording-resized"

        def resize_then_record(page, pump):
            page.evaluate("document.querySelector('#note').value = ''")
            page.click("#note")
            pump()
            page.keyboard.type("before-resize")
            pump()
            pump()
            cdp = page.context.new_cdp_session(page)
            cdp.send(
                "Emulation.setDeviceMetricsOverride",
                {
                    "width": 900,
                    "height": 600,
                    "deviceScaleFactor": 1,
                    "mobile": False,
                },
            )
            pump()
            page.click("#note")
            pump()
            page.keyboard.type("after-resize")
            pump()
            pump()

        recording = record_interactive(
            attach_app_url,
            resized_recording,
            cdp_endpoint=endpoint,
            script=resize_then_record,
        )
        meta = json.loads((recording / "meta.json").read_text())
        events = [
            json.loads(line)
            for line in (recording / "events.jsonl").read_text().splitlines()
        ]
        assert meta["viewport_mode"] == "per-event"
        assert meta["viewport_history"][-1]["viewport"] == [900, 600]
        assert len(meta["viewport_history"]) >= 2
        assert events
        assert {tuple(event["viewport_before"]) for event in events} == {
            (1000, 650),
            (900, 600),
        }
        assert all(
            event["viewport_before"] == event["viewport_after"] for event in events
        )
        resized_bundle = tmp_path / "bundle-resized"
        resized_workflow = compile_recording(
            recording,
            resized_bundle,
            name="attached-browser-resized",
        )
        assert resized_workflow.steps
        assert process.poll() is None
        with urlopen(f"{endpoint}/json/version", timeout=2) as response:
            assert response.status == 200
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


# ---------------------------------------------------------------------------
# Source-time secret boundary: a short keystroke prefix must not corrupt or
# abort anything. Every OTHER live secret in this file is an uppercase phrase
# that shares no character with an http://127.0.0.1:<port>/ URL, which hid
# three defects. The cases below use lowercase secrets whose first characters
# occur in the page URL, the page title, and the element identity.
# ---------------------------------------------------------------------------


def _sample_reflected_state(page: Any, session_id: str) -> dict:
    """Sample reflected evidence exactly the way the recorder does.

    ``InteractiveRecorder._read_scrubbed_page_state`` calls this same entry
    point, from Python, at a settled boundary after the page has processed the
    action. The in-page capture-phase listeners emit no URL and no title at
    all, so this is the ONLY path by which reflected text reaches a recording.
    Tests assert here for that reason: an assertion on an event field would
    test a channel that no longer reaches disk.
    """

    return page.evaluate(
        """sessionId => {
          const recorder = window.__oaflowRecorder;
          if (!recorder || recorder.sessionId !== sessionId) return null;
          return recorder.structuralState();
        }""",
        session_id,
    )


def _page_closure_init_js(session_id: str, binding_name: str, secrets: tuple) -> str:
    return (
        interactive_recorder_module._INIT_JS.replace(
            "__SESSION_ID__", json.dumps(session_id)
        )
        .replace("__BINDING_NAME__", json.dumps(binding_name))
        .replace("__SECRET_NAMES__", json.dumps(list(secrets)))
        .replace("__SECRET_MARKER__", json.dumps("data-oaflow-secret-test"))
        .replace("__IDENT_NAMES__", "[]")
        .replace("__SPECIAL_KEYS__", "[]")
    )


@pytest.mark.timeout(60)
def test_page_closure_keeps_url_and_identity_evidence_for_a_lowercase_secret() -> None:
    """A typed prefix must never rewrite the URL, the title, or the identity.

    Real Chromium types ``charlie1`` one character at a time into a declared
    secret field on ``http://host.test/hospital/charts``. The prefixes ``c``,
    ``ch``, ``cha`` and ``char`` occur in that URL, in the page title, and in
    the id of an unrelated button.
    """

    executable = _chromium_executable()
    if executable is None:
        pytest.skip("no Chromium executable is installed")
    session_id = "page-closure-lowercase-test"
    binding_name = "__oaflow_emit_lowercase_test"
    init_js = _page_closure_init_js(session_id, binding_name, ("password",))
    events: list[dict] = []
    from playwright.sync_api import sync_playwright

    secret = "charlie1"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(executable),
            headless=True,
            args=["--no-sandbox"],
        )
        try:
            page = browser.new_page()
            page.route(
                "http://host.test/**",
                lambda route: route.fulfill(
                    content_type="text/html",
                    body=(
                        "<title>Charts home</title>"
                        "<input id='password' name='password' type='password'>"
                        "<button id='chart-save'>Save chart</button>"
                    ),
                ),
            )
            page.goto("http://host.test/hospital/charts")
            page.expose_binding(
                binding_name,
                lambda _source, detail: events.append(detail),
            )
            page.evaluate(init_js)
            page.click("#password")
            page.keyboard.type(secret)
            page.click("#chart-save")
            page.wait_for_timeout(50)
            reflected_state = _sample_reflected_state(page, session_id)
        finally:
            browser.close()

    payload = json.dumps(events)
    assert secret not in payload
    assert events
    # The origin travels beside the event, so the origin guard reads a value
    # no redaction rule can touch. Every event stays on the real origin.
    assert {event["__oaflow_origin"] for event in events} == {"http://host.test"}
    # An event carries NO reflected text: the capture-phase listener runs
    # before the page's own handlers, so anything it read would be one action
    # out of date.
    assert not any("url" in event or "title" in event for event in events)
    # This page never changes its URL or its title, so both predate the secret
    # value and both stay EXACT -- including the path segment ``charts``, which
    # contains the typed prefix ``char``.
    assert reflected_state["url"] == "http://host.test/hospital/charts"
    assert reflected_state["title"] == "Charts home"
    assert reflected_state["url_withheld"] is None
    secret_inputs = [
        event
        for event in events
        if event.get("kind") == "input" and event.get("secret") is True
    ]
    assert len(secret_inputs) == len(secret)
    clicks = [event for event in events if event.get("kind") == "click"]
    click = clicks[-1]
    # The DOM identity tier stays armed: an unrelated button keeps its exact
    # id and name, and nothing is withheld.
    assert click["structural"]["selector"] == "#chart-save"
    assert click["structural"]["name"] == "Save chart"
    assert "identity_withheld" not in click["structural"]


@pytest.mark.timeout(60)
def test_page_closure_scrubs_a_cached_label_holding_another_declared_secret() -> None:
    """A cached field label must be scrubbed against EVERY declared secret.

    Discovery walks the document in order. A declared field that appears
    BEFORE another declared field caches its label while the other field
    already holds a pre-filled value.
    """

    executable = _chromium_executable()
    if executable is None:
        pytest.skip("no Chromium executable is installed")
    session_id = "page-closure-cached-label-test"
    binding_name = "__oaflow_emit_cached_label_test"
    init_js = _page_closure_init_js(
        session_id, binding_name, ("confirm-secret", "primary-secret")
    )
    events: list[dict] = []
    from playwright.sync_api import sync_playwright

    primary = "hunter2-primary-value"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(executable),
            headless=True,
            args=["--no-sandbox"],
        )
        try:
            page = browser.new_page()
            page.route(
                "http://host.test/**",
                lambda route: route.fulfill(
                    content_type="text/html",
                    body=(
                        "<title>Sign in</title>"
                        f"<label for='confirm-secret'>Confirm {primary}</label>"
                        "<input id='confirm-secret' name='confirm-secret'"
                        " type='password'>"
                        "<input id='primary-secret' name='primary-secret'"
                        f" type='password' value='{primary}'>"
                    ),
                ),
            )
            page.goto("http://host.test/sign-in")
            page.expose_binding(
                binding_name,
                lambda _source, detail: events.append(detail),
            )
            page.evaluate(init_js)
            page.click("#confirm-secret")
            page.keyboard.type("second-value")
            page.wait_for_timeout(50)
        finally:
            browser.close()

    payload = json.dumps(events)
    assert primary not in payload
    labels = {
        event.get("label")
        for event in events
        if event.get("kind") == "input" and event.get("field") == "confirm-secret"
    }
    # WITHHELD, not rewritten. The cached label holds the OTHER declared
    # field's pre-filled value, so Flow reports no label for this field rather
    # than a placeholder the page never showed.
    assert labels == {None}


def test_attached_recorder_reads_the_origin_the_page_reports(tmp_path: Path) -> None:
    """The origin guard must not parse the scrubbed URL text.

    A declared secret that shares one character with the tab URL used to
    rewrite the URL of every event, which refused the whole recording with a
    false diagnosis on the first keystroke.
    """

    session = InteractiveRecorder(
        "http://host.test/app",
        tmp_path / "recording",
        cdp_endpoint="http://127.0.0.1:9222",
    )
    selected_frame = object()
    session.page = SimpleNamespace(main_frame=selected_frame)
    session._enqueue_browser_event(
        {
            "__oaflow_session": session._session_id,
            "__oaflow_top_level": True,
            "__oaflow_viewport": [1280, 800],
            "__oaflow_dpr": 1.0,
            "__oaflow_origin": "http://host.test",
            "__oaflow_doc": "doc-1",
            "kind": "click",
            "url": "http://host.test/[secret]",
            "x": 10,
            "y": 20,
        },
        source={"page": session.page, "frame": selected_frame},
    )
    assert session._listener_error is None
    assert session.done is False
    assert len(session._pyq) == 1

    # An event without a reported origin is refused, and says exactly that.
    session._enqueue_browser_event(
        {
            "__oaflow_session": session._session_id,
            "__oaflow_top_level": True,
            "__oaflow_viewport": [1280, 800],
            "__oaflow_dpr": 1.0,
            "kind": "click",
            "url": "http://host.test/app",
            "x": 10,
            "y": 20,
        },
        source={"page": session.page, "frame": selected_frame},
    )
    assert session.done is True
    assert "did not report its document origin" in str(session._listener_error)


def test_structural_text_is_withheld_after_a_secret_leaves_its_document(
    tmp_path: Path,
) -> None:
    """A later document cannot scrub a value the previous document received."""

    session = InteractiveRecorder(
        "http://host.test/app",
        tmp_path / "recording",
        cdp_endpoint="http://127.0.0.1:9222",
    )
    selected_frame = object()
    session.page = SimpleNamespace(main_frame=selected_frame)

    def send(doc_id: str, event: dict) -> None:
        session._enqueue_browser_event(
            {
                "__oaflow_session": session._session_id,
                "__oaflow_top_level": True,
                "__oaflow_viewport": [1280, 800],
                "__oaflow_dpr": 1.0,
                "__oaflow_origin": "http://host.test",
                "__oaflow_doc": doc_id,
                **event,
            },
            source={"page": session.page, "frame": selected_frame},
        )

    page_state = {
        "url": "http://host.test/app",
        "title": "App",
        "doc": "doc-1",
        "secret": True,
        "url_withheld": None,
        "title_withheld": None,
        "dropped": [],
        "secret_in_url": False,
        "secret_in_title": False,
    }
    session.page.evaluate = lambda _js, _args: dict(page_state)

    send(
        "doc-1",
        {
            "kind": "input",
            "field": "token",
            "secret": True,
            "__oaflow_secret_mask_bound": True,
            "__oaflow_input_session": f"{session._session_id}:input:1",
        },
    )
    assert session._listener_error is None
    # An event carries no reflected text of its own. The recorder samples it at
    # the settled boundary, and the document that received the value can still
    # report text that predates the value.
    assert "url" not in session._pyq[-1]
    assert session._read_scrubbed_page_state() == {
        "url": "http://host.test/app",
        "title": "App",
    }
    assert session._structural_text_withheld is False

    # A same-origin GET form submit builds a NEW document. Its closure never
    # saw the value, and it cannot prove the URL it loaded with predates that
    # value: a server that answers the submit with a redirect to
    # `/results/<value>` puts the value in the PATH, which no parameter name
    # identifies. Structure protects the query channel, not that one, so the
    # whole URL and the title are withheld.
    page_state.update(
        {
            "url": "http://host.test/results?token=",
            "title": "Done",
            "doc": "doc-2",
            "secret": False,
            "dropped": [
                {
                    "name": "token",
                    "where": "query",
                    "reason": "declared-secret-parameter",
                }
            ],
        }
    )
    send("doc-2", {"kind": "click"})
    assert session._listener_error is None
    assert session._read_scrubbed_page_state() == {
        "url": "http://host.test/",
        "title": "",
    }
    assert session._structural_text_withheld is True
    assert session._structural_text_withheld_reasons == {
        "secret-value-left-its-document"
    }
    # A drop is recorded only for a URL Flow reports. This one was withheld
    # whole, so nothing is claimed about its parameters.
    assert session._dropped_url_parameters == set()


def test_recording_privacy_notices_report_what_flow_withheld(tmp_path: Path) -> None:
    """The operator decides on the recording from exactly these lines."""

    from openadapt_flow.__main__ import _recording_privacy_notices

    recording = tmp_path / "recording"
    recording.mkdir()
    assert _recording_privacy_notices(recording) == []
    (recording / "meta.json").write_text(
        json.dumps({"surface": "web", "identity_withheld_events": 2})
    )
    (notice,) = _recording_privacy_notices(recording)
    assert "no DOM selector" in notice
    (recording / "meta.json").write_text(
        json.dumps(
            {
                "surface": "web",
                "structural_text_withheld": "secret-value-left-its-document",
            }
        )
    )
    (notice,) = _recording_privacy_notices(recording)
    assert "withheld the page URL and title" in notice


def test_stamping_a_recorded_surface_does_not_rewrite_a_published_recording(
    tmp_path: Path,
) -> None:
    """The recorder stamps the surface, so the publish step writes nothing."""

    from openadapt_flow.__main__ import _stamp_recording_surface

    recording = tmp_path / "recording"
    recording.mkdir()
    meta_path = recording / "meta.json"
    meta_path.write_text(json.dumps({"surface": "web", "source": "test"}))
    before = meta_path.read_bytes()
    _stamp_recording_surface(recording, "web")
    assert meta_path.read_bytes() == before
    _stamp_recording_surface(recording, "windows")
    assert json.loads(meta_path.read_text())["surface"] == "windows"


@pytest.mark.timeout(120)
def test_launched_recording_withholds_a_later_document_url_after_a_get_submit(
    attach_app_url: str,
    tmp_path: Path,
) -> None:
    """A same-origin GET submit builds a NEW document, and it is withheld.

    Structure closes the QUERY channel: the parameter keeps its name and loses
    its value. It does not close the PATH channel, because no parameter name
    identifies a path segment, and a server that answers the submit with a
    redirect to `/results/<value>` uses exactly that. A fresh closure holds no
    value to match it against either. So a document that comes after the one
    that first held a declared value reports an origin-only URL and an empty
    title. Flow stamps the surface before it publishes and says what it
    withheld.
    """

    if _chromium_executable() is None:
        pytest.skip("no Chromium executable is installed")
    secret = "hunter2-token-value"

    def drive(page, pump):
        page.click("#token")
        pump()
        page.keyboard.type(secret)
        pump()
        pump()
        page.click("#submit-token")
        pump()
        pump()

    recording = record_interactive(
        f"{attach_app_url}get-form",
        tmp_path / "recording-get-form",
        secret_fields=("token",),
        headless=True,
        script=drive,
    )
    body = "\n".join(
        path.read_text(errors="replace") for path in sorted(recording.glob("*.json*"))
    )
    assert secret not in body
    assert f"token={secret}" not in body
    events = [
        json.loads(line)
        for line in (recording / "events.jsonl").read_text().splitlines()
    ]
    submit = events[-1]
    # The document reached by the submit reports an origin-only URL and an
    # empty title. Nothing about the results document survives.
    assert submit["url_after"] == attach_app_url
    assert submit["title_after"] == ""
    meta = json.loads((recording / "meta.json").read_text())
    # Stamped before the atomic publish, not mutated afterwards.
    assert meta["surface"] == "web"
    assert meta["structural_text_withheld"] == "secret-value-left-its-document"
    # A drop is recorded only for a URL Flow actually reports. This URL was
    # withheld whole, so naming a dropped parameter would say less than
    # nothing.
    assert "url_dropped_params" not in meta
    from openadapt_flow.__main__ import _recording_privacy_notices

    notices = _recording_privacy_notices(recording)
    assert any("withheld the page URL and title" in n for n in notices)


@pytest.mark.timeout(60)
def test_page_closure_keeps_evidence_when_a_secret_input_swaps_its_node() -> None:
    """A controlled input that swaps its node per keystroke leaves prefixes.

    Each removed node keeps the value it held. Those are keystroke prefixes of
    the value the page still holds, and treating them as declared values would
    withhold unrelated evidence on a chance match. The complete value stays
    redacted.
    """

    executable = _chromium_executable()
    if executable is None:
        pytest.skip("no Chromium executable is installed")
    session_id = "page-closure-swap-test"
    binding_name = "__oaflow_emit_swap_test"
    init_js = _page_closure_init_js(session_id, binding_name, ("swap-secret",))
    events: list[dict] = []
    from playwright.sync_api import sync_playwright

    secret = "charlie1"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(executable),
            headless=True,
            args=["--no-sandbox"],
        )
        try:
            page = browser.new_page()
            page.route(
                "http://host.test/**",
                lambda route: route.fulfill(
                    content_type="text/html",
                    body=(
                        "<title>Charts home</title>"
                        "<input id='swap-secret' name='swap-secret'"
                        " type='password' oninput=\""
                        "const next = document.createElement('input');"
                        "next.name = 'swap-secret';"
                        "next.type = 'password';"
                        "next.setAttribute('oninput',"
                        " this.getAttribute('oninput'));"
                        "next.value = this.value;"
                        "this.replaceWith(next);"
                        "next.focus();"
                        '">'
                        "<button id='chart-save'>Save chart</button>"
                    ),
                ),
            )
            page.goto("http://host.test/hospital/charts")
            page.expose_binding(
                binding_name,
                lambda _source, detail: events.append(detail),
            )
            page.evaluate(init_js)
            page.click("[name='swap-secret']")
            page.keyboard.type(secret)
            page.click("#chart-save")
            page.wait_for_timeout(50)
            reflected_state = _sample_reflected_state(page, session_id)
        finally:
            browser.close()

    payload = json.dumps(events)
    assert secret not in payload
    clicks = [event for event in events if event.get("kind") == "click"]
    click = clicks[-1]
    assert click["structural"]["selector"] == "#chart-save"
    assert click["structural"]["name"] == "Save chart"
    assert "identity_withheld" not in click["structural"]
    # Only a CONNECTED node's value is ever matched, so the prefixes the
    # detached nodes still hold cannot withhold unrelated evidence. This page
    # never changes its URL, so the URL predates the value and stays exact.
    assert reflected_state["url"] == "http://host.test/hospital/charts"
    assert reflected_state["url_withheld"] is None


@pytest.mark.timeout(60)
def test_page_closure_withholds_reflected_text_the_field_no_longer_matches() -> None:
    """The shorter value left in the field must never reach reflected text.

    The operator types a value, leaves the field, returns and deletes one
    character. The field now holds a SHORTER value, and the page still shows
    the longer one in its URL and title until the next keystroke reflects
    again. No rule that reads only the current DOM can scrub that, so Flow
    withholds the reflected text and reports why. Reproduction (a).
    """

    executable = _chromium_executable()
    if executable is None:
        pytest.skip("no Chromium executable is installed")
    session_id = "page-closure-backspace-test"
    binding_name = "__oaflow_emit_backspace_test"
    init_js = _page_closure_init_js(session_id, binding_name, ("token",))
    events: list[dict] = []
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(executable),
            headless=True,
            args=["--no-sandbox"],
        )
        try:
            page = browser.new_page()
            page.route(
                "http://host.test/**",
                lambda route: route.fulfill(
                    content_type="text/html",
                    body=(
                        "<title>session</title>"
                        "<input id='token' name='token' type='password'"
                        ' oninput="'
                        "document.title = 'session for ' + this.value;"
                        "history.replaceState({}, '', '/charts/' + this.value);"
                        '">'
                        "<button id='chart-save'>Save chart</button>"
                    ),
                ),
            )
            page.goto("http://host.test/charts")
            page.expose_binding(
                binding_name,
                lambda _source, detail: events.append(detail),
            )
            page.evaluate(init_js)
            page.click("#token")
            page.keyboard.type("hunter2")
            page.click("#chart-save")
            page.click("#token")
            page.keyboard.press("Backspace")
            page.click("#chart-save")
            page.wait_for_timeout(50)
            reflected_state = _sample_reflected_state(page, session_id)
            raw = page.evaluate("() => [location.href, document.title]")
        finally:
            browser.close()

    payload = json.dumps(events)
    # `hunter` is what the field holds now; `hunter2` is what it held before.
    # Neither may reach an event or the sampled reflected state, in whole or in
    # part.
    assert "hunter" not in payload
    assert "hunter" not in json.dumps(reflected_state)
    # The page really does still show the value: this is a live leak the
    # withholding rule is closing, not a page that never reflected.
    assert "hunter" in raw[0] and "hunter" in raw[1]
    assert reflected_state["url"] == "http://host.test/"
    assert reflected_state["title"] == ""
    assert reflected_state["url_withheld"] == "declared-value-in-url"
    # Identity evidence is unaffected: the button holds no declared value, so
    # it stays exact.
    click = [event for event in events if event.get("kind") == "click"][-1]
    assert click["structural"]["selector"] == "#chart-save"


@pytest.mark.timeout(60)
def test_page_closure_keeps_evidence_when_an_unnamed_password_swaps_its_node() -> None:
    """A password field with no name and no ID still needs a stable key.

    Discovery derives a NEW input session for each replacement, so without an
    inherited session every keystroke looks like a new declared field, no
    prefix is ever recognised, and every later scrub becomes ambiguous.
    """

    executable = _chromium_executable()
    if executable is None:
        pytest.skip("no Chromium executable is installed")
    session_id = "page-closure-unnamed-swap-test"
    binding_name = "__oaflow_emit_unnamed_swap_test"
    init_js = _page_closure_init_js(session_id, binding_name, ())
    events: list[dict] = []
    from playwright.sync_api import sync_playwright

    secret = "charlie1"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(executable),
            headless=True,
            args=["--no-sandbox"],
        )
        try:
            page = browser.new_page()
            page.route(
                "http://host.test/**",
                lambda route: route.fulfill(
                    content_type="text/html",
                    body=(
                        "<title>Charts home</title>"
                        "<input type='password' oninput=\""
                        "const next = document.createElement('input');"
                        "next.type = 'password';"
                        "next.setAttribute('oninput',"
                        " this.getAttribute('oninput'));"
                        "next.value = this.value;"
                        "this.replaceWith(next);"
                        "next.focus();"
                        '">'
                        "<button id='chart-save'>Save chart</button>"
                    ),
                ),
            )
            page.goto("http://host.test/hospital/charts")
            page.expose_binding(
                binding_name,
                lambda _source, detail: events.append(detail),
            )
            page.evaluate(init_js)
            page.click("input[type=password]")
            page.keyboard.type(secret)
            page.click("#chart-save")
            page.wait_for_timeout(50)
            reflected_state = _sample_reflected_state(page, session_id)
        finally:
            browser.close()

    payload = json.dumps(events)
    assert secret not in payload
    click = [event for event in events if event.get("kind") == "click"][-1]
    assert click["structural"]["selector"] == "#chart-save"
    assert click["structural"]["name"] == "Save chart"
    assert "identity_withheld" not in click["structural"]
    assert reflected_state["url"] == "http://host.test/hospital/charts"
    assert reflected_state["url_withheld"] is None


@pytest.mark.timeout(60)
def test_page_closure_marks_a_withheld_name_and_row_identity() -> None:
    """A withheld accessible name or row identity must be visible too.

    A withheld selector already carries its reason. A name or a row identity
    that Flow withholds disarms the same identity check, so it must carry one
    as well, and the recorder must count the action.
    """

    executable = _chromium_executable()
    if executable is None:
        pytest.skip("no Chromium executable is installed")
    session_id = "page-closure-withheld-name-test"
    binding_name = "__oaflow_emit_withheld_name_test"
    init_js = _page_closure_init_js(session_id, binding_name, ("token",))
    events: list[dict] = []
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(executable),
            headless=True,
            args=["--no-sandbox"],
        )
        try:
            page = browser.new_page()
            page.route(
                "http://host.test/**",
                lambda route: route.fulfill(
                    content_type="text/html",
                    body=(
                        "<title>Charts</title>"
                        "<input id='token' name='token' type='password'>"
                        "<ul><li>Alice Example row<button>Save chart</button>"
                        "</li></ul>"
                    ),
                ),
            )
            page.goto("http://host.test/list")
            page.expose_binding(
                binding_name,
                lambda _source, detail: events.append(detail),
            )
            page.evaluate(init_js)
            page.click("#token")
            # One character. It is too short to tell a reflection from a
            # coincidence, so every text that contains it is withheld.
            page.keyboard.type("a")
            page.click("button")
            page.wait_for_timeout(50)
        finally:
            browser.close()

    click = [event for event in events if event.get("kind") == "click"][-1]
    assert click["structural"]["selector"] is None
    assert click["structural"]["name"] is None
    assert click["structural"]["identity_withheld"] == "ambiguous-secret-in-identity"
    assert click["sid"] is None
    assert click["sid_withheld"] == "ambiguous-secret-in-identity"


def test_withheld_row_identity_is_counted_for_the_operator(tmp_path: Path) -> None:
    """A withheld row identity counts as a withheld identity, like a selector."""

    session = InteractiveRecorder(
        "http://host.test/app",
        tmp_path / "recording",
        cdp_endpoint="http://127.0.0.1:9222",
    )
    selected_frame = object()
    session.page = SimpleNamespace(main_frame=selected_frame)
    session._enqueue_browser_event(
        {
            "__oaflow_session": session._session_id,
            "__oaflow_top_level": True,
            "__oaflow_viewport": [1280, 800],
            "__oaflow_dpr": 1.0,
            "__oaflow_origin": "http://host.test",
            "__oaflow_doc": "doc-1",
            "kind": "click",
            "url": "http://host.test/app",
            "sid": None,
            "sid_withheld": "ambiguous-secret-in-identity",
            "structural": {"selector": None, "role": "button", "name": None},
            "x": 10,
            "y": 20,
        },
        source={"page": session.page, "frame": selected_frame},
    )
    assert session._listener_error is None
    assert session._identity_withheld_events == 1


# ---------------------------------------------------------------------------
# Regression tests for the redaction redesign (fourth review round).
#
# Three earlier revisions tried to remove a REMEMBERED value from text that was
# already captured. Three independent reviews each found a different defect in
# the retention rule that approach needs. The tests below pin the rules that
# replaced it: match only what a bound element holds right now, report identity
# evidence exactly or withhold it, and sample reflected text at the settled
# boundary. Each test states the defect it closes and fails on commit 7acd716.
# ---------------------------------------------------------------------------


@pytest.mark.timeout(60)
def test_page_closure_keeps_all_evidence_for_a_password_starting_with_a_word() -> None:
    """A password that begins with a common word rewrites nothing. Finding 1.

    The operator types ``invoice-2026-quarterly-passphrase`` on a page whose
    URL, title, clicked-row identity and button id all contain the word
    ``invoice``. While the field held exactly ``invoice`` that word was a live
    declared value; the retention rule then kept it for the rest of the
    recording, and every one of those five pieces of evidence was rewritten or
    nulled. Nothing is retained now, so at click time the only declared value
    is the complete passphrase, which none of that evidence contains.
    """

    executable = _chromium_executable()
    if executable is None:
        pytest.skip("no Chromium executable is installed")
    session_id = "page-closure-common-word-test"
    binding_name = "__oaflow_emit_common_word_test"
    init_js = _page_closure_init_js(session_id, binding_name, ("password",))
    events: list[dict] = []
    from playwright.sync_api import sync_playwright

    secret = "invoice-2026-quarterly-passphrase"
    row_identity = "MRN 44120 invoice Alice Example"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(executable),
            headless=True,
            args=["--no-sandbox"],
        )
        try:
            page = browser.new_page()
            page.route(
                "http://host.test/**",
                lambda route: route.fulfill(
                    content_type="text/html",
                    body=(
                        "<title>invoice queue 2026</title>"
                        "<input id='password' name='password' type='password'>"
                        f"<ul><li>{row_identity}"
                        "<button id='invoice-submit'>Post invoice</button>"
                        "</li></ul>"
                    ),
                ),
            )
            page.goto("http://host.test/invoices/2026-queue")
            page.expose_binding(
                binding_name,
                lambda _source, detail: events.append(detail),
            )
            page.evaluate(init_js)
            page.click("#password")
            page.keyboard.type(secret)
            page.click("#invoice-submit")
            page.wait_for_timeout(50)
            reflected_state = _sample_reflected_state(page, session_id)
        finally:
            browser.close()

    payload = json.dumps(events)
    assert secret not in payload
    click = [event for event in events if event.get("kind") == "click"][-1]
    # IDENTITY / MACHINE EVIDENCE, exact. Replay re-reads the page and compares
    # against these, so each one must be what the page held.
    assert click["sid"] == row_identity
    assert "sid_withheld" not in click
    assert click["structural"]["selector"] == "#invoice-submit"
    assert click["structural"]["name"] == "Post invoice"
    assert "identity_withheld" not in click["structural"]
    # REFLECTED / CONTEXT EVIDENCE, exact: neither changed after the field held
    # a value, so neither can be a reflection of it.
    assert reflected_state["url"] == "http://host.test/invoices/2026-queue"
    assert reflected_state["title"] == "invoice queue 2026"
    assert reflected_state["url_withheld"] is None


@pytest.mark.timeout(60)
def test_page_closure_withholds_a_stale_reflection_from_a_swapping_input() -> None:
    """A node swap combined with an as-you-type reflection. The second P1.

    The page replaces its input element on every keystroke AND writes the value
    into the URL and the title -- but stops reflecting past eight characters,
    so what it shows at the end is a PREFIX the field no longer holds. The
    retention rule treated that prefix as a droppable keystroke artifact of the
    replaced node, so nothing redacted it and it reached the recording. Flow
    now proves reflected text safe only while it has not changed since before
    the field held a value, so this text is withheld whole.
    """

    executable = _chromium_executable()
    if executable is None:
        pytest.skip("no Chromium executable is installed")
    session_id = "page-closure-stale-swap-test"
    binding_name = "__oaflow_emit_stale_swap_test"
    init_js = _page_closure_init_js(session_id, binding_name, ("swap-secret",))
    events: list[dict] = []
    from playwright.sync_api import sync_playwright

    secret = "charlie-alpha"
    stale_prefix = "charlie-"
    reflect = (
        "if (this.value.length <= 8) {"
        "document.title = 'session for ' + this.value;"
        "history.replaceState({}, '', '/charts/' + this.value);"
        "}"
        "const next = document.createElement('input');"
        "next.name = 'swap-secret';"
        "next.type = 'password';"
        "next.setAttribute('oninput', this.getAttribute('oninput'));"
        "next.value = this.value;"
        "this.replaceWith(next);"
        "next.focus();"
    )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(executable),
            headless=True,
            args=["--no-sandbox"],
        )
        try:
            page = browser.new_page()
            page.route(
                "http://host.test/**",
                lambda route: route.fulfill(
                    content_type="text/html",
                    body=(
                        "<title>session</title>"
                        "<input id='swap-secret' name='swap-secret'"
                        f" type='password' oninput=\"{reflect}\">"
                        "<button id='chart-save'>Save chart</button>"
                    ),
                ),
            )
            page.goto("http://host.test/charts")
            page.expose_binding(
                binding_name,
                lambda _source, detail: events.append(detail),
            )
            page.evaluate(init_js)
            page.click("[name='swap-secret']")
            page.keyboard.type(secret)
            page.click("#chart-save")
            page.wait_for_timeout(50)
            reflected_state = _sample_reflected_state(page, session_id)
            raw = page.evaluate("() => [location.href, document.title]")
        finally:
            browser.close()

    # The page really is showing a value the field no longer holds. Without
    # this the test would pass on a page that simply never reflected.
    assert raw == [
        f"http://host.test/charts/{stale_prefix}",
        f"session for {stale_prefix}",
    ]
    payload = json.dumps(events)
    assert secret not in payload
    assert stale_prefix not in payload
    assert stale_prefix not in json.dumps(reflected_state)
    assert reflected_state["url"] == "http://host.test/"
    assert reflected_state["title"] == ""
    assert reflected_state["url_withheld"] == "declared-value-in-url"
    # Identity evidence is untouched by the withholding: the button holds no
    # declared value, so replay keeps its strongest identity tier.
    click = [event for event in events if event.get("kind") == "click"][-1]
    assert click["structural"]["selector"] == "#chart-save"
    assert click["structural"]["name"] == "Save chart"
    assert "identity_withheld" not in click["structural"]


@pytest.mark.timeout(60)
def test_page_closure_withholds_identity_that_holds_a_declared_value() -> None:
    """Identity evidence is EXACT or WITHHELD-AND-MARKED. Never rewritten.

    The page copies the typed value into a row. The old rule rewrote the row
    identity to ``Ticket [secret] owner`` and reported nothing, so replay would
    have compared against characters the page never showed and no downstream
    check could see the substitution. Flow withholds the field and marks it.
    """

    executable = _chromium_executable()
    if executable is None:
        pytest.skip("no Chromium executable is installed")
    session_id = "page-closure-identity-rewrite-test"
    binding_name = "__oaflow_emit_identity_rewrite_test"
    init_js = _page_closure_init_js(session_id, binding_name, ("token",))
    events: list[dict] = []
    from playwright.sync_api import sync_playwright

    secret = "alpha-charlie-9"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(executable),
            headless=True,
            args=["--no-sandbox"],
        )
        try:
            page = browser.new_page()
            page.route(
                "http://host.test/**",
                lambda route: route.fulfill(
                    content_type="text/html",
                    body=(
                        "<title>Tickets</title>"
                        "<input id='token' name='token' type='password'"
                        ' oninput="'
                        "document.getElementById('label').textContent ="
                        " 'Ticket ' + this.value + ' owner';"
                        '">'
                        "<ul><li><span id='label'>Ticket owner</span>"
                        "<button id='save'>Save</button></li></ul>"
                    ),
                ),
            )
            page.goto("http://host.test/tickets")
            page.expose_binding(
                binding_name,
                lambda _source, detail: events.append(detail),
            )
            page.evaluate(init_js)
            page.click("#token")
            page.keyboard.type(secret)
            page.click("#save")
            page.wait_for_timeout(50)
        finally:
            browser.close()

    payload = json.dumps(events)
    assert secret not in payload
    # No rewritten copy of anything exists: Flow writes no placeholder into
    # captured text, so the placeholder string appears nowhere at all.
    assert "[secret]" not in payload
    click = [event for event in events if event.get("kind") == "click"][-1]
    assert click["sid"] is None
    assert click["sid_withheld"] == "secret-value-in-identity"
    # The button itself holds no declared value, so its identity is untouched.
    assert click["structural"]["selector"] == "#save"


@pytest.mark.timeout(60)
def test_page_closure_emits_no_reflected_text_from_the_capture_phase() -> None:
    """An event carries no URL and no title, because it cannot carry a true one.

    The in-page listeners run in the CAPTURE phase, before the page's own
    handlers. Any URL or title read there belongs to the state BEFORE the
    action, so an event that carried one described the previous screen. That
    staleness was the only reason the closure kept a value history at all.
    Flow samples reflected text from Python at the settled boundary instead.
    """

    executable = _chromium_executable()
    if executable is None:
        pytest.skip("no Chromium executable is installed")
    session_id = "page-closure-no-event-text-test"
    binding_name = "__oaflow_emit_no_event_text_test"
    init_js = _page_closure_init_js(session_id, binding_name, ())
    events: list[dict] = []
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(executable),
            headless=True,
            args=["--no-sandbox"],
        )
        try:
            page = browser.new_page()
            page.route(
                "http://host.test/**",
                lambda route: route.fulfill(
                    content_type="text/html",
                    body=(
                        "<title>step one</title>"
                        "<button id='next' onclick=\""
                        "document.title = 'step two';"
                        "history.replaceState({}, '', '/two');"
                        '">Next</button>'
                    ),
                ),
            )
            page.goto("http://host.test/one")
            page.expose_binding(
                binding_name,
                lambda _source, detail: events.append(detail),
            )
            page.evaluate(init_js)
            page.click("#next")
            page.wait_for_timeout(50)
            reflected_state = _sample_reflected_state(page, session_id)
        finally:
            browser.close()

    assert events
    for event in events:
        assert "url" not in event
        assert "title" not in event
    # Sampled after the page processed the click, so it is the screen the click
    # produced -- not the one it left. A capture-phase read would say "/one"
    # and "step one" here.
    assert reflected_state["url"] == "http://host.test/two"
    assert reflected_state["title"] == "step two"
    assert reflected_state["url_withheld"] is None


@pytest.mark.timeout(60)
def test_page_closure_withholds_after_a_shorter_value_replaces_a_reflection() -> None:
    """Reproduction (b): blur, clear, retype something shorter.

    The page reflects the first value, the operator leaves the field, the page
    clears it, and the operator types a shorter value that the page does not
    reflect. The URL still shows the first value, and nothing in the DOM can
    identify it any more.
    """

    executable = _chromium_executable()
    if executable is None:
        pytest.skip("no Chromium executable is installed")
    session_id = "page-closure-cleared-field-test"
    binding_name = "__oaflow_emit_cleared_field_test"
    init_js = _page_closure_init_js(session_id, binding_name, ("token",))
    events: list[dict] = []
    from playwright.sync_api import sync_playwright

    first = "hunter2-primary"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(executable),
            headless=True,
            args=["--no-sandbox"],
        )
        try:
            page = browser.new_page()
            page.route(
                "http://host.test/**",
                lambda route: route.fulfill(
                    content_type="text/html",
                    body=(
                        "<title>session</title>"
                        "<input id='token' name='token' type='password'"
                        ' oninput="'
                        "if (!this.dataset.done) {"
                        "history.replaceState({}, '', '/charts/' + this.value);"
                        "}"
                        '">'
                        "<button id='chart-save'>Save chart</button>"
                    ),
                ),
            )
            page.goto("http://host.test/charts")
            page.expose_binding(
                binding_name,
                lambda _source, detail: events.append(detail),
            )
            page.evaluate(init_js)
            page.click("#token")
            page.keyboard.type(first)
            page.click("#chart-save")
            # The page clears the field and stops reflecting.
            page.evaluate(
                """() => {
                  const el = document.getElementById('token');
                  el.dataset.done = '1';
                  el.value = '';
                }"""
            )
            page.click("#token")
            page.keyboard.type("abc")
            page.click("#chart-save")
            page.wait_for_timeout(50)
            reflected_state = _sample_reflected_state(page, session_id)
            raw_url = page.evaluate("() => location.href")
        finally:
            browser.close()

    assert raw_url == f"http://host.test/charts/{first}"
    payload = json.dumps(events)
    assert first not in payload
    assert first not in json.dumps(reflected_state)
    assert reflected_state["url"] == "http://host.test/"
    assert reflected_state["url_withheld"] == "declared-value-in-url"


@pytest.mark.timeout(60)
def test_page_closure_withholds_a_reflection_the_field_extended_past() -> None:
    """Reproduction (c): ``alpha-one`` reflected, then extended to
    ``alpha-one-two``.

    The URL keeps the first value. Matching the current value against it finds
    nothing, because the current value only CONTAINS the reflected one -- it is
    not equal to it and does not appear in the URL.
    """

    executable = _chromium_executable()
    if executable is None:
        pytest.skip("no Chromium executable is installed")
    session_id = "page-closure-extended-value-test"
    binding_name = "__oaflow_emit_extended_value_test"
    init_js = _page_closure_init_js(session_id, binding_name, ("token",))
    events: list[dict] = []
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(executable),
            headless=True,
            args=["--no-sandbox"],
        )
        try:
            page = browser.new_page()
            page.route(
                "http://host.test/**",
                lambda route: route.fulfill(
                    content_type="text/html",
                    body=(
                        "<title>session</title>"
                        "<input id='token' name='token' type='password'"
                        ' oninput="'
                        "if (this.value.length <= 9) {"
                        "history.replaceState({}, '', '/charts/' + this.value);"
                        "}"
                        '">'
                        "<button id='chart-save'>Save chart</button>"
                    ),
                ),
            )
            page.goto("http://host.test/charts")
            page.expose_binding(
                binding_name,
                lambda _source, detail: events.append(detail),
            )
            page.evaluate(init_js)
            page.click("#token")
            page.keyboard.type("alpha-one-two")
            page.click("#chart-save")
            page.wait_for_timeout(50)
            reflected_state = _sample_reflected_state(page, session_id)
            raw_url = page.evaluate("() => location.href")
        finally:
            browser.close()

    assert raw_url == "http://host.test/charts/alpha-one"
    payload = json.dumps(events)
    assert "alpha-one" not in payload
    assert "alpha-one" not in json.dumps(reflected_state)
    assert reflected_state["url"] == "http://host.test/"
    assert reflected_state["url_withheld"] == "declared-value-in-url"


def test_withheld_reflected_text_names_every_reason_for_the_operator(
    tmp_path: Path,
) -> None:
    """One recording can withhold reflected text for more than one reason."""

    from openadapt_flow.__main__ import _recording_privacy_notices

    recording = tmp_path / "recording"
    recording.mkdir()
    (recording / "meta.json").write_text(
        json.dumps(
            {
                "surface": "web",
                "structural_text_withheld": (
                    "reflected-text-changed-after-a-secret-value,"
                    "secret-value-left-its-document"
                ),
            }
        )
    )
    notices = _recording_privacy_notices(recording)
    assert len(notices) == 2
    assert any(
        "changed after a declared secret field held a value" in n for n in notices
    )
    assert any("left its document" in n for n in notices)
    # Flow never rewrites captured text, and the operator is told so.
    assert all("never rewrites it" in notice for notice in notices)


def test_reflected_text_withheld_reasons_reach_the_recording_metadata(
    tmp_path: Path,
) -> None:
    """Every distinct reason Flow withheld reflected text reaches meta.json."""

    session = InteractiveRecorder(
        "http://host.test/app",
        tmp_path / "recording",
        cdp_endpoint="http://127.0.0.1:9222",
    )
    page_state = {
        "url": "http://host.test/",
        "title": "",
        "doc": "doc-1",
        "secret": True,
        "url_withheld": "declared-value-in-url",
        "title_withheld": "declared-value-in-title",
        "dropped": [],
        "secret_in_url": True,
        "secret_in_title": True,
    }
    session.page = SimpleNamespace(
        main_frame=object(),
        evaluate=lambda _js, _args: dict(page_state),
    )
    # The page withheld both: the application put the value into its own URL
    # and its own title, where structure could not remove it.
    assert session._read_scrubbed_page_state() == {
        "url": "http://host.test/",
        "title": "",
    }
    assert session._app_placed_secret_in_url is True
    assert session._app_placed_secret_in_title is True
    # A later document adds the cross-document reason.
    page_state.update(
        {
            "doc": "doc-2",
            "secret": False,
            "url_withheld": None,
            "title_withheld": None,
            "secret_in_url": False,
            "secret_in_title": False,
            "url": "http://host.test/next",
        }
    )
    assert session._read_scrubbed_page_state() == {
        "url": "http://host.test/",
        "title": "",
    }
    assert session._structural_text_withheld_reasons == {
        "declared-value-in-url",
        "declared-value-in-title",
        "secret-value-left-its-document",
    }


@pytest.mark.timeout(60)
def test_page_closure_warns_when_the_application_puts_a_secret_in_its_url() -> None:
    """A value structure cannot name is caught by the net, and reported.

    The operator opens a URL that already carries the value and then types it
    into a declared field. Nothing about the URL changed, so the baseline rule
    reports it. The net catches it anyway: the URL Flow is about to report
    holds a value a bound field is holding right now. Flow withholds the whole
    URL and tells the operator, because an application that puts a secret in a
    URL has a defect that exists with or without Flow -- OWASP lists browser
    history, server logs, proxies and the Referer header as places it is
    already exposed.
    """

    executable = _chromium_executable()
    if executable is None:
        pytest.skip("no Chromium executable is installed")
    session_id = "page-closure-secret-in-url-test"
    binding_name = "__oaflow_emit_secret_in_url_test"
    init_js = _page_closure_init_js(session_id, binding_name, ("token",))
    from playwright.sync_api import sync_playwright

    secret = "quarterly-passphrase-9"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(executable),
            headless=True,
            args=["--no-sandbox"],
        )
        try:
            page = browser.new_page()
            page.route(
                "http://host.test/**",
                lambda route: route.fulfill(
                    content_type="text/html",
                    body=(
                        "<title>session</title>"
                        "<input id='token' name='token' type='password'>"
                        "<button id='chart-save'>Save chart</button>"
                    ),
                ),
            )
            # NOT a declared parameter name, so structure alone cannot find it.
            page.goto(f"http://host.test/charts?ref={secret}")
            page.expose_binding(binding_name, lambda _source, _detail: None)
            page.evaluate(init_js)
            before = _sample_reflected_state(page, session_id)
            page.click("#token")
            page.keyboard.type(secret)
            page.click("#chart-save")
            page.wait_for_timeout(50)
            after = _sample_reflected_state(page, session_id)
        finally:
            browser.close()

    # Before anything is typed, nothing declared holds a value, so this URL is
    # ordinary page context and is reported.
    assert before["url"] == f"http://host.test/charts?ref={secret}"
    assert before["url_withheld"] is None
    # Once a bound field holds it, the net fires and the operator is warned.
    assert secret not in json.dumps(after)
    assert after["url"] == "http://host.test/"
    assert after["url_withheld"] == "declared-value-in-url"
    assert after["secret_in_url"] is True


@pytest.mark.timeout(60)
def test_page_closure_states_its_debounce_limit() -> None:
    """The stated residual, pinned so a change to it has to be deliberate.

    The net matches the value a bound field holds NOW against the text the page
    shows NOW. An application that updates its URL on a timer longer than the
    settle window still shows an earlier value at the moment Flow samples, and
    the net will not match it. Flow does NOT keep a previous value to catch
    that: the rule that kept previous values is the one three reviews broke.

    Here the page reflects only the first eight characters and then stops, and
    the field goes on to hold more. The PATH net catches this particular shape
    -- the value Flow can see contains that path segment -- so the URL is
    withheld. What is NOT caught, and is written down in
    docs/BROWSER_RECORDING.md, is a reflection that no value Flow can see
    contains: an application-defined transform of the value.
    """

    executable = _chromium_executable()
    if executable is None:
        pytest.skip("no Chromium executable is installed")
    session_id = "page-closure-transform-limit-test"
    binding_name = "__oaflow_emit_transform_limit_test"
    init_js = _page_closure_init_js(session_id, binding_name, ("token",))
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(executable),
            headless=True,
            args=["--no-sandbox"],
        )
        try:
            page = browser.new_page()
            page.route(
                "http://host.test/**",
                lambda route: route.fulfill(
                    content_type="text/html",
                    body=(
                        "<title>session</title>"
                        "<input id='token' name='token' type='password'"
                        ' oninput="'
                        # An application-defined TRANSFORM: the page reflects
                        # the value REVERSED, so no value Flow can see contains
                        # the text on show.
                        "history.replaceState({}, '',"
                        " '/charts/' + this.value.split('').reverse().join(''));"
                        '">'
                        "<button id='chart-save'>Save chart</button>"
                    ),
                ),
            )
            page.goto("http://host.test/charts")
            page.expose_binding(binding_name, lambda _source, _detail: None)
            page.evaluate(init_js)
            page.click("#token")
            page.keyboard.type("quarterly-passphrase-9")
            page.click("#chart-save")
            page.wait_for_timeout(50)
            state = _sample_reflected_state(page, session_id)
            raw_url = page.evaluate("() => location.href")
        finally:
            browser.close()

    # The page really is showing a transform of the value.
    assert raw_url == "http://host.test/charts/9-esarhpssap-ylretrauq"
    # Flow reports it: no value it can see contains that text, in either
    # direction. This is the declared limit, not an oversight.
    assert state["url"] == raw_url
    assert state["url_withheld"] is None


# ---------------------------------------------------------------------------
# Round-4 blockers and the structured-URL redesign.
#
# Every test below FAILS at ff5d71e and passes here.
# ---------------------------------------------------------------------------


def _closure_page(playwright, executable, body: str, url: str, session_id: str):
    browser = playwright.chromium.launch(
        executable_path=str(executable), headless=True, args=["--no-sandbox"]
    )
    page = browser.new_page()
    page.route(
        "http://host.test/**",
        lambda route: route.fulfill(content_type="text/html", body=body),
    )
    page.goto(url)
    return browser, page


@pytest.mark.timeout(60)
def test_page_closure_withholds_identity_after_the_field_is_removed() -> None:
    """Blocker P1-B. An SPA wizard removes the field and shows the value.

    The operator types into a declared field, the page replaces the form with a
    summary row holding that value, and the operator clicks the row. Matching
    only what a CONNECTED element holds finds nothing, because the field is
    gone. Flow retains the value the field held at a COMMIT POINT and uses it
    for exactly one thing: withholding identity text. It is never used to
    rewrite anything, and never for the URL or the title.
    """

    executable = _chromium_executable()
    if executable is None:
        pytest.skip("no Chromium executable is installed")
    session_id = "page-closure-wizard-test"
    binding_name = "__oaflow_emit_wizard_test"
    init_js = _page_closure_init_js(session_id, binding_name, ("token",))
    events: list[dict] = []
    from playwright.sync_api import sync_playwright

    secret = "hunter2-token-value"
    body = (
        "<title>Wizard</title>"
        "<div id='step1'><input id='token' name='token' type='text'>"
        "<button id='next'>Next</button></div><div id='step2'></div>"
        "<script>document.getElementById('next').addEventListener('click',"
        " () => {const value = document.getElementById('token').value;"
        "document.getElementById('step1').remove();"
        "document.getElementById('step2').innerHTML ="
        " '<table><tr id=\"summary\"><td>Token</td><td>' + value +"
        " '</td><td><button id=\"confirm\">Confirm</button></td></tr></table>';"
        "});</script>"
    )
    with sync_playwright() as playwright:
        browser, page = _closure_page(
            playwright, executable, body, "http://host.test/wizard", session_id
        )
        try:
            page.expose_binding(
                binding_name, lambda _source, detail: events.append(detail)
            )
            page.evaluate(init_js)
            page.click("#token")
            page.keyboard.type(secret)
            page.click("#next")
            page.click("#confirm")
            page.wait_for_timeout(50)
        finally:
            browser.close()

    assert secret not in json.dumps(events)
    click = [event for event in events if event.get("kind") == "click"][-1]
    assert click["sid"] is None
    assert click["sid_withheld"] == "secret-value-in-identity"


@pytest.mark.timeout(60)
def test_page_closure_withholds_identity_from_an_inbound_declared_parameter() -> None:
    """Blocker P1-B across documents. The value arrives under its own name.

    A same-origin GET submit carries the field's value under the field's NAME,
    because that is how an HTML form works. The results document builds a fresh
    closure with no bound element holding that value, so it recovers the value
    from its own URL by NAME and uses it to withhold identity text.
    """

    executable = _chromium_executable()
    if executable is None:
        pytest.skip("no Chromium executable is installed")
    session_id = "page-closure-inbound-param-test"
    binding_name = "__oaflow_emit_inbound_param_test"
    init_js = _page_closure_init_js(session_id, binding_name, ("token",))
    events: list[dict] = []
    from playwright.sync_api import sync_playwright

    secret = "hunter2-token-value"
    body = (
        "<title>Results</title>"
        "<table><tr id='row'><td>Token</td><td>" + secret + "</td>"
        "<td><button id='confirm'>Confirm</button></td></tr></table>"
    )
    with sync_playwright() as playwright:
        browser, page = _closure_page(
            playwright,
            executable,
            body,
            f"http://host.test/results?token={secret}",
            session_id,
        )
        try:
            page.expose_binding(
                binding_name, lambda _source, detail: events.append(detail)
            )
            page.evaluate(init_js)
            page.click("#confirm")
            page.wait_for_timeout(50)
            state = _sample_reflected_state(page, session_id)
        finally:
            browser.close()

    assert secret not in json.dumps(events)
    assert secret not in json.dumps(state)
    click = [event for event in events if event.get("kind") == "click"][-1]
    assert click["sid"] is None
    assert click["sid_withheld"] == "secret-value-in-identity"
    # The URL keeps the parameter NAME and loses its VALUE, by name.
    assert state["url"] == "http://host.test/results?token="
    assert state["dropped"] == [
        {"name": "token", "where": "query", "reason": "declared-secret-parameter"}
    ]


@pytest.mark.timeout(60)
def test_page_closure_marks_a_withheld_secret_field_name() -> None:
    """Blocker P2-A. The last silent null.

    Flow never reads the visible text of a bound secret field, because that
    text IS the value. A declared field with an aria-label therefore reports no
    accessible name. That is a WITHHELD name, not an absent one: a control
    field beside it returns its name normally.
    """

    executable = _chromium_executable()
    if executable is None:
        pytest.skip("no Chromium executable is installed")
    session_id = "page-closure-secret-name-test"
    binding_name = "__oaflow_emit_secret_name_test"
    init_js = _page_closure_init_js(session_id, binding_name, ("token",))
    events: list[dict] = []
    from playwright.sync_api import sync_playwright

    body = (
        "<title>Login</title>"
        "<input id='token' name='token' type='text' aria-label='Token field'>"
        "<input id='note' name='note' type='text' aria-label='Note field'>"
    )
    with sync_playwright() as playwright:
        browser, page = _closure_page(
            playwright, executable, body, "http://host.test/login", session_id
        )
        try:
            page.expose_binding(
                binding_name, lambda _source, detail: events.append(detail)
            )
            page.evaluate(init_js)
            page.click("#note")
            page.click("#token")
            page.wait_for_timeout(50)
        finally:
            browser.close()

    clicks = [event for event in events if event.get("kind") == "click"]
    note = next(c for c in clicks if c["structural"]["selector"] == "#note")
    token = next(c for c in clicks if c["structural"]["selector"] == "#token")
    assert note["structural"]["name"] == "Note field"
    assert "identity_withheld" not in note["structural"]
    assert token["structural"]["name"] is None
    assert token["structural"]["identity_withheld"] == "secret-field-name-not-read"


@pytest.mark.timeout(60)
def test_page_closure_reports_a_single_page_app_route_change() -> None:
    """The evidence a whole-URL refusal used to destroy.

    A single-page application routes after a login. The path is app structure,
    not operator input, so Flow reports the origin and the path exactly. The
    old rule withheld an origin-only URL for the rest of the document, which
    cost the URL evidence of an entire session on every well-behaved app.
    """

    executable = _chromium_executable()
    if executable is None:
        pytest.skip("no Chromium executable is installed")
    session_id = "page-closure-spa-route-test"
    binding_name = "__oaflow_emit_spa_route_test"
    init_js = _page_closure_init_js(session_id, binding_name, ())
    from playwright.sync_api import sync_playwright

    body = (
        "<title>Sign in</title>"
        "<input id='password' name='password' type='password'>"
        "<button id='sign-in' onclick=\""
        "history.replaceState({}, '', '/dashboard/overview');"
        '">Sign in</button>'
    )
    with sync_playwright() as playwright:
        browser, page = _closure_page(
            playwright, executable, body, "http://host.test/login", session_id
        )
        try:
            page.expose_binding(binding_name, lambda _source, _detail: None)
            page.evaluate(init_js)
            page.click("#password")
            page.keyboard.type("hunter2-token-value")
            page.click("#sign-in")
            page.wait_for_timeout(50)
            state = _sample_reflected_state(page, session_id)
        finally:
            browser.close()

    assert state["url"] == "http://host.test/dashboard/overview"
    assert state["url_withheld"] is None
    assert state["dropped"] == []


@pytest.mark.timeout(60)
def test_page_closure_drops_only_the_unproven_parameter_value() -> None:
    """One parameter value is dropped; the path and the others stay exact.

    A parameter Flow cannot prove predates the value loses only its own value.
    The whole URL is not withheld for it, and every parameter NAME survives.
    """

    executable = _chromium_executable()
    if executable is None:
        pytest.skip("no Chromium executable is installed")
    session_id = "page-closure-one-param-test"
    binding_name = "__oaflow_emit_one_param_test"
    init_js = _page_closure_init_js(session_id, binding_name, ())
    from playwright.sync_api import sync_playwright

    body = (
        "<title>Charts</title>"
        "<input id='password' name='password' type='password'>"
        "<button id='go' onclick=\""
        "history.replaceState({}, '', '/charts?view=open&filter=changed');"
        '">Go</button>'
    )
    with sync_playwright() as playwright:
        browser, page = _closure_page(
            playwright,
            executable,
            body,
            "http://host.test/charts?view=open&filter=start",
            session_id,
        )
        try:
            page.expose_binding(binding_name, lambda _source, _detail: None)
            page.evaluate(init_js)
            page.click("#password")
            page.keyboard.type("hunter2-token-value")
            page.click("#go")
            page.wait_for_timeout(50)
            state = _sample_reflected_state(page, session_id)
        finally:
            browser.close()

    # `view` is unchanged since before the field held a value, so it is proven
    # and reported. `filter` changed, so only ITS value is dropped.
    assert state["url"] == "http://host.test/charts?view=open&filter="
    assert state["url_withheld"] is None
    assert state["dropped"] == [
        {"name": "filter", "where": "query", "reason": "unproven-parameter-value"}
    ]


@pytest.mark.timeout(60)
def test_page_closure_withholds_a_url_that_holds_the_value_in_its_path() -> None:
    """The net: a value structure cannot name, in a path segment."""

    executable = _chromium_executable()
    if executable is None:
        pytest.skip("no Chromium executable is installed")
    session_id = "page-closure-path-secret-test"
    binding_name = "__oaflow_emit_path_secret_test"
    init_js = _page_closure_init_js(session_id, binding_name, ("token",))
    from playwright.sync_api import sync_playwright

    secret = "hunter2-token-value"
    body = (
        "<title>Charts</title>"
        "<input id='token' name='token' type='password'"
        " oninput=\"history.replaceState({}, '', '/charts/' + this.value);\">"
        "<button id='save'>Save</button>"
    )
    with sync_playwright() as playwright:
        browser, page = _closure_page(
            playwright, executable, body, "http://host.test/charts", session_id
        )
        try:
            page.expose_binding(binding_name, lambda _source, _detail: None)
            page.evaluate(init_js)
            page.click("#token")
            page.keyboard.type(secret)
            page.click("#save")
            page.wait_for_timeout(50)
            state = _sample_reflected_state(page, session_id)
            raw_url = page.evaluate("() => location.href")
        finally:
            browser.close()

    assert raw_url == f"http://host.test/charts/{secret}"
    assert secret not in json.dumps(state)
    assert state["url"] == "http://host.test/"
    assert state["url_withheld"] == "declared-value-in-url"
    assert state["secret_in_url"] is True


# ---------------------------------------------------------------------------
# Round-5: the URL PATH channel, and the evidence the fix must NOT cost.
#
# Every test below FAILS at 07372d5 and passes here.
# ---------------------------------------------------------------------------


@pytest.mark.timeout(180)
def test_launched_recording_withholds_a_redirect_that_puts_the_value_in_a_path(
    tmp_path: Path,
) -> None:
    """A REST redirect answers a GET submit with `/results/<value>`.

    Nothing in the new document can find that value: no bound element holds it,
    the closure committed nothing, and no parameter NAME identifies a path
    segment. Structure closes the query channel and not this one, so the whole
    URL is withheld for every document after the one that first held a declared
    value.
    """

    if _chromium_executable() is None:
        pytest.skip("no Chromium executable is installed")
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlparse

    secret = "hunter2-primary"
    form = (
        b"<!doctype html><html><head><title>Token form</title></head><body>"
        b'<form id="token-form" method="get" action="/lookup">'
        b'<input id="token" name="token" type="text">'
        b'<button id="submit-token" type="submit">Submit</button>'
        b"</form></body></html>"
    )
    results = (
        b"<!doctype html><html><head><title>Results</title></head><body>"
        b'<ul><li id="row"><span id="cell">open</span></li></ul>'
        b"</body></html>"
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/lookup":
                value = parse_qs(parsed.query).get("token", [""])[0]
                self.send_response(302)
                self.send_header("Location", f"/results/{value}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            payload = results if parsed.path.startswith("/results") else form
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    app_url = f"http://127.0.0.1:{server.server_address[1]}/"
    try:

        def drive(page, pump):
            page.click("#token")
            pump()
            page.keyboard.type(secret)
            pump()
            pump()
            page.click("#submit-token")
            pump()
            pump()
            page.click("#cell")
            pump()
            pump()

        recording = record_interactive(
            f"{app_url}form",
            tmp_path / "recording-redirect",
            secret_fields=("token",),
            headless=True,
            script=drive,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    body = "\n".join(
        path.read_text(errors="replace") for path in sorted(recording.glob("*.json*"))
    )
    assert secret not in body
    events = [
        json.loads(line)
        for line in (recording / "events.jsonl").read_text().splitlines()
    ]
    assert events[-1]["url_after"] == app_url
    assert events[-1]["title_after"] == ""
    meta = json.loads((recording / "meta.json").read_text())
    assert meta["structural_text_withheld"] == "secret-value-left-its-document"


@pytest.mark.timeout(60)
def test_page_closure_still_reports_a_same_document_route_after_the_fix() -> None:
    """The cross-document rule must not cost the SPA evidence again.

    A single-page application routes with `history.pushState`, which does NOT
    build a new document. The closure that held the value is the closure being
    sampled, so its URL is still reported exactly. The cross-document rule
    bites only on a real navigation, which is where the redirect leak lives.
    """

    executable = _chromium_executable()
    if executable is None:
        pytest.skip("no Chromium executable is installed")
    session_id = "page-closure-pushstate-test"
    binding_name = "__oaflow_emit_pushstate_test"
    init_js = _page_closure_init_js(session_id, binding_name, ())
    from playwright.sync_api import sync_playwright

    body = (
        "<title>Sign in</title>"
        "<input id='password' name='password' type='password'>"
        "<button id='sign-in' onclick=\""
        "history.pushState({}, '', '/dashboard/overview');"
        '">Sign in</button>'
    )
    with sync_playwright() as playwright:
        browser, page = _closure_page(
            playwright, executable, body, "http://host.test/login", session_id
        )
        try:
            page.expose_binding(binding_name, lambda _source, _detail: None)
            page.evaluate(init_js)
            page.click("#password")
            page.keyboard.type("hunter2-primary")
            page.click("#sign-in")
            page.wait_for_timeout(50)
            state = _sample_reflected_state(page, session_id)
            doc_id = state["doc"]
            # Same document: the recorder closure was not rebuilt, so its
            # document id is unchanged and Python's cross-document rule cannot
            # apply to it.
            after = _sample_reflected_state(page, session_id)
        finally:
            browser.close()

    assert state["url"] == "http://host.test/dashboard/overview"
    assert state["url_withheld"] is None
    assert after["doc"] == doc_id


def test_a_same_document_route_is_never_treated_as_a_later_document(
    tmp_path: Path,
) -> None:
    """The Python half of the same claim, without a browser.

    Only the FIRST document to hold a declared value reports its reflected
    text. A same-document route change keeps that document id, so it keeps its
    URL; a real navigation produces a new id and is withheld.
    """

    session = InteractiveRecorder(
        "http://host.test/app",
        tmp_path / "recording",
        cdp_endpoint="http://127.0.0.1:9222",
    )
    page_state = {
        "url": "http://host.test/login",
        "title": "Sign in",
        "doc": "doc-1",
        "secret": True,
        "url_withheld": None,
        "title_withheld": None,
        "dropped": [],
        "secret_in_url": False,
        "secret_in_title": False,
    }
    session.page = SimpleNamespace(
        main_frame=object(),
        evaluate=lambda _js, _args: dict(page_state),
    )
    assert session._read_scrubbed_page_state()["url"] == "http://host.test/login"
    # Same document, new route: reported exactly.
    page_state["url"] = "http://host.test/dashboard/overview"
    assert (
        session._read_scrubbed_page_state()["url"]
        == "http://host.test/dashboard/overview"
    )
    assert session._structural_text_withheld is False
    # A real navigation builds a new document. Even one that holds a declared
    # value of its own is withheld: holding a value says nothing about whether
    # it loaded with an EARLIER document's value in its path.
    page_state.update({"doc": "doc-2", "url": "http://host.test/results/x"})
    assert session._read_scrubbed_page_state() == {
        "url": "http://host.test/",
        "title": "",
    }
    assert session._structural_text_withheld_reasons == {
        "secret-value-left-its-document"
    }


@pytest.mark.timeout(60)
def test_page_closure_withholds_a_selector_built_from_an_inbound_value() -> None:
    """A selector is identity too, and it used the narrower value set.

    In a document reached by a GET submit the accessible name and the row
    identity were correctly refused, while the element id went out verbatim as
    `#row-<value>`. All three identity paths now use the same value set.
    """

    executable = _chromium_executable()
    if executable is None:
        pytest.skip("no Chromium executable is installed")
    session_id = "page-closure-inbound-selector-test"
    binding_name = "__oaflow_emit_inbound_selector_test"
    init_js = _page_closure_init_js(session_id, binding_name, ("token",))
    events: list[dict] = []
    from playwright.sync_api import sync_playwright

    secret = "hunter2-primary"
    body = (
        "<title>Results</title>"
        f"<ul><li id='row-{secret}'><span id='cell-{secret}'>open</span>"
        "</li></ul>"
    )
    with sync_playwright() as playwright:
        browser, page = _closure_page(
            playwright,
            executable,
            body,
            f"http://host.test/results?token={secret}",
            session_id,
        )
        try:
            page.expose_binding(
                binding_name, lambda _source, detail: events.append(detail)
            )
            page.evaluate(init_js)
            page.click("#cell-" + secret)
            page.wait_for_timeout(50)
        finally:
            browser.close()

    assert secret not in json.dumps(events)
    click = [event for event in events if event.get("kind") == "click"][-1]
    assert click["structural"]["selector"] is None
    assert click["structural"]["identity_withheld"] == "secret-value-in-identity"


# ---------------------------------------------------------------------------
# Round-6: a page that CONSUMES its own field.
#
# A scanner input writes the badge into the URL and clears the field inside its
# own `input` handler. Nothing in the DOM holds the value at any moment Python
# samples. Each test below FAILS at d1a762e and passes here.
# ---------------------------------------------------------------------------


_SCANNER_BODY = (
    "<title>Scan station</title>"
    "<input id='token' name='token' type='text' autocomplete='off'>"
    "<button id='next'>Next</button>"
    "<script>const el = document.getElementById('token');"
    "el.addEventListener('input', () => {"
    "  if (el.value.length >= 15) {"
    "    history.replaceState({}, '', '/scan/' + el.value);"
    "    document.title = 'Scan ' + el.value;"
    "    el.value = '';"
    "  }"
    "});</script>"
)


@pytest.mark.timeout(60)
def test_page_closure_withholds_a_title_a_consumed_field_produced() -> None:
    """F1. The title branch was gated on a flag the live DOM never set.

    `documentHeldSecretValue` was derived from what a bound field HOLDS at a
    sample. A field the page clears inside its own `input` handler holds
    nothing at every sample, so the flag stayed false for the whole recording
    and the title check was never reached, while the URL check ran regardless.
    The flag is now armed in the capture-phase `input` handler, which is the
    moment the document provably held a value -- and is what the documentation
    already said: "once a declared secret field RECEIVES INPUT".
    """

    executable = _chromium_executable()
    if executable is None:
        pytest.skip("no Chromium executable is installed")
    session_id = "page-closure-consumed-title-test"
    binding_name = "__oaflow_emit_consumed_title_test"
    init_js = _page_closure_init_js(session_id, binding_name, ("token",))
    from playwright.sync_api import sync_playwright

    secret = "hunter2-primary"
    with sync_playwright() as playwright:
        browser, page = _closure_page(
            playwright,
            executable,
            _SCANNER_BODY,
            "http://host.test/station",
            session_id,
        )
        try:
            page.expose_binding(binding_name, lambda _source, _detail: None)
            page.evaluate(init_js)
            page.click("#token")
            page.keyboard.type(secret)
            page.wait_for_timeout(50)
            state = _sample_reflected_state(page, session_id)
            raw = page.evaluate("() => [location.href, document.title]")
        finally:
            browser.close()

    # The page really is showing the value in both channels.
    assert raw == [f"http://host.test/scan/{secret}", f"Scan {secret}"]
    assert secret not in json.dumps(state)
    assert state["secret"] is True
    assert state["title"] == ""
    assert state["title_withheld"] == "declared-value-in-title"
    assert state["url_withheld"] == "declared-value-in-url"


def test_an_input_event_alone_arms_the_cross_document_boundary(
    tmp_path: Path,
) -> None:
    """F2. Both document markers must move together.

    `_track_secret_document` added to `_secret_doc_ids` from the input event
    but set `_first_secret_doc_id` only from the settled page read. A document
    that never HELD a value at a sampling instant therefore reached one marker
    and not the other, and the cross-document rule -- which keys off the second
    -- never engaged, so every later document reported its URL.
    """

    session = InteractiveRecorder(
        "http://host.test/app",
        tmp_path / "recording",
        cdp_endpoint="http://127.0.0.1:9222",
    )
    selected_frame = object()
    session.page = SimpleNamespace(main_frame=selected_frame)
    session._enqueue_browser_event(
        {
            "__oaflow_session": session._session_id,
            "__oaflow_top_level": True,
            "__oaflow_viewport": [1280, 800],
            "__oaflow_dpr": 1.0,
            "__oaflow_origin": "http://host.test",
            "__oaflow_doc": "doc-1",
            "__oaflow_doc_holds_secret": False,
            "kind": "input",
            "field": "token",
            "secret": True,
            "__oaflow_secret_mask_bound": True,
            "__oaflow_input_session": f"{session._session_id}:input:1",
        },
        source={"page": session.page, "frame": selected_frame},
    )
    assert session._listener_error is None
    assert session._secret_doc_ids == {"doc-1"}
    assert session._first_secret_doc_id == "doc-1"
    # A later document is therefore withheld, which is the whole point.
    assert session._secret_document_left("doc-2") is True
    assert session._secret_document_left("doc-1") is False


@pytest.mark.timeout(60)
def test_page_closure_keeps_a_consumed_value_across_a_second_entry() -> None:
    """F3. One field, two scans.

    Badge one is consumed into the URL and the field is cleared; the operator
    starts badge two. The per-element cache holds ONE value and badge two was
    about to displace badge one while badge one was still on show. A value the
    next one does not CONTINUE was not edited away by the operator -- the page
    took it -- so it is promoted into the withhold-only committed set, which is
    not per element.
    """

    executable = _chromium_executable()
    if executable is None:
        pytest.skip("no Chromium executable is installed")
    session_id = "page-closure-two-scans-test"
    binding_name = "__oaflow_emit_two_scans_test"
    init_js = _page_closure_init_js(session_id, binding_name, ("token",))
    from playwright.sync_api import sync_playwright

    first = "hunter2-primary"
    with sync_playwright() as playwright:
        browser, page = _closure_page(
            playwright,
            executable,
            _SCANNER_BODY,
            "http://host.test/station",
            session_id,
        )
        try:
            page.expose_binding(binding_name, lambda _source, _detail: None)
            page.evaluate(init_js)
            page.click("#token")
            page.keyboard.type(first)
            page.wait_for_timeout(30)
            page.keyboard.type("beta9t")  # badge two, still live
            page.wait_for_timeout(50)
            state = _sample_reflected_state(page, session_id)
            live = page.evaluate("() => document.getElementById('token').value")
        finally:
            browser.close()

    assert live == "beta9t", "badge two must still be live for this to be the case"
    assert first not in json.dumps(state)
    assert state["url_withheld"] == "declared-value-in-url"


@pytest.mark.timeout(60)
def test_page_closure_keeps_a_consumed_value_while_another_field_is_live() -> None:
    """F4. Two declared fields.

    The first is consumed into the URL and cleared; the second still holds a
    PIN. The cache used to be skipped for the WHOLE document as soon as
    anything held a value, so the same URL was withheld before the PIN was
    typed and reported afterwards. The test is per element now.
    """

    executable = _chromium_executable()
    if executable is None:
        pytest.skip("no Chromium executable is installed")
    session_id = "page-closure-two-fields-test"
    binding_name = "__oaflow_emit_two_fields_test"
    init_js = _page_closure_init_js(session_id, binding_name, ("token", "pin"))
    from playwright.sync_api import sync_playwright

    first = "hunter2-primary"
    body = _SCANNER_BODY.replace(
        "<button id='next'>Next</button>",
        "<input id='pin' name='pin' type='text'><button id='next'>Next</button>",
    )
    with sync_playwright() as playwright:
        browser, page = _closure_page(
            playwright, executable, body, "http://host.test/station", session_id
        )
        try:
            page.expose_binding(binding_name, lambda _source, _detail: None)
            page.evaluate(init_js)
            page.click("#token")
            page.keyboard.type(first)
            page.wait_for_timeout(30)
            before_pin = _sample_reflected_state(page, session_id)
            page.click("#pin")
            page.keyboard.type("4821")
            page.wait_for_timeout(50)
            after_pin = _sample_reflected_state(page, session_id)
        finally:
            browser.close()

    # The SAME URL, withheld before the PIN and withheld after it. A second
    # declared field holding a value says nothing about the first field's.
    assert before_pin["url_withheld"] == "declared-value-in-url"
    assert first not in json.dumps(after_pin)
    assert after_pin["url_withheld"] == "declared-value-in-url"
