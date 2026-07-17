"""Hosted authentication -- one interface, two providers, one keychain store.

Public API (import from here):
    - ``Credential`` / ``AuthProvider``      the shared contract (``provider``)
    - ``store_credential`` / ``load_credential`` / ``clear_credential``
      / ``auth_header`` / ``active_credential``   the keychain store (``store``)
    - ``PasteTokenProvider``                 token paste / headless (``paste``)
    - ``BrowserPkceProvider``                system-browser + loopback PKCE (``browser_pkce``)
    - ``available_providers`` / ``login``    provider dispatch (this module)

Only W1a edits ``provider``/``paste``/``store``; W1b owns ``browser_pkce``.
Everything else imports and consumes -- swapping providers is additive.
"""

from __future__ import annotations

from engine.auth.browser_pkce import BrowserPkceProvider
from engine.auth.paste import PasteTokenProvider, TokenValidationError
from engine.auth.provider import AuthProvider, Credential
from engine.auth.store import (
    DEFAULT_HOST,
    active_credential,
    active_host,
    auth_header,
    clear_credential,
    load_credential,
    store_credential,
)

__all__ = [
    "AuthProvider",
    "Credential",
    "PasteTokenProvider",
    "BrowserPkceProvider",
    "TokenValidationError",
    "DEFAULT_HOST",
    "store_credential",
    "load_credential",
    "clear_credential",
    "active_credential",
    "active_host",
    "auth_header",
    "available_providers",
    "login",
]


def available_providers(host: str = DEFAULT_HOST) -> list[AuthProvider]:
    """Return the provider chain for ``host``, best interactive option first.

    ``BrowserPkceProvider`` leads on an interactive desktop; it self-reports
    ``is_available() == False`` on a headless box, where ``PasteTokenProvider``
    (always available) is used instead.
    """
    return [BrowserPkceProvider(host=host), PasteTokenProvider(host=host)]


def login(host: str = DEFAULT_HOST, prefer: str | None = None) -> Credential:
    """Authenticate against ``host`` using the first available provider.

    Args:
        host: Hosted base URL.
        prefer: Force a provider by ``name`` (``"paste"`` or ``"browser_pkce"``).

    Returns:
        The stored ``Credential``.

    Raises:
        RuntimeError: If no provider could complete a login.
    """
    providers = available_providers(host)
    if prefer:
        providers = [p for p in providers if p.name == prefer] or providers

    last_error: Exception | None = None
    for provider in providers:
        if not provider.is_available():
            continue
        try:
            return provider.login()
        except Exception as exc:  # try the next provider (e.g. browser -> paste)
            last_error = exc
    if last_error:
        raise RuntimeError(f"Login failed: {last_error}") from last_error
    raise RuntimeError("No auth provider is available on this machine.")
