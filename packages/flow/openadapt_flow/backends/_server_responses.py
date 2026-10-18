"""The server's answers to write requests, as the browser sees them.

When a person or a program presses Save in a web application, the browser
usually sends a write request (POST, PUT, PATCH, DELETE) and the server
answers it. That answer is worth keeping: a 500 behind a green "Saved"
banner is a clear sign something went wrong. It is not proof that the record
is right, because it comes from the same transaction as the save. The
replayer keeps it on the step as evidence at check level ``same_screen``,
and only a read-back decides whether the save is confirmed.

:class:`ServerResponseLog` listens to one Playwright page. It keeps a bounded
log of answers to write requests and returns them as
:class:`~openadapt_flow.ir.ServerResponseEvidence`: method, status, digests
of the endpoint and the body, the body's size and media type. It never keeps
a body, a URL, a header, or a query string.
"""

from __future__ import annotations

import hashlib
import threading
from collections import deque
from typing import Any, Optional
from urllib.parse import urlsplit

from openadapt_flow.ir import ServerResponseEvidence

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Answers kept in the log; older ones drop off.
_MAX_LOG = 64
#: Bodies above this size are not read to compute a digest.
_MAX_BODY_BYTES = 5 * 1024 * 1024


def _endpoint_digest(url: str) -> str:
    parts = urlsplit(url)
    endpoint = f"{parts.scheme}://{parts.netloc}{parts.path}".lower()
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()


class ServerResponseLog:
    """A bounded log of answers to write requests on one page.

    The Playwright event handler only stores the response object; the body is
    read later, outside the handler, because a sync handler must not block on
    the page.
    """

    def __init__(self, page: Any) -> None:
        self._lock = threading.Lock()
        self._log: deque[tuple[int, Any]] = deque(maxlen=_MAX_LOG)
        self._seq = 0
        self.armed = False
        try:
            page.on("response", self._on_response)
            self.armed = True
        except Exception:  # noqa: BLE001 - evidence only
            self.armed = False

    def _on_response(self, response: Any) -> None:
        try:
            method = str(response.request.method).upper()
        except Exception:  # noqa: BLE001
            return
        if method not in WRITE_METHODS:
            return
        with self._lock:
            self._seq += 1
            self._log.append((self._seq, response))

    def mark(self) -> int:
        """The log position now; answers after it belong to what comes next."""

        with self._lock:
            return self._seq

    def since(self, mark: int) -> list[ServerResponseEvidence]:
        """Answers logged after ``mark``, as evidence (oldest first)."""

        with self._lock:
            entries = [(seq, r) for seq, r in self._log if seq > mark]
        out: list[ServerResponseEvidence] = []
        for _seq, response in entries:
            evidence = _evidence(response)
            if evidence is not None:
                out.append(evidence)
        return out


def _evidence(response: Any) -> Optional[ServerResponseEvidence]:
    try:
        method = str(response.request.method).upper()
        status = int(response.status)
        url = str(response.url)
    except Exception:  # noqa: BLE001 - a response we can't describe is skipped
        return None
    if method not in WRITE_METHODS or not 100 <= status <= 599:
        return None
    content_type: Optional[str] = None
    try:
        headers = response.headers or {}
        raw = headers.get("content-type")
        if raw:
            content_type = str(raw).split(";", 1)[0].strip().lower()[:100] or None
    except Exception:  # noqa: BLE001
        content_type = None
    body_sha256: Optional[str] = None
    body_bytes: Optional[int] = None
    try:
        length = int((response.headers or {}).get("content-length") or 0)
    except (TypeError, ValueError):
        length = 0
    if length <= _MAX_BODY_BYTES:
        try:
            body = response.body()
        except Exception:  # noqa: BLE001 - the page moved on; no body
            body = None
        if isinstance(body, (bytes, bytearray)) and len(body) <= _MAX_BODY_BYTES:
            body_sha256 = hashlib.sha256(bytes(body)).hexdigest()
            body_bytes = len(body)
    return ServerResponseEvidence(
        method=method,  # type: ignore[arg-type]
        status=status,
        endpoint_sha256=_endpoint_digest(url),
        body_sha256=body_sha256,
        body_bytes=body_bytes,
        content_type=content_type,
    )


__all__ = ["ServerResponseLog", "WRITE_METHODS"]
