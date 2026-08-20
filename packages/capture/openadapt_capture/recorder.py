"""OpenAdapt's original, hand-built recorder — the live recording engine.

This is the production recorder carried forward from OpenAdapt's record.py:
the most battle-tested recording code in the org, refined against years of
real desktop workflows. It is the recorder behind OpenAdapt's desktop path
(openadapt-flow ``record --backend windows|rdp`` wraps ``Recorder``).

Architecture: a multiprocessing pipeline. Native platform input observers feed
normalized mouse/keyboard events into synchronized queues; dedicated writer processes
persist action events, screenshots, video frames, and (optionally) audio and
window state into a per-capture SQLite database plus time-aligned media files.
Adapted from the original for per-capture databases. Importing this module
must never touch the display (no screenshot at module scope — enforced by
tests/test_headless_import.py).

Usage:

    $ python -m openadapt_capture.recorder "<description of task>"

"""

import io
import multiprocessing
import os
import queue
import signal
import sqlite3
import sys
import threading
import time
import tracemalloc
import uuid
from collections import namedtuple
from functools import partial
from typing import Any, Callable

import fire
import numpy as np
import psutil
from loguru import logger
from pympler import tracker
from tqdm import tqdm

from openadapt_capture import platform, utils, video, window
from openadapt_capture.config import config
from openadapt_capture.db import create_db, crud, get_session_for_path
from openadapt_capture.db.models import ActionEvent, Recording
from openadapt_capture.desktop_capture import DesktopCaptureScope
from openadapt_capture.extensions import synchronized_queue as sq
from openadapt_capture.input_observer import (
    ObservedInput,
    ObservedKey,
    ObservedMouseButton,
    ObservedMouseMove,
    ObservedMouseScroll,
    add_exception_note,
    create_input_observer,
)
from openadapt_capture.structural import (
    StructuralObservationRequest,
    StructuralObserver,
    create_structural_observer,
    observe_structural_action,
)
from openadapt_capture.window_capture import (
    WindowCaptureScope,
    build_window_scope,
)

CoordinateScope = WindowCaptureScope | DesktopCaptureScope

try:
    import soundfile
except ImportError:
    soundfile = None


def _send_profiling_via_wormhole(profile_path: str, timeout: int = 60) -> None:
    """Auto-send profiling JSON via Magic Wormhole after recording.

    Args:
        profile_path: Path to the profiling JSON file.
        timeout: Maximum seconds to wait for a receiver (default: 60).
    """
    import subprocess as _sp

    from openadapt_capture.share import _find_wormhole

    wormhole_bin = _find_wormhole()
    if not wormhole_bin:
        print("wormhole not found. To enable auto-send:")
        print("  pip install 'openadapt-capture[share]'")
        print(f"Profiling saved to: {profile_path}")
        return

    print(f"Sending profiling via wormhole (waiting up to {timeout}s for receiver)...")
    print("Give the wormhole code below to the receiver.\n")
    try:
        _sp.run([wormhole_bin, "send", profile_path], check=True, timeout=timeout)
    except _sp.TimeoutExpired:
        logger.warning(f"Wormhole send timed out after {timeout}s. File at: {profile_path}")
    except _sp.CalledProcessError:
        print(f"Wormhole send failed. File at: {profile_path}")
    except KeyboardInterrupt:
        print(f"\nCancelled. File at: {profile_path}")


Event = namedtuple("Event", ("timestamp", "type", "data"))

EVENT_TYPES = ("screen", "action", "window", "browser")
LOG_LEVEL = "INFO"
BROWSER_RECORDING_GUIDANCE = (
    "The Chrome-extension WebSocket prototype is not part of the supported "
    "openadapt-capture runtime. Use openadapt-flow Playwright launch or attach "
    "recording instead."
)


class _ScreenTimingStats:
    """Accumulate screen timing stats without storing every data point."""

    def __init__(self):
        self.count = 0
        self.ss_sum = 0.0
        self.ss_max = 0.0
        self.ss_min = float("inf")
        self.total_sum = 0.0
        self.total_max = 0.0

    def append(self, pair):
        ss_dur, total_dur = pair
        self.count += 1
        self.ss_sum += ss_dur
        self.ss_max = max(self.ss_max, ss_dur)
        self.ss_min = min(self.ss_min, ss_dur)
        self.total_sum += total_dur
        self.total_max = max(self.total_max, total_dur)

    def to_dict(self):
        if self.count == 0:
            return {}
        return {
            "iterations": self.count,
            "screenshot_avg_ms": round(self.ss_sum / self.count * 1000, 1),
            "screenshot_max_ms": round(self.ss_max * 1000, 1),
            "screenshot_min_ms": round(self.ss_min * 1000, 1),
            "total_avg_ms": round(self.total_sum / self.count * 1000, 1),
            "total_max_ms": round(self.total_max * 1000, 1),
        }

    def __bool__(self):
        return self.count > 0


# whether to write events of each type in a separate process
PROC_WRITE_BY_EVENT_TYPE = {
    "screen": True,
    "screen/video": True,
    "action": True,
    "window": True,
    "browser": True,
}
NUM_MEMORY_STATS_TO_LOG = 3
STARTUP_WAIT_POLL_SECONDS = 0.1
PRE_READY_TASK_JOIN_TIMEOUT_SECONDS = 2.0

stop_sequence_detected = False


def _run_task_fail_loud(
    task_name: str,
    target: Callable[..., None],
    args: tuple[Any, ...],
    terminate_processing: Any,
    task_errors: queue.Queue,
) -> None:
    """Propagate reader-thread failures back through the recording boundary."""
    try:
        target(*args)
    except BaseException as exc:
        task_errors.put((task_name, exc))
        terminate_processing.set()


def _wait_for_tasks_started(
    task_by_name: dict[str, Any],
    task_started_events: dict[str, Any],
    terminate_processing: Any,
) -> bool:
    """Wait for pipeline readiness while honoring shutdown and worker failure.

    Returns ``True`` only when every task has announced readiness. A shutdown
    request or a task that exits before setting its readiness event fails the
    startup and signals the rest of the pipeline to stop.
    """
    expected_starts = len(task_by_name)
    logger.info(f"{expected_starts=}")

    while True:
        if terminate_processing.is_set():
            logger.info("Recording startup cancelled before all tasks were ready")
            return False

        waiting_for = [name for name, event in task_started_events.items() if not event.is_set()]
        if not waiting_for:
            return True

        stopped_before_ready = [
            name
            for name in waiting_for
            if name in task_by_name and not task_by_name[name].is_alive()
        ]
        if stopped_before_ready:
            logger.error(f"Recording tasks exited before readiness: {stopped_before_ready}")
            terminate_processing.set()
            return False

        logger.info(f"Waiting for tasks to start: {waiting_for}")
        logger.info(f"Started tasks: {expected_starts - len(waiting_for)}/{expected_starts}")
        terminate_processing.wait(STARTUP_WAIT_POLL_SECONDS)


def _join_tasks(
    task_by_name: dict[str, Any],
    task_names: list[str],
    *,
    timeout: float | None = None,
) -> list[str]:
    """Join pipeline tasks, bounding teardown when startup never completed."""
    deadline = time.monotonic() + timeout if timeout is not None else None
    lingering: list[str] = []

    for task_name in task_names:
        task = task_by_name.get(task_name)
        if task is None:
            continue

        logger.info(f"joining {task_name=}...")
        if deadline is None:
            task.join()
        else:
            task.join(timeout=max(0.0, deadline - time.monotonic()))

        if not task.is_alive():
            continue

        # Processes can be stopped after the shared graceful-shutdown deadline.
        # Python cannot forcibly stop threads; recorder-owned threads are daemons
        # and all receive terminate_processing before this helper is called.
        if isinstance(task, multiprocessing.process.BaseProcess):
            logger.warning(f"terminating {task_name!r} after pre-ready shutdown timeout")
            task.terminate()
            task.join(timeout=0.5)

        if task.is_alive():
            lingering.append(task_name)

    if lingering:
        logger.warning(f"tasks still exiting after bounded shutdown: {lingering}")
    return lingering


def _raise_for_failed_processes(task_by_name: dict[str, Any]) -> None:
    """Surface required child-process failures through the recording boundary."""
    failures = {
        name: task.exitcode
        for name, task in task_by_name.items()
        if isinstance(task, multiprocessing.process.BaseProcess) and task.exitcode not in (None, 0)
    }
    if failures:
        detail = ", ".join(
            f"{name} (exit code {exitcode})" for name, exitcode in sorted(failures.items())
        )
        raise RuntimeError(f"Recording child process failed: {detail}")


def collect_stats(performance_snapshots: list[tracemalloc.Snapshot]) -> None:
    """Collects and appends performance snapshots using tracemalloc.

    Args:
        performance_snapshots (list[tracemalloc.Snapshot]): The list of snapshots.
    """
    performance_snapshots.append(tracemalloc.take_snapshot())


def log_memory_usage(
    tracker: tracker.SummaryTracker,
    performance_snapshots: list[tracemalloc.Snapshot],
) -> None:
    """Logs memory usage stats and allocation trace based on snapshots.

    Args:
        tracker (tracker.SummaryTracker): The tracker to use.
        performance_snapshots (list[tracemalloc.Snapshot]): The list of snapshots.
    """
    assert len(performance_snapshots) == 2, performance_snapshots
    first_snapshot, last_snapshot = performance_snapshots
    stats = last_snapshot.compare_to(first_snapshot, "lineno")

    for stat in stats[:NUM_MEMORY_STATS_TO_LOG]:
        new_KiB = stat.size_diff / 1024
        total_KiB = stat.size / 1024
        new_blocks = stat.count_diff
        total_blocks = stat.count
        source = stat.traceback.format()[0].strip()
        logger.info(f"{source=}")
        logger.info(f"\t{new_KiB=} {total_KiB=} {new_blocks=} {total_blocks=}")

    trace_str = "\n".join(list(tracker.format_diff()))
    logger.info(f"trace_str=\n{trace_str}")


def process_event(
    event: ActionEvent,
    write_q: sq.SynchronizedQueue,
    write_fn: Callable,
    recording: Recording,
    perf_q: sq.SynchronizedQueue,
) -> None:
    """Process an event and take appropriate action based on its type.

    Args:
        event: The event to process.
        write_q: The queue for writing the event.
        write_fn: The function for writing the event.
        recording: The recording object.
        perf_q: The queue for collecting performance statistics.

    Returns:
        None
    """
    if PROC_WRITE_BY_EVENT_TYPE[event.type]:
        write_q.put(event)
    else:
        write_fn(recording, event, perf_q)


