"""Script for creating Recordings.

Usage:

    $ python puterbot/record.py "<description of task to be recorded>"

"""

from collections import Counter, defaultdict, namedtuple
from functools import partial
from typing import Any, Callable, Dict
import multiprocessing
import os
import queue
import signal
import sys
import threading
import time

from loguru import logger
from pynput import keyboard, mouse
import fire
import matplotlib.pyplot as plt
import mss.tools
import pygetwindow as pgw

from puterbot.crud import (
    insert_input_event,
    insert_screenshot,
    insert_recording,
    insert_window_event,
    insert_perf_stat,
    get_perf_stats,
)
from puterbot.utils import (
    configure_logging,
    get_double_click_distance_pixels,
    get_double_click_interval_seconds,
    get_monitor_dims,
    take_screenshot,
    get_timestamp,
    set_start_time,
    rows2dicts,
)


EVENT_TYPES = ("screen", "input", "window")
LOG_LEVEL = "INFO"
PROC_WRITE_BY_EVENT_TYPE = {
    "screen": True,
    "input": True,
    "window": True,
}
DIRNAME_PERFORMANCE_PLOTS = "performance"
PLOT_PERFORMANCE = False


Event = namedtuple("Event", ("timestamp", "type", "data"))


def process_event(event, write_q, write_fn, recording_timestamp, perf_q):
    if PROC_WRITE_BY_EVENT_TYPE[event.type]:
        write_q.put(event)
    else:
        write_fn(recording_timestamp, event, perf_q)


def process_events(
    event_q: queue.Queue,
    screen_write_q: multiprocessing.Queue,
    input_write_q: multiprocessing.Queue,
    window_write_q: multiprocessing.Queue,
    perf_q: multiprocessing.Queue,
    recording_timestamp: float,
    terminate_event: multiprocessing.Event,
):
    """
    Process events from event queue and write them to respective write queues.

    Args:
        event_q: A queue with events to be processed.
        screen_write_q: A queue for writing screen events.
        input_write_q: A queue for writing input events.
        window_write_q: A queue for writing window events.
        perf_q: A queue for collecting performance data.
        recording_timestamp: The timestamp of the recording.
        terminate_event: An event to signal the termination of the process.
    """

    configure_logging(logger, LOG_LEVEL)
    set_start_time(recording_timestamp)
    logger.info(f"starting")

    prev_event = None
    prev_screen_event = None
    prev_window_event = None
    prev_saved_screen_timestamp = 0
    prev_saved_window_timestamp = 0
    while not terminate_event.is_set() or not event_q.empty():
        event = event_q.get()
        logger.debug(f"{event=}")
        assert event.type in EVENT_TYPES, event
        if prev_event is not None:
            assert event.timestamp > prev_event.timestamp, (event, prev_event)
        if event.type == "screen":
            prev_screen_event = event
        elif event.type == "window":
            prev_window_event = event
        elif event.type == "input":
            if prev_screen_event is None:
                logger.warning("discarding input that came before screen")
                continue
            if prev_window_event is None:
                logger.warning("discarding input that came before window")
                continue
            event.data["screenshot_timestamp"] = prev_screen_event.timestamp
            event.data["window_event_timestamp"] = prev_window_event.timestamp
            process_event(
                event,
                input_write_q,
                write_input_event,
                recording_timestamp,
                perf_q,
            )
            if prev_saved_screen_timestamp < prev_screen_event.timestamp:
                process_event(
                    prev_screen_event,
                    screen_write_q,
                    write_screen_event,
                    recording_timestamp,
                    perf_q,
                )
                prev_saved_screen_timestamp = prev_screen_event.timestamp
            if prev_saved_window_timestamp < prev_window_event.timestamp:
                process_event(
                    prev_window_event,
                    window_write_q,
                    write_window_event,
                    recording_timestamp,
                    perf_q,
                )
                prev_saved_window_timestamp = prev_window_event.timestamp
        else:
            raise Exception(f"unhandled {event.type=}")
        prev_event = event
    logger.info("done")


