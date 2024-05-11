"""Script for creating Recordings.

Usage:

    $ python -m openadapt.record "<description of task to be recorded>"

"""

from collections import namedtuple
from functools import partial, wraps
from typing import Any, Callable
import io
import multiprocessing
import os
import queue
import signal
import sys
import threading
import time
import tracemalloc

from loguru import logger
from oa_pynput import keyboard, mouse
from pympler import tracker
import av

from openadapt.build_utils import redirect_stdout_stderr

with redirect_stdout_stderr():
    from tqdm import tqdm
    import fire

import psutil

from openadapt import utils, video, window
from openadapt.config import config
from openadapt.db import crud
from openadapt.extensions import synchronized_queue as sq
from openadapt.models import ActionEvent

Event = namedtuple("Event", ("timestamp", "type", "data"))

EVENT_TYPES = ("screen", "action", "window")
LOG_LEVEL = "INFO"
PROC_WRITE_BY_EVENT_TYPE = {
    "screen": True,
    "action": True,
    "window": True,
}
PLOT_PERFORMANCE = config.PLOT_PERFORMANCE
NUM_MEMORY_STATS_TO_LOG = 3
STOP_SEQUENCES = config.STOP_SEQUENCES

stop_sequence_detected = False


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


def args_to_str(*args: tuple) -> str:
    """Convert positional arguments to a string representation.

    Args:
        *args: Positional arguments.

    Returns:
        str: Comma-separated string representation of positional arguments.
    """
    return ", ".join(map(str, args))


def kwargs_to_str(**kwargs: dict[str, Any]) -> str:
    """Convert keyword arguments to a string representation.

    Args:
        **kwargs: Keyword arguments.

    Returns:
        str: Comma-separated string representation of keyword arguments
          in form "key=value".
    """
    return ",".join([f"{k}={v}" for k, v in kwargs.items()])


def trace(logger: logger) -> Any:
    """Decorator that logs the function entry and exit using the provided logger.

    Args:
        logger: The logger object to use for logging.

    Returns:
        A decorator that can be used to wrap functions and log their entry and exit.
    """

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper_logging(*args: tuple[tuple, ...], **kwargs: dict[str, Any]) -> Any:
            func_name = func.__qualname__
            func_args = args_to_str(*args)
            func_kwargs = kwargs_to_str(**kwargs)

            if func_kwargs != "":
                logger.info(f" -> Enter: {func_name}({func_args}, {func_kwargs})")
            else:
                logger.info(f" -> Enter: {func_name}({func_args})")

            result = func(*args, **kwargs)

            logger.info(f" <- Leave: {func_name}({result})")
            return result

        return wrapper_logging

    return decorator


def process_event(
    event: ActionEvent,
    write_q: sq.SynchronizedQueue,
    write_fn: Callable,
    recording_timestamp: int,
    perf_q: sq.SynchronizedQueue,
) -> None:
    """Process an event and take appropriate action based on its type.

    Args:
        event: The event to process.
        write_q: The queue for writing the event.
        write_fn: The function for writing the event.
        recording_timestamp: The timestamp of the recording.
        perf_q: The queue for collecting performance statistics.

    Returns:
        None
    """
    if PROC_WRITE_BY_EVENT_TYPE[event.type]:
        write_q.put(event)
    else:
        write_fn(recording_timestamp, event, perf_q)