@utils.trace(logger)
def process_events(
    event_q: queue.Queue,
    screen_write_q: sq.SynchronizedQueue,
    action_write_q: sq.SynchronizedQueue,
    window_write_q: sq.SynchronizedQueue,
    browser_write_q: sq.SynchronizedQueue,
    video_write_q: sq.SynchronizedQueue,
    perf_q: sq.SynchronizedQueue,
    recording: Recording,
    terminate_processing: multiprocessing.Event,
    started_event: threading.Event,
    num_screen_events: multiprocessing.Value,
    num_action_events: multiprocessing.Value,
    num_window_events: multiprocessing.Value,
    num_browser_events: multiprocessing.Value,
    num_video_events: multiprocessing.Value,
) -> None:
    """Process events from the event queue and write them to write queues.

    Args:
        event_q: A queue with events to be processed.
        screen_write_q: A queue for writing screen events.
        action_write_q: A queue for writing action events.
        window_write_q: A queue for writing window events.
        browser_write_q: A queue for writing browser events,
        video_write_q: A queue for writing video events.
        perf_q: A queue for collecting performance data.
        recording: The recording object.
        terminate_processing: An event to signal the termination of the process.
        started_event: Event to set once started.
        num_screen_events: A counter for the number of screen events.
        num_action_events: A counter for the number of action events.
        num_window_events: A counter for the number of window events.
        num_browser_events: A counter for the number of browser events.
        num_video_events: A counter for the number of video events.
    """
    utils.set_start_time(recording.timestamp)

    logger.info("Starting")

    prev_event = None
    prev_screen_event = None
    prev_window_event = None
    prev_saved_screen_timestamp = 0
    prev_saved_window_timestamp = 0
    started = False
    while not terminate_processing.is_set() or not event_q.empty():
        # Bounded get: a bare event_q.get() deadlocks shutdown when terminate
        # is set while the queue is empty and the readers have already exited
        # (nobody left to feed an event, so the loop condition is never
        # re-checked and join_tasks() hangs forever on this thread).
        try:
            event = event_q.get(timeout=1)
        except queue.Empty:
            continue
        if not started:
            started_event.set()
            started = True
        logger.trace(f"{event=}")
        assert event.type in EVENT_TYPES, event
        if prev_event is not None:
            try:
                assert event.timestamp > prev_event.timestamp, (
                    event,
                    prev_event,
                )
            except AssertionError:
                delta = event.timestamp - prev_event.timestamp
                log_prev_event = prev_event._replace(data="")
                log_event = event._replace(data="")
                logger.error(f"{delta=} {log_prev_event=} {log_event=}")
                # behavior undefined, swallow for now
                # XXX TODO: mitigate
        if event.type == "screen":
            prev_screen_event = event
            if config.RECORD_FULL_VIDEO:
                video_event = event._replace(type="screen/video")
                process_event(
                    video_event,
                    video_write_q,
                    write_video_event,
                    recording,
                    perf_q,
                )
                num_video_events.value += 1
        elif event.type == "window":
            prev_window_event = event
        elif event.type == "browser":
            if config.RECORD_BROWSER_EVENTS:
                process_event(
                    event,
                    browser_write_q,
                    write_browser_event,
                    recording,
                    perf_q,
                )
                num_browser_events.value += 1
        elif event.type == "action":
            if prev_screen_event is None:
                logger.warning("Discarding action that came before screen")
                continue
            else:
                event.data["screenshot_timestamp"] = prev_screen_event.timestamp

            if prev_window_event is None:
                if config.RECORD_WINDOW_DATA:
                    logger.warning("Discarding action that came before window")
                    continue
                # Window capture disabled — skip window timestamp requirement
            else:
                event.data["window_event_timestamp"] = prev_window_event.timestamp

            process_event(
                event,
                action_write_q,
                write_action_event,
                recording,
                perf_q,
            )

            num_action_events.value += 1

            if prev_saved_screen_timestamp < prev_screen_event.timestamp:
                process_event(
                    prev_screen_event,
                    screen_write_q,
                    write_screen_event,
                    recording,
                    perf_q,
                )
                num_screen_events.value += 1
                prev_saved_screen_timestamp = prev_screen_event.timestamp
                if config.RECORD_VIDEO and not config.RECORD_FULL_VIDEO:
                    prev_video_event = prev_screen_event._replace(type="screen/video")
                    process_event(
                        prev_video_event,
                        video_write_q,
                        write_video_event,
                        recording,
                        perf_q,
                    )
                    num_video_events.value += 1
            if prev_window_event is not None:
                if prev_saved_window_timestamp < prev_window_event.timestamp:
                    process_event(
                        prev_window_event,
                        window_write_q,
                        write_window_event,
                        recording,
                        perf_q,
                    )
                    num_window_events.value += 1
                    prev_saved_window_timestamp = prev_window_event.timestamp
        else:
            raise Exception(f"unhandled {event.type=}")
        del prev_event
        prev_event = event
    logger.info("Done")


def write_action_event(
    db: crud.SaSession,
    recording: Recording,
    event: Event,
    perf_q: sq.SynchronizedQueue,
) -> None:
    """Write an action event to the database and update the performance queue.

    Args:
        db: The database session.
        recording: The recording object.
        event: An action event to be written.
        perf_q: A queue for collecting performance data.
    """
    assert event.type == "action", event
    crud.insert_action_event(db, recording, event.timestamp, event.data)
    perf_q.put((event.type, event.timestamp, utils.get_timestamp()))


def write_screen_event(
    db: crud.SaSession,
    recording: Recording,
    event: Event,
    perf_q: sq.SynchronizedQueue,
) -> None:
    """Write a screen event to the database and update the performance queue.

    Args:
        db: The database session.
        recording: The recording object.
        event: A screen event to be written.
        perf_q: A queue for collecting performance data.
    """
    assert event.type == "screen", event
    image = event.data
    if config.RECORD_IMAGES:
        with io.BytesIO() as output:
            image.save(output, format="PNG")
            png_data = output.getvalue()
        event_data = {"png_data": png_data}
    else:
        event_data = {}
    crud.insert_screenshot(db, recording, event.timestamp, event_data)
    perf_q.put((event.type, event.timestamp, utils.get_timestamp()))


def write_window_event(
    db: crud.SaSession,
    recording: Recording,
    event: Event,
    perf_q: sq.SynchronizedQueue,
) -> None:
    """Write a window event to the database and update the performance queue.

    Args:
        db: The database session.
        recording: The recording object.
        event: A window event to be written.
        perf_q: A queue for collecting performance data.
    """
    assert event.type == "window", event
    crud.insert_window_event(db, recording, event.timestamp, event.data)
    perf_q.put((event.type, event.timestamp, utils.get_timestamp()))


def write_browser_event(
    db: crud.SaSession,
    recording: Recording,
    event: Event,
    perf_q: sq.SynchronizedQueue,
) -> None:
    """Write a browser event to the database and update the performance queue.

    Args:
        db: The database session.
        recording: The recording object.
        event: A browser event to be written.
        perf_q: A queue for collecting performance data.
    """
    assert event.type == "browser", event
    crud.insert_browser_event(db, recording, event.timestamp, event.data)
    perf_q.put((event.type, event.timestamp, utils.get_timestamp()))


@utils.trace(logger)
def write_events(
    event_type: str,
    write_fn: Callable,
    write_q: sq.SynchronizedQueue,
    num_events: multiprocessing.Value,
    perf_q: sq.SynchronizedQueue,
    recording: Recording,
    db_path: str,
    terminate_processing: multiprocessing.Event,
    started_event: multiprocessing.Event,
    pre_callback: Callable[[float], dict] | None = None,
    post_callback: Callable[[dict], None] | None = None,
) -> None:
    """Write events of a specific type to the db using the provided write function.

    Args:
        event_type: The type of events to be written.
        write_fn: A function to write events to the database.
        write_q: A queue with events to be written.
        num_events: A counter for the number of events.
        perf_q: A queue for collecting performance data.
        recording: The recording object.
        db_path: Path to the per-capture database file.
        terminate_processing: An event to signal the termination of the process.
        started_event: Event to increment once started.
        pre_callback: Optional function to call before main loop. Takes recording
            timestamp as only argument, returns a state dict.
        post_callback: Optional function to call after main loop. Takes state dict as
            only argument, returns None.
    """
    utils.set_start_time(recording.timestamp)

    logger.info(f"{event_type=} starting")
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    session = get_session_for_path(db_path)

    if pre_callback:
        state = pre_callback(session, recording)
    else:
        state = None

    num_processed = 0
    progress = None
    started = False
    while not terminate_processing.is_set() or not write_q.empty():
        if terminate_processing.is_set() and progress is None:
            # if processing is over, create a progress bar
            total_events = num_events.value
            progress = tqdm(
                total=total_events,
                desc=f"Writing {event_type} events...",
                unit="event",
                colour="green",
                dynamic_ncols=True,
            )
            # update the progress bar with the number of events that have already
            # been processed
            for _ in range(num_processed):
                progress.update()
        if not started:
            started_event.set()
            started = True
        try:
            event = write_q.get_nowait()
        except queue.Empty:
            continue
        assert event.type == event_type, (event_type, event)
        state = write_fn(session, recording, event, perf_q, **(state or {}))
        num_processed += 1
        with num_events.get_lock():
            if progress is not None:
                if progress.total < num_events.value:
                    # update the total number of events in the progress bar
                    progress.total = num_events.value
                    progress.refresh()
                progress.update()
        logger.debug(f"{event_type=} written")

    if post_callback:
        post_callback(state)

    if progress is not None:
        progress.close()

    logger.info(f"{event_type=} done")


