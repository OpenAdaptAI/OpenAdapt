"""Owned browser setup failure controls with no browser, process, or network."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openadapt_flow import _browser_readiness, _browser_setup
from openadapt_flow.agent import browser_teach
from openadapt_flow.backends import (
    playwright_backend,
    redirect_guard,
    worker_containment,
)
from openadapt_flow.qualify import session


class _Cancelled(BaseException):
    pass


class _Acquisition:
    """Record lifecycle calls at the Playwright boundary."""

    def __init__(self, failure: str = "", *, stop_fails: bool = False) -> None:
        self.calls: list[str] = []
        self.primary = _Cancelled("synthetic setup failure")
        self.failure = failure
        self.stop_fails = stop_fails
        self.page = SimpleNamespace(
            set_default_timeout=lambda _ms: self.step("page-timeout"),
            on=lambda _event, _handler: None,
            goto=lambda *_args, **_kwargs: self.step("navigate"),
        )
        self.context = SimpleNamespace(
            route=lambda _pattern, _handler: self.step("route"),
            new_page=self.new_page,
            route_web_socket=lambda _pattern, _handler: self.step("socket-route"),
            close=self.close_context,
        )
        self.browser = SimpleNamespace(
            new_context=self.new_context,
            close=lambda: self.step("close-browser"),
        )
        self.driver = SimpleNamespace(
            chromium=SimpleNamespace(name="chromium", launch=self.launch),
            stop=self.stop,
        )
        self.context_options: dict[str, Any] = {}

    def step(self, name: str) -> None:
        self.calls.append(name)
        if name == self.failure:
            raise self.primary

    def launch(self, **options: Any) -> Any:
        self.step("launch")
        assert options["args"] == ["--use-mock-keychain", "--password-store=basic"]
        return self.browser

    def new_context(self, **options: Any) -> Any:
        self.step("new-context")
        self.context_options = options
        return self.context

    def new_page(self) -> Any:
        self.step("new-page")
        return self.page

    def close_context(self) -> None:
        self.step("close-context")
        if self.failure and self.failure != "navigate":
            raise RuntimeError("synthetic context cleanup failure")

    def stop(self) -> None:
        self.step("stop-driver")
        if self.stop_fails:
            raise RuntimeError("synthetic driver cleanup failure")

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(
            sys.modules,
            "playwright.sync_api",
            SimpleNamespace(
                sync_playwright=lambda: SimpleNamespace(start=lambda: self.driver)
            ),
        )
        monkeypatch.setattr(
            _browser_readiness,
            "require_rendering_ready",
            lambda _browser: self.step("readiness"),
        )

    @property
    def cleanup(self) -> list[str]:
        return [
            call
            for call in self.calls
            if call.startswith("close-") or call == "stop-driver"
        ]


def _teach_hooks(acquisition: _Acquisition, monkeypatch: pytest.MonkeyPatch) -> None:
    def guard(*_args: Any) -> Any:
        acquisition.step("containment")
        return SimpleNamespace(ok=True)

    monkeypatch.setattr(worker_containment, "contain_workers", guard)
    monkeypatch.setattr(
        redirect_guard, "guard_redirects", lambda *_args: SimpleNamespace(ok=True)
    )


@pytest.mark.parametrize("failure", ["new-context", "route", "new-page", "containment"])
def test_teach_closes_only_acquired_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    acquisition = _Acquisition(failure)
    acquisition.install(monkeypatch)
    _teach_hooks(acquisition, monkeypatch)
    downloads = tmp_path / "downloads"

    with pytest.raises(_Cancelled) as raised:
        browser_teach._Browser("https://synthetic.invalid", downloads, True)

    assert raised.value is acquisition.primary
    context_cleanup = [] if failure == "new-context" else ["close-context"]
    assert acquisition.cleanup == [*context_cleanup, "close-browser", "stop-driver"]
    assert not downloads.exists()
    assert "navigate" not in acquisition.calls


def test_teach_transfers_the_contained_browser_to_its_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    acquisition = _Acquisition()
    acquisition.install(monkeypatch)
    _teach_hooks(acquisition, monkeypatch)
    browser = browser_teach._Browser("https://synthetic.invalid", tmp_path, True)

    assert acquisition.cleanup == []
    assert acquisition.context_options["service_workers"] == "block"
    assert acquisition.calls.index("route") < acquisition.calls.index("containment")
    assert acquisition.calls.index("socket-route") < acquisition.calls.index(
        "containment"
    )
    browser.close()
    browser.close()
    assert acquisition.cleanup == ["close-context", "close-browser", "stop-driver"]


def _qualification_opener(
    acquisition: _Acquisition, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Any:
    def make_backend(page: Any) -> Any:
        acquisition.step("backend")
        return page

    monkeypatch.setattr(_browser_setup, "ensure_chromium_installed", lambda: None)
    monkeypatch.setattr(playwright_backend, "PlaywrightBackend", make_backend)
    monkeypatch.setattr(
        playwright_backend,
        "prepare_ws_guard",
        lambda _page: acquisition.step("socket-guard"),
    )
    config = SimpleNamespace(
        app=SimpleNamespace(
            storage_state="state.json",
            headless=True,
            viewport=(800, 600),
            entry_url="https://synthetic.invalid/entry",
            url="https://synthetic.invalid",
        ),
        resolve_path=lambda value: tmp_path / value,
    )
    return session.playwright_session(config)


@pytest.mark.parametrize(
    "failure", ["new-context", "new-page", "socket-guard", "navigate", "backend"]
)
def test_qualification_closes_only_acquired_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    acquisition = _Acquisition(failure)
    acquisition.install(monkeypatch)
    open_session = _qualification_opener(acquisition, monkeypatch, tmp_path)

    with pytest.raises(_Cancelled) as raised:
        open_session()

    assert raised.value is acquisition.primary
    context_cleanup = [] if failure == "new-context" else ["close-context"]
    assert acquisition.cleanup == [*context_cleanup, "close-browser", "stop-driver"]
    if failure in ("new-context", "new-page", "socket-guard"):
        assert "navigate" not in acquisition.calls


def test_qualification_transfers_cleanup_with_the_guarded_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    acquisition = _Acquisition()
    acquisition.install(monkeypatch)
    open_session = _qualification_opener(acquisition, monkeypatch, tmp_path)
    backend, close = open_session()

    assert backend is acquisition.page
    assert acquisition.cleanup == []
    assert acquisition.context_options["storage_state"] == str(tmp_path / "state.json")
    assert acquisition.context_options["service_workers"] == "block"
    assert acquisition.calls.index("socket-guard") < acquisition.calls.index("navigate")
    close()
    close()
    assert acquisition.cleanup == ["close-context", "close-browser", "stop-driver"]


def _environment(
    acquisition: _Acquisition, monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, dict[int, Any]]:
    pytest.importorskip("openenv")
    from openadapt_flow.rl_env import environment

    monkeypatch.setattr(
        environment,
        "load_environment",
        lambda _path: SimpleNamespace(
            manifest=SimpleNamespace(name="synthetic", app=SimpleNamespace(kind="url"))
        ),
    )
    monkeypatch.setattr(environment, "_driver_in_own_session", lambda: None)
    monkeypatch.setattr(environment, "_driver_pid", lambda _pw: 1234)
    supervision = {
        1234: SimpleNamespace(close=lambda: acquisition.step("close-supervision"))
    }
    monkeypatch.setattr(environment, "_SUPERVISED", supervision)
    return environment.OpenAdaptWebEnvironment("synthetic"), supervision


@pytest.mark.parametrize(
    "failure", ["new-context", "route", "new-page", "page-timeout"]
)
def test_rl_closes_only_acquired_resources(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    acquisition = _Acquisition(failure)
    acquisition.install(monkeypatch)
    env, supervision = _environment(acquisition, monkeypatch)

    with pytest.raises(_Cancelled) as raised:
        env._ensure_page()

    assert raised.value is acquisition.primary
    context_cleanup = [] if failure == "new-context" else ["close-context"]
    assert acquisition.cleanup == [
        *context_cleanup,
        "close-browser",
        "stop-driver",
        "close-supervision",
    ]
    assert env._page is env._context is env._browser is env._pw is None
    assert env._driver_pid is None
    assert supervision == {}
    assert "navigate" not in acquisition.calls


def test_rl_failed_driver_stop_preserves_owned_supervision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openadapt_flow.rl_env.lifecycle import ProcessNotReaped

    acquisition = _Acquisition("route", stop_fails=True)
    acquisition.install(monkeypatch)
    env, supervision = _environment(acquisition, monkeypatch)
    launch = supervision[1234]

    with pytest.raises(_Cancelled) as raised:
        env._ensure_page()

    assert raised.value is acquisition.primary
    assert acquisition.cleanup == ["close-context", "close-browser", "stop-driver"]
    assert env._page is env._context is env._browser is None
    assert env._pw is acquisition.driver and env._driver_pid == 1234
    assert supervision == {1234: launch}
    assert env._unproven is not None
    with pytest.raises(ProcessNotReaped):
        env._ensure_page()
    assert acquisition.calls.count("launch") == 1


def test_rl_navigation_failure_never_publishes_an_episode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    acquisition = _Acquisition("navigate")
    acquisition.install(monkeypatch)
    env, supervision = _environment(acquisition, monkeypatch)
    from openadapt_flow.rl_env import environment

    def close_judge() -> None:
        acquisition.step("close-judge")
        raise RuntimeError("synthetic judge cleanup failure")

    task = SimpleNamespace(verify_text="synthetic", params={}, start_path="/entry")
    env.env.task = lambda _task_id: task
    monkeypatch.setattr(env, "_ensure_app", lambda: "https://synthetic.invalid")
    monkeypatch.setattr(env, "_reset_app", lambda _task: None)
    monkeypatch.setattr(environment, "render_verify_config", lambda *_args: {})
    judge = SimpleNamespace(close=close_judge)
    monkeypatch.setattr(environment, "EpisodeJudge", lambda *_args, **_kwargs: judge)

    with pytest.raises(_Cancelled) as raised:
        env._reset(task_id="synthetic")

    assert raised.value is acquisition.primary
    assert acquisition.cleanup == [
        "close-context",
        "close-context",
        "close-browser",
        "stop-driver",
        "close-supervision",
        "close-judge",
    ]
    assert acquisition.calls.count("navigate") == 1
    assert env._task is env._judge is env._page is env._pw is None
    assert env.state.done
    assert supervision == {}
