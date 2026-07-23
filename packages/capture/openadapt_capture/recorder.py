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
import json
import multiprocessing
import os
import queue
import signal
import sys
import threading
import time
import tracemalloc
from collections import namedtuple
from functools import partial
from typing import Any, Callable

import av
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
from openadapt_capture.window_capture import (
    WindowCaptureError,
    WindowCaptureScope,
    build_window_scope,
)

try:
    import soundfile
    import websockets.sync.server
except ImportError:
    soundfile = None
    websockets = None

def set_browser_mode(
    mode: str, websocket: "websockets.sync.server.ServerConnection"
) -> None:
    """Send a message to the browser extension to set the mode."""
    logger.info(f"{type(websocket)=}")
    VALID_MODES = ("idle", "record", "replay")
    assert mode in VALID_MODES, f"{mode=} not in {VALID_MODES=}"
    message = json.dumps({"type": "SET_MODE", "mode": mode})
    logger.info(f"sending {message=}")
    websocket.send(message)


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
ws_server_instance = None


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

        waiting_for = [
            name
            for name, event in task_started_events.items()
            if not event.is_set()
        ]
        if not waiting_for:
            return True

        stopped_before_ready = [
            name
            for name in waiting_for
            if name in task_by_name and not task_by_name[name].is_alive()
        ]
        if stopped_before_ready:
            logger.error(
                "Recording tasks exited before readiness: "
                f"{stopped_before_ready}"
            )
            terminate_processing.set()
            return False

        logger.info(f"Waiting for tasks to start: {waiting_for}")
        logger.info(
            f"Started tasks: {expected_starts - len(waiting_for)}/{expected_starts}"
        )
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
            logger.warning(
                f"terminating {task_name!r} after pre-ready shutdown timeout"
            )
            task.terminate()
            task.join(timeout=0.5)

        if task.is_alive():
            lingering.append(task_name)

    if lingering:
        logger.warning(f"tasks still exiting after bounded shutdown: {lingering}")
    return lingering


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
) -> dict[str, Any]:
    """Function to call before main loop.

    Args:
        db: The database session.
        recording: The recording object.
        video_dir: Directory for video files.
        frame_size: Explicit (width, height) for the video stream. Used by
            window-scoped capture, whose frames are the target window's pixels
            rather than the monitor's. Defaults to the full-screen size.

    Returns:
        dict[str, Any]: The updated state.
    """
    video_file_path = video.get_video_file_path(recording.timestamp, video_dir)
    if frame_size is not None:
        monitor_width, monitor_height = frame_size
    else:
        # TODO XXX replace with utils.get_monitor_dims() once fixed
        monitor_width, monitor_height = utils.take_screenshot().size
    video_container, video_stream, video_start_timestamp = (
        video.initialize_video_writer(video_file_path, monitor_width, monitor_height)
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
    video_container: av.container.OutputContainer,
    video_stream: av.stream.Stream,
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
        video_container (av.container.OutputContainer): The output container to which
            the frame is written.
        video_stream (av.stream.Stream): The video stream within the container.
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
        # A frame whose size differs from the stream (e.g. the target window
        # of a window-scoped recording was resized mid-recording) cannot be
        # encoded into this stream. Skip it LOUDLY: screenshots and the
        # bounds timeline still record the change exactly.
        logger.warning(
            f"Skipping video frame {screenshot_image.size} != stream "
            f"{stream_size} (window resized mid-recording?)"
        )
        perf_q.put((event.type, event.timestamp, utils.get_timestamp()))
        return {
            **kwargs,
            **{
                "video_container": video_container,
                "video_stream": video_stream,
                "video_start_timestamp": video_start_timestamp,
                "last_pts": last_pts,
            },
        }
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
    window_scope: WindowCaptureScope | None = None,
    timestamp: float | None = None,
) -> None:
    """Triggers an action event and adds it to the event queue.

    Args:
        event_q: The event queue to add the action event to.
        action_event_args: A dictionary containing the arguments for the action event.
        window_scope: When set (window-scoped capture), global mouse
            coordinates are translated into the target window's pixel space
            before being recorded, so recorded coordinates match the captured
            frames directly.
        timestamp: Native event-receipt time. Defaults to the current recording
            clock only for legacy/direct callers.

    Returns:
        None
    """
    x = action_event_args.get("mouse_x")
    y = action_event_args.get("mouse_y")
    if x is not None and y is not None:
        if config.RECORD_READ_ACTIVE_ELEMENT_STATE:
            # element lookup needs GLOBAL coordinates: translate afterwards.
            element_state = window.get_active_element_state(x, y)
        else:
            element_state = {}
        action_event_args["element_state"] = element_state
        if window_scope is not None:
            try:
                wx, wy = window_scope.translate(x, y)
            except WindowCaptureError as exc:
                # No frame captured yet: recording a global coordinate in a
                # window-scoped session would silently mix coordinate spaces.
                logger.warning(f"Discarding input before first window frame: {exc}")
                return
            action_event_args["mouse_x"] = wx
            action_event_args["mouse_y"] = wy
    event_q.put(
        Event(
            utils.get_timestamp() if timestamp is None else timestamp,
            "action",
            action_event_args,
        )
    )


