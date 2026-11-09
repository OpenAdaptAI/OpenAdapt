"""The server's answers to a save's write requests, as the browser sees them.

When a person or a program presses Save in a web application, the browser
usually sends a write request (POST, PUT, PATCH, DELETE) and the server
answers it. That answer is worth keeping: a 500 behind a green "Saved"
banner is a clear sign something went wrong. It is not proof that the record
is right, because it comes from the same transaction as the save. The
replayer keeps it on the step as evidence at check level ``same_screen``,
and only a read-back decides whether the save is confirmed.

:class:`ServerResponseLog` listens to one Playwright page. It keeps answers
to write requests that:

- the page's own code sent (``fetch``, XHR, or a form submission), not a
  beacon, an image or a script;
- went to the page's own origin;
- were sent after the save began (requests already in flight are ignored);
- came from the network, not from a service worker (an offline-first app's
  worker can answer a write that never reached the server).

It keeps the method, the status and the media type, and whether the request
went to the page's origin. It never keeps a URL, a path, a header, a body or
a digest of any of them: a digest of a short path or body can be guessed. It
never reads a response body, so a streaming answer can't hold up the run.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Any, Optional
from urllib.parse import urlsplit

from openadapt_flow.ir import ServerResponseEvidence

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
#: Requests the page's own code makes: fetch, XHR, and form submissions.
_PAGE_REQUESTS = frozenset({"fetch", "xhr", "document"})
#: Answers kept in the log; older ones drop off.
_MAX_LOG = 64


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


class ServerResponseLog:
    """A bounded log of answers to the page's own write requests."""

    def __init__(self, page: Any) -> None:
        self._page = page
        self._lock = threading.Lock()
        #: Sequence number of each tracked write request, by request object.
        self._issued: dict[int, int] = {}
        self._seq = 0
        self._log: deque[tuple[int, ServerResponseEvidence]] = deque(maxlen=_MAX_LOG)
        #: Listeners are added at the first :meth:`mark` (the first save), not
        #: before: nothing earlier is kept, and a page that never saves gets
        #: no extra listeners.
        self.armed = False

    def _arm(self) -> None:
        if self.armed:
            return
        try:
            self._page.on("request", self._on_request)
            self._page.on("response", self._on_response)
            self.armed = True
        except Exception:  # noqa: BLE001 - evidence only
            self.armed = False

    def close(self) -> None:
        """Remove the listeners (the backend is done with the page)."""

        if not self.armed:
            return
        for event, listener in (
            ("request", self._on_request),
            ("response", self._on_response),
        ):
            try:
                self._page.remove_listener(event, listener)
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass
        self.armed = False

    def _on_request(self, request: Any) -> None:
        try:
            method = str(request.method).upper()
            kind = str(request.resource_type)
        except Exception:  # noqa: BLE001
            return
        if method not in WRITE_METHODS or kind not in _PAGE_REQUESTS:
            return
        with self._lock:
            self._seq += 1
            self._issued[id(request)] = self._seq
            if len(self._issued) > 4 * _MAX_LOG:
                # Requests that never got an answer don't pile up.
                oldest = sorted(self._issued.items(), key=lambda kv: kv[1])
                for key, _ in oldest[: len(oldest) - 2 * _MAX_LOG]:
                    self._issued.pop(key, None)

    def _on_response(self, response: Any) -> None:
        try:
            request = response.request
        except Exception:  # noqa: BLE001
            return
        with self._lock:
            seq = self._issued.pop(id(request), None)
        if seq is None:
            return
        evidence = _evidence(self._page, response)
        if evidence is None:
            return
        with self._lock:
            self._log.append((seq, evidence))

    def mark(self) -> int:
        """Requests sent after this mark belong to what comes next."""

        self._arm()
        with self._lock:
            return self._seq

    def since(self, mark: int) -> list[ServerResponseEvidence]:
        """Answers to write requests sent after ``mark`` (oldest first)."""

        with self._lock:
            return [evidence for seq, evidence in self._log if seq > mark]


def _evidence(page: Any, response: Any) -> Optional[ServerResponseEvidence]:
    try:
        if getattr(response, "from_service_worker", False):
            return None
        method = str(response.request.method).upper()
        status = int(response.status)
        url = str(response.url)
    except Exception:  # noqa: BLE001 - a response we can't describe is skipped
        return None
    if method not in WRITE_METHODS or not 100 <= status <= 599:
        return None
    try:
        page_origin = _origin(str(page.url))
    except Exception:  # noqa: BLE001
        return None
    if _origin(url) != page_origin:
        # Another origin: analytics, a third-party widget. Not the save.
        return None
    content_type: Optional[str] = None
    try:
        raw = (response.headers or {}).get("content-type")
        if raw:
            content_type = str(raw).split(";", 1)[0].strip().lower()[:100] or None
    except Exception:  # noqa: BLE001
        content_type = None
    return ServerResponseEvidence(
        method=method,  # type: ignore[arg-type]
        status=status,
        content_type=content_type,
    )


__all__ = ["ServerResponseLog", "WRITE_METHODS"]
