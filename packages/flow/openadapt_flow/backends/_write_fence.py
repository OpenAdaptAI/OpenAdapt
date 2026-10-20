"""A write fence: run one browser action while the page cannot write.

Some controls read as writes but are not: "Continue to tasks" on a What's
New screen, "Next" on an informational dialog. Their label can't settle
that; what the browser sends when they are clicked can. :func:`write_fence`
runs the action with every possible write path blocked and reports any
attempt, so a caller can both prove the click wrote nothing and know that,
had it tried, nothing reached the server:

- every HTTP request whose method is not ``GET``, ``HEAD`` or ``OPTIONS`` is
  aborted in the browser and recorded, whatever its origin or type (fetch,
  XHR, form submission, beacon, navigation);
- every WebSocket opened while the fence is up is answered by the fence
  itself, never connected to the server, and recorded;
- the fence refuses to start when a WebSocket may already be open (one this
  tracker saw open, or a document loaded before tracking began), or when a
  service worker controls the page (its own requests bypass routing).

A refused fence is not an error: the caller treats it as "no evidence" and
fails closed. ``GET`` requests are let through, so a ``GET`` with a side
effect is outside what the fence can see; that residual is the same one the
recording-side rule accepts (``docs/design/LOCAL_EDIT_CLASSIFICATION.md``).
"""

from __future__ import annotations

import threading
import weakref
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: How long the fence stays up after the action, so a request the action
#: queued just before the fence would come down is still caught.
TAIL_MS = 500

_TRACKERS: "weakref.WeakKeyDictionary[Any, WebSocketTracker]" = (
    weakref.WeakKeyDictionary()
)


class WebSocketTracker:
    """Which WebSockets a page has open, and whether that is fully known.

    Armed before the page opens its document (``complete``), or later, in
    which case it becomes complete only at the next document load: a new
    document starts with no sockets, and every one it opens is seen.
    """

    def __init__(self, page: Any, *, before_document: bool) -> None:
        self._lock = threading.Lock()
        self._open: set[int] = set()
        #: Sockets ever opened, so a fence can see one open during it.
        self.opened = 0
        # WebSocket routing applies to documents loaded after it is set up,
        # so the gate goes up with the tracker, before the next document.
        self.gate = _socket_gate(page.context) if hasattr(page, "context") else None
        self.complete = before_document and self.gate is not None
        page.on("websocket", self._opened)
        if not before_document:
            page.on("load", self._loaded)

    def _opened(self, ws: Any) -> None:
        key = id(ws)
        with self._lock:
            self._open.add(key)
            self.opened += 1

        def closed(*_: Any) -> None:
            with self._lock:
                self._open.discard(key)

        ws.on("close", closed)

    def _loaded(self, *_: Any) -> None:
        self.complete = self.gate is not None

    def open_count(self) -> int:
        with self._lock:
            return len(self._open)


def track(page: Any) -> WebSocketTracker:
    """Start tracking ``page``'s WebSockets before it opens its document."""

    tracker = _TRACKERS.get(page)
    if tracker is None:
        tracker = WebSocketTracker(page, before_document=True)
        _TRACKERS[page] = tracker
    return tracker


def tracker_for(page: Any) -> WebSocketTracker:
    """The page's tracker; one armed now is complete only after a load."""

    tracker = _TRACKERS.get(page)
    if tracker is None:
        blank = (getattr(page, "url", "") or "about:blank") == "about:blank"
        tracker = WebSocketTracker(page, before_document=blank)
        _TRACKERS[page] = tracker
    return tracker


@dataclass
class FenceReport:
    """What the fence saw. ``refusal`` set means it never went up."""

    refusal: Optional[str] = None
    #: Write attempts, blocked: ``"POST"``, ``"websocket"``, ...
    attempts: list[str] = field(default_factory=list)

    @property
    def held(self) -> bool:
        """The fence was up for the whole action and nothing tried to write."""

        return self.refusal is None and not self.attempts


def _service_worker_controls(page: Any) -> Optional[bool]:
    try:
        return bool(
            page.evaluate(
                "() => !!(navigator.serviceWorker && "
                "navigator.serviceWorker.controller)"
            )
        )
    except Exception:  # noqa: BLE001 - unreadable is refused by the caller
        return None


@contextmanager
def write_fence(page: Any, tracker: WebSocketTracker) -> Iterator[FenceReport]:
    """Run the ``with`` body while ``page``'s context cannot write.

    Yields a :class:`FenceReport`. When ``report.refusal`` is set the fence
    did not go up; the body still runs, so callers must check it before
    acting.
    """

    report = FenceReport()
    if tracker.gate is None:
        report.refusal = "this browser cannot intercept WebSockets"
    elif not tracker.complete:
        report.refusal = (
            "the page's document loaded before its WebSockets were tracked, "
            "so an open one can't be ruled out"
        )
    elif tracker.open_count():
        report.refusal = "a WebSocket was already open"
    else:
        controlled = _service_worker_controls(page)
        if controlled is None:
            report.refusal = "could not read whether a service worker controls the page"
        elif controlled:
            report.refusal = "a service worker controls the page"
    if report.refusal is not None:
        yield report
        return

    context = page.context
    gate = tracker.gate
    assert gate is not None  # a complete tracker has one
    opened_before = tracker.opened
    lock = threading.Lock()

    def on_request(route: Any) -> None:
        method = str(route.request.method or "").upper()
        if method in READ_METHODS:
            route.fallback()
            return
        with lock:
            report.attempts.append(method or "unknown")
        route.abort("blockedbyclient")

    context.route("**/*", on_request)
    gate.fence(report)
    try:
        yield report
        # Let anything the action queued reach the handlers while the fence
        # is still up. The request handler keeps aborting writes even if
        # Playwright calls it after its route comes down.
        page.wait_for_timeout(TAIL_MS)
    finally:
        gate.fence(None)
        context.unroute("**/*", on_request)
    if tracker.opened != opened_before and "websocket" not in report.attempts:
        # Seen by the page but not by the route: count it, whatever happened.
        report.attempts.append("websocket")


class _SocketGate:
    """One WebSocket route per browser context.

    Outside a fence every socket is connected to its server and passes
    messages both ways unchanged. Inside one, a new socket is never
    connected, is closed, and counts as a write attempt.
    """

    def __init__(self, context: Any) -> None:
        self._lock = threading.Lock()
        self._report: Optional[FenceReport] = None
        context.route_web_socket("**/*", self._route)

    def fence(self, report: Optional[FenceReport]) -> None:
        with self._lock:
            self._report = report

    def _route(self, ws: Any) -> None:
        with self._lock:
            report = self._report
            if report is not None:
                report.attempts.append("websocket")
        if report is None:
            ws.connect_to_server()
            return
        try:
            ws.close()
        except Exception:  # noqa: BLE001 - already closed is fine
            pass


_GATES: "weakref.WeakKeyDictionary[Any, _SocketGate]" = weakref.WeakKeyDictionary()


def _socket_gate(context: Any) -> Optional[_SocketGate]:
    gate = _GATES.get(context)
    if gate is None:
        try:
            gate = _SocketGate(context)
        except Exception:  # noqa: BLE001 - no WebSocket routing: no fence
            return None
        _GATES[context] = gate
    return gate