def on_move(
    event_q: queue.Queue,
    window_scope: WindowCaptureScope | None,
    x: float,
    y: float,
    injected: bool = False,
    timestamp: float | None = None,
) -> None:
    """Handles the 'move' event.

    Args:
        event_q: The event queue to add the 'move' event to.
        window_scope: Optional window scope for coordinate translation.
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
            window_scope,
            timestamp,
        )


def on_click(
    event_q: queue.Queue,
    window_scope: WindowCaptureScope | None,
    x: float,
    y: float,
    button: str,
    pressed: bool,
    injected: bool = False,
    timestamp: float | None = None,
) -> None:
    """Handles the 'click' event.

    Args:
        event_q: The event queue to add the 'click' event to.
        window_scope: Optional window scope for coordinate translation.
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
            window_scope,
            timestamp,
        )


def on_scroll(
    event_q: queue.Queue,
    window_scope: WindowCaptureScope | None,
    x: float,
    y: float,
    dx: float,
    dy: float,
    injected: bool = False,
    timestamp: float | None = None,
) -> None:
    """Handles the 'scroll' event.

    Args:
        event_q: The event queue to add the 'scroll' event to.
        window_scope: Optional window scope for coordinate translation.
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
            window_scope,
            timestamp,
        )


def handle_key(
    event_q: queue.Queue,
    key: ObservedKey,
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
    )


def read_screen_events(
    event_q: queue.Queue,
    terminate_processing: multiprocessing.Event,
    recording: Recording,
    started_event: threading.Event,
    _screen_timing: _ScreenTimingStats | None = None,
    window_scope: WindowCaptureScope | None = None,
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
    """
    utils.set_start_time(recording.timestamp)

    fps = config.SCREEN_CAPTURE_FPS
    min_interval = 1.0 / fps if fps > 0 else 0.0

    logger.info(f"Starting (fps={fps}, min_interval={min_interval:.3f}s)")
    started = False
    announced_window = False
    while not terminate_processing.is_set():
        t_start = time.perf_counter()
        if window_scope is not None:
            try:
                screenshot, window_changed = window_scope.capture_frame()
            except WindowCaptureError as exc:
                # Loud + recoverable: the window may be mid-move/minimized.
                # No frame is queued (never fall back to full-screen pixels
                # in a window-scoped recording), and we retry.
                logger.warning(f"Window capture failed (retrying): {exc}")
                time.sleep(0.5)
                continue
            if window_changed or not announced_window:
                event_q.put(
                    Event(
                        utils.get_timestamp(),
                        "window",
                        window_scope.window_event_data(),
                    )
                )
                announced_window = True
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
) -> tuple[Recording, str]:
    """Create a new recording entry in the per-capture database.

    Args:
        task_description: A text description of the task being recorded.
        capture_dir: Path to the capture directory.
        window_capture_info: Window-scoping metadata
            (``WindowCaptureScope.snapshot()``) persisted in the recording's
            config JSON under ``capture_window`` so converters know the
            session's coordinates are in window-pixel space.

    Returns:
        tuple of (Recording object, db_path).
    """
    os.makedirs(capture_dir, exist_ok=True)
    db_path = os.path.join(capture_dir, "recording.db")

    timestamp = utils.set_start_time()
    monitor_width, monitor_height = utils.get_monitor_dims()
    pixel_ratio = platform.get_display_pixel_ratio()
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
    if window_capture_info is not None:
        recording_data["config"] = {"capture_window": window_capture_info}
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
    window_scope: WindowCaptureScope | None = None,
) -> None:
    """Read globally ordered keyboard and mouse events from one native observer."""
    stop_sequences = [sequence for sequence in config.STOP_SEQUENCES if sequence]
    stop_sequence_indices = [0 for _ in stop_sequences]

    def on_observed(event: ObservedInput) -> None:
        if isinstance(event, ObservedMouseMove):
            on_move(
                event_q,
                window_scope,
                event.x,
                event.y,
                event.injected,
                timestamp=event.timestamp,
            )
            return
        if isinstance(event, ObservedMouseButton):
            on_click(
                event_q,
                window_scope,
                event.x,
                event.y,
                event.button,
                event.pressed,
                event.injected,
                timestamp=event.timestamp,
            )
            return
        if isinstance(event, ObservedMouseScroll):
            on_scroll(
                event_q,
                window_scope,
                event.x,
                event.y,
                event.dx,
                event.dy,
                event.injected,
                timestamp=event.timestamp,
            )
            return
        if event.injected:
            return

        logger.debug(f"{event=}")
        handle_key(event_q, event)
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
                stop_sequence_indices[index] = (
                    1 if candidate == sequence[0].lower() else 0
                )
            if stop_sequence_indices[index] == len(sequence):
                stop_sequence_indices[index] = 0
                logger.info("Stop sequence entered! Stopping recording now.")
                stop_sequence_detected = True

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

    Args:
        recording: The recording object.
        db_path: Path to the per-capture database file.
        terminate_processing: An event to signal the termination of the process.
        started_event: Event to set once started.
    """
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
    audio_stream = sounddevice.InputStream(
        callback=audio_callback, samplerate=16000, channels=1
    )
    logger.info("Audio recording started.")
    start_timestamp = utils.get_timestamp()
    audio_stream.start()

    # NOTE: listener may not have actually started by now
    # TODO: handle race condition, e.g. by sending synthetic events from main thread
    started_event.set()

    terminate_processing.wait()
    audio_stream.stop()
    audio_stream.close()

    # Concatenate into one Numpy array
    concatenated_audio = np.concatenate(audio_frames, axis=0)
    # convert concatenated_audio to format expected by whisper
    converted_audio = concatenated_audio.flatten().astype(np.float32)

    # Convert audio to text using OpenAI's Whisper
    logger.info("Transcribing audio...")
    import whisper
    model = whisper.load_model("base")
    result_info = model.transcribe(converted_audio, word_timestamps=True, fp16=False)
    logger.info(f"The narrated text is: {result_info['text']}")
    # empty word_list if the user didn't say anything
    word_list = []
    # segments could be empty
    if len(result_info["segments"]) > 0:
        # there won't be a 'words' list if the user didn't say anything
        if "words" in result_info["segments"][0]:
            word_list = result_info["segments"][0]["words"]

    # compress and convert to bytes to save to database
    logger.info(
        "Size of uncompressed audio data: {} bytes".format(converted_audio.nbytes)
    )
    # Create an in-memory file-like object
    file_obj = io.BytesIO()
    # Write the audio data using lossless compression
    soundfile.write(
        file_obj, converted_audio, int(audio_stream.samplerate), format="FLAC"
    )
    # Get the compressed audio data as bytes
    compressed_audio_bytes = file_obj.getvalue()

    logger.info(
        "Size of compressed audio data: {} bytes".format(len(compressed_audio_bytes))
    )

    file_obj.close()

    # To decompress the audio and restore it to its original form:
    # restored_audio, restored_samplerate = sf.read(
    # io.BytesIO(compressed_audio_bytes))

    session = get_session_for_path(db_path)
    # Create AudioInfo entry
    crud.insert_audio_info(
        session,
        compressed_audio_bytes,
        result_info["text"],
        recording,
        start_timestamp,
        int(audio_stream.samplerate),
        word_list,
    )


@logger.catch
@utils.trace(logger)
def read_browser_events(
    websocket: "websockets.sync.server.ServerConnection",
    event_q: queue.Queue,
    terminate_processing: Event,
    recording: Recording,
) -> None:
    """Read browser events and add them to the event queue.

    Params:
        websocket: The websocket object.
        event_q: A queue for adding browser events.
        terminate_processing: An event to signal the termination of the process.
        recording: The recording object.

    Returns:
        None
    """
    utils.set_start_time(recording.timestamp)

    # set the browser mode
    set_browser_mode("record", websocket)

    logger.info("Starting Reading Browser Events ...")

    while not terminate_processing.is_set():
        try:
            message = websocket.recv(0.01)
        except TimeoutError:
            continue
        timestamp = utils.get_timestamp()
        data = json.loads(message)
        event_q.put(
            Event(
                timestamp,
                "browser",
                {"message": data},
            )
        )

    set_browser_mode("idle", websocket)


@logger.catch
@utils.trace(logger)
def run_browser_event_server(
    event_q: queue.Queue,
    terminate_processing: Event,
    recording: Recording,
    started_event: threading.Event,
) -> None:
    """Run the browser event server.

    Params:
        event_q: A queue for adding browser events.
        terminate_processing: An event to signal the termination of the process.
        recording: The recording object.
        started_event: Event to set once started.

    Returns:
        None
    """
    global ws_server_instance

    # Function to run the server in a separate thread
    def run_server() -> None:
        global ws_server_instance
        with websockets.sync.server.serve(
            lambda ws: read_browser_events(
                ws,
                event_q,
                terminate_processing,
                recording,
            ),
            config.BROWSER_WEBSOCKET_SERVER_IP,
            config.BROWSER_WEBSOCKET_PORT,
            max_size=config.BROWSER_WEBSOCKET_MAX_SIZE,
        ) as server:
            ws_server_instance = server
            logger.info("WebSocket server started")
            started_event.set()
            server.serve_forever()

    # Start the server in a separate thread
    server_thread = threading.Thread(target=run_server)
    server_thread.start()

    # Wait for a termination signal
    terminate_processing.wait()
    logger.info("Termination signal received, shutting down server")

    if ws_server_instance:
        ws_server_instance.shutdown()

    # Ensure the server thread is terminated cleanly
    server_thread.join()


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
) -> None:
    """Record Screenshots/ActionEvents/WindowEvents/BrowserEvents.

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
    """
    assert config.RECORD_VIDEO or config.RECORD_IMAGES, (
        config.RECORD_VIDEO,
        config.RECORD_IMAGES,
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
    if window_scope is not None:
        initial_window_frame, _ = window_scope.capture_frame()
        logger.info(
            f"window-scoped capture resolved: {window_scope.snapshot()} "
            f"initial frame {initial_window_frame.size}"
        )

    if capture_dir is None:
        capture_dir = os.path.join(os.getcwd(), "capture")
    recording, db_path = create_recording(
        task_description,
        capture_dir,
        window_capture_info=(
            window_scope.snapshot() if window_scope is not None else None
        ),
    )
    recording_timestamp = recording.timestamp

    event_q = queue.Queue()
    screen_write_q = sq.SynchronizedQueue()
    action_write_q = sq.SynchronizedQueue()
    window_write_q = sq.SynchronizedQueue()
    browser_write_q = sq.SynchronizedQueue()
    video_write_q = sq.SynchronizedQueue()
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
            target=read_window_events,
            daemon=True,
            args=(
                event_q,
                terminate_processing,
                recording,
                task_started_events.setdefault(
                    "window_event_reader", threading.Event()
                ),
            ),
        )
        window_event_reader.start()
        task_by_name["window_event_reader"] = window_event_reader

    if config.RECORD_BROWSER_EVENTS:
        browser_event_reader = threading.Thread(
            target=run_browser_event_server,
            daemon=True,
            args=(
                event_q,
                terminate_processing,
                recording,
                task_started_events.setdefault(
                    "browser_event_reader", threading.Event()
                ),
            ),
        )
        browser_event_reader.start()
        task_by_name["browser_event_reader"] = browser_event_reader

    screen_event_reader = threading.Thread(
        target=read_screen_events,
        daemon=True,
        args=(
            event_q,
            terminate_processing,
            recording,
            task_started_events.setdefault("screen_event_reader", threading.Event()),
            _screen_timing,
            window_scope,
        ),
    )
    screen_event_reader.start()
    task_by_name["screen_event_reader"] = screen_event_reader

    input_reader_args = (
        event_q,
        terminate_processing,
        recording,
        task_started_events.setdefault("input_event_reader", threading.Event()),
        window_scope,
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

    event_processor = threading.Thread(
        target=process_events,
        daemon=True,
        args=(
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
            terminate_processing,
            task_started_events.setdefault(
                "screen_event_writer", multiprocessing.Event()
            ),
        ),
    )
    screen_event_writer.start()
    task_by_name["screen_event_writer"] = screen_event_writer

    if config.RECORD_BROWSER_EVENTS:
        browser_event_writer = multiprocessing.Process(
            target=write_events,
            args=(
                "browser",
                write_browser_event,
                browser_write_q,
                num_browser_events,
                perf_q,
                recording,
                db_path,
                terminate_processing,
                task_started_events.setdefault(
                    "browser_event_writer", multiprocessing.Event()
                ),
            ),
        )
        browser_event_writer.start()
        task_by_name["browser_event_writer"] = browser_event_writer

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
            terminate_processing,
            task_started_events.setdefault(
                "action_event_writer", multiprocessing.Event()
            ),
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
                terminate_processing,
                task_started_events.setdefault(
                    "window_event_writer", multiprocessing.Event()
                ),
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
                terminate_processing,
                task_started_events.setdefault("video_writer", multiprocessing.Event()),
                partial(
                    video_pre_callback,
                    video_dir=capture_dir,
                    # Window-scoped frames are the window's pixels, not the
                    # monitor's: size the stream from the initial frame.
                    frame_size=(
                        initial_window_frame.size
                        if initial_window_frame is not None
                        else None
                    ),
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
                task_started_events.setdefault(
                    "audio_event_writer", multiprocessing.Event()
                ),
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
            task_started_events.setdefault(
                "perf_stats_writer", multiprocessing.Event()
            ),
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
        logger.info(
            "All readers and writers have started. Waiting for input events..."
        )

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

    pre_ready_timeout = (
        None if startup_ready else PRE_READY_TASK_JOIN_TIMEOUT_SECONDS
    )
    _join_tasks(
        task_by_name,
        [
            "window_event_reader",
            "browser_event_reader",
            "screen_event_reader",
            "input_event_reader",
            "event_processor",
            "screen_event_writer",
            "browser_event_writer",
            "action_event_writer",
            "window_event_writer",
            "video_writer",
            "audio_recorder",
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

    if config.PLOT_PERFORMANCE and startup_ready:
        from openadapt_capture import plotting

        session = get_session_for_path(db_path)
        plotting.plot_performance(
            session, recording, save_dir=capture_dir,
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
            print(f"  screenshot: avg={st['screenshot_avg_ms']}ms "
                  f"max={st['screenshot_max_ms']}ms "
                  f"min={st['screenshot_min_ms']}ms")
        print(f"Config: WINDOW_DATA={config.RECORD_WINDOW_DATA} "
              f"VIDEO={config.RECORD_VIDEO} "
              f"PLOT_PERF={config.PLOT_PERFORMANCE} "
              f"FPS={config.SCREEN_CAPTURE_FPS}")
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
        capture_browser_events: bool | None = None,
        capture_full_video: bool | None = None,
        video_encoding: str | None = None,
        video_pixel_format: str | None = None,
        stop_sequences: list[list[str]] | None = None,
        log_memory: bool | None = None,
        plot_performance: bool | None = None,
        screen_capture_fps: float | None = None,
        send_profile: bool = False,
        window: dict | None = None,
    ) -> None:
        from pathlib import Path

        from openadapt_capture.config import RecordingConfig
        from openadapt_capture.window_capture import WindowTarget

        self.capture_dir = str(Path(capture_dir).resolve())
        self.task_description = task_description
        self._send_profile = send_profile

        # Validate the window spec up front (loud, before any thread starts).
        window_target = WindowTarget.from_spec(window)

        # Build recording config from constructor params
        self._recording_config = RecordingConfig(
            capture_video=capture_video,
            capture_audio=capture_audio,
            capture_images=capture_images,
            capture_window_data=capture_window_data,
            capture_browser_events=capture_browser_events,
            capture_full_video=capture_full_video,
            video_encoding=video_encoding,
            video_pixel_format=video_pixel_format,
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

        # Internal
        self._record_thread: threading.Thread | None = None
        self._status_thread: threading.Thread | None = None
        self._capture = None  # lazy CaptureSession
        self._worker_error: BaseException | None = None
        self._worker_error_lock = threading.Lock()

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
                        elif msg.get("type") == "record.stopped":
                            self._stopped_event.set()
                            self._ready_or_stopped_event.set()
        except (EOFError, OSError):
            pass

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
                )
        except BaseException as exc:
            # A setup exception must wake wait_for_ready() and let context-manager
            # teardown finish instead of leaving callers blocked for its timeout.
            with self._worker_error_lock:
                if self._worker_error is None:
                    self._worker_error = exc
            self._terminate_processing.set()
            try:
                self._status_send.send({"type": "record.stopped"})
            except (BrokenPipeError, EOFError, OSError):
                self._stopped_event.set()
                self._ready_or_stopped_event.set()

    def __enter__(self) -> "Recorder":
        # Start status drain thread
        self._status_thread = threading.Thread(
            target=self._drain_status_pipe, daemon=True,
        )
        self._status_thread.start()

        # Start recording thread
        self._record_thread = threading.Thread(target=self._run_record)
        self._record_thread.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self._terminate_processing.set()
        if self._record_thread is not None:
            self._record_thread.join()
        self._stopped_event.set()  # ensure status thread exits
        if self._status_thread is not None:
            self._status_thread.join(timeout=5)
        if self._worker_error is not None:
            if exc_val is not None:
                add_exception_note(
                    exc_val,
                    f"the recorder worker also failed: {self._worker_error!r}"
                )
            else:
                raise self._worker_error

    def stop(self) -> None:
        """Stop, join, and surface any recording-worker failure."""
        self._terminate_processing.set()
        if self._record_thread is not None:
            self._record_thread.join()
        self.check_health()

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
