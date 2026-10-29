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


class _Request:
    def __init__(self, method: str, resource_type: str = "fetch") -> None:
        self.method = method
        self.resource_type = resource_type
        self.url = "http://app.test/api/encounter"
        self.headers: dict[str, str] = {}
        self.post_data_buffer = None
        self.frame = None

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
    assert capture.pending_writes == 2
    context.emit("requestfinished", save)
    assert capture.pending_writes == 1
    context.emit("requestfailed", other_save)
    assert capture.pending_writes == 0
    context.emit("requestfinished", read)
    assert capture.pending_writes == 0


def test_images_and_fonts_are_never_waited_for() -> None:
    context = _Context()
    capture = NetworkCapture(context).start()
    context.emit("request", _Request("POST", resource_type="image"))
    assert capture.pending_writes == 0


def test_stop_detaches_the_request_listener_too() -> None:
    context = _Context()
    capture = NetworkCapture(context).start()
    capture.stop()
    assert context.handlers == {}


def _recorder(network, page) -> InteractiveRecorder:
    recorder = InteractiveRecorder.__new__(InteractiveRecorder)
    recorder._network = network
    recorder.page = page
    return recorder


def test_the_recorder_waits_until_the_held_save_has_gone_through() -> None:
    held = {"waits": 0}

    class _Network:
        @property
        def pending_writes(self) -> int:
            # The guard sends the save after three of the recorder's waits.
            return 1 if held["waits"] < 3 else 0

    page = SimpleNamespace(
        wait_for_timeout=lambda ms: held.__setitem__("waits", held["waits"] + 1)
    )
    _recorder(_Network(), page)._wait_for_writes()
    assert held["waits"] == 3


def test_the_wait_is_bounded_and_survives_a_closed_page() -> None:
    class _Stuck:
        pending_writes = 1

    calls = {"n": 0}

    def wait(ms: int) -> None:
        calls["n"] += 1

    _recorder(_Stuck(), SimpleNamespace(wait_for_timeout=wait))._wait_for_writes(
        timeout_s=0.05
    )
    assert calls["n"] >= 1

    def closed(ms: int) -> None:
        raise RuntimeError("Target page, context or browser has been closed")

    _recorder(_Stuck(), SimpleNamespace(wait_for_timeout=closed))._wait_for_writes()


def test_no_network_capture_means_no_wait() -> None:
    def never(ms: int) -> None:
        raise AssertionError("waited without a network capture")

    _recorder(None, SimpleNamespace(wait_for_timeout=never))._wait_for_writes()