def write_input_event(
    recording_timestamp: float,
    event: Event,
    perf_q: multiprocessing.Queue,
):
    """
    Write an input event to the database and update the performance queue.

    Args:
        recording_timestamp: The timestamp of the recording.
        event: An input event to be written.
        perf_q: A queue for collecting performance data.
    """

    assert event.type == "input", event
    insert_input_event(recording_timestamp, event.timestamp, event.data)
    perf_q.put((event.type, event.timestamp, get_timestamp()))


def write_screen_event(
    recording_timestamp: float,
    event: Event,
    perf_q: multiprocessing.Queue,
):
    """
    Write a screen event to the database and update the performance queue.

    Args:
        recording_timestamp: The timestamp of the recording.
        event: A screen event to be written.
        perf_q: A queue for collecting performance data.
    """

    assert event.type == "screen", event
    screenshot = event.data
    png_data = mss.tools.to_png(screenshot.rgb, screenshot.size)
    event_data = {"png_data": png_data}
    insert_screenshot(recording_timestamp, event.timestamp, event_data)
    perf_q.put((event.type, event.timestamp, get_timestamp()))


def write_window_event(
    recording_timestamp: float,
    event: Event,
    perf_q: multiprocessing.Queue,
):
    """
    Write a window event to the database and update the performance queue.

    Args:
        recording_timestamp: The timestamp of the recording.
        event: A window event to be written.
        perf_q: A queue for collecting performance data.
    """

    assert event.type == "window", event
    insert_window_event(recording_timestamp, event.timestamp, event.data)
    perf_q.put((event.type, event.timestamp, get_timestamp()))


def write_events(
    event_type: str,
    write_fn: Callable,
    write_q: multiprocessing.Queue,
    perf_q: multiprocessing.Queue,
    recording_timestamp: float,
    terminate_event: multiprocessing.Event,
):
    """
    Write events of a specific type to the db using the provided write function.

    Args:
        event_type: The type of events to be written.
        write_fn: A function to write events to the database.
        write_q: A queue with events to be written.
        perf_q: A queue for collecting performance data.
        recording_timestamp: The timestamp of the recording.
        terminate_event: An event to signal the termination of the process.
    """

    configure_logging(logger, LOG_LEVEL)
    set_start_time(recording_timestamp)
    logger.info(f"{event_type=} starting")
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    while not terminate_event.is_set() or not write_q.empty():
        try:
            event = write_q.get_nowait()
        except queue.Empty:
            continue
        assert event.type == event_type, (event_type, event)
        write_fn(recording_timestamp, event, perf_q)
    logger.info(f"{event_type=} done")


def trigger_input_event(
    event_q: queue.Queue,
    input_event_args: Dict[str, Any],
) -> None:

    event_q.put(Event(get_timestamp(), "input", input_event_args))


def on_move(
    event_q: queue.Queue,
    x: int,
    y: int,
    injected: bool,
) -> None:

    logger.debug(f"{x=} {y=} {injected=}")
    if not injected:
        trigger_input_event(
            event_q,
            {
                "name": "move",
                "mouse_x": x,
                "mouse_y": y,
            }
        )


def on_click(
    event_q: queue.Queue,
    x: int,
    y: int,
    button: mouse.Button,
    pressed: bool,
    injected: bool,
) -> None:
    logger.debug(f"{x=} {y=} {button=} {pressed=} {injected=}")
    if not injected:
        trigger_input_event(
            event_q,
            {
                "name": "click",
                "mouse_x": x,
                "mouse_y": y,
                "mouse_button_name": button.name,
                "mouse_pressed": pressed,
            }
        )


def on_scroll(
    event_q: queue.Queue,
    x: int,
    y: int,
    dx: int,
    dy: int,
    injected: bool,
) -> None:
    logger.debug(f"{x=} {y=} {dx=} {dy=} {injected=}")
    if not injected:
        trigger_input_event(
            event_q,
            {
                "name": "scroll",
                "mouse_x": x,
                "mouse_y": y,
                "mouse_dx": dx,
                "mouse_dy": dy,
            }
        )


