"""A generic environment observer for a web application.

The runtime re-observes the environment at every input edge and compares it
with the qualification project. This observer reads what any web page has:
its origin (the application identity), a digest of the browser session, and
a digest of the browser, viewport, platform, runtime and the application
version you declared (the page cannot report it).
"""

from __future__ import annotations

import hashlib
import json
import platform
from typing import Any, Literal

from openadapt_flow import __version__ as FLOW_VERSION
from openadapt_flow.qualification_environment import (
    QualificationEnvironmentObservation,
)

WEB_OBSERVER_ID = "openadapt.qualify.web-environment"
WEB_OBSERVER_CONTRACT_SHA256 = hashlib.sha256(
    b"openadapt.qualify.web-environment/v1\n"
    b"application_identity: page origin\n"
    b"session_identity: sha256(origin, timeOrigin, userAgent)\n"
    b"environment: sha256(browser, viewport, platform, runtime, declared version)\n"
).hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def web_environment_payload(
    backend: Any, *, application_version: str, headless: bool
) -> dict[str, Any]:
    """The PHI-free browser and application boundary read from the live page."""

    page = backend.page
    browser = page.context.browser
    if browser is None:
        raise ValueError("the qualification page has no owning browser")
    return {
        "browser": "chromium",
        "browser_version": browser.version,
        "user_agent": page.evaluate("navigator.userAgent"),
        "viewport": list(backend.viewport),
        "device_scale_factor": page.evaluate("window.devicePixelRatio"),
        "headless": bool(headless),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "application": application_version,
        "runtime_version": FLOW_VERSION,
        "target_kind": "web",
    }


def web_environment_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload)).hexdigest()


class WebEnvironmentObserver:
    """Observe the exact live browser boundary for qualification and runs."""

    observer_id = WEB_OBSERVER_ID
    contract_sha256 = WEB_OBSERVER_CONTRACT_SHA256

    def __init__(self, *, application_version: str, headless: bool = True) -> None:
        self._application_version = application_version
        self._headless = headless

    def observe(
        self,
        backend: Any,
        target_kind: Literal["web", "windows", "macos", "linux", "rdp", "citrix"],
    ) -> QualificationEnvironmentObservation:
        if target_kind != "web" or getattr(backend, "page", None) is None:
            raise ValueError("the web environment observer needs a browser page")
        page = backend.page
        origin = page.evaluate("location.origin")
        session_material = page.evaluate(
            """() => JSON.stringify({origin: location.origin,
                timeOrigin: performance.timeOrigin,
                userAgent: navigator.userAgent})"""
        )
        payload = web_environment_payload(
            backend,
            application_version=self._application_version,
            headless=self._headless,
        )
        return QualificationEnvironmentObservation(
            target_kind="web",
            application_identity=origin,
            application_version=self._application_version,
            session_identity_sha256=hashlib.sha256(
                session_material.encode("utf-8")
            ).hexdigest(),
            environment_digest=web_environment_digest(payload),
        )


def observe_web_environment(
    url: str, *, application_version: str, headless: bool = True
) -> dict[str, str]:
    """Open ``url`` once, read its environment, and close it.

    For binding a qualification project to the generic web observer before
    the first case runs: the digest is the one a governed run of the same
    browser on this machine observes. Nothing on the page is clicked or typed.
    """

    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    backend, close = PlaywrightBackend.launch(url, headless=headless)
    try:
        observed = WebEnvironmentObserver(
            application_version=application_version, headless=headless
        ).observe(backend, "web")
    finally:
        close()
    return {
        "environment_observer_id": WEB_OBSERVER_ID,
        "environment_observer_contract_sha256": WEB_OBSERVER_CONTRACT_SHA256,
        "application_identity": observed.application_identity,
        "application_version": observed.application_version,
        "environment_digest": observed.environment_digest,
    }


__all__ = [
    "WEB_OBSERVER_CONTRACT_SHA256",
    "WEB_OBSERVER_ID",
    "WebEnvironmentObserver",
    "observe_web_environment",
    "web_environment_digest",
    "web_environment_payload",
]
