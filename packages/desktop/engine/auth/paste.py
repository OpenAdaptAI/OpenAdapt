"""PasteTokenProvider -- v1 token-paste auth (also the headless / BYOC path).

The user mints an ingest token in the cloud dashboard
(``/dashboard/settings/ingest``, shown once) and pastes it into the desktop
Login screen; a deep-link button opens that settings page in the system
browser. ``login()`` validates the token with a cheap authed
``GET /api/needs-attention/count`` and stores it as a ``kind="ingest_token"``
:class:`~engine.auth.provider.Credential`.

Non-interactive form (headless / CI / BYOC server): the token is read from
``OPENADAPT_INGEST_TOKEN`` with no prompt. ``is_available()`` is always True --
this provider is the universal fallback.

Spec: ``.private/desktop_tray_architecture_2026_07_14.md`` section 3a.
"""

from __future__ import annotations

import os

import httpx
from loguru import logger

from engine.auth.provider import Credential
from engine.auth.store import (
    DEFAULT_HOST,
    INGEST_TOKEN_ENV,
    store_credential,
)

# Where the user mints an ingest token; the Login screen deep-links here.
INGEST_SETTINGS_PATH = "/dashboard/settings/ingest"

# The cheap authed endpoint used to validate a token (resolves org + auth).
VALIDATE_PATH = "/api/needs-attention/count"


class TokenValidationError(Exception):
    """Raised when a pasted/env ingest token fails server-side validation."""


class PasteTokenProvider:
    """Interactive-paste + headless-env ingest-token provider.

    Args:
        host: Hosted base URL (e.g. ``https://app.openadapt.ai``).
        prompt: Callable returning the pasted token when interactive. Defaults
            to :func:`input`. Injected for tests / the desktop UI.
        timeout: HTTP timeout in seconds for token validation.
    """

    name = "paste"

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        prompt=input,
        timeout: float = 15.0,
    ) -> None:
        self.host = host.rstrip("/")
        self._prompt = prompt
        self._timeout = timeout

    def is_available(self) -> bool:
        """Token paste works everywhere -- always available."""
        return True

    @property
    def settings_url(self) -> str:
        """Deep-link the Login screen opens so the user can mint a token."""
        return f"{self.host}{INGEST_SETTINGS_PATH}"

    def login(self, token: str | None = None) -> Credential:
        """Authenticate with an ingest token and store the credential.

        Token source precedence: explicit ``token`` arg, then
        ``OPENADAPT_INGEST_TOKEN`` env (headless), then an interactive prompt.

        Args:
            token: An ingest token supplied directly (e.g. from the desktop UI).

        Returns:
            The stored ``Credential``.

        Raises:
            TokenValidationError: If no token is available or validation fails.
        """
        token = (token or os.environ.get(INGEST_TOKEN_ENV, "") or "").strip()
        if not token:
            token = self._prompt_for_token()

        if not token:
            raise TokenValidationError("No ingest token provided.")

        org_id = self._validate(token)

        cred: Credential = {
            "kind": "ingest_token",
            "token": token,
            "refresh_token": None,
            "org_id": org_id,
            "host": self.host,
            "expires_at": None,
        }
        store_credential(cred)
        logger.info("Stored ingest token for {host}", host=self.host)
        return cred

    def _prompt_for_token(self) -> str:
        """Prompt the user to paste a token, surfacing the mint URL."""
        print(f"Mint an ingest token at: {self.settings_url}")
        try:
            return (self._prompt("Paste your ingest token: ") or "").strip()
        except (EOFError, KeyboardInterrupt):
            return ""

    def _validate(self, token: str) -> str | None:
        """Validate a token via the count endpoint; return org_id if exposed.

        Raises:
            TokenValidationError: on any non-2xx / network failure.
        """
        url = f"{self.host}{VALIDATE_PATH}"
        headers = {"Authorization": f"Bearer {token}"}
        try:
            resp = httpx.get(url, headers=headers, timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise TokenValidationError(f"Could not reach {self.host}: {exc}") from exc

        if resp.status_code == 401:
            raise TokenValidationError("Ingest token was rejected (401).")
        if resp.status_code >= 400:
            raise TokenValidationError(
                f"Token validation failed ({resp.status_code})."
            )
        try:
            body = resp.json()
        except ValueError:
            body = {}
        return body.get("org_id")