@trace(logger)
def process_events(
    event_q: queue.Queue,
    screen_write_q: sq.SynchronizedQueue,
    action_write_q: sq.SynchronizedQueue,
    window_write_q: sq.SynchronizedQueue,
    video_write_q: sq.SynchronizedQueue,
    perf_q: sq.SynchronizedQueue,
    recording_timestamp: float,
    terminate_processing: multiprocessing.Event,
    started_counter: multiprocessing.Value,
    num_screen_events: multiprocessing.Value,
    num_action_events: multiprocessing.Value,
    num_window_events: multiprocessing.Value,
    num_video_events: multiprocessing.Value,
) -> None:
    """Process events from the event queue and write them to write queues.

    Args:
        event_q: A queue with events to be processed.
        screen_write_q: A queue for writing screen events.
        action_write_q: A queue for writing action events.
        window_write_q: A queue for writing window events.
        video_write_q: A queue for writing video events.
        perf_q: A queue for collecting performance data.
        recording_timestamp: The timestamp of the recording.
        terminate_processing: An event to signal the termination of the process.
        started_counter: Value to increment once started.
        num_screen_events: A counter for the number of screen events.
        num_action_events: A counter for the number of action events.
        num_window_events: A counter for the number of window events.
        num_video_events: A counter for the number of video events.
    """
    utils.set_start_time(recording_timestamp)

    logger.info("Starting")

    prev_event = None
    prev_screen_event = None
    prev_window_event = None
    prev_saved_screen_timestamp = 0
    prev_saved_window_timestamp = 0
    started = False
    while not terminate_processing.is_set() or not event_q.empty():
        event = event_q.get()
        if not started:
            with started_counter.get_lock():
                started_counter.value += 1
            started = True
        logger.trace(f"{event=}")
        assert event.type in EVENT_TYPES, event
        if prev_event is not None:
            try:
                assert event.timestamp > prev_event.timestamp, (
                    event,
                    prev_event,
                )
            except AssertionError as exc:
                logger.error(exc)
                # behavior undefined, swallow for now
                # XXX TODO: mitigate
        if event.type == "screen":
            prev_screen_event = event
            if config.RECORD_VIDEO and config.RECORD_FULL_VIDEO:
                video_write_q.put(event)
                num_video_events.value += 1
        elif event.type == "window":
            prev_window_event = event
        elif event.type == "action":
            if prev_screen_event is None:
                logger.warning("Discarding action that came before screen")
                continue
            if prev_window_event is None:
                logger.warning("Discarding input that came before window")
                continue
            event.data["screenshot_timestamp"] = prev_screen_event.timestamp
            event.data["window_event_timestamp"] = prev_window_event.timestamp
            process_event(
                event,
                action_write_q,
                write_action_event,
                recording_timestamp,
                perf_q,
            )
            num_action_events.value += 1
            if prev_saved_screen_timestamp < prev_screen_event.timestamp:
                process_event(
                    prev_screen_event,
                    screen_write_q,
                    write_screen_event,
                    recording_timestamp,
                    perf_q,
                )
                num_screen_events.value += 1
                prev_saved_screen_timestamp = prev_screen_event.timestamp
                if config.RECORD_VIDEO and not config.RECORD_FULL_VIDEO:
                    video_write_q.put(prev_screen_event)
                    num_video_events.value += 1
            if prev_saved_window_timestamp < prev_window_event.timestamp:
                process_event(
                    prev_window_event,
                    window_write_q,
                    write_window_event,
                    recording_timestamp,
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
    recording_timestamp: float,
    event: Event,
    perf_q: sq.SynchronizedQueue,
) -> None:
    """Write an action event to the database and update the performance queue.

    Args:
        recording_timestamp: The timestamp of the recording.
        event: An action event to be written.
        perf_q: A queue for collecting performance data.
    """
    assert event.type == "action", event
    crud.insert_action_event(recording_timestamp, event.timestamp, event.data)
    perf_q.put((event.type, event.timestamp, utils.get_timestamp()))


