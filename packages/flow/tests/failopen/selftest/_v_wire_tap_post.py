# vendored from 769525ff1077:packages/flow/openadapt_flow/tx/wire_tap.py, lines 91-164, for the harness self-test only (re-indented)
# ruff: noqa: F401, F841, I001
"""Wire tap post-fix: '///host' is rebuilt on the tap's origin or refused (#2470).

Only the lines between ``# fmt: off`` and ``# fmt: on`` markers that
follow a ``# vendored:`` comment are verbatim; the wrapper ``def`` lines,
the ``return`` and the stubs above them are not."""

from __future__ import annotations

from typing import Optional
from urllib.parse import urljoin, urlsplit


# vendored: 769525ff1077:packages/flow/openadapt_flow/tx/wire_tap.py 91-164
# fmt: off
def _normalize_origin(url_or_origin: str) -> str:
    try:
        parts = urlsplit(url_or_origin.strip())
        if not parts.scheme or not parts.netloc:
            return ""
        scheme = parts.scheme.lower()
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        # A malformed port or bracketed host is no origin at all.
        return ""
    if ":" in host:
        # Keep IPv6 brackets so a port can never read as part of the address.
        host = f"[{host}]"
    if (
        port is None
        or (scheme == "http" and port == 80)
        or (scheme == "https" and port == 443)
    ):
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def relay_redirect_location(
    location: str,
    *,
    request_url: str,
    allowed_origins: frozenset[str],
    rewrite_from_origin: Optional[str] = None,
    rewrite_to_origin: Optional[str] = None,
) -> Optional[str]:
    """Return the ``Location`` to relay for a same-origin redirect, else ``None``.

    Reverse-proxy mode only: the client's next request comes back through the
    tap, so every hop is checked again. Browser route mode refuses redirects.

    The tap never follows a redirect itself. It hands a redirect back to the
    client only when the target, resolved against the request URL, stays on an
    allowed origin, so the next request passes through the tap again. A
    protocol-relative or absolute target on any other origin is refused.
    ``rewrite_from_origin``/``rewrite_to_origin`` map an absolute upstream
    target onto the tap's own origin in reverse-proxy mode.
    """
    raw = location or ""
    # Validate the header exactly as received. Browsers drop tabs and newlines
    # inside a URL and read a backslash as a slash, so refuse any space,
    # control character or backslash rather than guess how a client reads it.
    if not raw or any(ord(ch) <= 0x20 or ord(ch) == 0x7F or ch == "\\" for ch in raw):
        return None
    try:
        resolved = urljoin(request_url, raw)
        origin = _normalize_origin(resolved)
        parts = urlsplit(raw)
    except ValueError:
        return None
    if not origin or origin not in allowed_origins:
        return None
    # Only a plain path relays as written. Anything with a scheme or an
    # authority, including ``///host`` that urlsplit reads as a path but a
    # browser reads as a host, is relayed only as the URL checked here,
    # rebuilt on the tap's own origin.
    if not parts.scheme and not parts.netloc and not raw.startswith("//"):
        return raw
    if rewrite_from_origin is None or rewrite_to_origin is None:
        return None
    if origin != rewrite_from_origin:
        return None
    target = urlsplit(resolved)
    rewritten = rewrite_to_origin.rstrip("/") + (target.path or "/")
    if target.query:
        rewritten += "?" + target.query
    if target.fragment:
        rewritten += "#" + target.fragment
    return rewritten
# fmt: on
