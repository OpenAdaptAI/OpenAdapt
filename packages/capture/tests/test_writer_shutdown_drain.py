"""Shutdown-drain contracts for the event writers.

The historical event-loss race: a writer that stopped on the terminate event
without first draining its queue silently dropped every event still in
flight, and the capture looked complete. record() now orders producers to
exit before writers are released, and Recorder._verify_completed_capture
reconciles produced counters against committed rows. These tests pin the
underlying writer contract itself:

- a writer released while events remain buffered drains EVERY one,
- a writer with an empty queue exits promptly instead of hanging,
- a failing write surfaces loudly instead of a clean-looking exit.

The drain contract is queue semantics plus loop ordering, so these run the
real ``write_events`` against a real SQLite database in threads; the
cross-process reconciliation contract is pinned separately in
tests/test_control.py and re-proved live per OS by the production
qualification trials.

These tests need no display, listeners, or injected input.
"""

from __future__ import annotations

import multiprocessing
import threading
import time
from types import SimpleNamespace

import pytest

from openadapt_capture.db import create_db, crud
from openadapt_capture.extensions import synchronized_queue as sq
from openadapt_capture.recorder import Event as RecorderEvent
from openadapt_capture.recorder import write_events, write_window_event

NUM_EVENTS = 64
JOIN_TIMEOUT = 30.0


def _make_recording(tmp_path):
    db_path = str(tmp_path / "recording.db")
    _, Session = create_db(db_path)
    session = Session()
    recording = crud.insert_recording(
        session,
        {
            "timestamp": time.time(),
            "monitor_width": 1000,
            "monitor_height": 800,
            "platform": "test",
            "task_description": "writer drain contract",
        },
    )
    # A detached stand-in carrying only the identity the writer reads.
    stub = SimpleNamespace(id=recording.id, timestamp=recording.timestamp)
    session.close()
    return stub, db_path


def _window_event(recording, index: int) -> RecorderEvent:
    return RecorderEvent(
        recording.timestamp + index * 1e-3,
        "window",
        {
            "title": f"window {index}",
            "left": 0,
            "top": 0,
            "width": 10,
            "height": 10,
            "window_id": str(index),
        },
    )


def _run_writer(
    write_q,
    num_events,
    perf_q,
    recording,
    db_path,
    terminate,
    started,
):
    write_events(
        "window",
        write_window_event,
        write_q,
        num_events,
        perf_q,
        recording,
        db_path,
        terminate,
        started,
    )


def _count_window_events(db_path: str) -> int:
    from sqlalchemy import select

    from openadapt_capture.db import get_session_for_path
    from openadapt_capture.db.models import WindowEvent

    session = get_session_for_path(db_path)
    try:
        return len(session.execute(select(WindowEvent.id)).scalars().all())
    finally:
        bind = session.get_bind()
        session.close()
        if bind is not None:
            bind.dispose()


@pytest.fixture
def perf_q():
    queue = sq.SynchronizedQueue()
    yield queue
    while not queue.empty():
        queue.get()


@pytest.fixture(autouse=True)
def _allow_non_main_thread(monkeypatch):
    # write_events installs a SIGINT handler, which POSIX refuses outside the
    # main thread. The real writers are processes; here they run in threads,
    # so stub the registration out without touching the drain behavior.
    import signal

    monkeypatch.setattr(signal, "signal", lambda *_args, **_kwargs: None)


def test_writer_released_with_buffered_events_drains_every_one(tmp_path, perf_q):
    """Terminate-before-start must not drop one buffered event."""
    recording, db_path = _make_recording(tmp_path)
    write_q = sq.SynchronizedQueue()
    for index in range(NUM_EVENTS):
        write_q.put(_window_event(recording, index))
    terminate = multiprocessing.Event()
    terminate.set()
    started = multiprocessing.Event()

    writer = threading.Thread(
        target=_run_writer,
        args=(write_q, multiprocessing.Value("i", NUM_EVENTS), perf_q, recording, db_path, terminate, started),
    )
    writer.start()
    writer.join(timeout=JOIN_TIMEOUT)

    assert not writer.is_alive(), "writer hung after terminate"
    assert _count_window_events(db_path) == NUM_EVENTS