def write_screen_event(
    recording_timestamp: float,
    event: Event,
    perf_q: sq.SynchronizedQueue,
) -> None:
    """Write a screen event to the database and update the performance queue.

    Args:
        recording_timestamp: The timestamp of the recording.
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
    crud.insert_screenshot(recording_timestamp, event.timestamp, event_data)
    perf_q.put((event.type, event.timestamp, utils.get_timestamp()))


def write_window_event(
    recording_timestamp: float,
    event: Event,
    perf_q: sq.SynchronizedQueue,
) -> None:
    """Write a window event to the database and update the performance queue.

    Args:
        recording_timestamp: The timestamp of the recording.
        event: A window event to be written.
        perf_q: A queue for collecting performance data.
    """
    assert event.type == "window", event
    crud.insert_window_event(recording_timestamp, event.timestamp, event.data)
    perf_q.put((event.type, event.timestamp, utils.get_timestamp()))


@trace(logger)
def write_events(
    event_type: str,
    write_fn: Callable,
    write_q: sq.SynchronizedQueue,
    num_events: multiprocessing.Value,
    perf_q: sq.SynchronizedQueue,
    recording_timestamp: float,
    terminate_processing: multiprocessing.Event,
    started_counter: multiprocessing.Value,
    pre_callback: Callable[[float], dict] | None = None,
    post_callback: Callable[[dict], None] | None = None,
    event_type_modifier: str = "",
) -> None:
    """Write events of a specific type to the db using the provided write function.

    Args:
        event_type: The type of events to be written.
        write_fn: A function to write events to the database.
        write_q: A queue with events to be written.
        num_events: A counter for the number of events.
        perf_q: A queue for collecting performance data.
        recording_timestamp: The timestamp of the recording.
        terminate_processing: An event to signal the termination of the process.
        started_counter: Value to increment once started.
        pre_callback: Optional function to call before main loop. Takes recording
            timestamp as only argument, returns a state dict.
        post_callback: Optional function to call after main loop. Takes state dict as
            only argument, returns None.
        event_type_modifier: Optional string to differentiate identical event_types
    """
    utils.set_start_time(recording_timestamp)

    logger.info(f"{event_type=} starting")
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    if pre_callback:
        state = pre_callback(recording_timestamp)
    else:
        state = None

    num_processed = 0
    progress = None
    started = False
    while not terminate_processing.is_set() or not write_q.empty():
        if terminate_processing.is_set() and progress is None:
            # if processing is over, create a progress bar
            with redirect_stdout_stderr():
                total_events = num_events.value
                progress = tqdm(
                    total=total_events,
                    desc=f"Writing {event_type}{event_type_modifier} events...",
                    unit="event",
                    colour="green",
                    dynamic_ncols=True,
                )
                # update the progress bar with the number of events that have already
                # been processed
                for _ in range(num_processed):
                    progress.update()
        if not started:
            with started_counter.get_lock():
                started_counter.value += 1
            started = True
        try:
            event = write_q.get_nowait()
        except queue.Empty:
            continue
        assert event.type == event_type, (event_type, event)
        state = write_fn(recording_timestamp, event, perf_q, **(state or {}))
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


def video_pre_callback(recording_timestamp: float) -> dict[str, Any]:
    """Function to call before main loop.

    Args:
        recording_timestmap(float): The Recording timestamp.

    Returns:
        dict[str, Any]: The updated state.
    """
    video_file_path = video.get_video_file_path(recording_timestamp)
    # TODO XXX replace with utils.get_monitor_dims() once fixed
    width, height = utils.take_screenshot().size
    video_container, video_stream, video_start_timestamp = (
        video.initialize_video_writer(video_file_path, width, height)
    )
    crud.update_video_start_time(recording_timestamp, video_start_timestamp)
    return {
        "video_container": video_container,
        "video_stream": video_stream,
        "video_start_timestamp": video_start_timestamp,
        "last_pts": 0,
    }


def video_post_callback(state: dict) -> None:
    """Function to call after main loop.

    Args:
        state (dict): The current state.
    """
    video.finalize_video_writer(
        state["video_container"],
        state["video_stream"],
    )


def write_video_event(
    recording_timestamp: float,
    event: Event,
    perf_q: sq.SynchronizedQueue,
    video_container: av.container.OutputContainer,
    video_stream: av.stream.Stream,
    video_start_timestamp: float,
    last_pts: int = 0,
    num_copies: int = 2,
) -> dict[str, Any]:
    """Write a screen event to the video file and update the performance queue.

    Args:
        recording_timestamp: The timestamp of the recording.
        event: A screen event to be written.
        perf_q: A queue for collecting performance data.
        video_container (av.container.OutputContainer): The output container to which
            the frame is written.
        video_stream (av.stream.Stream): The video stream within the container.
        video_start_timestamp (float): The base timestamp from which the video
            recording started.
        last_pts: The last presentation timestamp.
        num_copies: The number of times to write the first each frame.

    Returns:
        dict containing state.
    """
    if last_pts != 0:
        num_copies = 1
    # ensure that the first frame is available (otherwise occasionally it is not)
    for _ in range(num_copies):
        last_pts = video.write_video_frame(
            video_container,
            video_stream,
            event.data,
            event.timestamp,
            video_start_timestamp,
            last_pts,
        )
    perf_q.put((f"{event.type}(video)", event.timestamp, utils.get_timestamp()))
    return {
        "video_container": video_container,
        "video_stream": video_stream,
        "video_start_timestamp": video_start_timestamp,
        "last_pts": last_pts,
    }


def trigger_action_event(
    event_q: queue.Queue, action_event_args: dict[str, Any]
) -> None:
    """Triggers an action event and adds it to the event queue.

    Args:
        event_q: The event queue to add the action event to.
        action_event_args: A dictionary containing the arguments for the action event.

    Returns:
        None
    """
    x = action_event_args.get("mouse_x")
    y = action_event_args.get("mouse_y")
    if x is not None and y is not None:
        if config.RECORD_READ_ACTIVE_ELEMENT_STATE:
            element_state = window.get_active_element_state(x, y)
        else:
            element_state = {}
        action_event_args["element_state"] = element_state
    event_q.put(Event(utils.get_timestamp(), "action", action_event_args))


def on_move(event_q: queue.Queue, x: int, y: int, injected: bool) -> None:
    """Handles the 'move' event.

    Args:
        event_q: The event queue to add the 'move' event to.
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
        )