def handle_key(
    event_q: queue.Queue,
    event_name: str,
    key: keyboard.KeyCode,
    canonical_key: keyboard.KeyCode,
) -> None:
    attr_names = [
        "name",
        "char",
        "vk",
    ]
    attrs = {
        f"key_{attr_name}": getattr(key, attr_name, None)
        for attr_name in attr_names
    }
    logger.debug(f"{attrs=}")
    canonical_attrs = {
        f"canonical_key_{attr_name}": getattr(canonical_key, attr_name, None)
        for attr_name in attr_names
    }
    logger.debug(f"{canonical_attrs=}")
    trigger_input_event(
        event_q,
        {
            "name": event_name,
            **attrs,
            **canonical_attrs
        }
    )


def read_screen_events(
    event_q: queue.Queue,
    terminate_event: multiprocessing.Event,
    recording_timestamp: float,
) -> None:
    """
    Read screen events and add them to the event queue.

    Args:
        event_q: A queue for adding screen events.
        terminate_event: An event to signal the termination of the process.
        recording_timestamp: The timestamp of the recording.
    """

    configure_logging(logger, LOG_LEVEL)
    set_start_time(recording_timestamp)
    logger.info(f"starting")
    while not terminate_event.is_set():
        screenshot = take_screenshot()
        if screenshot is None:
            logger.warning("screenshot was None")
            continue
        event_q.put(Event(get_timestamp(), "screen", screenshot))
    logger.info("done")


def read_window_events(
    event_q: queue.Queue,
	terminate_event: multiprocessing.Event,
	recording_timestamp: float,
) -> None:
    """
    Read window events and add them to the event queue.

    Args:
        event_q: A queue for adding window events.
        terminate_event: An event to signal the termination of the process.
        recording_timestamp: The timestamp of the recording.
    """

    configure_logging(logger, LOG_LEVEL)
    set_start_time(recording_timestamp)
    logger.info(f"starting")
    prev_title = None
    prev_geometry = None
    while not terminate_event.is_set():
        # TODO: save window identifier (a window's title can change, or
        # multiple windows can have the same title)
        if sys.platform == "darwin":
            # pywinctl performance on mac is unusable, see:
            # https://github.com/Kalmat/PyWinCtl/issues/29
            title = pgw.getActiveWindow()
            geometry = pgw.getWindowGeometry(title)
            if geometry is None:
                logger.warning(f"{geometry=}")
                continue
        else:
            window = pgw.getActiveWindow()
            if not window:
                logger.warning(f"{window=}")
                continue
            title = window.title
            geometry = window.box
        if title != prev_title or geometry != prev_geometry:

            # TODO: fix exception sometimes triggered by the next line on win32:
            #   File "\Python39\lib\threading.py" line 917, in run
            #   File "...\puterbot\record.py", line 277, in read window events
            #   File "...\env\lib\site-packages\loguru\logger.py" line 1977, in info
            #   File "...\env\lib\site-packages\loguru\_logger.py", line 1964, in _log
            #       for handler in core.handlers.values):
            #   RuntimeError: dictionary changed size during iteration
            logger.info(f"{title=} {prev_title=} {geometry=} {prev_geometry=}")

            left, top, width, height = geometry
            event_q.put(Event(
                get_timestamp(),
                "window",
                {
                    "title": title,
                    "left": left,
                    "top": top,
                    "width": width,
                    "height": height,
                }
            ))
        prev_title = title
        prev_geometry = geometry


def performance_stats_writer (
    perf_q: multiprocessing.Queue,
    recording_timestamp: float,
    terminate_event: multiprocessing.Event,
):
    """
    Write performance stats to the db.
    Each entry includes the event type, start time and end time

    Args:
        perf_q: A queue for collecting performance data.
        recording_timestamp: The timestamp of the recording.
        terminate_event: An event to signal the termination of the process.
    """

    configure_logging(logger, LOG_LEVEL)
    set_start_time(recording_timestamp)
    logger.info("performance stats writer starting")
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    while not terminate_event.is_set() or not perf_q.empty():
        try:
            event_type, start_time, end_time = perf_q.get_nowait()
        except queue.Empty:
            continue

        insert_perf_stat(recording_timestamp, event_type, start_time, end_time)
    logger.info("performance stats writer done")

