"""Sign-in, reset and the record-check token for the local Medplum fixture.

``benchmark/fhir_medplum/verify.yaml`` names these functions. They are
environment setup for a synthetic test server, not workflow logic:

- ``open_session`` signs the dedicated user into the web app and opens the
  new-patient form;
- ``reset`` deletes every Patient (the cases create their own);
- ``record_token`` sets ``MEDPLUM_RECORD_TOKEN`` (the FHIR verifier reads it)
  from the record-check client. The FHIR verifier takes a token from the
  environment only, so a long run refreshes it with this function.

Environment (all optional): ``MEDPLUM_STATE_DIR``, ``MEDPLUM_UI_URL``.
"""

from __future__ import annotations

import atexit
import os
from typing import Any, Callable

from benchmark.fhir_medplum.fixture import MedplumFixture

#: The dedicated user the program runs as (created by ``ensure_actor``).
ACTOR_EMAIL = "openadapt.actor@example.invalid"

_fixture: Any = None
_browser: Any = None


def fixture() -> MedplumFixture:
    global _fixture
    if _fixture is None:
        _fixture = MedplumFixture()
    return _fixture


def actor_password() -> str:
    return fixture().ensure_user(ACTOR_EMAIL, "OpenAdapt", "Actor")


def record_token() -> str:
    """A fresh record-check token, also exported for the FHIR verifier."""

    token = fixture().client_token("record-check")
    os.environ["MEDPLUM_RECORD_TOKEN"] = token
    return token


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


def sign_in(page: Any, email: str, password: str) -> None:
    fx = fixture()
    page.goto(f"{fx.ui_url}signin", wait_until="networkidle", timeout=60_000)
    page.locator("input[name=email]").fill(email)
    page.get_by_role("button", name="Continue").click()
    page.locator("input[name=password]").fill(password)
    page.get_by_role("button", name="Sign In").click()
    page.wait_for_url(lambda url: "/signin" not in url, timeout=60_000)


def open_form(page: Any) -> None:
    fx = fixture()
    page.goto(f"{fx.ui_url}Patient/new", wait_until="networkidle", timeout=60_000)
    page.get_by_role("button", name="Create").wait_for(state="visible", timeout=30_000)


def open_session(config: Any) -> tuple[Any, Callable[[], None]]:
    """A fresh signed-in context at the new-patient form."""

    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    record_token()
    width, height = config.app.viewport
    context = _shared_browser().new_context(
        viewport={"width": width, "height": height}, device_scale_factor=1
    )
    page = context.new_page()
    sign_in(page, ACTOR_EMAIL, actor_password())
    open_form(page)
    return PlaywrightBackend(page), context.close


def reset() -> None:
    """Delete every Patient on the test server."""

    fx = fixture()
    token = fx.client_token("harness")
    for pid in fx.patient_ids(token=token):
        fx.delete_patient(pid, token=token)
    record_token()