def video_pre_callback(
    db: crud.SaSession,
    recording: Recording,
    video_dir: str = None,
    frame_size: tuple[int, int] | None = None,
    provision: video.FFmpegProvision | None = None,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """Function to call before main loop.

    Args:
        db: The database session.
        recording: The recording object.
        video_dir: Directory for video files.
        frame_size: Explicit (width, height) for the video stream. Used by
            window-scoped capture, whose frames are the target window's pixels
            rather than the monitor's. Defaults to the full-screen size.
        provision: Parent-preflighted encoder contract. Passing this immutable
            value keeps spawn-based writers on the exact executable and codec.
        timeout_seconds: Bound for final encoding and verification.

    Returns:
        dict[str, Any]: The updated state.
    """
    video_file_path = video.get_video_file_path(recording.timestamp, video_dir)
    if frame_size is not None:
        monitor_width, monitor_height = frame_size
    else:
        # TODO XXX replace with utils.get_monitor_dims() once fixed
        monitor_width, monitor_height = utils.take_screenshot().size
    video_container, video_stream, video_start_timestamp = video.initialize_video_writer(
        video_file_path,
        monitor_width,
        monitor_height,
        timeout_seconds=timeout_seconds,
        preflight_provision=provision,
    )
    crud.update_video_start_time(db, recording, video_start_timestamp)
    return {
        "video_container": video_container,
        "video_stream": video_stream,
        "video_start_timestamp": video_start_timestamp,
        "last_pts": 0,
        "video_file_path": video_file_path,
    }


def video_post_callback(state: dict) -> None:
    """Function to call after main loop.

    Args:
        state (dict): The current state.
    """
    if state is None or "last_frame" not in state:
        logger.warning("No video frames captured — skipping finalization")
        if state and "video_container" in state:
            state["video_container"].close()
        return
    video.finalize_video_writer(
        state["video_container"],
        state["video_stream"],
        state["video_start_timestamp"],
        state["last_frame"],
        state["last_frame_timestamp"],
        state["last_pts"],
        state["video_file_path"],
    )


def write_video_event(
    db: crud.SaSession,
    recording_timestamp: float,
    event: Event,
    perf_q: sq.SynchronizedQueue,
    video_container: Any,
    video_stream: Any,
    video_start_timestamp: float,
    last_pts: int = 0,
    num_copies: int = 2,
    **kwargs: dict,
) -> dict[str, Any]:
    """Write a screen event to the video file and update the performance queue.

    Args:
        db: The database session.
        recording_timestamp: The timestamp of the recording.
        event: A screen event to be written.
        perf_q: A queue for collecting performance data.
        video_container: The direct external-encoder stream.
        video_stream: The configured external-encoder stream.
        video_start_timestamp (float): The base timestamp from which the video
            recording started.
        last_pts: The last presentation timestamp.
        num_copies: The number of times to write the frame.

    Returns:
        dict containing state.
    """
    assert event.type == "screen/video"
    screenshot_image = event.data
    screenshot_timestamp = event.timestamp
    stream_size = (video_stream.width, video_stream.height)
    if (screenshot_image.width, screenshot_image.height) != stream_size:
        # Window-scoped capture normalizes every resize into its initial fixed
        # viewport. A mismatch now means a producer violated the stream
        # contract. Stop instead of emitting a complete-looking video with a
        # silent evidence gap.
        raise ValueError(
            f"video frame {screenshot_image.size} differs from fixed stream {stream_size}"
        )
    force_key_frame = last_pts == 0
    # ensure that the first frame is available (otherwise occasionally it is not)
    # TODO: why isn't force_key_frame sufficient?
    if last_pts != 0:
        num_copies = 1
    for _ in range(num_copies):
        last_pts = video.write_video_frame(
            video_container,
            video_stream,
            screenshot_image,
            screenshot_timestamp,
            video_start_timestamp,
            last_pts,
            force_key_frame,
        )
    perf_q.put((event.type, event.timestamp, utils.get_timestamp()))
    return {
        **kwargs,
        **{
            "video_container": video_container,
            "video_stream": video_stream,
            "video_start_timestamp": video_start_timestamp,
            "last_frame": screenshot_image,
            "last_frame_timestamp": screenshot_timestamp,
            "last_pts": last_pts,
        },
    }


def trigger_action_event(
    event_q: queue.Queue,
    action_event_args: dict[str, Any],
    coordinate_scope: CoordinateScope | None = None,
    timestamp: float | None = None,
    structural_observer: StructuralObserver | None = None,
) -> None:
    """Triggers an action event and adds it to the event queue.

    Args:
        event_q: The event queue to add the action event to.
        action_event_args: A dictionary containing the arguments for the action event.
        coordinate_scope: When set, global mouse coordinates are translated
            into the exact captured-frame pixel space. A window scope tracks
            the target window. A desktop scope subtracts the combined virtual
            desktop origin, including negative-origin secondary monitors.
        timestamp: Native event-receipt time. Defaults to the current recording
            clock only for legacy/direct callers.
        structural_observer: Optional accessibility observer. Evidence is
            captured against global coordinates before any window translation.

    Returns:
        None
    """
    event_timestamp = utils.get_timestamp() if timestamp is None else timestamp
    x = action_event_args.get("mouse_x")
    y = action_event_args.get("mouse_y")
    observation = observe_structural_action(
        structural_observer,
        StructuralObservationRequest(
            event_timestamp=event_timestamp,
            action_name=str(action_event_args.get("name") or "unknown"),
            x=x,
            y=y,
        ),
    )
    if observation is not None:
        action_event_args["structural_observation"] = observation.model_dump(
            mode="json",
            exclude_none=True,
        )
    if x is not None and y is not None:
        if config.RECORD_READ_ACTIVE_ELEMENT_STATE:
            # element lookup needs GLOBAL coordinates: translate afterwards.
            element_state = window.get_active_element_state(x, y)
        else:
            element_state = {}
        action_event_args["element_state"] = element_state
        if coordinate_scope is not None:
            # The recorder captures and validates its first scoped frame before
            # starting input. A translation failure after that boundary means
            # evidence is incomplete and must terminate the session.
            wx, wy = coordinate_scope.translate(x, y)
            action_event_args["mouse_x"] = wx
            action_event_args["mouse_y"] = wy
    event_q.put(
        Event(
            event_timestamp,
            "action",
            action_event_args,
        )
    )


def on_move(
    event_q: queue.Queue,
    coordinate_scope: CoordinateScope | None,
    x: float,
    y: float,
    injected: bool = False,
    timestamp: float | None = None,
) -> None:
    """Handles the 'move' event.

    Args:
        event_q: The event queue to add the 'move' event to.
        coordinate_scope: Optional captured-frame coordinate translator.
        x: The x-coordinate of the mouse.
        y: The y-coordinate of the mouse.
        injected: Whether the event was injected or not.

    Returns:
        None
    """
    logger.debug(f"{x=} {y=} {injected=}")
    if not injected:
        trigger_action_event(
            event_q,
            {"name": "move", "mouse_x": x, "mouse_y": y},
            coordinate_scope,
            timestamp,
        )


def on_click(
    event_q: queue.Queue,
    coordinate_scope: CoordinateScope | None,
    x: float,
    y: float,
    button: str,
    pressed: bool,
    injected: bool = False,
    timestamp: float | None = None,
    structural_observer: StructuralObserver | None = None,
) -> None:
    """Handles the 'click' event.

    Args:
        event_q: The event queue to add the 'click' event to.
        coordinate_scope: Optional captured-frame coordinate translator.
        x: The x-coordinate of the mouse.
        y: The y-coordinate of the mouse.
        button: The mouse button.
        pressed: Whether the button is pressed or released.
        injected: Whether the event was injected or not.

    Returns:
        None
    """
    logger.debug(f"{x=} {y=} {button=} {pressed=} {injected=}")
    if not injected:
        trigger_action_event(
            event_q,
            {
                "name": "click",
                "mouse_x": x,
                "mouse_y": y,
                "mouse_button_name": button,
                "mouse_pressed": pressed,
            },
            coordinate_scope,
            timestamp,
            structural_observer if pressed else None,
        )


def on_scroll(
    event_q: queue.Queue,
    coordinate_scope: CoordinateScope | None,
    x: float,
    y: float,
    dx: float,
    dy: float,
    injected: bool = False,
    timestamp: float | None = None,
    structural_observer: StructuralObserver | None = None,
) -> None:
    """Handles the 'scroll' event.

    Args:
        event_q: The event queue to add the 'scroll' event to.
        coordinate_scope: Optional captured-frame coordinate translator.
        x: The x-coordinate of the mouse.
        y: The y-coordinate of the mouse.
        dx: The horizontal scroll amount.
        dy: The vertical scroll amount.
        injected: Whether the event was injected or not.

    Returns:
        None
    """
    logger.debug(f"{x=} {y=} {dx=} {dy=} {injected=}")
    if not injected:
        trigger_action_event(
            event_q,
            {
                "name": "scroll",
                "mouse_x": x,
                "mouse_y": y,
                "mouse_dx": dx,
                "mouse_dy": dy,
            },
            coordinate_scope,
            timestamp,
            structural_observer,
        )


def handle_key(
    event_q: queue.Queue,
    key: ObservedKey,
    structural_observer: StructuralObserver | None = None,
) -> None:
    """Persist a normalized native key transition.

    Args:
        event_q: The event queue to add the key event to.
        key: Normalized physical/canonical key identity.

    Returns:
        None
    """
    trigger_action_event(
        event_q,
        {
            "name": "press" if key.pressed else "release",
            "key_name": key.key_name,
            "key_char": key.key_char,
            "key_vk": key.key_vk,
            "canonical_key_name": key.canonical_key_name,
            "canonical_key_char": key.canonical_key_char,
            "canonical_key_vk": key.canonical_key_vk,
        },
        timestamp=key.timestamp,
        structural_observer=structural_observer if key.pressed else None,
    )


def read_screen_events(
    event_q: queue.Queue,
    terminate_processing: multiprocessing.Event,
    recording: Recording,
    started_event: threading.Event,
    _screen_timing: _ScreenTimingStats | None = None,
    window_scope: WindowCaptureScope | None = None,
    desktop_scope: DesktopCaptureScope | None = None,
) -> None:
    """Read screen events and add them to the event queue.

    Captures at most ``config.SCREEN_CAPTURE_FPS`` frames per second.
    Set to 0 for unlimited (legacy behaviour).

    In window-scoped mode (``window_scope`` set) each frame captures the
    TARGET WINDOW's pixels instead of the full screen; the window is
    re-resolved every frame (windows move/resize) and a bounds-timeline
    "window" event is queued whenever the resolved bounds change, so
    converters can reconstruct the exact window position for every action.

    Args:
        event_q: A queue for adding screen events.
        terminate_processing: An event to signal the termination of the process.
        recording: The recording object.
        started_event: Event to set once started.
        _screen_timing: If provided, record (screenshot_dur, total_dur) per iteration.
        window_scope: Optional window scope for window-pixel-space capture.
        desktop_scope: Full-screen virtual-desktop contract. It verifies the
            monitor topology before and after each captured frame.
    """
    if window_scope is not None and desktop_scope is not None:
        raise ValueError("screen reader cannot use both window and desktop scopes")
    utils.set_start_time(recording.timestamp)

    fps = config.SCREEN_CAPTURE_FPS
    min_interval = 1.0 / fps if fps > 0 else 0.0

    logger.info(f"Starting (fps={fps}, min_interval={min_interval:.3f}s)")
    started = False
    announced_window = False
    while not terminate_processing.is_set():
        t_start = time.perf_counter()
        if window_scope is not None:
            # Any failed capture terminates the session. Retrying would omit a
            # frame while input continues and could produce complete-looking
            # evidence with a missing interval.
            screenshot, window_changed = window_scope.capture_frame()
            if window_changed or not announced_window:
                event_q.put(
                    Event(
                        utils.get_timestamp(),
                        "window",
                        window_scope.window_event_data(),
                    )
                )
                announced_window = True
        elif desktop_scope is not None:
            # A monitor can move or change scale while the combined frame keeps
            # the same dimensions. Check both sides of the grab so neither the
            # frame nor later input uses stale origin or monitor geometry.
            desktop_scope.assert_current(force=True)
            screenshot = utils.take_screenshot()
            desktop_scope.assert_current(force=True)
        else:
            screenshot = utils.take_screenshot()
        t_screenshot = time.perf_counter()
        if screenshot is None:
            logger.warning("Screenshot was None")
            continue
        if not started:
            started_event.set()
            started = True
        event_q.put(Event(utils.get_timestamp(), "screen", screenshot))
        # Throttle: sleep for the remainder of the frame interval
        if min_interval > 0:
            elapsed = time.perf_counter() - t_start
            sleep_time = min_interval - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)
        if _screen_timing is not None:
            t_end = time.perf_counter()
            _screen_timing.append((t_screenshot - t_start, t_end - t_start))
    logger.info("Done")