def plot_performance(recording_timestamp: float) -> None:
    """
    Plot the performance of the event processing and writing.

    Args:
        recording_timestamp: The timestamp of the recording.
        perf_q: A queue with performance data.
    """

    type_to_prev_start_time = defaultdict(list)
    type_to_start_time_deltas = defaultdict(list)
    type_to_proc_times = defaultdict(list)
    type_to_count = Counter()
    type_to_timestamps = defaultdict(list)

    perf_stats = get_perf_stats(recording_timestamp)
    perf_stat_dicts = rows2dicts(perf_stats)
    for perf_stat in perf_stat_dicts:
        prev_start_time = type_to_prev_start_time.get(perf_stat["event_type"], perf_stat["start_time"])
        start_time_delta = perf_stat["start_time"] - prev_start_time
        type_to_start_time_deltas[perf_stat["event_type"]].append(start_time_delta)
        type_to_prev_start_time[perf_stat["event_type"]] = perf_stat["start_time"]
        type_to_proc_times[perf_stat["event_type"]].append(perf_stat["end_time"] - perf_stat["start_time"])
        type_to_count[perf_stat["event_type"]] += 1
        type_to_timestamps[perf_stat["event_type"]].append(perf_stat["start_time"])

    y_data = {"proc_times": {}, "start_time_deltas": {}}
    for i, event_type in enumerate(type_to_count):
        type_count = type_to_count[event_type]
        start_time_deltas = type_to_start_time_deltas[event_type]
        proc_times = type_to_proc_times[event_type]
        y_data["proc_times"][event_type] = proc_times
        y_data["start_time_deltas"][event_type] = start_time_deltas

    fig, axes = plt.subplots(2, 1, sharex=True, figsize=(20,10))
    for i, data_type in enumerate(y_data):
        for event_type in y_data[data_type]:
            x = type_to_timestamps[event_type]
            y = y_data[data_type][event_type]
            axes[i].scatter(x, y, label=event_type)
        axes[i].set_title(data_type)
        axes[i].legend()
    # TODO: add PROC_WRITE_BY_EVENT_TYPE
    fname_parts = ["performance", f"{recording_timestamp}"]
    fname = "-".join(fname_parts) + ".png"
    os.makedirs(DIRNAME_PERFORMANCE_PLOTS, exist_ok=True)
    fpath = os.path.join(DIRNAME_PERFORMANCE_PLOTS, fname)
    logger.info(f"{fpath=}")
    plt.savefig(fpath)
    os.system(f"open {fpath}")


def create_recording(
    task_description: str,
) -> Dict[str, Any]:
    """
    Create a new recording entry in the database.

    Args:
        task_description: a text description of the task being implemented
            in the recording

    Returns:
        The newly created Recording object
    """

    timestamp = set_start_time()
    monitor_width, monitor_height = get_monitor_dims()
    double_click_distance_pixels = get_double_click_distance_pixels()
    double_click_interval_seconds = get_double_click_interval_seconds()
    recording_data = {
        "timestamp": timestamp,
        "monitor_width": monitor_width,
        "monitor_height": monitor_height,
        "double_click_distance_pixels": double_click_distance_pixels,
        "double_click_interval_seconds": double_click_interval_seconds,
        "platform": sys.platform,
        "task_description": task_description,
    }
    recording = insert_recording(recording_data)
    logger.info(f"{recording=}")
    return recording


def read_keyboard_events(
    event_q: queue.Queue,
	terminate_event: multiprocessing.Event,
	recording_timestamp: float,
) -> None:


    def on_press(event_q, key, injected):
        canonical_key = keyboard_listener.canonical(key)
        logger.debug(f"{key=} {injected=} {canonical_key=}")
        if not injected:
            handle_key(event_q, "press", key, canonical_key)


    def on_release(event_q, key, injected):
        canonical_key = keyboard_listener.canonical(key)
        logger.debug(f"{key=} {injected=} {canonical_key=}")
        if not injected:
            handle_key(event_q, "release", key, canonical_key)


    set_start_time(recording_timestamp)
    keyboard_listener = keyboard.Listener(
        on_press=partial(on_press, event_q),
        on_release=partial(on_release, event_q),
    )
    keyboard_listener.start()
    terminate_event.wait()
    keyboard_listener.stop()


