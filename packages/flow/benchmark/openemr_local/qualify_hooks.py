"""Sign-in, reset and read-back session for the local OpenEMR fixture.

``benchmark/openemr_local/verify.yaml`` names these three functions. They are
environment setup for a made-up-patient test system, not workflow logic:
OpenEMR's sign-in opens the new-patient form through its tab shell, the
fixture resets by restoring a database snapshot, and the read-back client
uses an OAuth password grant that a static header cannot express.

Environment (all optional):

- ``OPENEMR_FIXTURE_STATE``: fixture state directory
  (default ``benchmark/openemr_local/state``);
- ``OPENEMR_FIXTURE_LOCK``: a different environment lock file;
- ``OPENEMR_UI_URL`` / ``OPENEMR_API_URL``: non-default ports.
"""

from __future__ import annotations

import atexit
import os
import time
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
_fixture: Any = None
_browser: Any = None


def fixture() -> Any:
    """The fixture named by the environment, created once per process."""

    global _fixture
    if _fixture is not None:
        return _fixture
    import benchmark.openemr_local.fixture as fx

    lock = os.environ.get("OPENEMR_FIXTURE_LOCK")
    if lock:
        fx.LOCK_PATH = Path(lock)
    ui = os.environ.get("OPENEMR_UI_URL")
    api = os.environ.get("OPENEMR_API_URL")

    class _Fixture(fx.OpenEMRFixture):
        @property
        def ui_base_url(self) -> str:
            return ui or super().ui_base_url

        @property
        def api_base_url(self) -> str:
            return api or super().api_base_url

        def _validate_runtime_env(self) -> None:
            if ui or api:  # non-default ports: the runtime env names them
                self.runtime_env.chmod(0o600)
                return
            super()._validate_runtime_env()

    _fixture = _Fixture(Path(os.environ.get("OPENEMR_FIXTURE_STATE", HERE / "state")))
    return _fixture


def _shared_browser() -> Any:
    global _browser
    if _browser is None:
        from openadapt_flow._browser_setup import ensure_chromium_installed
        from playwright.sync_api import sync_playwright

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


def open_session(config: Any) -> tuple[Any, Callable[[], None]]:
    """A fresh signed-in context at the new-patient form."""

    from scripts.openemr_local_demo import (
        OpenEMRPlaywrightBackend,
        _preauthenticate_browser,
    )

    fx = fixture()
    width, height = config.app.viewport
    context = _shared_browser().new_context(
        viewport={"width": width, "height": height}, device_scale_factor=1
    )
    page = context.new_page()
    page.goto(fx.ui_base_url)
    backend = OpenEMRPlaywrightBackend(page)
    _preauthenticate_browser(backend, fx)
    return backend, context.close


def reset() -> None:
    """Restore the verified baseline database."""

    fixture().reset()


def oracle_session(config: Any, attempts: int = 5) -> Any:
    """A read-only OAuth client; retries transport errors right after a reset."""

    import httpx

    for attempt in range(1, attempts + 1):
        try:
            return fixture().token_session("oracle")
        except httpx.TransportError:
            if attempt == attempts:
                raise
            time.sleep(2.0 * attempt)
    raise AssertionError("unreachable")