def on_click(
    event_q: queue.Queue,
    x: int,
    y: int,
    button: mouse.Button,
    pressed: bool,
    injected: bool,
) -> None:
    """Handles the 'click' event.

    Args:
        event_q: The event queue to add the 'click' event to.
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
                "mouse_button_name": button.name,
                "mouse_pressed": pressed,
            },
        )


def on_scroll(
    event_q: queue.Queue,
    x: int,
    y: int,
    dx: int,
    dy: int,
    injected: bool,
) -> None:
    """Handles the 'scroll' event.

    Args:
        event_q: The event queue to add the 'scroll' event to.
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
        )


def handle_key(
    event_q: queue.Queue,
    event_name: str,
    key: keyboard.KeyCode,
    canonical_key: keyboard.KeyCode,
) -> None:
    """Handles a key event.

    Args:
        event_q: The event queue to add the key event to.
        event_name: The name of the key event.
        key: The key code of the key event.
        canonical_key: The canonical key code of the key event.

    Returns:
        None
    """
    attr_names = [
        "name",
        "char",
        "vk",
    ]
    attrs = {
        f"key_{attr_name}": getattr(key, attr_name, None) for attr_name in attr_names
    }
    logger.debug(f"{attrs=}")
    canonical_attrs = {
        f"canonical_key_{attr_name}": getattr(canonical_key, attr_name, None)
        for attr_name in attr_names
    }
    logger.debug(f"{canonical_attrs=}")
    trigger_action_event(event_q, {"name": event_name, **attrs, **canonical_attrs})


