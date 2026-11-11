"""One acquisition/ownership model for launched and attached browsers."""

from __future__ import annotations

import ast
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openadapt_flow import _browser_readiness as readiness
from openadapt_flow import _browser_setup as setup


class Cancelled(BaseException):
    pass


class Handle:
    def __init__(self) -> None:
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True


class Page:
    def __init__(self, context: Context, failure: str = "") -> None:
        self.context = context
        self.failure = failure
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.handles: list[Handle] = []

    def is_closed(self) -> bool:
        return self.failure == "closed_page"

    def wait_for_function(self, expression: str, **kwargs: Any) -> Handle:
        self.calls.append((expression, kwargs))
        phase = {
            readiness._START: "schedule",
            readiness._PROGRESS: "progress",
            readiness._CANCEL: "cleanup",
        }[expression]
        if self.failure == phase:
            raise RuntimeError("private page value must not escape")
        if self.failure == "cancel" and phase == "progress":
            raise Cancelled()
        handle = Handle()
        self.handles.append(handle)
        return handle


class Context:
    def __init__(self, browser: Browser, failure: str = "") -> None:
        self.browser = browser
        self.closed = False
        self.page = Page(self, failure)

    def new_page(self) -> Page:
        return self.page

    def close(self) -> None:
        self.closed = True


class Browser:
    def __init__(self, failure: str = "") -> None:
        self.failure = failure
        self.closed = False
        self.contexts: list[Context] = []

    def is_connected(self) -> bool:
        return self.failure != "closed_browser"

    def new_context(self, **kwargs: Any) -> Context:
        assert kwargs == {"service_workers": "block"}
        context = Context(self, self.failure)
        self.contexts.append(context)
        return context

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize("attached", [False, True])
@pytest.mark.parametrize(
    "failure", ["", "schedule", "progress", "cleanup", "cancel", "closed_browser"]
)
def test_acquisition_model_refuses_and_cleans_only_owned_resources(
    attached: bool, failure: str
) -> None:
    browser = Browser(failure)
    user_context = Context(browser, failure)
    options = {"attached_page": user_context.page} if attached else {}
    if failure:
        error = Cancelled if failure == "cancel" else readiness.BrowserReadinessError
        with pytest.raises(error) as caught:
            readiness.require_rendering_ready(browser, **options)
        assert "private page value" not in str(caught.value)
    else:
        readiness.require_rendering_ready(browser, **options)
    assert browser.closed is (bool(failure) and not attached)
    assert not user_context.closed
    assert all(context.closed for context in browser.contexts)
    assert len(browser.contexts) == (
        0 if attached or failure == "closed_browser" else 1
    )
    page = (
        user_context.page
        if attached
        else (browser.contexts[0].page if browser.contexts else None)
    )
    if page is not None:
        assert all(handle.disposed for handle in page.handles)
        for _expression, kwargs in page.calls:
            assert 0 < kwargs["timeout"] <= readiness.READINESS_TIMEOUT_MS
            assert kwargs["polling"] == 10
        if failure in ("progress", "cancel"):
            assert page.calls[-1][0] == readiness._CANCEL


@pytest.mark.parametrize("kind", ["none", "foreign", "closed", "persistent"])
def test_unsupported_attach_never_treats_the_browser_as_owned(kind: str) -> None:
    browser = Browser()
    context = Context(browser, "closed_page" if kind == "closed" else "")
    page: Any = context.page
    if kind == "none":
        page = None
    elif kind == "foreign":
        context.browser = Browser()
    elif kind == "persistent":
        context.browser = None  # type: ignore[assignment]
    with pytest.raises(readiness.BrowserReadinessError, match="readiness_unavailable"):
        readiness.require_rendering_ready(browser, attached_page=page)
    assert not browser.closed
    assert not context.closed
    assert not browser.contexts
    assert not context.page.calls


def test_host_deadline_can_refuse_before_scheduling_without_a_timer_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    times = iter([0.0, 2.0])
    monkeypatch.setattr(readiness.time, "monotonic", lambda: next(times))
    browser = Browser()
    context = Context(browser)
    with pytest.raises(readiness.BrowserReadinessError, match="rendering_unavailable"):
        readiness.require_rendering_ready(browser, attached_page=context.page)
    assert context.page.calls == []
    assert not browser.closed
    assert not context.closed


