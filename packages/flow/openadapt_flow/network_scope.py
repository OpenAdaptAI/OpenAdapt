"""Classify a URL as loopback (this computer) or external.

Run reports and receipts state whether a run made external network calls. A
run whose target and system-of-record reads all point at this computer made
none of its own calls off the machine, so those signals must not count as
egress. Only an exact loopback host qualifies: ``localhost`` or a loopback IP
literal (``127.0.0.0/8``, ``::1``). The parsed host name is compared, never a
string prefix, so ``localhost.example.com`` or ``127.0.0.1.nip.io`` stays
external.
"""

from __future__ import annotations

import ipaddress
from typing import Literal, Optional
from urllib.parse import urlsplit

EndpointScope = Literal["loopback", "external", "unknown"]


def is_loopback_url(url: Optional[str]) -> bool:
    """True only when ``url`` names this computer by an exact loopback host."""

    if not url:
        return False
    try:
        host = urlsplit(url.strip()).hostname
    except ValueError:
        return False
    if not host:
        return False
    host = host.lower()
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return address.ipv4_mapped.is_loopback
    return address.is_loopback


def endpoint_scope(url: Optional[str]) -> EndpointScope:
    """``loopback`` or ``external`` for a URL with a host, else ``unknown``."""

    if not url:
        return "unknown"
    try:
        host = urlsplit(url.strip()).hostname
    except ValueError:
        return "unknown"
    if not host:
        return "unknown"
    return "loopback" if is_loopback_url(url) else "external"