def read_screen_events(
    event_q: queue.Queue,
    terminate_processing: multiprocessing.Event,
    recording_timestamp: float,
    started_counter: multiprocessing.Value,
    # TODO: throttle
    # max_cpu_percent: float = 50.0,  # Maximum allowed CPU percent
    # max_memory_percent: float = 50.0,  # Maximum allowed memory percent
    # fps_warning_threshold: float = 10.0,  # FPS threshold below which to warn
) -> None:
    """Read screen events and add them to the event queue.

    Args:
        event_q: A queue for adding screen events.
        terminate_processing: An event to signal the termination of the process.
        recording_timestamp: The timestamp of the recording.
        started_counter: Value to increment once started.
    """
    utils.set_start_time(recording_timestamp)

    logger.info("Starting")
    started = False
    while not terminate_processing.is_set():
        screenshot = utils.take_screenshot()
        if screenshot is None:
            logger.warning("Screenshot was None")
            continue
        if not started:
            with started_counter.get_lock():
                started_counter.value += 1
            started = True
        event_q.put(Event(utils.get_timestamp(), "screen", screenshot))
    logger.info("Done")


@trace(logger)
def read_window_events(
    event_q: queue.Queue,
    terminate_processing: multiprocessing.Event,
    recording_timestamp: float,
    started_counter: multiprocessing.Value,
) -> None:
    """Read window events and add them to the event queue.

    Args:
        event_q: A queue for adding window events.
        terminate_processing: An event to signal the termination of the process.
        recording_timestamp: The timestamp of the recording.
        started_counter: Value to increment once started.
    """
    utils.set_start_time(recording_timestamp)

    logger.info("Starting")
    prev_window_data = {}
    started = False
    while not terminate_processing.is_set():
        window_data = window.get_active_window_data()
        if not window_data:
            continue

        if not started:
            with started_counter.get_lock():
                started_counter.value += 1
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


@trace(logger)
def performance_stats_writer(
    perf_q: sq.SynchronizedQueue,
    recording_timestamp: float,
    terminate_processing: multiprocessing.Event,
    started_counter: multiprocessing.Value,
) -> None:
    """Write performance stats to the database.

    Each entry includes the event type, start time, and end time.

    Args:
        perf_q: A queue for collecting performance data.
        recording_timestamp: The timestamp of the recording.
        terminate_processing: An event to signal the termination of the process.
        started_counter: Value to increment once started.
    """
    utils.set_start_time(recording_timestamp)

    logger.info("Performance stats writer starting")
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    started = False
    while not terminate_processing.is_set() or not perf_q.empty():
        if not started:
            with started_counter.get_lock():
                started_counter.value += 1
            started = True
        try:
            event_type, start_time, end_time = perf_q.get_nowait()
        except queue.Empty:
            continue

        crud.insert_perf_stat(
            recording_timestamp,
            event_type,
            start_time,
            end_time,
        )
    logger.info("Performance stats writer done")


def memory_writer(
    recording_timestamp: float,
    terminate_processing: multiprocessing.Event,
    record_pid: int,
    started_counter: multiprocessing.Value,
) -> None:
    """Writes memory usage statistics to the database.

    Args:
        recording_timestamp (float): The timestamp of the recording.
        terminate_processing (multiprocessing.Event): The event used to terminate
          the process.
        record_pid (int): The process ID to monitor memory usage for.
        started_counter: Value to increment once started.

    Returns:
        None
    """
    utils.set_start_time(recording_timestamp)

    logger.info("Memory writer starting")
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    process = psutil.Process(record_pid)

    started = False
    while not terminate_processing.is_set():
        if not started:
            with started_counter.get_lock():
                started_counter.value += 1
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
            recording_timestamp,
            rss,
            timestamp,
        )
    logger.info("Memory writer done")


@trace(logger)
def create_recording(
    task_description: str,
) -> dict[str, Any]:
    """Create a new recording entry in the database.

    Args:
        task_description: A text description of the task being recorded.

    Returns:
        The newly created Recording object.
    """
    timestamp = utils.set_start_time()
    monitor_width, monitor_height = utils.get_monitor_dims()
    double_click_distance_pixels = utils.get_double_click_distance_pixels()
    double_click_interval_seconds = utils.get_double_click_interval_seconds()
    recording_data = {
        # TODO: rename
        "timestamp": timestamp,
        "monitor_width": monitor_width,
        "monitor_height": monitor_height,
        "double_click_distance_pixels": double_click_distance_pixels,
        "double_click_interval_seconds": double_click_interval_seconds,
        "platform": sys.platform,
        "task_description": task_description,
        "config": config.model_dump(obfuscated=True),
    }
    recording = crud.insert_recording(recording_data)
    logger.info(f"{recording=}")
    return recording


