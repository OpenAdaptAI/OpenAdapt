"""Transport-only primitives for outbound decision relay traffic.

This module has no workflow, runtime, continuation, replay, or actuator imports.
Services that only exchange signed decision data can use it without loading an
execution path into their process.
"""

from __future__ import annotations

import os
import re
from typing import Any, Optional, Protocol

_MAX_RELAY_BYTES = 16 * 1024
_RUNNER_TOKEN_RE = re.compile(r"^oar_[A-Za-z0-9_-]{28,}$")


class RelayRefused(RuntimeError):
    """The relay could not be used safely. Nothing was executed."""


class RelayTransport(Protocol):
    """The one network capability required by a decision relay."""

    def post(
        self, path: str, payload: dict[str, Any], *, timeout_s: float
    ) -> tuple[int, dict[str, Any]]:
        """POST ``payload`` and return ``(status, decoded_body)``.

        Raises:
            RelayUncertain: If the request left the process without a terminal
                response, so its effect is unknown.
        """


class RelayUncertain(RuntimeError):
    """A request may have been delivered. The caller must not blindly retry."""


class HttpxRelayTransport:
    """``httpx`` over HTTPS to one exact origin, with a bearer runner token."""

    def __init__(self, origin: str, token: str) -> None:
        import httpx

        from openadapt_flow.hosted import _origin

        resolved = _origin(origin)
        if not resolved.startswith("https://"):
            raise RelayRefused(
                "the decision relay origin must be https; OpenAdapt does not "
                "send a runner credential over plaintext"
            )
        self._origin = resolved
        self._token = token
        self._httpx = httpx

    def post(
        self, path: str, payload: dict[str, Any], *, timeout_s: float
    ) -> tuple[int, dict[str, Any]]:
        try:
            response = self._httpx.post(
                f"{self._origin}{path}",
                json=payload,
                headers={
                    "authorization": f"Bearer {self._token}",
                    "content-type": "application/json",
                },
                timeout=timeout_s,
            )
        except Exception as exc:  # noqa: BLE001 - any transport failure is uncertain
            # The request may have been fully written and processed before the
            # failure. Classify, never assume it did not arrive.
            raise RelayUncertain(f"decision relay request did not complete: {exc}")
        if response.status_code == 204 or not response.content:
            return response.status_code, {}
        if len(response.content) > _MAX_RELAY_BYTES:
            raise RelayRefused("the decision relay response exceeded its size bound")
        try:
            body = response.json()
        except ValueError:
            return response.status_code, {}
        return response.status_code, body if isinstance(body, dict) else {}


def resolve_runner_token(token: Optional[str] = None) -> str:
    """Resolve the runner credential from an argument or the environment."""

    resolved = (token or os.environ.get("OPENADAPT_RUNNER_TOKEN") or "").strip()
    if not resolved:
        raise RelayRefused(
            "no runner token; set OPENADAPT_RUNNER_TOKEN or pass one explicitly"
        )
    if not _RUNNER_TOKEN_RE.match(resolved):
        raise RelayRefused(
            "the runner token is not a control-plane runner credential "
            "(expected an 'oar_' token); a portal or ingest credential is not "
            "interchangeable with one"
        )
    return resolved


__all__ = [
    "HttpxRelayTransport",
    "RelayRefused",
    "RelayTransport",
    "RelayUncertain",
    "resolve_runner_token",
]