@pytest.mark.parametrize("entry", ["selected", "direct"])
def test_failed_rendering_never_retries_a_launch_or_changes_arguments(
    monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    browser = Browser("progress")
    calls = []

    class Chromium:
        def launch(self, **options: Any) -> Browser:
            calls.append(options)
            return browser

    monkeypatch.setattr(
        setup, "chromium_launch_options", lambda **_: {"channel": "msedge"}
    )
    with pytest.raises(readiness.BrowserReadinessError):
        if entry == "selected":
            setup.launch_chromium(Chromium(), headless=True)
        else:
            readiness.launch_rendering_ready(
                Chromium(), headless=True, channel="msedge"
            )
    assert calls == [{"headless": True, "channel": "msedge"}]
    assert browser.closed


@pytest.mark.parametrize("name", ["chromium", "firefox", "webkit"])
def test_direct_launch_applies_only_chromium_keychain_args(name: str) -> None:
    browser = Browser()
    calls: list[dict[str, Any]] = []

    def launch(**options: Any) -> Browser:
        calls.append(options)
        return browser

    launcher = SimpleNamespace(name=name, launch=launch)
    caller_args = ["--synthetic-option"]
    original = {"headless": True, "executable_path": "/synthetic/browser"}
    launched = readiness.launch_rendering_ready(launcher, **original, args=caller_args)
    assert launched is browser
    expected = ["--synthetic-option"]
    if name == "chromium":
        expected += ["--use-mock-keychain", "--password-store=basic"]
    assert calls == [{**original, "args": expected}]
    assert caller_args == ["--synthetic-option"]
    assert len(browser.contexts) == 1 and browser.contexts[0].closed
    assert not browser.closed


def _unchecked_launches(source: str) -> list[int]:
    """Inspect launch references as well as calls, including dynamic aliases.

    The public backend class delegates to the checked browser-process helper;
    every other launch reference belongs in the shared acquisition primitive.
    This is a source inventory, not arbitrary Python data-flow verification.
    """
    tree = ast.parse(source)
    delegated = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "openadapt_flow.backends.playwright_backend"
        for alias in node.names
        if alias.name == "PlaywrightBackend"
    }
    unchecked = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "launch":
            if not (isinstance(node.value, ast.Name) and node.value.id in delegated):
                unchecked.append(node.lineno)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "launch"
        ):
            if not (
                isinstance(node.args[0], ast.Name) and node.args[0].id in delegated
            ):
                unchecked.append(node.lineno)
    return sorted(unchecked)


@pytest.mark.parametrize(
    "source",
    [
        "pw.chromium.launch(headless=True)",
        "pw.firefox.launch(headless=True)",
        "pw.webkit.launch(headless=True)",
        "launcher = getattr(pw, spec.browser)\nlauncher.launch(**options)",
        "launcher = pw.chromium\nlaunch = launcher.launch\nlaunch(**options)",
        "getattr(getattr(pw, spec.browser), 'launch')(**options)",
        "PlaywrightBackend.launch(url)",
    ],
)
def test_acquisition_inventory_recognizes_direct_and_aliased_browser_launches(
    source: str,
) -> None:
    assert len(_unchecked_launches(source)) == 1


@pytest.mark.parametrize(
    "source",
    [
        "launch_rendering_ready(getattr(pw, spec.browser), **options)",
        "from openadapt_flow.backends.playwright_backend import PlaywrightBackend\n"
        "PlaywrightBackend.launch(url)",
        "from openadapt_flow.backends.playwright_backend import PlaywrightBackend as B\n"
        "B.launch(url)",
    ],
)
def test_acquisition_inventory_retains_shared_primitives_and_backend_delegation(
    source: str,
) -> None:
    assert _unchecked_launches(source) == []


@pytest.mark.parametrize(
    ("source", "unchecked"),
    [
        (
            "from openadapt_flow.backends.playwright_backend import PlaywrightBackend\n"
            "launch = getattr(PlaywrightBackend, 'launch', None)\nlaunch(url)",
            False,
        ),
        (
            "from openadapt_flow.backends.playwright_backend import PlaywrightBackend as B\n"
            "getattr(B, 'launch')(url)",
            False,
        ),
        ("getattr(PlaywrightBackend, 'launch')(url)", True),
        (
            "from unrelated import PlaywrightBackend\n"
            "getattr(PlaywrightBackend, 'launch')(url)",
            True,
        ),
    ],
)
def test_acquisition_inventory_requires_exact_import_for_dynamic_delegation(
    source: str, unchecked: bool
) -> None:
    assert bool(_unchecked_launches(source)) is unchecked


def test_production_acquisition_inventory_has_no_unchecked_direct_launch() -> None:
    root = Path(__file__).parents[1] / "openadapt_flow"
    unchecked = []
    for path in root.rglob("*.py"):
        if path.name in ("_browser_setup.py", "_browser_readiness.py"):
            continue
        unchecked.extend(
            f"{path}:{line}" for line in _unchecked_launches(path.read_text())
        )
    assert unchecked == []


def test_all_cdp_acquisitions_check_their_selected_page() -> None:
    root = Path(__file__).parents[1] / "openadapt_flow"
    expected = {
        "__main__.py": "_attached_web_page",
        "interactive_recorder.py": "start",
        "cli_worklist_pool.py": "_open",
        "agent_guard/session.py": "_connect",
    }
    observed = {}
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        connects = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "connect_over_cdp"
        ]
        if not connects:
            continue
        relative = path.relative_to(root).as_posix()
        assert relative in expected
        function = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == expected[relative]
        )
        checks = [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "require_rendering_ready"
        ]
        assert len(connects) == len(checks) == 1
        assert checks[0].lineno > connects[0].lineno
        assert [keyword.arg for keyword in checks[0].keywords] == ["attached_page"]
        observed[relative] = function.name
    assert observed == expected


