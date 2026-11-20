"""Refuse accidental headed Playwright launches in local pytest runs."""

from __future__ import annotations

import importlib
import os
import subprocess
from functools import wraps
from pathlib import Path
from typing import Any

import pytest


def _headed(options: dict[str, Any]) -> bool:
    ignored = options.get("ignore_default_args") or []
    return (
        options.get("headless") is False
        or options.get("devtools") is True
        or ignored is True
        or any(
            str(arg).split("=", 1)[0] in {"--headless", "-headless"} for arg in ignored
        )
        or bool(os.environ.get("PWDEBUG"))
    )


class _LaunchGuard:
    def __init__(self) -> None:
        self.patch = pytest.MonkeyPatch()
        self.item: Any = None
        self.shell_present: dict[Path, bool] = {}

    def options(self, options: dict[str, Any]) -> dict[str, Any]:
        if os.environ.get("CI", "").strip().lower() in {"true", "1"}:
            return options
        headed = _headed(options)
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

    def fixture_options(
        self, browser_type: Any, options: dict[str, Any], *, persistent: bool
    ) -> dict[str, Any]:
        options = self.options(options)
        if getattr(browser_type, "name", None) != "chromium":
            return options
        from openadapt_flow._browser_readiness import chromium_launch_args

        options["args"] = chromium_launch_args(options.get("args"))
        if (
            persistent
            or _headed(options)
            or options.get("executable_path") is not None
            or options.get("channel") is not None
        ):
            return options

        # Implicit headless launches select Playwright's matching shell. Only
        # override that choice when its shell is absent and its full build exists.
        from openadapt_flow._browser_setup import _bundled_present

        full = Path(browser_type.executable_path)
        if full.is_file():
            if full not in self.shell_present:
                try:
                    self.shell_present[full] = _bundled_present(only_shell=True)
                except (OSError, subprocess.SubprocessError):
                    return options
            if not self.shell_present[full]:
                options["executable_path"] = str(full)
        return options

    def sync_launch(self, original: Any, *, persistent: bool) -> Any:
        @wraps(original)
        def guarded(browser_type: Any, *args: Any, **kwargs: Any) -> Any:
            options = self.fixture_options(browser_type, kwargs, persistent=persistent)
            return original(browser_type, *args, **options)

        return guarded

    def async_launch(self, original: Any, *, persistent: bool) -> Any:
        @wraps(original)
        async def guarded(browser_type: Any, *args: Any, **kwargs: Any) -> Any:
            options = self.fixture_options(browser_type, kwargs, persistent=persistent)
            return await original(browser_type, *args, **options)

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
                    browser_type,
                    name,
                    wrap(
                        getattr(browser_type, name),
                        persistent=name == "launch_persistent_context",
                    ),
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