@utils.trace(logger)
def read_window_events(
    event_q: queue.Queue,
    terminate_processing: multiprocessing.Event,
    recording: Recording,
    started_event: threading.Event,
) -> None:
    """Read window events and add them to the event queue.

    Args:
        event_q: A queue for adding window events.
        terminate_processing: An event to signal the termination of the process.
        recording: The recording object.
        started_event: Event to set once started.
    """
    utils.set_start_time(recording.timestamp)

    # Refuse at the boundary. Without this the loop below polls a backend that
    # can never answer, never sets started_event, and the recording hangs in
    # startup with no stated cause.
    window.require_impl()

    logger.info("Starting")
    prev_window_data = {}
    started = False
    while not terminate_processing.is_set():
        window_data = window.get_active_window_data()
        if not window_data:
            time.sleep(0.1)
            continue

        if not started:
            started_event.set()
            started = True

        if window_data["title"] != prev_window_data.get("title") or window_data[
            "window_id"
        ] != prev_window_data.get("window_id"):
            # TODO: fix exception sometimes triggered by the next line on win32:
            #   File "\Python39\lib\threading.py" line 917, in run
            #   File "...\openadapt\record.py", line 277, in read window events
            #   File "...\env\lib\site-packages\loguru\logger.py" line 1977, in info
            #   File "...\env\lib\site-packages\loguru\_logger.py", line 1964, in _log
            #       for handler in core.handlers.values):
            #   RuntimeError: dictionary changed size during iteration
            _window_data = window_data
            _window_data.pop("state")
            logger.info(f"{_window_data=}")
        if window_data != prev_window_data:
            logger.debug("Queuing window event for writing")
            event_q.put(
                Event(
                    utils.get_timestamp(),
                    "window",
                    window_data,
                )
            )
        prev_window_data = window_data
        time.sleep(0.1)  # poll ~10 times/sec instead of tight loop


@utils.trace(logger)
def performance_stats_writer(
    perf_q: sq.SynchronizedQueue,
    recording: Recording,
    db_path: str,
    terminate_processing: multiprocessing.Event,
    started_event: multiprocessing.Event,
) -> None:
    """Write performance stats to the database.

    Each entry includes the event type, start time, and end time.

    Args:
        perf_q: A queue for collecting performance data.
        recording: The recording object.
        db_path: Path to the per-capture database file.
        terminate_processing: An event to signal the termination of the process.
        started_event: Event to set once started.
    """
    utils.set_start_time(recording.timestamp)

    logger.info("Performance stats writer starting")
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    started = False
    session = get_session_for_path(db_path)
    while not terminate_processing.is_set() or not perf_q.empty():
        if not started:
            started_event.set()
            started = True
        try:
            event_type, start_time, end_time = perf_q.get_nowait()
        except queue.Empty:
            continue

        crud.insert_perf_stat(
            session,
            recording,
            event_type,
            start_time,
            end_time,
        )
    logger.info("Performance stats writer done")


def memory_writer(
    recording: Recording,
    db_path: str,
    terminate_processing: multiprocessing.Event,
    record_pid: int,
    started_event: multiprocessing.Event,
) -> None:
    """Writes memory usage statistics to the database.

    Args:
        recording (Recording): The recording object.
        db_path: Path to the per-capture database file.
        terminate_processing (multiprocessing.Event): The event used to terminate
          the process.
        record_pid (int): The process ID to monitor memory usage for.
        started_event: Event to set once started.

    Returns:
        None
    """
    utils.set_start_time(recording.timestamp)

    logger.info("Memory writer starting")
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    process = psutil.Process(record_pid)

    started = False
    session = get_session_for_path(db_path)
    while not terminate_processing.is_set():
        if not started:
            started_event.set()
            started = True
        memory_usage_bytes = 0

        memory_info = process.memory_info()
        rss = memory_info.rss  # Resident Set Size: non-swapped physical memory
        memory_usage_bytes += rss

        for child in process.children(recursive=True):
            # after ctrl+c, children may terminate before the next line
            try:
                child_memory_info = child.memory_info()
            except psutil.NoSuchProcess:
                continue
            child_rss = child_memory_info.rss
            rss += child_rss

        timestamp = utils.get_timestamp()

        crud.insert_memory_stat(
            session,
            recording,
            rss,
            timestamp,
        )
        time.sleep(1)  # sample once per second instead of tight loop
    logger.info("Memory writer done")


@utils.trace(logger)
def create_recording(
    task_description: str,
    capture_dir: str,
    window_capture_info: dict | None = None,
    desktop_capture_info: dict | None = None,
) -> tuple[Recording, str]:
    """Create a new recording entry in the per-capture database.

    Args:
        task_description: A text description of the task being recorded.
        capture_dir: Path to the capture directory.
        window_capture_info: Window-scoping metadata
            (``WindowCaptureScope.snapshot()``) persisted in the recording's
            config JSON under ``capture_window`` so converters know the
            session's coordinates are in window-pixel space.
        desktop_capture_info: Combined-monitor geometry persisted under
            ``capture_desktop`` for full-screen recordings.

    Returns:
        tuple of (Recording object, db_path).
    """
    if window_capture_info is not None and desktop_capture_info is not None:
        raise ValueError("a recording cannot declare both window and desktop capture scopes")
    os.makedirs(capture_dir, exist_ok=True)
    db_path = os.path.join(capture_dir, "recording.db")

    timestamp = utils.set_start_time()
    monitor_width, monitor_height = utils.get_monitor_dims()
    try:
        pixel_ratio = platform.get_display_pixel_ratio()
    except platform.DisplayMetricsUnavailable as exc:
        # Persist NULL, not 1.0. The column is nullable precisely so that
        # "unknown" is representable; recording 1.0 would report an
        # unmeasured display as a verified standard-density one and every
        # downstream coordinate would be rescaled from a fabricated number.
        logger.error(f"Display pixel ratio could not be measured: {exc}")
        pixel_ratio = None
    double_click_distance_pixels = utils.get_double_click_distance_pixels()
    double_click_interval_seconds = utils.get_double_click_interval_seconds()
    recording_data = {
        # TODO: rename
        "timestamp": timestamp,
        "monitor_width": monitor_width,
        "monitor_height": monitor_height,
        "pixel_ratio": pixel_ratio,
        "double_click_distance_pixels": double_click_distance_pixels,
        "double_click_interval_seconds": double_click_interval_seconds,
        "platform": sys.platform,
        "task_description": task_description,
    }
    capture_config: dict[str, Any] = {}
    if window_capture_info is not None:
        capture_config["capture_window"] = window_capture_info
    if desktop_capture_info is not None:
        capture_config["capture_desktop"] = desktop_capture_info
    if capture_config:
        recording_data["config"] = capture_config
    engine, Session = create_db(db_path)
    session = Session()
    recording = crud.insert_recording(session, recording_data)
    logger.info(f"{recording=}")
    return recording, db_path


def read_input_events(
    event_q: queue.Queue,
    terminate_processing: multiprocessing.Event,
    recording: Recording,
    started_event: threading.Event,
    coordinate_scope: CoordinateScope | None = None,
    structural_observer: StructuralObserver | None = None,
) -> None:
    """Read globally ordered keyboard and mouse events from one native observer."""
    stop_sequences = [sequence for sequence in config.STOP_SEQUENCES if sequence]
    stop_sequence_indices = [0 for _ in stop_sequences]

    def on_observed(event: ObservedInput) -> None:
        if isinstance(event, ObservedMouseMove):
            on_move(
                event_q,
                coordinate_scope,
                event.x,
                event.y,
                event.injected,
                timestamp=event.timestamp,
            )
            return
        if isinstance(event, ObservedMouseButton):
            on_click(
                event_q,
                coordinate_scope,
                event.x,
                event.y,
                event.button,
                event.pressed,
                event.injected,
                timestamp=event.timestamp,
                structural_observer=structural_observer,
            )
            return
        if isinstance(event, ObservedMouseScroll):
            on_scroll(
                event_q,
                coordinate_scope,
                event.x,
                event.y,
                event.dx,
                event.dy,
                event.injected,
                timestamp=event.timestamp,
                structural_observer=structural_observer,
            )
            return
        if event.injected:
            return

        logger.debug(f"{event=}")
        handle_key(event_q, event, structural_observer)
        if not event.pressed:
            return

        nonlocal stop_sequence_indices
        global stop_sequence_detected
        candidate = event.canonical_key_char or event.canonical_key_name
        if candidate is None:
            stop_sequence_indices = [0 for _ in stop_sequences]
            return
        candidate = candidate.lower()
        for index, sequence in enumerate(stop_sequences):
            expected = sequence[stop_sequence_indices[index]].lower()
            if candidate == expected:
                stop_sequence_indices[index] += 1
            else:
                stop_sequence_indices[index] = 1 if candidate == sequence[0].lower() else 0
            if stop_sequence_indices[index] == len(sequence):
                stop_sequence_indices[index] = 0
                logger.info("Stop sequence entered! Stopping recording now.")
                stop_sequence_detected = True

    if structural_observer is not None:
        start_hook = getattr(structural_observer, "open_current_thread", None)
        stop_hook = getattr(structural_observer, "close_current_thread", None)
        if callable(start_hook):
            setattr(on_observed, "_openadapt_delivery_thread_start", start_hook)
        if callable(stop_hook):
            setattr(on_observed, "_openadapt_delivery_thread_stop", stop_hook)

    utils.set_start_time(recording.timestamp)
    observer = create_input_observer(
        on_observed,
        observe_keyboard=True,
        observe_mouse=True,
        capture_mouse_moves=True,
    )
    started = False
    try:
        observer.start()
        started = True
        started_event.set()
        while not terminate_processing.wait(0.1):
            observer.check_health()
    except BaseException:
        terminate_processing.set()
        raise
    finally:
        if started:
            observer.stop()


