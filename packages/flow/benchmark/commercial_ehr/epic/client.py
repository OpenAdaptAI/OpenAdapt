"""A backend client for Epic's public FHIR sandbox (open.epic), R4.

Epic's backend OAuth: the app signs a short-lived JWT with its private key,
Epic checks it against the app's registered JWK Set URL, and returns an
access token for ``client_credentials``. Credentials come from the
environment (``EPIC_SANDBOX_CLIENT_ID``, ``EPIC_SANDBOX_PRIVATE_KEY``,
``EPIC_SANDBOX_KID``, ``EPIC_SANDBOX_ALG``) and are never logged or written.

The sandbox is shared by every Epic developer and holds synthetic data only.
Keep calls few and run-unique (Epic's "Responsible Sandbox Testing").
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Mapping, Optional

import httpx

BASE = "https://fhir.epic.com/interconnect-fhir-oauth"
FHIR = f"{BASE}/api/FHIR/R4"
TOKEN_URL = f"{BASE}/oauth2/token"


class EpicError(RuntimeError):
    pass


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise EpicError(f"{name} is not set")
    return value


def client_assertion(now: Optional[float] = None) -> str:
    import jwt

    now = time.time() if now is None else now
    client_id = _env("EPIC_SANDBOX_CLIENT_ID")
    key = Path(_env("EPIC_SANDBOX_PRIVATE_KEY")).expanduser().read_text()
    claims = {
        "iss": client_id,
        "sub": client_id,
        "aud": TOKEN_URL,
        "jti": uuid.uuid4().hex,
        "iat": int(now),
        "nbf": int(now),
        "exp": int(now) + 240,
    }
    headers = {"kid": _env("EPIC_SANDBOX_KID"), "typ": "JWT"}
    return jwt.encode(
        claims, key, algorithm=os.environ.get("EPIC_SANDBOX_ALG", "RS384"),
        headers=headers,
    )


class EpicClient:
    """One backend identity. ``get`` and ``post`` return (status, headers, body)."""

    def __init__(self, timeout_s: float = 30.0) -> None:
        self._http = httpx.Client(timeout=timeout_s, follow_redirects=False)
        self._token: Optional[str] = None
        self._expires = 0.0
        self.calls = 0

    def token(self) -> str:
        if self._token and time.time() < self._expires - 30:
            return self._token
        resp = self._http.post(
            TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_assertion_type": (
                    "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
                ),
                "client_assertion": client_assertion(),
            },
        )
        self.calls += 1
        if resp.status_code != 200:
            # The body names the OAuth error (invalid_client and so on); it
            # holds no secret.
            raise EpicError(f"token: HTTP {resp.status_code} {resp.text[:300]}")
        body = resp.json()
        self._token = body["access_token"]
        self._expires = time.time() + float(body.get("expires_in", 300))
        self.scope = body.get("scope", "")
        return self._token

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/fhir+json",
            "Authorization": f"Bearer {self.token()}",
        }

    def get(
        self, path: str, params: Optional[Mapping[str, Any]] = None
    ) -> tuple[int, dict[str, str], Any]:
        url = path if path.startswith("http") else f"{FHIR}/{path}"
        resp = self._http.get(url, params=dict(params or {}), headers=self._headers())
        self.calls += 1
        return resp.status_code, dict(resp.headers), _json(resp)

    def post(
        self, path: str, body: Mapping[str, Any], prefer: str = "return=representation"
    ) -> tuple[int, dict[str, str], Any]:
        headers = {**self._headers(), "Content-Type": "application/fhir+json",
                   "Prefer": prefer}
        resp = self._http.post(f"{FHIR}/{path}", content=json.dumps(body),
                               headers=headers)
        self.calls += 1
        return resp.status_code, dict(resp.headers), _json(resp)

    def getter(self):
        """A ``FhirReader`` getter: ``get(url, params) -> (status, headers, json)``."""

        return lambda url, params: self.get(url, params)


def _json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None
