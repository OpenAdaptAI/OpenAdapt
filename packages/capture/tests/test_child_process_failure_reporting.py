"""Contracts for what a dying recorder child process tells its parent.

A recording runs its writers as separate processes. When one of them raises
during startup, the parent used to learn only an exit code, and the reason lived
in whatever the child's stderr happened to reach. That cost a full diagnosis
cycle for a failure that was fully explained in the child. Worse, a child left
running after such a failure keeps the standard output it inherited open and
stops the parent's own interpreter from exiting, so the failure never gets
reported at all.

These tests pin three things:

- a child's traceback reaches the parent and lands in the error the parent
  raises,
- a child that ignores a stop request is killed rather than waited on forever,
- a queue whose reader is gone does not hold the parent open.

They need no display, listeners, or injected input.
"""

from __future__ import annotations

import multiprocessing
import time

import pytest

from openadapt_capture import utils
from openadapt_capture.extensions import synchronized_queue as sq
from openadapt_capture.recorder import (
    _describe_child_failure,
    _drain_process_errors,
    _force_reap_processes,
    _raise_for_failed_processes,
    _release_queues,
)

JOIN_TIMEOUT = 30.0


class _DistinctiveStartupError(RuntimeError):
    """Named so the assertions cannot pass on some other failure."""


def _raise_distinctively() -> None:
    raise _DistinctiveStartupError("the video encoder never opened")


def _ignore_the_stop_request() -> None:
    while True:
        time.sleep(0.05)


def _fill_a_queue_and_leave(write_q: sq.SynchronizedQueue) -> None:
    write_q.put(b"x" * 4096)


def test_a_child_traceback_reaches_the_parent():
    """The parent must be able to read why its child died, not just that it did."""
    error_sink = multiprocessing.SimpleQueue()
    child = multiprocessing.Process(
        target=utils.WrapStdout(_raise_distinctively, "video_writer", error_sink),
    )
    child.start()
    child.join(timeout=JOIN_TIMEOUT)

    assert child.exitcode == 1
    reported = _drain_process_errors(error_sink, {})
    assert "video_writer" in reported
    assert "_DistinctiveStartupError" in reported["video_writer"]
    assert "the video encoder never opened" in reported["video_writer"]


def test_the_raised_recording_error_quotes_the_child_traceback():
    """The traceback must cross the recording boundary, not stop at the child."""
    error_sink = multiprocessing.SimpleQueue()
    child = multiprocessing.Process(
        target=utils.WrapStdout(_raise_distinctively, "video_writer", error_sink),
    )
    child.start()
    child.join(timeout=JOIN_TIMEOUT)

    with pytest.raises(RuntimeError) as raised:
        _raise_for_failed_processes({"video_writer": child}, error_sink)

    # The message, not an exception note: notes need Python 3.11, and this
    # package supports 3.10, where a note is discarded without a word.
    reported = str(raised.value)
    assert "video_writer (exit code 1)" in reported
    assert "_DistinctiveStartupError" in reported
    assert "the video encoder never opened" in reported


def test_a_child_stopped_by_a_signal_is_reported_as_unexplained():
    """Say a child was stopped, rather than imply it reported nothing useful."""
    described = _describe_child_failure("mem_writer", None)
    assert "mem_writer" in described
    assert "stopped rather than raised" in described


def test_a_child_that_ignores_the_stop_request_is_killed():
    """A survivor would hold the parent's output open and block its exit."""
    child = multiprocessing.Process(target=_ignore_the_stop_request, daemon=True)
    child.start()
    try:
        assert _force_reap_processes({"perf_stats_writer": child}) == []
        assert not child.is_alive()
    finally:
        if child.is_alive():
            child.kill()
            child.join(timeout=JOIN_TIMEOUT)


def test_reaping_leaves_a_finished_child_alone():
    """Normal teardown already joined its writers; reaping must be a no-op."""
    child = multiprocessing.Process(target=time.sleep, args=(0,))
    child.start()
    child.join(timeout=JOIN_TIMEOUT)

    assert _force_reap_processes({"action_event_writer": child}) == []
    assert child.exitcode == 0


def test_a_queue_whose_reader_died_does_not_hold_the_parent_open():
    """The feeder thread must be released once nothing will ever read it."""
    write_q = sq.SynchronizedQueue()
    reader = multiprocessing.Process(target=_fill_a_queue_and_leave, args=(write_q,))
    reader.start()
    reader.join(timeout=JOIN_TIMEOUT)
    write_q.put(b"y" * 4096)

    started_at = time.monotonic()
    _release_queues([write_q])
    assert time.monotonic() - started_at < JOIN_TIMEOUT
