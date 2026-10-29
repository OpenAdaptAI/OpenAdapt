"""A recorded save shows its result even when the save is held in flight.

The interactive recorder takes a click's after-frame once the screen settles,
and its system-of-record read with it. While the agent guard holds a save for
its checks, the screen holds still too, so on a loaded runner the frame
settled and the record was read before the write landed. The compiler then saw
no change for the save step and emitted a placeholder effect, and admission
refused the taught program ("no canary ran: a write step lacks an independent
read-back: step_005: record_written is placeholder"). NetworkCapture now
counts the writes in flight, and the recorder waits for them first.
tests/e2e/test_agent_guard_slow_hold_e2e.py checks the whole path with a held
save.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

from openadapt_flow.interactive_recorder import InteractiveRecorder
from openadapt_flow.network_capture import NetworkCapture


class _Context:
    def __init__(self) -> None:
        self.handlers: dict[str, object] = {}

    def on(self, event: str, handler) -> None:
        self.handlers[event] = handler

    def remove_listener(self, event: str, handler) -> None:
        self.handlers.pop(event, None)

    def emit(self, event: str, request) -> None:
        self.handlers[event](request)


RECORDED_PAGE = object()
OTHER_TAB = object()


class _Request:
    def __init__(
        self, method: str, resource_type: str = "fetch", page=RECORDED_PAGE
    ) -> None:
        self.method = method
        self.resource_type = resource_type
        self.url = "http://app.test/api/encounter"
        self.headers: dict[str, str] = {}
        self.post_data_buffer = None
        self.frame = SimpleNamespace(page=page)

    def all_headers(self) -> dict[str, str]:
        return {}

    def response(self):
        return None


def test_writes_count_as_in_flight_until_they_finish_or_fail() -> None:
    context = _Context()
    capture = NetworkCapture(context).start()
    save, other_save, read = _Request("POST"), _Request("PUT"), _Request("GET")
    for request in (save, other_save, read):
        context.emit("request", request)
    assert len(capture.pending_writes()) == 2
    context.emit("requestfinished", save)
    assert capture.pending_writes() == [other_save]
    context.emit("requestfailed", other_save)
    assert capture.pending_writes() == []
    context.emit("requestfinished", read)
    assert capture.pending_writes() == []


def test_images_and_fonts_are_never_waited_for() -> None:
    context = _Context()
    capture = NetworkCapture(context).start()
    context.emit("request", _Request("POST", resource_type="image"))
    assert capture.pending_writes() == []


def test_another_tabs_long_poll_is_not_this_recordings_write() -> None:
    # Attached to a person's browser, the context holds every tab.
    context = _Context()
    capture = NetworkCapture(context).start()
    poll = _Request("POST", page=OTHER_TAB)

    class _WorkerRequest(_Request):
        @property
        def frame(self):  # a service worker's request has no frame
            raise RuntimeError(
                "Service Worker requests do not have an associated frame"
            )

        @frame.setter
        def frame(self, value) -> None:
            pass

    worker = _WorkerRequest("POST")
    context.emit("request", poll)
    context.emit("request", worker)
    assert capture.pending_writes([RECORDED_PAGE]) == []
    assert len(capture.pending_writes()) == 2


def test_a_request_whose_finish_never_comes_ages_out() -> None:
    context = _Context()
    capture = NetworkCapture(context).start()
    beacon = _Request("POST")
    context.emit("request", beacon)
    capture._pending_writes[beacon] = time.monotonic() - 60
    assert capture.pending_writes(max_age_s=30) == []
    assert capture.pending_writes() == [beacon]


def test_stop_detaches_the_request_listener_too() -> None:
    context = _Context()
    capture = NetworkCapture(context).start()
    capture.stop()
    assert context.handlers == {}


class _Network:
    """In-flight writes the guard sends after ``after`` of the recorder's waits."""

    def __init__(self, waits: dict, after: int, requests=None) -> None:
        self.waits, self.after = waits, after
        self.requests = requests or [object()]

    def pending_writes(self, pages=None, *, max_age_s=None):
        return [] if self.waits["n"] >= self.after else list(self.requests)


def _recorder(network, wait) -> InteractiveRecorder:
    recorder = InteractiveRecorder.__new__(InteractiveRecorder)
    recorder._network = network
    recorder._pyq = []
    recorder._primary_page = SimpleNamespace(wait_for_timeout=wait)
    recorder.page = recorder._primary_page
    recorder._pending_pages = []
    recorder.recorder = None
    return recorder


def _counting(waits: dict):
    return lambda ms: waits.__setitem__("n", waits["n"] + 1)


def test_the_recorder_waits_until_the_held_save_has_gone_through() -> None:
    waits = {"n": 0}
    assert _recorder(_Network(waits, after=3), _counting(waits))._wait_for_writes()
    assert waits["n"] == 3


def test_nothing_in_flight_means_no_wait() -> None:
    waits = {"n": 0}
    assert not _recorder(_Network(waits, after=0), _counting(waits))._wait_for_writes()
    assert waits["n"] == 0


def test_the_next_action_ends_the_wait() -> None:
    # The recording must not hold a person's or agent's next click.
    waits = {"n": 0}
    recorder = _recorder(_Network(waits, after=10_000), _counting(waits))

    def wait(ms: int) -> None:
        waits["n"] += 1
        recorder._pyq.append({"kind": "click"})

    recorder._primary_page = SimpleNamespace(wait_for_timeout=wait)
    recorder._wait_for_writes()
    assert waits["n"] == 1


def test_only_writes_in_flight_when_the_wait_starts_are_waited_for() -> None:
    first, later = object(), object()
    waits = {"n": 0}

    class _Later:
        def pending_writes(self, pages=None, *, max_age_s=None):
            # The first save finishes after one wait; another starts then.
            return [first] if waits["n"] == 0 else [later]

    _recorder(_Later(), _counting(waits))._wait_for_writes()
    assert waits["n"] == 1


def test_the_wait_is_bounded_and_survives_a_closed_page() -> None:
    waits = {"n": 0}
    _recorder(_Network(waits, after=10_000), _counting(waits))._wait_for_writes(
        timeout_s=0.05
    )
    assert waits["n"] >= 1

    def closed(ms: int) -> None:
        raise RuntimeError("Target page, context or browser has been closed")

    _recorder(_Network({"n": 0}, after=10_000), closed)._wait_for_writes()


def test_no_network_capture_means_no_wait() -> None:
    def never(ms: int) -> None:
        raise AssertionError("waited without a network capture")

    assert not _recorder(None, never)._wait_for_writes()