@pytest.mark.parametrize("failure", ["", "progress", "cancel"])
def test_actual_attach_entry_checks_selected_page_before_existing_guards(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from openadapt_flow import interactive_recorder
    from openadapt_flow.__main__ import _attached_web_page
    from openadapt_flow.backends import playwright_backend

    browser = Browser(failure)
    context = Context(browser, failure)
    guarded = []
    stopped = []

    @contextmanager
    def playwright():
        try:
            yield SimpleNamespace(
                chromium=SimpleNamespace(connect_over_cdp=lambda _: browser)
            )
        finally:
            stopped.append(True)

    monkeypatch.setitem(
        sys.modules, "playwright.sync_api", SimpleNamespace(sync_playwright=playwright)
    )
    monkeypatch.setattr(
        interactive_recorder, "select_attached_page", lambda *a, **k: context.page
    )
    monkeypatch.setattr(
        playwright_backend, "prepare_ws_guard", lambda page: guarded.append(page)
    )
    cfg = SimpleNamespace(
        browser_cdp_endpoint="http://127.0.0.1:9222", browser_page_url=None
    )
    if failure:
        error = Cancelled if failure == "cancel" else SystemExit
        with pytest.raises(error) as caught:
            with _attached_web_page(cfg, url="https://synthetic.invalid"):
                pytest.fail("an unready page reached the application")
        if failure != "cancel":
            assert str(caught.value) == (
                "REFUSED: browser_rendering_unavailable. Nothing was executed."
            )
        assert guarded == []
    else:
        with _attached_web_page(cfg, url="https://synthetic.invalid") as selected:
            assert selected is context.page
            assert guarded == [selected]
    assert stopped == [True]
    assert not context.closed
    assert not browser.closed


def test_attached_callback_progress_grants_no_qualification_authority() -> None:
    from openadapt_flow.ir import Workflow
    from openadapt_flow.qualification import (
        QualificationRefusalCode,
        evaluate_qualification,
    )

    workflow = Workflow(name="synthetic-unqualified")
    before = evaluate_qualification(workflow)
    browser = Browser()
    context = Context(browser)
    assert (
        readiness.require_rendering_ready(browser, attached_page=context.page) is None
    )
    after = evaluate_qualification(workflow)
    assert after.report_sha256() == before.report_sha256()
    assert after.passed is False
    assert [refusal.code for refusal in after.refusals] == [
        QualificationRefusalCode.PROJECT_MISSING
    ]
    assert workflow.qualification is None
    assert not browser.closed and not context.closed


@pytest.mark.parametrize("cancel", [False, True])
def test_owned_driver_stops_on_readiness_refusal_and_cancellation(
    monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    from openadapt_flow.backends._browser_process import _start_browser

    stopped = []
    driver = SimpleNamespace(chromium=object(), stop=lambda: stopped.append(True))
    monkeypatch.setitem(
        sys.modules,
        "playwright.sync_api",
        SimpleNamespace(sync_playwright=lambda: SimpleNamespace(start=lambda: driver)),
    )
    monkeypatch.setattr(setup, "require_browser_support", lambda: None)
    error = Cancelled if cancel else readiness.BrowserReadinessError

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise error("browser_rendering_unavailable")

    monkeypatch.setattr(setup, "launch_chromium", refuse)
    with pytest.raises(error):
        _start_browser(True)
    assert stopped == [True]


@pytest.mark.parametrize("failure", ["", "start", "progress", "cancel"])
def test_ci_browser_models_keep_readiness_before_business_use(failure: str) -> None:
    from tests.browser_readiness import ReadyBrowserMixin

    events: list[str] = []

    class Model(ReadyBrowserMixin):
        _readiness_failure = failure

        def _readiness_mark(self, name: str) -> None:
            events.append(name)

        def close(self) -> None:
            events.append("browser-close")

    browser = Model()
    launcher = SimpleNamespace(launch=lambda **_options: browser)
    if failure:
        with pytest.raises(readiness.BrowserReadinessError):
            readiness.launch_rendering_ready(launcher, headless=True)
        assert events[-1] == "browser-close"
    else:
        assert readiness.launch_rendering_ready(launcher, headless=True) is browser
        events.append("business-use")
        assert events[-2:] == ["readiness-close", "business-use"]
    assert events[:3] == ["readiness-context", "readiness-page", "readiness-start"]
    assert events.count("readiness-close") == 1
    assert events.count("readiness-dispose") == (
        0 if failure == "start" else 3 if not failure else 2
    )
    if failure == "progress":
        assert "readiness-cancel" in events
    if failure:
        assert "business-use" not in events