def read_mouse_events(
    event_q: queue.Queue,
    terminate_event: multiprocessing.Event,
    recording_timestamp: float,
) -> None:
    set_start_time(recording_timestamp)
    mouse_listener = mouse.Listener(
        on_move=partial(on_move, event_q),
        on_click=partial(on_click, event_q),
        on_scroll=partial(on_scroll, event_q),
    )
    mouse_listener.start()
    terminate_event.wait()
    mouse_listener.stop()


def record(
    task_description: str,
):
    """
    Record Screenshots/InputEvents/WindowEvents.

    Args:
        task_description: a text description of the task that will be recorded
    """

    configure_logging(logger, LOG_LEVEL)

    logger.info(f"{task_description=}")

    recording = create_recording(task_description)
    recording_timestamp = recording.timestamp

    event_q = queue.Queue()
    screen_write_q = multiprocessing.Queue()
    input_write_q = multiprocessing.Queue()
    window_write_q = multiprocessing.Queue()
    # TODO: save write times to DB; display performance plot in visualize.py
    perf_q = multiprocessing.Queue()
    terminate_event = multiprocessing.Event()

    window_event_reader = threading.Thread(
        target=read_window_events,
        args=(event_q, terminate_event, recording_timestamp),
    )
    window_event_reader.start()

    screen_event_reader = threading.Thread(
        target=read_screen_events,
        args=(event_q, terminate_event, recording_timestamp),
    )
    screen_event_reader.start()

    keyboard_event_reader = threading.Thread(
        target=read_keyboard_events,
        args=(event_q, terminate_event, recording_timestamp),
    )
    keyboard_event_reader.start()

    mouse_event_reader = threading.Thread(
        target=read_mouse_events,
        args=(event_q, terminate_event, recording_timestamp),
    )
    mouse_event_reader.start()

    event_processor = threading.Thread(
        target=process_events,
        args=(
            event_q,
            screen_write_q,
            input_write_q,
            window_write_q,
            perf_q,
            recording_timestamp,
            terminate_event,
        ),
    )
    event_processor.start()

    screen_event_writer = multiprocessing.Process(
        target=write_events,
        args=(
            "screen",
            write_screen_event,
            screen_write_q,
            perf_q,
            recording_timestamp,
            terminate_event,
        ),
    )
    screen_event_writer.start()

    input_event_writer = multiprocessing.Process(
        target=write_events,
        args=(
            "input",
            write_input_event,
            input_write_q,
            perf_q,
            recording_timestamp,
            terminate_event,
        ),
    )
    input_event_writer.start()

    window_event_writer = multiprocessing.Process(
        target=write_events,
        args=(
            "window",
            write_window_event,
            window_write_q,
            perf_q,
            recording_timestamp,
            terminate_event,
        ),
    )
    window_event_writer.start()

    terminate_perf_event = multiprocessing.Event()
    perf_stat_writer = multiprocessing.Process(
        target=performance_stats_writer,
        args=(
            perf_q,
            recording_timestamp,
            terminate_perf_event,
        ),
    )
    perf_stat_writer.start()

    # TODO: discard events until everything is ready

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        terminate_event.set()

    logger.info(f"joining...")
    keyboard_event_reader.join()
    mouse_event_reader.join()
    screen_event_reader.join()
    window_event_reader.join()
    event_processor.join()
    screen_event_writer.join()
    input_event_writer.join()
    window_event_writer.join()

    terminate_perf_event.set()

    if PLOT_PERFORMANCE:
        plot_performance(recording_timestamp)

    logger.info("done")

if __name__ == "__main__":
    fire.Fire(record)