def read_keyboard_events(
    event_q: queue.Queue,
    terminate_processing: multiprocessing.Event,
    recording_timestamp: float,
    started_counter: multiprocessing.Value,
) -> None:
    """Reads keyboard events and adds them to the event queue.

    Args:
        event_q (queue.Queue): The event queue to add the keyboard events to.
        terminate_processing (multiprocessing.Event): The event to signal termination
          of event reading.
        recording_timestamp (float): The timestamp of the recording.
        started_counter: Value to increment once started.

    Returns:
        None
    """
    # create list of indices for sequence detection
    # one index for each stop sequence in STOP_SEQUENCES
    stop_sequence_indices = [0 for _ in STOP_SEQUENCES]

    def on_press(
        event_q: queue.Queue,
        key: keyboard.Key | keyboard.KeyCode,
        injected: bool,
    ) -> None:
        """Event handler for key press events.

        Args:
            event_q (queue.Queue): The event queue for processing key events.
            key (keyboard.KeyboardEvent): The key event object representing
              the pressed key.
            injected (bool): A flag indicating whether the key event was injected.

        Returns:
            None
        """
        canonical_key = keyboard_listener.canonical(key)
        logger.debug(f"{key=} {injected=} {canonical_key=}")
        if not injected:
            handle_key(event_q, "press", key, canonical_key)

        # stop sequence code
        nonlocal stop_sequence_indices
        global stop_sequence_detected
        canonical_key_name = getattr(canonical_key, "name", None)

        for i in range(0, len(STOP_SEQUENCES)):
            # check each stop sequence
            stop_sequence = STOP_SEQUENCES[i]
            # stop_sequence_indices[i] is the index for this stop sequence
            # get canonical KeyCode of current letter in this sequence
            canonical_sequence = keyboard_listener.canonical(
                keyboard.KeyCode.from_char(stop_sequence[stop_sequence_indices[i]])
            )

            # Check if the pressed key matches the current key in this sequence
            if (
                canonical_key == canonical_sequence
                or canonical_key_name == stop_sequence[stop_sequence_indices[i]]
            ):
                # increment this index
                stop_sequence_indices[i] += 1
            else:
                # Reset index since pressed key doesn't match sequence key
                stop_sequence_indices[i] = 0

            # Check if the entire sequence has been entered correctly
            if stop_sequence_indices[i] == len(stop_sequence):
                logger.info("Stop sequence entered! Stopping recording now.")
                stop_sequence_detected = True

    def on_release(
        event_q: queue.Queue,
        key: keyboard.Key | keyboard.KeyCode,
        injected: bool,
    ) -> None:
        """Event handler for key release events.

        Args:
            event_q (queue.Queue): The event queue for processing key events.
            key (keyboard.KeyboardEvent): The key event object representing
              the released key.
            injected (bool): A flag indicating whether the key event was injected.

        Returns:
            None
        """
        canonical_key = keyboard_listener.canonical(key)
        logger.debug(f"{key=} {injected=} {canonical_key=}")
        if not injected:
            handle_key(event_q, "release", key, canonical_key)

    utils.set_start_time(recording_timestamp)

    keyboard_listener = keyboard.Listener(
        on_press=partial(on_press, event_q),
        on_release=partial(on_release, event_q),
    )
    keyboard_listener.start()

    # NOTE: listener may not have actually started by now
    # TODO: handle race condition, e.g. by sending synthetic events from main thread
    with started_counter.get_lock():
        started_counter.value += 1

    terminate_processing.wait()
    keyboard_listener.stop()


