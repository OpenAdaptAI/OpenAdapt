"""The request headers a web app's own script sends, for an API step to reuse.

A single-page app often authenticates its API calls with a header its script
adds (``Authorization: Bearer ...``, a CSRF header), not with cookies alone.
The browser context's request API shares the cookies but not those headers.

:func:`arm` watches a browser context's requests. For each script request
(fetch or XHR) it keeps, per origin, the last value of every header outside
the browser's own. An API step whose recorded save carried such headers (the
capture keeps their names, never their values) sends the values the app's own
script sent most recently in this session. Values are kept in memory only,
for the life of the context; they are never written anywhere. A header the
app hasn't sent in this session has no value, and the API step doesn't run.
"""

from __future__ import annotations

import threading
import urllib.parse as up
import weakref
from typing import Any, Iterable, Optional

#: Headers the browser itself sets or the API step sets: never taken from the
#: app's requests.
BROWSER_HEADERS = frozenset(
    {
        "accept",
        "accept-encoding",
        "accept-language",
        "cache-control",
        "connection",
        "content-length",
        "content-type",
        "cookie",
        "host",
        "origin",
        "pragma",
        "referer",
        "user-agent",
        "priority",
        "dnt",
        "upgrade-insecure-requests",
    }
)


def app_header_names(headers: Iterable[str]) -> list[str]:
    """The header names in ``headers`` that the page's script chose to send."""

    return sorted(
        {
            n.lower()
            for n in headers
            if n
            and not n.startswith(":")
            and n.lower() not in BROWSER_HEADERS
            and not n.lower().startswith("sec-")
        }
    )


_lock = threading.Lock()
_seen: "weakref.WeakKeyDictionary[Any, dict[str, dict[str, str]]]" = (
    weakref.WeakKeyDictionary()
)


def _origin(url: str) -> str:
    parts = up.urlsplit(url)
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


def arm(context: Any) -> None:
    """Start keeping the app headers of ``context``'s script requests (once)."""

    with _lock:
        if context in _seen:
            return
        _seen[context] = {}
    store = _seen[context]

    def on_request(request: Any) -> None:
        try:
            if request.resource_type not in ("fetch", "xhr"):
                return
            headers = request.headers or {}
            names = app_header_names(headers)
            if not names:
                return
            with _lock:
                by_name = store.setdefault(_origin(request.url), {})
                for name in names:
                    by_name[name] = headers[name]
        except Exception:  # noqa: BLE001 - never disturb the page
            return

    try:
        context.on("request", on_request)
    except Exception:  # noqa: BLE001 - not a Playwright context
        with _lock:
            _seen.pop(context, None)


def values(context: Any, origin: str, names: Iterable[str]) -> Optional[dict[str, str]]:
    """The last value the app sent for each of ``names`` to ``origin``.

    ``None`` when the context isn't watched or any name has no value yet.
    """

    with _lock:
        store = _seen.get(context)
        if store is None:
            return None
        by_name = dict(store.get(origin.lower(), {}))
    out: dict[str, str] = {}
    for name in names:
        if name.lower() not in by_name:
            return None
        out[name.lower()] = by_name[name.lower()]
    return out
