"""An opt-in tap that hands retained events to a subscriber as they happen.

The recorder publishes to a :class:`LiveEventTap` only when the caller passes
one (``Recorder(..., live_event_tap=tap)`` or ``record(..., live_event_tap=tap)``).
Without a tap nothing is published and nothing changes.

The tap sees an event at the point the recorder retains it, after the
authentication-handoff gate, so an interval that Capture keeps out of the
recording is kept out of the tap too. Screen frames are never published.

Delivery is decoupled from recording: :meth:`LiveEventTap.publish` copies the
event into a bounded queue and returns at once, and a daemon thread calls the
subscriber. When the queue is full the event is dropped and counted, so a slow
subscriber can't stall or grow the recorder. A subscriber that raises is
logged and counted; it never stops the recording.
"""

from __future__ import annotations

import copy
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from loguru import logger

#: Event types a tap may publish. Frames and audio are excluded by design.
LIVE_EVENT_TYPES = frozenset({"action", "window", "browser"})


@dataclass(frozen=True)
class LiveEvent:
    """A copy of one retained recorder event."""

    timestamp: float
    type: str
    data: dict[str, Any]


Subscriber = Callable[[LiveEvent], None]


class LiveEventTap:
    """Bounded, non-blocking fan-out from the recorder to one subscriber."""

    def __init__(
        self,
        subscriber: Subscriber,
        *,
        max_pending: int = 4096,
        types: frozenset[str] = LIVE_EVENT_TYPES,
    ) -> None:
        if max_pending < 1:
            raise ValueError("max_pending must be at least 1")
        unknown = set(types) - LIVE_EVENT_TYPES
        if unknown:
            raise ValueError(f"a live tap can't publish {sorted(unknown)}")
        self._subscriber = subscriber
        self._types = frozenset(types)
        self._queue: queue.Queue[LiveEvent | None] = queue.Queue(maxsize=max_pending)
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._closed = False
        self.dropped = 0
        self.failed = 0
        self.delivered = 0

    def start(self) -> LiveEventTap:
        with self._lock:
            if self._thread is None and not self._closed:
                self._thread = threading.Thread(
                    target=self._run, name="capture-live-tap", daemon=True
                )
                self._thread.start()
        return self

    def publish(self, timestamp: float, event_type: str, data: Any) -> None:
        """Queue a copy of one event. Never blocks and never raises."""

        if self._closed or event_type not in self._types or not isinstance(data, dict):
            return
        try:
            event = LiveEvent(float(timestamp), event_type, copy.deepcopy(data))
        except Exception:  # an uncopyable payload must not break recording
            self.failed += 1
            return
        if self._thread is None:
            self.start()
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            self.dropped += 1

    def close(self, timeout: float = 2.0) -> None:
        """Deliver what's queued (up to ``timeout``), then stop."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            thread = self._thread
        if thread is None:
            return
        try:
            self._queue.put(None, timeout=timeout)
        except queue.Full:
            # A stuck subscriber: drop the oldest pending event to make room
            # for the stop marker rather than wait forever.
            try:
                self._queue.get_nowait()
                self.dropped += 1
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass
        thread.join(timeout)

    def _run(self) -> None:
        while True:
            event = self._queue.get()
            if event is None:
                return
            try:
                self._subscriber(event)
                self.delivered += 1
            except Exception as exc:
                self.failed += 1
                logger.warning("live event subscriber failed: {}", type(exc).__name__)


def publish_live(tap: LiveEventTap | None, event: Any) -> None:
    """Recorder-side helper: publish ``event`` when a tap was supplied."""

    if tap is None:
        return
    try:
        tap.publish(event.timestamp, event.type, event.data)
    except Exception:  # pragma: no cover - publish is already non-raising
        pass