def read_mouse_events(
    event_q: queue.Queue,
    terminate_processing: multiprocessing.Event,
    recording_timestamp: float,
    started_counter: multiprocessing.Value,
) -> None:
    """Reads mouse events and adds them to the event queue.

    Args:
        event_q: The event queue to add the mouse events to.
        terminate_processing: The event to signal termination of event reading.
        recording_timestamp: The timestamp of the recording.
        started_counter: Value to increment once started.

    Returns:
        None
    """
    utils.set_start_time(recording_timestamp)

    mouse_listener = mouse.Listener(
        on_move=partial(on_move, event_q),
        on_click=partial(on_click, event_q),
        on_scroll=partial(on_scroll, event_q),
    )
    mouse_listener.start()

    # NOTE: listener may not have actually started by now
    # TODO: handle race condition, e.g. by sending synthetic events from main thread
    with started_counter.get_lock():
        started_counter.value += 1

    terminate_processing.wait()
    mouse_listener.stop()


@logger.catch
@trace(logger)
def record(
    task_description: str,
    # these should be Event | None, but this raises:
    #   TypeError: unsupported operand type(s) for |: 'method' and 'NoneType'
    # type(multiprocessing.Event) appears to be <class 'method'>
    # TODO: fix this
    terminate_processing: multiprocessing.Event = None,
    terminate_recording: multiprocessing.Event = None,
    status_pipe: multiprocessing.connection.Connection | None = None,
    log_memory: bool = config.LOG_MEMORY,
) -> None:
    """Record Screenshots/ActionEvents/WindowEvents.

    Args:
        task_description: A text description of the task to be recorded.
        terminate_processing: An event to signal the termination of the events
        processing.
        terminate_recording: An event to signal the termination of the recording.
        status_pipe: A connection to communicate recording status.
        log_memory: Whether to log memory usage.
    """
    utils.configure_logging(logger, LOG_LEVEL)

    assert config.RECORD_VIDEO or config.RECORD_IMAGES, (
        config.RECORD_VIDEO,
        config.RECORD_IMAGES,
    )

    # logically it makes sense to communicate from here, but when running
    # from the tray it takes too long
    # TODO: fix this
    # if status_pipe:
    #    status_pipe.send({"type": "record.starting"})

    logger.info(f"{task_description=}")

    recording = create_recording(task_description)
    recording_timestamp = recording.timestamp

    event_q = queue.Queue()
    screen_write_q = sq.SynchronizedQueue()
    action_write_q = sq.SynchronizedQueue()
    window_write_q = sq.SynchronizedQueue()
    video_write_q = sq.SynchronizedQueue()
    # TODO: save write times to DB; display performance plot in visualize.py
    perf_q = sq.SynchronizedQueue()
    if terminate_processing is None:
        terminate_processing = multiprocessing.Event()
    started_counter = multiprocessing.Value("i", 0)
    expected_starts = 9

    window_event_reader = threading.Thread(
        target=read_window_events,
        args=(event_q, terminate_processing, recording_timestamp, started_counter),
    )
    window_event_reader.start()

    screen_event_reader = threading.Thread(
        target=read_screen_events,
        args=(event_q, terminate_processing, recording_timestamp, started_counter),
    )
    screen_event_reader.start()

    keyboard_event_reader = threading.Thread(
        target=read_keyboard_events,
        args=(event_q, terminate_processing, recording_timestamp, started_counter),
    )
    keyboard_event_reader.start()

    mouse_event_reader = threading.Thread(
        target=read_mouse_events,
        args=(event_q, terminate_processing, recording_timestamp, started_counter),
    )
    mouse_event_reader.start()

    num_action_events = multiprocessing.Value("i", 0)
    num_screen_events = multiprocessing.Value("i", 0)
    num_window_events = multiprocessing.Value("i", 0)
    num_video_events = multiprocessing.Value("i", 0)

    event_processor = threading.Thread(
        target=process_events,
        args=(
            event_q,
            screen_write_q,
            action_write_q,
            window_write_q,
            video_write_q,
            perf_q,
            recording_timestamp,
            terminate_processing,
            started_counter,
            num_screen_events,
            num_action_events,
            num_window_events,
            num_video_events,
        ),
    )
    event_processor.start()

    screen_event_writer = multiprocessing.Process(
        target=write_events,
        args=(
            "screen",
            write_screen_event,
            screen_write_q,
            num_screen_events,
            perf_q,
            recording_timestamp,
            terminate_processing,
            started_counter,
        ),
    )
    screen_event_writer.start()

    action_event_writer = multiprocessing.Process(
        target=write_events,
        args=(
            "action",
            write_action_event,
            action_write_q,
            num_action_events,
            perf_q,
            recording_timestamp,
            terminate_processing,
            started_counter,
        ),
    )
    action_event_writer.start()

    window_event_writer = multiprocessing.Process(
        target=write_events,
        args=(
            "window",
            write_window_event,
            window_write_q,
            num_window_events,
            perf_q,
            recording_timestamp,
            terminate_processing,
            started_counter,
        ),
    )
    window_event_writer.start()

    if config.RECORD_VIDEO:
        expected_starts += 1
        video_writer = multiprocessing.Process(
            target=write_events,
            args=(
                "screen",
                write_video_event,
                video_write_q,
                num_video_events,
                perf_q,
                recording_timestamp,
                terminate_processing,
                started_counter,
                video_pre_callback,
                video_post_callback,
                "(video)",
            ),
        )
        video_writer.start()

    terminate_perf_event = multiprocessing.Event()
    perf_stat_writer = multiprocessing.Process(
        target=performance_stats_writer,
        args=(
            perf_q,
            recording_timestamp,
            terminate_perf_event,
            started_counter,
        ),
    )
    perf_stat_writer.start()

    if PLOT_PERFORMANCE:
        expected_starts += 1
        record_pid = os.getpid()
        mem_plotter = multiprocessing.Process(
            target=memory_writer,
            args=(
                recording_timestamp,
                terminate_perf_event,
                record_pid,
                started_counter,
            ),
        )
        mem_plotter.start()

    if log_memory:
        performance_snapshots = []
        _tracker = tracker.SummaryTracker()
        tracemalloc.start()
        collect_stats(performance_snapshots)

    # TODO: discard events until everything is ready

    # Wait for all to signal they've started
    while True:
        if started_counter.value >= expected_starts:
            break
        time.sleep(0.1)  # Sleep to reduce busy waiting
    for _ in range(5):
        logger.info("*" * 40)
    if status_pipe:
        status_pipe.send({"type": "record.started"})
    logger.info("All readers and writers have started. Waiting for input events...")

    global stop_sequence_detected

    try:
        while not (stop_sequence_detected or terminate_processing.is_set()):
            time.sleep(1)

        terminate_processing.set()
    except KeyboardInterrupt:
        terminate_processing.set()

    if status_pipe:
        status_pipe.send({"type": "record.stopping"})

    if log_memory:
        collect_stats(performance_snapshots)
        log_memory_usage(_tracker, performance_snapshots)

    logger.info("joining...")
    keyboard_event_reader.join()
    mouse_event_reader.join()
    screen_event_reader.join()
    window_event_reader.join()
    event_processor.join()
    screen_event_writer.join()
    action_event_writer.join()
    window_event_writer.join()
    if config.RECORD_VIDEO:
        video_writer.join()
    terminate_perf_event.set()

    if PLOT_PERFORMANCE:
        mem_plotter.join()
        utils.plot_performance(recording_timestamp)

    logger.info(f"Saved {recording_timestamp=}")

    if terminate_recording is not None:
        terminate_recording.set()

    # TODO: consolidate terminate_recording and status_pipe
    if status_pipe:
        status_pipe.send({"type": "record.stopped"})


# Entry point
def start() -> None:
    """Starts the recording process."""
    fire.Fire(record)


if __name__ == "__main__":
    fire.Fire(record)
