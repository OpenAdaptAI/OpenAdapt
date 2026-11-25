# vendored from 576f87b41483:packages/flow/openadapt_flow/tx/wire_tap.py, lines 91-147, for the harness self-test only (re-indented)
# ruff: noqa: F401, F841, I001
"""Wire tap pre-fix: main relays '///host' raw (#2470); pinned so a fix on main can't turn the control green.

Only the lines between ``# fmt: off`` and ``# fmt: on`` markers that
follow a ``# vendored:`` comment are verbatim; the wrapper ``def`` lines,
the ``return`` and the stubs above them are not."""

from __future__ import annotations

from typing import Optional
from urllib.parse import urljoin, urlsplit


# vendored: 576f87b41483:packages/flow/openadapt_flow/tx/wire_tap.py 91-147
# fmt: off
def _normalize_origin(url_or_origin: str) -> str:
    parts = urlsplit(url_or_origin.strip())
    if not parts.scheme or not parts.netloc:
        return ""
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port
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

    The tap never follows a redirect itself. It hands a redirect back to the
    client only when the target, resolved against the request URL, stays on an
    allowed origin, so the next request passes through the tap again. A
    protocol-relative or absolute target on any other origin is refused.
    ``rewrite_from_origin``/``rewrite_to_origin`` map an absolute upstream
    target onto the tap's own origin in reverse-proxy mode.
    """
    raw = (location or "").strip()
    # Browsers read a backslash as a slash, so ``/\\host`` leaves the origin
    # even though URL parsers here treat it as a relative path.
    if not raw or any(ch in raw for ch in "\r\n\\"):
        return None
    resolved = urljoin(request_url, raw)
    origin = _normalize_origin(resolved)
    if not origin or origin not in allowed_origins:
        return None
    parts = urlsplit(raw)
    if not parts.scheme and not parts.netloc:
        return raw
    if rewrite_from_origin is not None and rewrite_to_origin is not None:
        if origin != rewrite_from_origin:
            return None
        target = urlsplit(resolved)
        rewritten = rewrite_to_origin.rstrip("/") + (target.path or "/")
        if target.query:
            rewritten += "?" + target.query
        if target.fragment:
            rewritten += "#" + target.fragment
        return rewritten
    return raw
# fmt: on
