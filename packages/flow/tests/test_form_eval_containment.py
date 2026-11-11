"""Scratch form ownership and containment with no browser or network."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from openadapt_flow.backends.worker_containment import (
    WORKER_REFUSED,
    WORKER_STARTED,
    WorkerContainment,
)
from openadapt_flow.ir import ApiBinding, WebFormField, WebFormSpec
from openadapt_flow.runtime.actuators import ActuationStatus, form_eval
from openadapt_flow.runtime.actuators.web_form import (
    PlaywrightSessionTransport,
    WebFormActuator,
)

BASE_URL = "https://form.example.test"
FORM_PATH = "/form"
SAVE_PATH = "/save"
FORM_HTML = (
    '<form method="post" action="/save">'
    '<input name="value" value="synthetic">'
    '<button id="submit" type="submit">Save</button></form>'
)
BODY = [("value", "synthetic")]
INSTALL_PROBLEM = "worker containment could not be installed"
WINDOW_REFUSAL = "the page tried to open a window, and it was blocked"
CLOSE_PROBLEM = "the scratch context could not be closed"

SETUP = ["scratch-context", "http-route", "blank-page", "containment"]
WORK = ["navigate", "setup-form", "click-submitter", "collect"]


class _Request:
    def __init__(self) -> None:
        self.gets: list[str] = []
        self.posts: list[str] = []

    def get(self, url, **kwargs):
        self.gets.append(url)
        return SimpleNamespace(status=200, text=lambda: FORM_HTML, headers={})

    def post(self, url, **kwargs):
        self.posts.append(url)
        return SimpleNamespace(status=200, text=lambda: "", headers={})


class _LiveContext:
    def __init__(self, browser) -> None:
        self.browser = browser
        self.request = _Request()
        self.close_calls = 0

    def storage_state(self):
        return {"cookies": [], "origins": []}

    def close(self) -> None:
        self.close_calls += 1


class _Browser:
    def __init__(self, scratch) -> None:
        self.scratch = scratch
        self.close_calls = 0

    def new_context(self, **options):
        assert options["service_workers"] == "block"
        assert options["storage_state"] == {"cookies": [], "origins": []}
        self.scratch.events.append("scratch-context")
        return self.scratch

    def close(self) -> None:
        self.close_calls += 1


class _Scratch:
    def __init__(self, *, phase: str = "", invalid: bool = False) -> None:
        self.events: list[str] = []
        self.phase = phase
        self.invalid = invalid
        self.state = WorkerContainment()
        self.browser = _Browser(self)
        self.live = _LiveContext(self.browser)
        self.live_page = SimpleNamespace(
            context=self.live, url=BASE_URL + FORM_PATH, viewport_size=None
        )
        self.page = _Page(self)

    def route(self, pattern, handler) -> None:
        assert pattern == "**/*" and callable(handler)
        self.events.append("http-route")

    def route_web_socket(self, pattern, handler) -> None:
        assert callable(handler)

    def add_init_script(self, script) -> None:
        assert script

    def new_page(self):
        self.events.append("blank-page")
        return self.page

    def on(self, event, handler) -> None:
        assert callable(handler)

    def close(self) -> None:
        self.events.append("scratch-close")
        if self.phase == "close-violation":
            self.state.violate(WINDOW_REFUSAL)
        elif self.phase == "close-error":
            raise RuntimeError("synthetic close failure")

    def install(self, page, *, report_window_open):
        assert page is self.page and page.context is self
        assert page.url == "about:blank"
        assert report_window_open is True
        self.events.append("containment")
        if self.phase == "installation":
            self.state.problem = INSTALL_PROBLEM
        elif self.phase == "immediate":
            self.state.violate(WORKER_STARTED)
        return self.state


class _Page:
    def __init__(self, context: _Scratch) -> None:
        self.context = context
        self.url = "about:blank"
        self.main_frame = object()
        self.frames = [self.main_frame]

    def on(self, event, handler) -> None:
        assert callable(handler)

    def goto(self, url, **kwargs):
        assert url == BASE_URL + FORM_PATH
        self.url = url
        self.context.events.append("navigate")
        return SimpleNamespace(status=200)

    def evaluate(self, script, *args):
        if script == form_eval._SETUP_JS:
            assert args[0]["actionPath"] == SAVE_PATH
            self.context.events.append("setup-form")
            return {}
        if script == form_eval._PRESUBMIT_JS:
            return {}
        assert script == form_eval._COLLECT_JS
        self.context.events.append("collect")
        if self.context.phase == "work":
            self.context.state.violate(WORKER_REFUSED)
        if self.context.invalid:
            return {"rules": ["value"]}
        return {
            "submits": [
                {
                    "destination": BASE_URL + SAVE_PATH,
                    "method": "post",
                    "encoding": form_eval.URLENCODED,
                    "target": "_self",
                    "fields": BODY,
                }
            ]
        }

    def wait_for_timeout(self, milliseconds) -> None:
        pass

    def locator(self, selector):
        assert selector == "#submit"
        return self

    def count(self) -> int:
        return 1

    def click(self, *, timeout) -> None:
        self.context.events.append("click-submitter")


def _assert_owned_close(scratch: _Scratch) -> None:
    assert scratch.events.count("scratch-close") == 1
    assert scratch.live.close_calls == scratch.browser.close_calls == 0
    assert scratch.live_page.url == BASE_URL + FORM_PATH


@pytest.mark.parametrize("invalid", [False, True], ids=["valid", "invalid"])
def test_scratch_containment_preserves_ordinary_form_evaluation(
    monkeypatch: pytest.MonkeyPatch, invalid: bool
) -> None:
    scratch = _Scratch(invalid=invalid)
    monkeypatch.setattr(form_eval, "contain_workers", scratch.install, raising=False)
    result = form_eval.evaluate_form(
        scratch.live_page, BASE_URL, FORM_PATH, SAVE_PATH, [], submitter="#submit"
    )
    assert scratch.events == SETUP + WORK + ["scratch-close"]
    _assert_owned_close(scratch)
    if invalid:
        assert result.status == "invalid" and not result.valid
        assert (
            result.reason == "the form's validation rules refuse these values (value)"
        )
        assert result.serialized is None and result.writes == []
    else:
        assert result.status == "valid" and result.valid
        assert result.reason == "" and result.via == "listener"
        assert result.serialized == BODY and result.writes == []


@pytest.mark.parametrize(
    ("phase", "reason", "navigated"),
    [
        ("installation", INSTALL_PROBLEM, False),
        ("immediate", WORKER_STARTED, False),
        ("work", WORKER_REFUSED, True),
        ("close-violation", WINDOW_REFUSAL, True),
        ("close-error", CLOSE_PROBLEM, True),
    ],
    ids=["installation", "immediate", "work", "close-violation", "close-error"],
)
def test_scratch_containment_refuses_before_api_save(
    monkeypatch: pytest.MonkeyPatch, phase: str, reason: str, navigated: bool
) -> None:
    scratch = _Scratch(phase=phase)
    monkeypatch.setattr(form_eval, "contain_workers", scratch.install, raising=False)
    binding = ApiBinding(
        kind="web_form",
        url_template=SAVE_PATH,
        web_form=WebFormSpec(
            form_path=FORM_PATH,
            fields=[
                WebFormField(name="value", source="form_default", value="synthetic")
            ],
            submitter="#submit",
        ),
    )
    transport = PlaywrightSessionTransport(scratch.live_page)
    actuator = WebFormActuator(transport)
    outcome = actuator.actuate(binding, {})

    assert outcome.status is ActuationStatus.UNAVAILABLE and not outcome.actuated
    assert outcome.reason == (
        f"{reason}; the browser did not reproduce the recorded submit; "
        "the save was not sent"
    )
    assert scratch.live.request.gets == [BASE_URL + FORM_PATH]
    assert scratch.live.request.posts == []
    assert scratch.events == SETUP + (WORK if navigated else []) + ["scratch-close"]
    _assert_owned_close(scratch)
    (evaluation,) = [entry for entry in actuator.log if entry["stage"] == "page_script"]
    assert evaluation["status"] == "unevaluable" and evaluation["via"] == ""