def record_audio(
    recording: Recording,
    db_path: str,
    terminate_processing: multiprocessing.Event,
    started_event: multiprocessing.Event,
) -> None:
    """Record audio narration during the recording and store data in database.

    Privacy posture, enforced here rather than documented elsewhere:

    - The on-device transcription backend is resolved BEFORE the microphone is
      opened. If none is installed this refuses immediately, so a session is
      never captured that could not have been transcribed locally anyway.
    - Transcription is on-device only; the waveform is never uploaded.
    - The waveform is discarded after transcription unless
      ``RECORD_AUDIO_RETAIN_WAVEFORM`` is explicitly enabled. Only transcript
      text is retained by default.
    - The transcript is never logged. Spoken narration may contain identifying
      details and must not be copied into logs or terminal scrollback.

    Args:
        recording: The recording object.
        db_path: Path to the per-capture database file.
        terminate_processing: An event to signal the termination of the process.
        started_event: Event to set once started.
    """
    from openadapt_capture.audio import require_local_transcription_backend

    # Fail closed BEFORE anything else, and in particular before the microphone
    # is opened. Previously the stream was opened, the whole session was
    # captured, and only then did the missing backend surface -- recording the
    # operator for nothing and failing the capture at the end.
    backend = require_local_transcription_backend()

    utils.set_start_time(recording.timestamp)

    signal.signal(signal.SIGINT, signal.SIG_IGN)

    audio_frames = []  # to store audio frames

    import sounddevice

    def audio_callback(
        indata: np.ndarray, frames: int, time: Any, status: sounddevice.CallbackFlags
    ) -> None:
        """Callback function used when new audio frames are recorded.

        Note: time is of type cffi.FFI.CData, but since we don't use this argument
        and we also don't use the cffi library, the Any type annotation is used.
        """
        # called whenever there is new audio frames
        audio_frames.append(indata.copy())

    # open InputStream and start recording while ActionEvents are recorded
    audio_stream = sounddevice.InputStream(callback=audio_callback, samplerate=16000, channels=1)
    logger.info("Audio recording started.")
    start_timestamp = utils.get_timestamp()
    audio_stream.start()

    # NOTE: listener may not have actually started by now
    # TODO: handle race condition, e.g. by sending synthetic events from main thread
    started_event.set()

    terminate_processing.wait()
    audio_stream.stop()
    audio_stream.close()

    sample_rate = int(audio_stream.samplerate)

    if not audio_frames:
        # No frames arrive when the microphone is unavailable or the OS denied
        # permission. Record the empty result honestly instead of raising a
        # bare ValueError from np.concatenate and failing the whole capture.
        logger.warning(
            "No audio frames were captured; the microphone may be unavailable "
            "or permission may have been denied. Storing an empty transcript."
        )
        session = get_session_for_path(db_path)
        crud.insert_audio_info(session, b"", "", recording, start_timestamp, sample_rate, [])
        return

    # Concatenate into one Numpy array
    concatenated_audio = np.concatenate(audio_frames, axis=0)
    # convert concatenated_audio to format expected by whisper
    converted_audio = concatenated_audio.flatten().astype(np.float32)

    # Transcribe on this machine. The waveform is never uploaded.
    logger.info(f"Transcribing audio on-device with {backend}...")
    result_info = _transcribe_on_device(converted_audio, backend)
    # NOTE: the transcript is deliberately NOT logged. Narration can contain
    # names, dates of birth, and diagnoses; logging it would copy that into
    # terminal scrollback and any configured log sink.
    logger.info(
        "Transcription complete ({} characters).".format(len(result_info.get("text") or ""))
    )

    # empty word_list if the user didn't say anything
    word_list = []
    # segments could be empty
    if len(result_info["segments"]) > 0:
        # there won't be a 'words' list if the user didn't say anything
        if "words" in result_info["segments"][0]:
            word_list = result_info["segments"][0]["words"]

    if config.RECORD_AUDIO_RETAIN_WAVEFORM:
        # Explicitly opted in. The retained waveform is biometric identifying
        # data and has no sanitized derivative; it must stay inside the
        # capture's approved local boundary.
        logger.warning(
            "RECORD_AUDIO_RETAIN_WAVEFORM is enabled: the raw waveform is being "
            "retained in the capture database and cannot be sanitized for egress."
        )
        file_obj = io.BytesIO()
        soundfile.write(file_obj, converted_audio, sample_rate, format="FLAC")
        compressed_audio_bytes = file_obj.getvalue()
        file_obj.close()
    else:
        # Default: discard the waveform, keep only the transcript.
        compressed_audio_bytes = b""

    # Drop in-memory references to the waveform now that it is no longer needed.
    del converted_audio, concatenated_audio
    audio_frames.clear()

    session = get_session_for_path(db_path)
    # Create AudioInfo entry
    crud.insert_audio_info(
        session,
        compressed_audio_bytes,
        result_info["text"],
        recording,
        start_timestamp,
        sample_rate,
        word_list,
    )


def _transcribe_on_device(audio: "np.ndarray", backend: str) -> dict:
    """Transcribe a waveform using a local backend. Never uploads audio.

    Args:
        audio: Mono float32 waveform.
        backend: An on-device backend from ``LOCAL_TRANSCRIPTION_BACKENDS``.

    Returns:
        A dict with ``text`` and ``segments`` keys.
    """
    from openadapt_capture.audio import AudioRecorder

    # Reuse the audio module's backend implementations without opening a second
    # microphone stream.
    recorder = AudioRecorder.__new__(AudioRecorder)
    if backend == "faster-whisper":
        return recorder._transcribe_faster_whisper(audio, "base", True)
    return recorder._transcribe_openai_whisper(audio, "base", True)


