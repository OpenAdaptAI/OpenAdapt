"""Sign-in and reset for the local Odoo fixture.

``benchmark/odoo_contacts/verify.yaml`` names these functions. They are
environment setup for a synthetic test copy, not workflow logic:

- ``open_session`` signs the dedicated user into Odoo and opens a new
  contact;
- ``reset`` archives every person contact (the cases create their own);
- ``save_signed_in_state`` writes a Playwright storage state for the
  dedicated user: what a user of the registry skill binds as
  ``SIGNED_IN_STATE``, since a shipped contract names no Python hooks.

Environment (optional): ``ODOO_STATE_DIR``.
"""

from __future__ import annotations

import atexit
import importlib.util
import os
import sys
from pathlib import Path
from typing import Any, Callable

_HERE = Path(__file__).resolve().parent


def _fixture_module() -> Any:
    # Loaded by path: another installed ``benchmark`` package can shadow
    # this repository's namespace package.
    name = "openadapt_benchmark_odoo_fixture"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, _HERE / "fixture.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


_fixture: Any = None
_browser: Any = None


def fixture() -> Any:
    global _fixture
    if _fixture is None:
        _fixture = _fixture_module().OdooFixture()
    return _fixture


def record_password() -> str:
    """The record check's read-only database password, exported for the verifier."""

    password = fixture().secrets()["record_check_password"]
    os.environ["ODOO_RECORD_PASSWORD"] = password
    return password


def _shared_browser() -> Any:
    global _browser
    if _browser is None:
        from playwright.sync_api import sync_playwright

        from openadapt_flow._browser_setup import ensure_chromium_installed

        ensure_chromium_installed()
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
        _browser = (pw, browser)

        def _stop() -> None:
            try:
                browser.close()
            finally:
                pw.stop()

        atexit.register(_stop)
    return _browser[1]


def sign_in(page: Any) -> None:
    fx = fixture()
    login = _fixture_module().ACTOR_LOGIN
    page.goto(f"{fx.ui_url}web/login", wait_until="load", timeout=60_000)
    page.locator("input[name=login]").fill(login)
    page.locator("input[name=password]").fill(fx.secrets()["actor_password"])
    page.get_by_role("button", name="Log in").click()
    page.wait_for_url(lambda url: "/web/login" not in url, timeout=60_000)


def open_form(page: Any) -> None:
    fx = fixture()
    page.goto(f"{fx.ui_url}odoo/contacts/new", wait_until="load", timeout=60_000)
    page.locator(".o_form_view").wait_for(state="visible", timeout=30_000)


def open_session(config: Any) -> tuple[Any, Callable[[], None]]:
    """A fresh signed-in context at a new contact."""

    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    record_password()
    width, height = config.app.viewport
    context = _shared_browser().new_context(
        viewport={"width": width, "height": height}, device_scale_factor=1
    )
    page = context.new_page()
    sign_in(page)
    open_form(page)
    return PlaywrightBackend(page), context.close


def save_signed_in_state(path: str) -> str:
    """Write the dedicated user's signed-in browser state to ``path``."""

    context = _shared_browser().new_context()
    try:
        page = context.new_page()
        sign_in(page)
        context.storage_state(path=path)
    finally:
        context.close()
    os.chmod(path, 0o600)
    return path


def reset() -> None:
    """Archive every person contact on the test copy."""

    fixture().archive_all_contacts()
    record_password()


if __name__ == "__main__":
    # python benchmark/odoo_contacts/qualify_hooks.py state PATH | reset
    if sys.argv[1:2] == ["state"]:
        print(save_signed_in_state(sys.argv[2]))
    elif sys.argv[1:2] == ["reset"]:
        reset()
    else:
        raise SystemExit("usage: qualify_hooks.py state PATH | reset")
