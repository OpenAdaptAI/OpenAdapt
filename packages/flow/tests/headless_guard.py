"""Refuse accidental headed Playwright launches in local pytest runs."""

from __future__ import annotations

import importlib
import os
from functools import wraps
from typing import Any

import pytest


class _LaunchGuard:
    def __init__(self) -> None:
        self.patch = pytest.MonkeyPatch()
        self.item: Any = None

    def options(self, options: dict[str, Any]) -> dict[str, Any]:
        if os.environ.get("CI", "").strip().lower() in {"true", "1"}:
            return options
        ignored = options.get("ignore_default_args") or []
        removes_headless = ignored is True or any(
            str(arg).split("=", 1)[0] in {"--headless", "-headless"} for arg in ignored
        )
        headed = (
            options.get("headless") is False
            or options.get("devtools") is True
            or removes_headless
            or bool(os.environ.get("PWDEBUG"))
        )
        allowed = (
            self.item is not None
            and self.item.get_closest_marker("headed") is not None
            and os.environ.get("OPENADAPT_ALLOW_HEADED") == "1"
        )
        if headed and not allowed:
            pytest.fail(
                "headed Playwright launch refused: run browser tests headless; "
                "a deliberate headed test needs @pytest.mark.headed and "
                "OPENADAPT_ALLOW_HEADED=1",
                pytrace=False,
            )
        if not headed:
            options["headless"] = True
        return options

    def sync_launch(self, original: Any) -> Any:
        @wraps(original)
        def guarded(browser_type: Any, *args: Any, **kwargs: Any) -> Any:
            return original(browser_type, *args, **self.options(kwargs))

        return guarded

    def async_launch(self, original: Any) -> Any:
        @wraps(original)
        async def guarded(browser_type: Any, *args: Any, **kwargs: Any) -> Any:
            return await original(browser_type, *args, **self.options(kwargs))

        return guarded

    def install(self) -> None:
        for api, wrap in (
            ("playwright.sync_api", self.sync_launch),
            ("playwright.async_api", self.async_launch),
        ):
            try:
                browser_type = importlib.import_module(api).BrowserType
            except ModuleNotFoundError as exc:
                if exc.name not in {"playwright", api}:
                    raise
                continue
            for name in ("launch", "launch_persistent_context"):
                self.patch.setattr(
                    browser_type, name, wrap(getattr(browser_type, name))
                )

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(self, item: Any):
        self.item = item
        try:
            yield
        finally:
            self.item = None

    def pytest_unconfigure(self) -> None:
        self.patch.undo()


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "headed: deliberately opens a browser; requires OPENADAPT_ALLOW_HEADED=1",
    )
    guard = _LaunchGuard()
    guard.install()
    config.pluginmanager.register(guard, "openadapt-headless-launches")
