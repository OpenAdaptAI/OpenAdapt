"""Which model endpoints a regulated run may send frames to.

Under the ``regulated`` execution profile a frame may leave the process only
over TLS (``https``), or over plain ``http`` to this machine's loopback
interface. Every other URL is refused: cleartext to another host, any other
scheme, and anything that doesn't parse.
"""

from __future__ import annotations

import ipaddress
from typing import Optional
from urllib.parse import urlsplit


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def regulated_transport_refusal(url: str) -> Optional[str]:
    """Why a regulated run must not send frames to ``url``, or None if it may."""

    try:
        parts = urlsplit((url or "").strip())
        host = (parts.hostname or "").lower()
    except ValueError:
        return "the model endpoint URL does not parse"
    if not host:
        return "the model endpoint URL names no host"
    if parts.scheme == "https":
        return None
    if parts.scheme == "http" and _loopback(host):
        return None
    if parts.scheme == "http":
        return f"the model endpoint {host} is cleartext http off this machine"
    return f"the model endpoint scheme {parts.scheme!r} is not https"