@logger.catch(reraise=True)
@utils.trace(logger)
def record(
    task_description: str,
    capture_dir: str = None,
    # these should be Event | None, but this raises:
    #   TypeError: unsupported operand type(s) for |: 'method' and 'NoneType'
    # type(multiprocessing.Event) appears to be <class 'method'>
    # TODO: fix this
    terminate_processing: multiprocessing.Event = None,
    terminate_recording: multiprocessing.Event = None,
    status_pipe: multiprocessing.connection.Connection | None = None,
    log_memory: bool = config.LOG_MEMORY,
    # Optional shared counters — if None, record() creates its own.
    # Pass externally-created Values to read counts from outside (e.g. Recorder).
    num_action_events: multiprocessing.Value = None,
    num_screen_events: multiprocessing.Value = None,
    num_window_events: multiprocessing.Value = None,
    num_browser_events: multiprocessing.Value = None,
    num_video_events: multiprocessing.Value = None,
    send_profile: bool = False,
    window_owner: str | None = None,
    window_title: str | None = None,
    structural_observer: StructuralObserver | None = None,
) -> None:
    """Record native screenshots, action events, and window events.

    Args:
        task_description: A text description of the task to be recorded.
        terminate_processing: An event to signal the termination of the events
        processing.
        terminate_recording: An event to signal the termination of the recording.
        status_pipe: A connection to communicate recording status.
        log_memory: Whether to log memory usage.
        window_owner: Owner-app substring for window-scoped capture (record ONE
            window in its own pixel space). Falls back to
            ``config.RECORD_WINDOW_OWNER``.
        window_title: Title substring for window-scoped capture. Falls back to
            ``config.RECORD_WINDOW_TITLE``.
        structural_observer: Optional injected accessibility observer. When
            omitted, the platform factory follows
            ``RECORD_STRUCTURAL_OBSERVATIONS``.
    """
    if config.RECORD_BROWSER_EVENTS:
        # Fail before encoder checks, display access, database creation, or any
        # listener bind. The source-only extension bridge has no governed
        # replay, authentication, or source-time secret exclusion contract.
        raise RuntimeError(BROWSER_RECORDING_GUIDANCE)

    assert config.RECORD_VIDEO or config.RECORD_IMAGES, (
        config.RECORD_VIDEO,
        config.RECORD_IMAGES,
    )
    # Refuse before touching the display or starting any worker. Capture never
    # downloads or bundles FFmpeg; Desktop/standalone provisioning owns it.
    video_provision = None
    if config.RECORD_VIDEO:
        video_provision = video.require_video_encoder(
            ffmpeg_path=config.VIDEO_FFMPEG_PATH,
            ffprobe_path=config.VIDEO_FFPROBE_PATH,
            codec=config.VIDEO_ENCODING,
            pixel_format=config.VIDEO_PIXEL_FORMAT,
            muxer=config.VIDEO_MUXER,
        )

    # Configure loguru level for recording (without destroying global config)
    logger.configure(handlers=[{"sink": sys.stderr, "level": LOG_LEVEL}])

    # logically it makes sense to communicate from here, but when running
    # from the tray it takes too long
    # TODO: fix this
    # if status_pipe:
    #    status_pipe.send({"type": "record.starting"})

    _profile_start = time.perf_counter()
    _profile_is_main_thread = threading.current_thread() is threading.main_thread()

    logger.info(f"{task_description=}")

    # Window-scoped capture: resolve + capture the target window ONCE, up
    # front, before any pipeline task starts. Fails loud if the window is
    # missing or capture is not permitted (a recording that silently fell
    # back to full-screen would be in the wrong coordinate space).
    window_scope = build_window_scope(
        window_owner or config.RECORD_WINDOW_OWNER,
        window_title or config.RECORD_WINDOW_TITLE,
    )
    initial_window_frame = None
    desktop_scope = None
    if window_scope is not None:
        initial_window_frame, _ = window_scope.capture_frame()
        logger.info(
            f"window-scoped capture resolved: {window_scope.snapshot()} "
            f"initial frame {initial_window_frame.size}"
        )
    else:
        # MSS monitor zero is the exact combined frame read by
        # ``utils.take_screenshot``. Retain its origin and translate native
        # input into that same pixel space so secondary monitors with negative
        # global coordinates remain aligned with the video.
        desktop_scope = DesktopCaptureScope.current()
        logger.info(f"virtual desktop capture resolved: {desktop_scope.snapshot()}")

    if structural_observer is None:
        structural_observer = create_structural_observer(
            enabled=config.RECORD_STRUCTURAL_OBSERVATIONS,
        )

    if capture_dir is None:
        capture_dir = os.path.join(os.getcwd(), "capture")
    recording, db_path = create_recording(
        task_description,
        capture_dir,
        window_capture_info=(window_scope.snapshot() if window_scope is not None else None),
        desktop_capture_info=(desktop_scope.snapshot() if desktop_scope is not None else None),
    )
    recording_timestamp = recording.timestamp

    event_q = queue.Queue()
    screen_write_q = sq.SynchronizedQueue()
    action_write_q = sq.SynchronizedQueue()
    window_write_q = sq.SynchronizedQueue()
    browser_write_q = sq.SynchronizedQueue()
    video_write_q = sq.SynchronizedQueue()
    terminate_writers = multiprocessing.Event()
    # TODO: save write times to DB; display performance plot in visualize.py
    perf_q = sq.SynchronizedQueue()
    if terminate_processing is None:
        terminate_processing = multiprocessing.Event()
    task_by_name = {}
    task_started_events = {}
    task_errors: queue.Queue = queue.Queue()
    _screen_timing = _ScreenTimingStats()  # running stats, no unbounded list

    # In window-scoped mode the screen reader emits the target window's
    # bounds timeline itself; the active-window poller would record a
    # DIFFERENT window (whichever is focused), so it stays off.
    if config.RECORD_WINDOW_DATA and window_scope is None:
        window_event_reader = threading.Thread(
            target=_run_task_fail_loud,
            daemon=True,
            args=(
                "window_event_reader",
                read_window_events,
                (
                    event_q,
                    terminate_processing,
                    recording,
                    task_started_events.setdefault("window_event_reader", threading.Event()),
                ),
                terminate_processing,
                task_errors,
            ),
        )
        window_event_reader.start()
        task_by_name["window_event_reader"] = window_event_reader

    screen_event_reader = threading.Thread(
        target=_run_task_fail_loud,
        daemon=True,
        args=(
            "screen_event_reader",
            read_screen_events,
            (
                event_q,
                terminate_processing,
                recording,
                task_started_events.setdefault("screen_event_reader", threading.Event()),
                _screen_timing,
                window_scope,
                desktop_scope,
            ),
            terminate_processing,
            task_errors,
        ),
    )
    screen_event_reader.start()
    task_by_name["screen_event_reader"] = screen_event_reader

    input_reader_args = (
        event_q,
        terminate_processing,
        recording,
        task_started_events.setdefault("input_event_reader", threading.Event()),
        window_scope or desktop_scope,
        structural_observer,
    )
    input_event_reader = threading.Thread(
        target=_run_task_fail_loud,
        daemon=True,
        args=(
            "input_event_reader",
            read_input_events,
            input_reader_args,
            terminate_processing,
            task_errors,
        ),
    )
    input_event_reader.start()
    task_by_name["input_event_reader"] = input_event_reader

    if num_action_events is None:
        num_action_events = multiprocessing.Value("i", 0)
    if num_screen_events is None:
        num_screen_events = multiprocessing.Value("i", 0)
    if num_window_events is None:
        num_window_events = multiprocessing.Value("i", 0)
    if num_browser_events is None:
        num_browser_events = multiprocessing.Value("i", 0)
    if num_video_events is None:
        num_video_events = multiprocessing.Value("i", 0)

    event_processor_args = (
        event_q,
        screen_write_q,
        action_write_q,
        window_write_q,
        browser_write_q,
        video_write_q,
        perf_q,
        recording,
        terminate_processing,
        task_started_events.setdefault("event_processor", threading.Event()),
        num_screen_events,
        num_action_events,
        num_window_events,
        num_browser_events,
        num_video_events,
    )
    event_processor = threading.Thread(
        target=_run_task_fail_loud,
        daemon=True,
        args=(
            "event_processor",
            process_events,
            event_processor_args,
            terminate_processing,
            task_errors,
        ),
    )
    event_processor.start()
    task_by_name["event_processor"] = event_processor

    screen_event_writer = multiprocessing.Process(
        target=utils.WrapStdout(write_events),
        args=(
            "screen",
            write_screen_event,
            screen_write_q,
            num_screen_events,
            perf_q,
            recording,
            db_path,
            terminate_writers,
            task_started_events.setdefault("screen_event_writer", multiprocessing.Event()),
        ),
    )
    screen_event_writer.start()
    task_by_name["screen_event_writer"] = screen_event_writer

    action_event_writer = multiprocessing.Process(
        target=utils.WrapStdout(write_events),
        args=(
            "action",
            write_action_event,
            action_write_q,
            num_action_events,
            perf_q,
            recording,
            db_path,
            terminate_writers,
            task_started_events.setdefault("action_event_writer", multiprocessing.Event()),
        ),
    )
    action_event_writer.start()
    task_by_name["action_event_writer"] = action_event_writer

    if config.RECORD_WINDOW_DATA or window_scope is not None:
        window_event_writer = multiprocessing.Process(
            target=utils.WrapStdout(write_events),
            args=(
                "window",
                write_window_event,
                window_write_q,
                num_window_events,
                perf_q,
                recording,
                db_path,
                terminate_writers,
                task_started_events.setdefault("window_event_writer", multiprocessing.Event()),
            ),
        )
        window_event_writer.start()
        task_by_name["window_event_writer"] = window_event_writer

    if config.RECORD_VIDEO:
        video_writer = multiprocessing.Process(
            target=utils.WrapStdout(write_events),
            args=(
                "screen/video",
                write_video_event,
                video_write_q,
                num_video_events,
                perf_q,
                recording,
                db_path,
                terminate_writers,
                task_started_events.setdefault("video_writer", multiprocessing.Event()),
                partial(
                    video_pre_callback,
                    video_dir=capture_dir,
                    # Window-scoped frames are the window's pixels, not the
                    # monitor's: size the stream from the initial frame.
                    frame_size=(
                        initial_window_frame.size if initial_window_frame is not None else None
                    ),
                    provision=video_provision,
                    timeout_seconds=config.VIDEO_FFMPEG_TIMEOUT_SECONDS,
                ),
                video_post_callback,
            ),
        )
        video_writer.start()
        task_by_name["video_writer"] = video_writer

    if config.RECORD_AUDIO:
        audio_recorder = multiprocessing.Process(
            target=utils.WrapStdout(record_audio),
            args=(
                recording,
                db_path,
                terminate_processing,
                task_started_events.setdefault("audio_event_writer", multiprocessing.Event()),
            ),
        )
        audio_recorder.start()
        task_by_name["audio_recorder"] = audio_recorder

    terminate_perf_event = multiprocessing.Event()
    perf_stats_writer = multiprocessing.Process(
        target=utils.WrapStdout(performance_stats_writer),
        args=(
            perf_q,
            recording,
            db_path,
            terminate_perf_event,
            task_started_events.setdefault("perf_stats_writer", multiprocessing.Event()),
        ),
    )
    perf_stats_writer.start()
    task_by_name["perf_stats_writer"] = perf_stats_writer

    if config.PLOT_PERFORMANCE:
        record_pid = os.getpid()
        mem_writer = multiprocessing.Process(
            target=utils.WrapStdout(memory_writer),
            args=(
                recording,
                db_path,
                terminate_perf_event,
                record_pid,
                task_started_events.setdefault("mem_writer", multiprocessing.Event()),
            ),
        )
        mem_writer.start()
        task_by_name["mem_writer"] = mem_writer

    if log_memory:
        performance_snapshots = []
        _tracker = tracker.SummaryTracker()
        tracemalloc.start()
        collect_stats(performance_snapshots)

    # TODO: discard events until everything is ready

    global stop_sequence_detected
    stop_sequence_detected = False
    startup_ready = _wait_for_tasks_started(
        task_by_name,
        task_started_events,
        terminate_processing,
    )
    if startup_ready:
        for _ in range(5):
            logger.info("*" * 40)
        logger.info("All readers and writers have started. Waiting for input events...")

        if status_pipe:
            status_pipe.send({"type": "record.started"})

        try:
            while not (stop_sequence_detected or terminate_processing.is_set()):
                terminate_processing.wait(1)
        except KeyboardInterrupt:
            terminate_processing.set()
    else:
        logger.info("Tearing down recording after incomplete startup")
    terminate_processing.set()

    if status_pipe:
        status_pipe.send({"type": "record.stopping"})

    if log_memory:
        collect_stats(performance_snapshots)
        log_memory_usage(_tracker, performance_snapshots)

    pre_ready_timeout = None if startup_ready else PRE_READY_TASK_JOIN_TIMEOUT_SECONDS
    _join_tasks(
        task_by_name,
        [
            "window_event_reader",
            "screen_event_reader",
            "input_event_reader",
            "event_processor",
            "audio_recorder",
        ],
        timeout=pre_ready_timeout,
    )

    # No writer can stop while the event processor can still enqueue work.
    # Signal writer completion only after all producers have exited.
    terminate_writers.set()
    _join_tasks(
        task_by_name,
        [
            "screen_event_writer",
            "action_event_writer",
            "window_event_writer",
            "video_writer",
        ],
        timeout=pre_ready_timeout,
    )

    terminate_perf_event.set()
    _join_tasks(
        task_by_name,
        [
            "perf_stats_writer",
            "mem_writer",
        ],
        timeout=pre_ready_timeout,
    )

    if not task_errors.empty():
        task_name, task_error = task_errors.get_nowait()
        add_exception_note(task_error, f"recording task {task_name!r} failed")
        raise task_error
    _raise_for_failed_processes(task_by_name)
    if desktop_scope is not None:
        # Close the interval between the last captured frame and operator stop.
        # A topology change in that interval still invalidates the session.
        desktop_scope.assert_current(force=True)

    if config.PLOT_PERFORMANCE and startup_ready:
        from openadapt_capture import plotting

        session = get_session_for_path(db_path)
        plotting.plot_performance(
            session,
            recording,
            save_dir=capture_dir,
        )

    logger.info(f"Saved {recording_timestamp=}")

    session = get_session_for_path(db_path)
    crud.post_process_events(session, recording)

    # --- Profiling summary ---
    _profile_duration = time.perf_counter() - _profile_start
    _profile_data = {
        "duration_seconds": round(_profile_duration, 2),
        "main_thread": _profile_is_main_thread,
        "platform": sys.platform,
        "python_version": sys.version,
        "threads_started": list(task_by_name.keys()),
        "thread_count": threading.active_count(),
        "event_counts": {
            "action": num_action_events.value,
            "screen": num_screen_events.value,
            "window": num_window_events.value,
            "browser": num_browser_events.value,
            "video": num_video_events.value,
        },
        "screen_timing": {},
        "config": {
            "RECORD_VIDEO": config.RECORD_VIDEO,
            "RECORD_AUDIO": config.RECORD_AUDIO,
            "RECORD_IMAGES": config.RECORD_IMAGES,
            "RECORD_WINDOW_DATA": config.RECORD_WINDOW_DATA,
            "RECORD_WINDOW_OWNER": window_owner or config.RECORD_WINDOW_OWNER,
            "RECORD_WINDOW_TITLE": window_title or config.RECORD_WINDOW_TITLE,
            "RECORD_BROWSER_EVENTS": config.RECORD_BROWSER_EVENTS,
            "RECORD_FULL_VIDEO": config.RECORD_FULL_VIDEO,
            "PLOT_PERFORMANCE": config.PLOT_PERFORMANCE,
            "SCREEN_CAPTURE_FPS": config.SCREEN_CAPTURE_FPS,
        },
        "capture_dir": capture_dir,
    }
    # Compute screen timing stats
    if _screen_timing:
        _profile_data["screen_timing"] = _screen_timing.to_dict()

    _profile_path = os.path.join(capture_dir, "profiling.json")
    try:
        import json as _json

        with open(_profile_path, "w") as _f:
            _json.dump(_profile_data, _f, indent=2)
        logger.info(f"Profiling saved to {_profile_path}")

        # Print compact summary
        print("\n=== Recording Profile ===")
        print(f"Duration: {_profile_duration:.1f}s")
        print(f"Main thread: {_profile_is_main_thread}")
        print(f"Threads started: {len(task_by_name)}")
        for k, v in _profile_data["event_counts"].items():
            rate = v / _profile_duration if _profile_duration > 0 else 0
            print(f"  {k}: {v} events ({rate:.1f}/s)")
        if _screen_timing:
            st = _profile_data["screen_timing"]
            print(
                f"  screenshot: avg={st['screenshot_avg_ms']}ms "
                f"max={st['screenshot_max_ms']}ms "
                f"min={st['screenshot_min_ms']}ms"
            )
        print(
            f"Config: WINDOW_DATA={config.RECORD_WINDOW_DATA} "
            f"VIDEO={config.RECORD_VIDEO} "
            f"PLOT_PERF={config.PLOT_PERFORMANCE} "
            f"FPS={config.SCREEN_CAPTURE_FPS}"
        )
        print("=========================\n")

        # Auto-send profiling via wormhole if requested
        if send_profile:
            _send_profiling_via_wormhole(_profile_path)
    except Exception as exc:
        logger.warning(f"Profiling save/send failed: {exc}")

    if terminate_recording is not None:
        terminate_recording.set()

    # TODO: consolidate terminate_recording and status_pipe
    if status_pipe:
        status_pipe.send({"type": "record.stopped"})


