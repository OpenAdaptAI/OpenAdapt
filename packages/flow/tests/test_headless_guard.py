"""Local browser launches must opt in twice before they can open a window.

Launch targets below are in-memory fakes, including the allowed-headed cases.
No test of the guard starts a browser.
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from tests.headless_guard import _LaunchGuard


@pytest.fixture
def launches(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("OPENADAPT_ALLOW_HEADED", raising=False)
    calls: list[dict[str, Any]] = []

    class SyncBrowserType:
        def launch(self, **kwargs):
            calls.append(kwargs)
            return kwargs

        def launch_persistent_context(self, user_data_dir, **kwargs):
            calls.append(kwargs)
            return kwargs

    class AsyncBrowserType:
        async def launch(self, **kwargs):
            calls.append(kwargs)
            return kwargs

        async def launch_persistent_context(self, user_data_dir, **kwargs):
            calls.append(kwargs)
            return kwargs

    monkeypatch.setitem(
        sys.modules, "playwright.sync_api", SimpleNamespace(BrowserType=SyncBrowserType)
    )
    monkeypatch.setitem(
        sys.modules,
        "playwright.async_api",
        SimpleNamespace(BrowserType=AsyncBrowserType),
    )
    guard = _LaunchGuard()
    guard.install()
    try:
        yield guard, (SyncBrowserType(), AsyncBrowserType()), calls
    finally:
        guard.patch.undo()


def _launch(browser_type, *, asynchronous: bool, persistent: bool, **kwargs):
    result = (
        browser_type.launch_persistent_context("synthetic-profile", **kwargs)
        if persistent
        else browser_type.launch(**kwargs)
    )
    return asyncio.run(result) if asynchronous else result


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("persistent", [False, True])
@pytest.mark.parametrize("options", [{}, {"headless": None}, {"headless": True}])
def test_every_launch_defaults_to_headless(launches, asynchronous, persistent, options):
    _, browsers, calls = launches
    result = _launch(
        browsers[asynchronous],
        asynchronous=asynchronous,
        persistent=persistent,
        **options,
    )
    assert result["headless"] is True
    assert calls == [result]


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("persistent", [False, True])
@pytest.mark.parametrize(
    "options",
    [
        {"headless": False},
        {"devtools": True},
        {"headless": True, "ignore_default_args": True},
        {"headless": True, "ignore_default_args": ["--headless"]},
    ],
)
def test_accidental_headed_launch_never_reaches_the_browser(
    launches, asynchronous, persistent, options
):
    _, browsers, calls = launches
    with pytest.raises(pytest.fail.Exception, match="headed Playwright launch refused"):
        _launch(
            browsers[asynchronous],
            asynchronous=asynchronous,
            persistent=persistent,
            **options,
        )
    assert calls == []


@pytest.mark.parametrize(
    "marked,enabled", [(False, False), (False, True), (True, False), (True, True)]
)
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("persistent", [False, True])
def test_both_opt_ins_are_required(
    launches, monkeypatch, marked, enabled, asynchronous, persistent
):
    guard, browsers, calls = launches
    guard.item = SimpleNamespace(
        get_closest_marker=lambda name: object() if marked else None
    )
    if enabled:
        monkeypatch.setenv("OPENADAPT_ALLOW_HEADED", "1")
    if marked and enabled:
        result = _launch(
            browsers[asynchronous],
            asynchronous=asynchronous,
            persistent=persistent,
            headless=False,
        )
        assert result["headless"] is False
        assert calls == [result]
    else:
        with pytest.raises(pytest.fail.Exception, match="OPENADAPT_ALLOW_HEADED=1"):
            _launch(
                browsers[asynchronous],
                asynchronous=asynchronous,
                persistent=persistent,
                headless=False,
            )
        assert calls == []


def test_ci_keeps_its_launch_options(launches, monkeypatch):
    _, browsers, calls = launches
    monkeypatch.setenv("CI", "true")
    assert browsers[0].launch(headless=False) == {"headless": False}
    assert calls == [{"headless": False}]


def test_harmless_ignored_arguments_keep_headless_mode(launches):
    _, browsers, _ = launches
    assert browsers[0].launch(ignore_default_args=["--mute-audio"]) == {
        "ignore_default_args": ["--mute-audio"],
        "headless": True,
    }
