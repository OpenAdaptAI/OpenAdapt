"""Service configuration, env-driven (12-factor)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


@dataclass
class ServiceConfig:
    """Runtime configuration for the VLM service.

    All fields are overridable by environment variables so the customer runs one
    command on the GPU box. The bearer ``token`` gates every inference endpoint;
    an on-prem appliance is still authenticated.
    """

    backend: str = "stub"
    model: Optional[str] = None
    token: str = ""  # shared bearer token; empty => auth disabled (dev only)
    window_ms: float = 15.0
    max_batch_size: int = 8
    max_tokens: int = 6  # identity/state need ~1 word; ground needs a short JSON
    ground_max_tokens: int = 64
    vllm_url: str = "http://localhost:8000/v1"

    @classmethod
    def from_env(cls) -> "ServiceConfig":
        return cls(
            backend=os.environ.get("VLM_BACKEND", "stub"),
            model=os.environ.get("VLM_MODEL") or None,
            token=os.environ.get("VLM_SERVICE_TOKEN", ""),
            window_ms=float(os.environ.get("VLM_BATCH_WINDOW_MS", "15")),
            max_batch_size=int(os.environ.get("VLM_MAX_BATCH_SIZE", "8")),
            max_tokens=int(os.environ.get("VLM_MAX_TOKENS", "6")),
            ground_max_tokens=int(os.environ.get("VLM_GROUND_MAX_TOKENS", "64")),
            vllm_url=os.environ.get("VLM_VLLM_URL", "http://localhost:8000/v1"),
        )


# Hosts that keep the service off the network (loopback / unspecified).
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "", None})


def insecure_exposure_warnings(host: str, token: str) -> list[str]:
    """Loud warnings for an insecure appliance exposure, or ``[]`` when safe.

    The VLM service serves **unauthenticated PHI inference** when no token is
    configured, and binds every network interface over cleartext HTTP when the
    host is non-loopback. Neither is wrong for a locked-down deployment, but it
    must never happen *unknowingly*. This returns human-readable WARNING lines
    (named exposure + remedy) for the caller (``__main__``) to log at startup.

    Pure and side-effect-free so it is directly unit-testable.
    """
    no_auth = not (token or "").strip()
    loopback = (host or "").strip().lower() in _LOOPBACK_HOSTS
    messages: list[str] = []
    if no_auth and not loopback:
        messages.append(
            f"UNAUTHENTICATED PHI ENDPOINT ON THE NETWORK: VLM_SERVICE_TOKEN is "
            f"empty (auth DISABLED) and --host={host!r} binds non-loopback "
            "interfaces over cleartext HTTP. Any host that can reach this port "
            "can submit patient screenshots/crops for inference. Set "
            "VLM_SERVICE_TOKEN and terminate TLS at a reverse proxy, or bind "
            "--host 127.0.0.1."
        )
    elif no_auth:
        messages.append(
            "AUTH DISABLED: VLM_SERVICE_TOKEN is empty, so every inference "
            "endpoint is unauthenticated. Safe only because --host is loopback. "
            "Set VLM_SERVICE_TOKEN before binding a non-loopback --host."
        )
    elif not loopback:
        messages.append(
            f"CLEARTEXT PHI OVER THE NETWORK: --host={host!r} binds non-loopback "
            "interfaces and traffic is plain HTTP. Requests are authenticated "
            "but the PHI payload is unencrypted in transit — terminate TLS at a "
            "reverse proxy on the trusted network."
        )
    return messages
