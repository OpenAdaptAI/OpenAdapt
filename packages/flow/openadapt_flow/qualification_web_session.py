"""Identify a web application by its address and signed-in session.

Real systems such as OpenEMR or openIMIS carry none of OpenAdapt's
``openadapt-*`` ``<meta>`` markers, so the backend's built-in observer cannot
label them. This observer identifies them by what any web application has:

* **application identity:** the page's exact HTTP(S) origin;
* **session identity:** a SHA-256 digest of the browser's cookies for that
  origin. No cookie means nobody is signed in, and the observation fails;
* **application version:** the version the operator confirmed in Check it.
  The page can't report it, so the confirmation is bound into the observer's
  contract digest and the environment digest. A project confirmed for one
  version never matches an observer built for another.

Every check stays fail-closed. The replayer still compares the origin, the
contract digest and the environment digest with the project, and re-observes
the session at every input edge. A sign-out, a new login or a move to another
origin stops the run before the next input.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal
from urllib.parse import urlsplit

from openadapt_flow.qualification_environment import (
    QualificationEnvironmentObservation,
)

SIGNED_IN_WEB_SESSION_OBSERVER_ID = "web-signed-in-session"

_CONTRACT_DOMAIN = b"openadapt.web-signed-in-session-observer/v1\x00"
_ENVIRONMENT_DOMAIN = b"openadapt.web-signed-in-session-environment/v1\x00"
_SESSION_DOMAIN = b"openadapt.web-signed-in-session-cookies/v1\x00"


def _clean_version(version: object) -> str:
    if not isinstance(version, str) or not version.strip() or len(version) > 128:
        raise ValueError("the confirmed application version must be 1-128 characters")
    if version != version.strip():
        raise ValueError("the confirmed application version has surrounding spaces")
    return version


def web_origin(url: object) -> str:
    """Return the exact ``scheme://host[:port]`` origin of ``url`` or raise."""

    if not isinstance(url, str):
        raise ValueError("page address is unavailable")
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ValueError("page is not an HTTP(S) address")
    host = (parts.hostname or "").lower()
    if not host or len(host) > 253:
        raise ValueError("page address has no host")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("page address has an invalid port") from exc
    rendered = f"[{host}]" if ":" in host else host
    origin = f"{scheme}://{rendered}"
    if port is not None and (scheme, port) not in {("http", 80), ("https", 443)}:
        origin += f":{port}"
    return origin


def signed_in_web_session_contract_sha256(confirmed_version: str) -> str:
    """The observer contract digest for one confirmed version."""

    version = _clean_version(confirmed_version)
    return hashlib.sha256(_CONTRACT_DOMAIN + version.encode("utf-8")).hexdigest()


def signed_in_web_session_environment_digest(
    origin: str, confirmed_version: str
) -> str:
    """The environment digest Check it records for ``origin`` at that version."""

    if web_origin(origin) != origin:
        raise ValueError("origin must be an exact scheme://host[:port] origin")
    version = _clean_version(confirmed_version)
    payload = json.dumps([origin, version], separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(_ENVIRONMENT_DOMAIN + payload).hexdigest()


def _session_digest(cookies: object) -> str:
    if not isinstance(cookies, list):
        raise ValueError("browser cookies are unavailable")
    rows = []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            raise ValueError("browser returned a malformed cookie")
        name, value = cookie.get("name"), cookie.get("value")
        if not isinstance(name, str) or not isinstance(value, str):
            raise ValueError("browser returned a malformed cookie")
        if value:
            rows.append(
                [
                    name,
                    str(cookie.get("domain", "")),
                    str(cookie.get("path", "")),
                    value,
                ]
            )
    if not rows:
        raise ValueError("no signed-in session: the page has no session cookie")
    rows.sort()
    payload = json.dumps(rows, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(_SESSION_DOMAIN + payload).hexdigest()


class SignedInWebSessionObserver:
    """Observe a web application by origin and signed-in session."""

    def __init__(self, confirmed_application_version: str) -> None:
        self._version = _clean_version(confirmed_application_version)
        self._contract = signed_in_web_session_contract_sha256(self._version)

    @property
    def observer_id(self) -> str:
        return SIGNED_IN_WEB_SESSION_OBSERVER_ID

    @property
    def contract_sha256(self) -> str:
        return self._contract

    def observe(
        self,
        backend: Any,
        target_kind: Literal["web", "windows", "macos", "linux", "rdp", "citrix"],
    ) -> QualificationEnvironmentObservation:
        if target_kind != "web":
            raise ValueError("the signed-in session observer only reads web pages")
        page = getattr(backend, "page", None)
        if page is None:
            raise ValueError("backend has no browser page")
        origin = web_origin(page.url)
        cookies = page.context.cookies([page.url])
        session = _session_digest(cookies)
        # Read the address again: a navigation between the two reads would
        # pair one origin with another origin's cookies.
        if web_origin(page.url) != origin:
            raise ValueError("page address changed during observation")
        return QualificationEnvironmentObservation(
            target_kind="web",
            application_identity=origin,
            application_version=self._version,
            session_identity_sha256=session,
            environment_digest=signed_in_web_session_environment_digest(
                origin, self._version
            ),
        )