class Recorder:
    """High-level recording interface.

    Wraps the legacy ``record()`` function with a clean Python API:

    - Constructor parameters override config defaults (``capture_video``, etc.)
    - Runtime introspection (``event_count``, ``is_recording``)
    - Post-recording access to ``CaptureSession``

    Usage::

        with Recorder('./my_capture', task_description='Demo task',
                       capture_video=True, capture_audio=False) as recorder:
            recorder.wait_for_ready()
            input('Press Enter to stop recording...')
        print(f"Recorded {recorder.event_count} events")

    Window-scoped recording (record ONE window in its own pixel space —
    frames captured from that window, input coordinates translated into the
    captured frame's pixels; see ``window_capture.py``)::

        with Recorder('./my_capture', task_description='Citrix demo',
                       window={'owner': 'Parallels', 'title': None}) as recorder:
            ...
    """

    def __init__(
        self,
        capture_dir: str,
        task_description: str = "",
        *,
        capture_video: bool | None = None,
        capture_audio: bool | None = None,
        capture_images: bool | None = None,
        capture_window_data: bool | None = None,
        capture_structural_observations: bool | None = None,
        capture_browser_events: bool | None = None,
        capture_full_video: bool | None = None,
        video_encoding: str | None = None,
        video_pixel_format: str | None = None,
        video_muxer: str | None = None,
        ffmpeg_path: str | None = None,
        ffprobe_path: str | None = None,
        ffmpeg_timeout_seconds: float | None = None,
        stop_sequences: list[list[str]] | None = None,
        log_memory: bool | None = None,
        plot_performance: bool | None = None,
        screen_capture_fps: float | None = None,
        send_profile: bool = False,
        window: dict | None = None,
        structural_observer: StructuralObserver | None = None,
        control_enabled: bool = True,
        control_runtime_dir: str | None = None,
    ) -> None:
        from pathlib import Path

        from openadapt_capture.config import RecordingConfig
        from openadapt_capture.window_capture import WindowTarget

        self.capture_dir = str(Path(capture_dir).resolve())
        self.task_description = task_description
        self._send_profile = send_profile
        self._control_enabled = control_enabled
        self._control_runtime_dir = control_runtime_dir

        if capture_browser_events:
            # Preserve a clear error for callers of the former keyword while
            # preventing the unsupported server from reaching record().
            raise ValueError(BROWSER_RECORDING_GUIDANCE)

        # Validate the window spec up front (loud, before any thread starts).
        window_target = WindowTarget.from_spec(window)

        # Build recording config from constructor params
        self._recording_config = RecordingConfig(
            capture_video=capture_video,
            capture_audio=capture_audio,
            capture_images=capture_images,
            capture_window_data=capture_window_data,
            capture_structural_observations=capture_structural_observations,
            capture_browser_events=capture_browser_events,
            capture_full_video=capture_full_video,
            video_encoding=video_encoding,
            video_pixel_format=video_pixel_format,
            video_muxer=video_muxer,
            ffmpeg_path=ffmpeg_path,
            ffprobe_path=ffprobe_path,
            ffmpeg_timeout_seconds=ffmpeg_timeout_seconds,
            stop_sequences=stop_sequences,
            log_memory=log_memory,
            plot_performance=plot_performance,
            screen_capture_fps=screen_capture_fps,
            window_owner=window_target.owner if window_target else None,
            window_title=window_target.title if window_target else None,
        )

        # Shared state for cross-thread communication
        self._terminate_processing = multiprocessing.Event()
        self._terminate_recording = multiprocessing.Event()
        self._num_action_events = multiprocessing.Value("i", 0)
        self._num_screen_events = multiprocessing.Value("i", 0)
        self._num_window_events = multiprocessing.Value("i", 0)
        self._num_browser_events = multiprocessing.Value("i", 0)
        self._num_video_events = multiprocessing.Value("i", 0)

        # Status communication
        self._status_recv, self._status_send = multiprocessing.Pipe(duplex=False)
        self._ready_event = threading.Event()
        self._stopped_event = threading.Event()
        self._ready_or_stopped_event = threading.Event()
        self._finalized_event = threading.Event()

        # Internal
        self._record_thread: threading.Thread | None = None
        self._status_thread: threading.Thread | None = None
        self._capture = None  # lazy CaptureSession
        self._worker_error: BaseException | None = None
        self._worker_error_lock = threading.Lock()
        self._structural_observer = structural_observer
        self._control_server = None
        self._control_state_lock = threading.RLock()
        self._control_stop_lock = threading.Lock()
        self._control_session_id = str(uuid.uuid4())
        self._process_started_at = psutil.Process(os.getpid()).create_time()
        self._control_phase = "starting"
        self._control_complete = False
        self._control_integrity_verified = False
        self._control_error_code: str | None = None
        self._control_started_at = time.time()
        self._control_finalized_at: float | None = None

    def _control_payload(self) -> dict[str, Any]:
        """Return the secret-free state shared with the client and state file."""
        with self._control_state_lock:
            return {
                "schema_version": "openadapt.capture-terminal.v1",
                "session_id": self._control_session_id,
                "pid": os.getpid(),
                "process_started_at": self._process_started_at,
                "capture_dir": self.capture_dir,
                "phase": self._control_phase,
                "ready": self._ready_event.is_set(),
                "complete": self._control_complete,
                "integrity_verified": self._control_integrity_verified,
                "error_code": self._control_error_code,
                "started_at": self._control_started_at,
                "finalized_at": self._control_finalized_at,
                "event_counts": {
                    "action": self._num_action_events.value,
                    "screen": self._num_screen_events.value,
                    "window": self._num_window_events.value,
                    "browser": self._num_browser_events.value,
                    "video": self._num_video_events.value,
                },
            }

    def _persist_control_state(self) -> None:
        if not self._control_enabled:
            return
        from openadapt_capture.control import write_terminal_state

        terminal = self._control_payload()
        # The file location already binds the capture. Do not retain an
        # absolute local path (which can disclose a user/profile name) in an
        # artifact that may later enter sanitization and review.
        terminal.pop("capture_dir", None)
        write_terminal_state(self.capture_dir, terminal)

    def _transition_control(
        self,
        phase: str,
        *,
        complete: bool = False,
        integrity_verified: bool = False,
        error_code: str | None = None,
        finalized: bool = False,
    ) -> None:
        with self._control_state_lock:
            if self._control_phase in {"complete", "failed", "crashed"}:
                return
            previous = (
                self._control_phase,
                self._control_complete,
                self._control_integrity_verified,
                self._control_error_code,
                self._control_finalized_at,
            )
            if (
                self._control_error_code == "finalization_timeout"
                and error_code is None
                and phase not in {"complete", "failed", "crashed"}
            ):
                error_code = self._control_error_code
            self._control_phase = phase
            self._control_complete = complete
            self._control_integrity_verified = integrity_verified
            self._control_error_code = error_code
            if finalized:
                self._control_finalized_at = time.time()
            try:
                self._persist_control_state()
            except BaseException:
                (
                    self._control_phase,
                    self._control_complete,
                    self._control_integrity_verified,
                    self._control_error_code,
                    self._control_finalized_at,
                ) = previous
                raise

    def _set_worker_error(self, exc: BaseException) -> None:
        with self._worker_error_lock:
            if self._worker_error is None:
                self._worker_error = exc

    def _verify_completed_capture(self) -> None:
        """Verify the finalized database before control reports completion."""
        from pathlib import Path

        from openadapt_capture.capture import (
            CaptureSession,
            _convert_action_event,
            _convert_browser_event,
        )

        db_path = Path(self.capture_dir) / "recording.db"
        details = db_path.lstat()
        if not db_path.is_file() or db_path.is_symlink():
            raise RuntimeError("The finalized Capture database is not a regular file.")
        if details.st_size <= 0:
            raise RuntimeError("The finalized Capture database is empty.")
        database = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            quick_check = database.execute("PRAGMA quick_check").fetchall()
            if quick_check != [("ok",)]:
                raise RuntimeError("The finalized Capture database failed integrity check.")
            foreign_key_errors = database.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_key_errors:
                raise RuntimeError("The finalized Capture database has broken relationships.")
            recordings = database.execute("SELECT id FROM recording").fetchall()
            if len(recordings) != 1:
                raise RuntimeError(
                    "The finalized Capture database does not contain one session."
                )
            recording_id = recordings[0][0]
            expected_counts = {
                "action_event": self._num_action_events.value,
                "screenshot": self._num_screen_events.value,
                "window_event": self._num_window_events.value,
                "browser_event": self._num_browser_events.value,
            }
            for table, expected in expected_counts.items():
                committed = database.execute(
                    f"SELECT COUNT(*) FROM {table}",
                ).fetchone()[0]
                if committed != expected:
                    raise RuntimeError(
                        f"The finalized Capture database lost {table} events "
                        f"(expected {expected}, committed {committed})."
                    )
                wrong_recording = database.execute(
                    f"SELECT COUNT(*) FROM {table} "
                    "WHERE recording_id IS NULL OR recording_id != ?",
                    (recording_id,),
                ).fetchone()[0]
                if wrong_recording:
                    raise RuntimeError(
                        f"The finalized Capture database has unbound {table} events."
                    )
            actions_without_screenshots = database.execute(
                "SELECT COUNT(*) FROM action_event "
                "WHERE recording_id = ? AND screenshot_id IS NULL",
                (recording_id,),
            ).fetchone()[0]
            if actions_without_screenshots:
                raise RuntimeError(
                    "The finalized Capture database has actions without screenshots."
                )
        finally:
            database.close()
        capture = CaptureSession.load(self.capture_dir)
        try:
            for event in capture._recording.action_events:
                _convert_action_event(event)
            for event in capture._recording.browser_events:
                if _convert_browser_event(event) is None:
                    raise RuntimeError(
                        "The finalized Capture database has an invalid browser event."
                    )
        finally:
            capture.close()

    def _start_control_server(self) -> None:
        from pathlib import Path

        from openadapt_capture.control import RecorderControlServer

        Path(self.capture_dir).mkdir(parents=True, exist_ok=True)
        self._persist_control_state()
        server = RecorderControlServer(
            capture_dir=self.capture_dir,
            snapshot=self._control_payload,
            stop=self._control_stop,
            session_id=self._control_session_id,
            runtime_dir=self._control_runtime_dir,
        )
        self._control_server = server.start()

    def _control_stop(self, timeout: float) -> dict[str, Any]:
        """Idempotently request stop and wait for the one finalization result."""
        with self._control_stop_lock:
            if (
                not self._finalized_event.is_set()
                and not self._terminate_processing.is_set()
            ):
                try:
                    self._transition_control("stopping")
                except BaseException as exc:
                    self._set_worker_error(exc)
                    self._terminate_processing.set()
                    return {
                        **self._control_payload(),
                        "error_code": "terminal_metadata_failed",
                    }
                self._terminate_processing.set()
        if not self._finalized_event.wait(timeout=timeout):
            try:
                self._transition_control(
                    "finalizing",
                    error_code="finalization_timeout",
                )
            except BaseException as exc:
                self._set_worker_error(exc)
            return {
                **self._control_payload(),
                "phase": "finalizing",
                "complete": False,
                "integrity_verified": False,
                "error_code": "finalization_timeout",
            }
        return self._control_payload()

    def _drain_status_pipe(self) -> None:
        """Background thread that reads status messages from record()."""
        try:
            while not self._stopped_event.is_set():
                if self._status_recv.poll(timeout=0.5):
                    msg = self._status_recv.recv()
                    if isinstance(msg, dict):
                        if msg.get("type") == "record.started":
                            self._ready_event.set()
                            self._ready_or_stopped_event.set()
                            self._transition_control("recording")
                        elif msg.get("type") == "record.stopping":
                            self._transition_control("finalizing")
                        elif msg.get("type") == "record.stopped":
                            self._stopped_event.set()
                            self._ready_or_stopped_event.set()
        except (EOFError, OSError):
            pass
        except BaseException as exc:
            self._set_worker_error(exc)
            self._terminate_processing.set()
            self._ready_or_stopped_event.set()

    def _run_record(self) -> None:
        """Thread target: apply config overrides, then call record()."""
        from openadapt_capture.config import config_override

        try:
            with config_override(self._recording_config):
                record(
                    task_description=self.task_description,
                    capture_dir=self.capture_dir,
                    terminate_processing=self._terminate_processing,
                    terminate_recording=self._terminate_recording,
                    status_pipe=self._status_send,
                    num_action_events=self._num_action_events,
                    num_screen_events=self._num_screen_events,
                    num_window_events=self._num_window_events,
                    num_browser_events=self._num_browser_events,
                    num_video_events=self._num_video_events,
                    send_profile=self._send_profile,
                    structural_observer=self._structural_observer,
                )
            self.check_health()
            if self._ready_event.is_set():
                self._verify_completed_capture()
                self._transition_control(
                    "complete",
                    complete=True,
                    integrity_verified=True,
                    finalized=True,
                )
            else:
                self._transition_control(
                    "failed",
                    error_code="startup_incomplete",
                    finalized=True,
                )
        except BaseException as exc:
            # A setup exception must wake wait_for_ready() and let context-manager
            # teardown finish instead of leaving callers blocked for its timeout.
            self._set_worker_error(exc)
            self._terminate_processing.set()
            try:
                self._transition_control(
                    "failed",
                    error_code="recording_or_finalization_failed",
                    finalized=True,
                )
            except BaseException as state_exc:
                add_exception_note(
                    exc,
                    f"terminal state persistence also failed: {state_exc!r}",
                )
            try:
                self._status_send.send({"type": "record.stopped"})
            except (BrokenPipeError, EOFError, OSError):
                self._stopped_event.set()
                self._ready_or_stopped_event.set()
        finally:
            self._stopped_event.set()
            self._ready_or_stopped_event.set()
            self._finalized_event.set()

    def __enter__(self) -> "Recorder":
        if self._control_enabled:
            try:
                self._start_control_server()
            except BaseException:
                try:
                    self._transition_control(
                        "failed",
                        error_code="control_channel_failed",
                        finalized=True,
                    )
                except BaseException:
                    pass
                raise

        # Start status drain thread
        self._status_thread = threading.Thread(
            target=self._drain_status_pipe,
            daemon=True,
        )
        self._record_thread = threading.Thread(target=self._run_record)
        try:
            self._status_thread.start()
            self._record_thread.start()
        except BaseException as exc:
            self._terminate_processing.set()
            self._stopped_event.set()
            self._ready_or_stopped_event.set()
            self._finalized_event.set()
            try:
                self._transition_control(
                    "failed",
                    error_code="recorder_thread_start_failed",
                    finalized=True,
                )
            except BaseException as state_exc:
                add_exception_note(
                    exc,
                    f"terminal state persistence also failed: {state_exc!r}",
                )
            if self._control_server is not None:
                self._control_server.close()
            raise
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if not self._finalized_event.is_set():
            try:
                self._transition_control("stopping")
            except BaseException as exc:
                self._set_worker_error(exc)
        self._terminate_processing.set()
        if self._record_thread is not None:
            self._record_thread.join()
        self._stopped_event.set()  # ensure status thread exits
        if self._status_thread is not None:
            self._status_thread.join(timeout=5)
        if self._control_server is not None:
            self._control_server.close()
        if self._worker_error is not None:
            if exc_val is not None:
                add_exception_note(
                    exc_val, f"the recorder worker also failed: {self._worker_error!r}"
                )
            else:
                raise self._worker_error

    def stop(self) -> None:
        """Stop, join, and surface any recording-worker failure."""
        if not self._finalized_event.is_set():
            try:
                self._transition_control("stopping")
            except BaseException as exc:
                self._set_worker_error(exc)
        self._terminate_processing.set()
        if self._record_thread is not None:
            self._record_thread.join()
        try:
            self.check_health()
        finally:
            if self._control_server is not None:
                self._control_server.close()

    def check_health(self) -> None:
        """Raise the first recording-worker error observed by the owner thread."""
        with self._worker_error_lock:
            worker_error = self._worker_error
        if worker_error is not None:
            raise worker_error

    def wait_for_ready(self, timeout: float = 60) -> bool:
        """Block until all recording threads/processes have started.

        Returns True if ready, False if startup stopped or the timeout expired.
        """
        self._ready_or_stopped_event.wait(timeout=timeout)
        self.check_health()
        return self._ready_event.is_set()

    @property
    def is_recording(self) -> bool:
        """Whether recording is currently active."""
        return (
            self._record_thread is not None
            and self._record_thread.is_alive()
            and not self._terminate_processing.is_set()
        )

    @property
    def event_count(self) -> int:
        """Number of action events recorded so far (or total after stop)."""
        return self._num_action_events.value

    @property
    def screen_count(self) -> int:
        """Number of screen events recorded."""
        return self._num_screen_events.value

    @property
    def video_frame_count(self) -> int:
        """Number of video frames written."""
        return self._num_video_events.value

    @property
    def stats(self) -> dict:
        """Recording statistics snapshot."""
        return {
            "action_events": self._num_action_events.value,
            "screen_events": self._num_screen_events.value,
            "window_events": self._num_window_events.value,
            "browser_events": self._num_browser_events.value,
            "video_frames": self._num_video_events.value,
            "is_recording": self.is_recording,
        }

    @property
    def control_session_id(self) -> str:
        """Stable identifier used by authenticated cross-process clients."""
        return self._control_session_id

    @property
    def capture(self):
        """Load the CaptureSession after recording completes.

        Returns None if recording has not finished yet.
        """
        self.check_health()
        if self._capture is None and not self.is_recording:
            try:
                from openadapt_capture.capture import CaptureSession

                self._capture = CaptureSession.load(self.capture_dir)
            except FileNotFoundError:
                return None
        return self._capture


# Entry point
def start() -> None:
    """Starts the recording process."""
    fire.Fire(record)


if __name__ == "__main__":
    fire.Fire(record)