def test_writer_terminating_mid_stream_keeps_the_tail(tmp_path, perf_q):
    """A terminate racing the drain still commits every buffered event."""
    recording, db_path = _make_recording(tmp_path)
    write_q = sq.SynchronizedQueue()
    for index in range(NUM_EVENTS):
        write_q.put(_window_event(recording, index))
    terminate = multiprocessing.Event()
    started = multiprocessing.Event()

    writer = threading.Thread(
        target=_run_writer,
        args=(write_q, multiprocessing.Value("i", NUM_EVENTS), perf_q, recording, db_path, terminate, started),
    )
    writer.start()

    deadline = time.monotonic() + JOIN_TIMEOUT
    while not started.is_set() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert started.is_set(), "writer never signalled start"

    terminate.set()
    writer.join(timeout=JOIN_TIMEOUT)

    assert not writer.is_alive(), "writer hung after mid-stream terminate"
    assert _count_window_events(db_path) == NUM_EVENTS


def test_producer_exits_before_release_and_the_tail_survives(tmp_path, perf_q):
    """The record() ordering contract: release writers only after the
    producer has fully exited; every queued event is then committed."""
    recording, db_path = _make_recording(tmp_path)
    write_q = sq.SynchronizedQueue()
    terminate = multiprocessing.Event()
    started = multiprocessing.Event()
    producer_done = threading.Event()

    def produce():
        for index in range(NUM_EVENTS):
            write_q.put(_window_event(recording, index))
        producer_done.set()

    producer = threading.Thread(target=produce)
    producer.start()

    writer = threading.Thread(
        target=_run_writer,
        args=(write_q, multiprocessing.Value("i", NUM_EVENTS), perf_q, recording, db_path, terminate, started),
    )
    writer.start()

    # Mirror record(): join the producer BEFORE releasing the writers.
    producer.join(timeout=JOIN_TIMEOUT)
    assert producer_done.is_set()
    terminate.set()
    writer.join(timeout=JOIN_TIMEOUT)

    assert not writer.is_alive(), "writer hung after ordered release"
    assert _count_window_events(db_path) == NUM_EVENTS


def test_writer_with_empty_queue_exits_promptly_after_terminate(tmp_path, perf_q):
    """An empty queue after terminate must exit, never spin or hang."""
    recording, db_path = _make_recording(tmp_path)
    write_q = sq.SynchronizedQueue()
    terminate = multiprocessing.Event()
    terminate.set()
    started = multiprocessing.Event()

    began = time.monotonic()
    writer = threading.Thread(
        target=_run_writer,
        args=(write_q, multiprocessing.Value("i", 0), perf_q, recording, db_path, terminate, started),
    )
    writer.start()
    writer.join(timeout=JOIN_TIMEOUT)
    elapsed = time.monotonic() - began

    assert not writer.is_alive(), "writer hung on an empty queue"
    assert elapsed < JOIN_TIMEOUT / 2, f"writer took {elapsed:.1f}s to exit"
    assert _count_window_events(db_path) == 0


def test_writer_never_swallows_a_write_error(tmp_path, perf_q):
    """A failing write_fn surfaces loudly instead of a clean-looking exit."""
    recording, db_path = _make_recording(tmp_path)
    write_q = sq.SynchronizedQueue()
    write_q.put(_window_event(recording, 0))
    terminate = multiprocessing.Event()
    started = multiprocessing.Event()

    def exploding_write_fn(session, rec, event, perf_queue):
        raise RuntimeError("simulated database failure")

    with pytest.raises(RuntimeError, match="simulated database failure"):
        write_events(
            "window",
            exploding_write_fn,
            write_q,
            multiprocessing.Value("i", 1),
            perf_q,
            recording,
            db_path,
            terminate,
            started,
        )
