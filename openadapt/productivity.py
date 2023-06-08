from pprint import pformat
from threading import Timer
import html
import os
import string

from bokeh.io import output_file, show
from bokeh.layouts import layout, row
from bokeh.models.widgets import Div
from loguru import logger

import pandas as pd
from cydets.algorithm import detect_cycles

from openadapt.crud import (
    get_latest_recording,
    get_window_events
)

from openadapt.events import (
    get_events,
)

from openadapt.utils import (
    configure_logging,
    display_event,
    evenly_spaced,
    image2utf8,
    EMPTY,
    row2dict,
    rows2dicts,
)

from openadapt.visualize import (
    IMG_WIDTH_PCT,
    MAX_TABLE_CHILDREN,
    MAX_EVENTS,
    dict2html
)

CSS = string.Template("""
    table {
        outline: 1px solid black;
    }
    table th {
        vertical-align: top;
    }
    .screenshot img {
        display: none;
        width: ${IMG_WIDTH_PCT}vw;
    }
    .screenshot img:nth-child(1) {
        display: block;
    }

    .screenshot:hover img:nth-child(1) {
        display: block;
    }
    .screenshot:active img:nth-child(1) {
        display: block;
    }
""").substitute(
    IMG_WIDTH_PCT=IMG_WIDTH_PCT,
)

MAX_GAP_SECONDS = 15
PROCESS_EVENTS = False
LOG_LEVEL = "INFO"
MAX_PIXEL_DIFF = 100


def find_gaps(action_events):
    num_gaps = 0
    time_in_gaps = 0
    # check every pair of action events for gap length
    for i in range(0, len(action_events) - 1):
        curr_gap = action_events[i + 1].screenshot_timestamp - action_events[i].screenshot_timestamp
        if curr_gap > MAX_GAP_SECONDS:
            num_gaps += 1
            time_in_gaps += curr_gap
    return num_gaps, time_in_gaps


def find_clicks(action_events):
    num_clicks = 0
    for action_event in action_events:
        if action_event.mouse_pressed:
            num_clicks += 1
    return num_clicks


def find_key_presses(action_events):
    num_key_presses = 0
    for action_event in action_events:
        if action_event.name == "press":
            num_key_presses += 1
    return num_key_presses


def is_within_margin(event1, event2, margin):
    return abs(event1.mouse_x - event2.mouse_x) <= margin and \
           abs(event1.mouse_y - event2.mouse_y) <= margin


def find_tasks_brute_force(action_events):
    repetitions = {}
    # find candidates that repeat within some error
    # store list of indices of repetitions
    for i in range(0, len(action_events)):
        is_repeated = False
        for stored_event in repetitions:
            if stored_event.name == "click" and action_events[i].name == "click":
                if is_within_margin(action_events[i], stored_event, MAX_PIXEL_DIFF):
                    repetitions[stored_event].append(i)
                    is_repeated = True
            elif stored_event.name == "press" and action_events[i].name == "press":
                if stored_event.key() == action_events[i].key():
                    repetitions[stored_event].append(i)
                    is_repeated = True
        if not is_repeated:
            repetitions[action_events[i]] = [i]

    candidates = [event for event, indices in repetitions.items() if len(indices) >= 2]


def find_tasks_cycles(action_events):
    data = []
    for action_event in action_events:
        # convert to real number
        if action_event.name == "press":
            if action_event.key_vk:
                data.append(int(action_event.key_vk))
            else:
                data.append(-1)
        elif action_event.name == "click":
            data.append(action_event.mouse_x)
            data.append(action_event.mouse_y)

    series = pd.Series(data)

    cycles = detect_cycles(series)

    print(data)

    print(cycles)


def filter_mouse_movement(action_events):
    filtered_action_events = []
    for action_event in action_events:
        if action_event.name != "move":
            filtered_action_events.append(action_event)


def calculate_productivity():
    configure_logging(logger, LOG_LEVEL)

    recording = get_latest_recording()
    logger.debug(f"{recording=}")

    action_events = get_events(recording, process=PROCESS_EVENTS)
    event_dicts = rows2dicts(action_events)
    logger.info(f"event_dicts=\n{pformat(event_dicts)}")
    window_events = get_window_events(recording)

    # overall info first
    gaps, time_in_gaps = find_gaps(action_events)
    num_clicks = find_clicks(action_events)
    num_key_presses = find_key_presses(action_events)
    duration = action_events[-1].timestamp - action_events[0].timestamp
    prod_info = {f"Number of pauses longer than {MAX_GAP_SECONDS} seconds": gaps,
                 "Total time spent during pauses": time_in_gaps,
                 "Total number of mouse clicks": num_clicks,
                 "Total number of key presses": num_key_presses,
                 "Number of window/tab changes": len(window_events),
                 "Recording length": duration}

    rows = [
        row(
            Div(
                text=f"<style>{CSS}</style>",
            ),
        ),
        row(
            Div(
                text=f"{dict2html(row2dict(recording))}",
            ),
        ),
        row(
            Div(
                text=f"{dict2html(prod_info)}"
            )
        )
    ]

    # window by window
    last_timestamp = -1
    curr_action_events = []
    for idx, action_event in enumerate(action_events):
        if idx == MAX_EVENTS:
            break
        curr_action_events.append(action_event)
        if action_event.window_event_timestamp != last_timestamp:
            last_timestamp = action_event.window_event_timestamp
            image = display_event(action_event)
            image_utf8 = image2utf8(image)
            width, height = image.size

            gaps, time_in_gaps = find_gaps(curr_action_events)
            num_clicks = find_clicks(curr_action_events)
            num_key_presses = find_key_presses(curr_action_events)
            window_duration = curr_action_events[-1].timestamp - curr_action_events[0].timestamp
            window_info = {f"Number of pauses longer than {MAX_GAP_SECONDS} seconds": gaps,
                           "Total time spent during pauses": time_in_gaps,
                           "Total number of mouse clicks": num_clicks,
                           "Total number of key presses": num_key_presses,
                           "Time spent on this window/tab": window_duration
                           }

            rows.append([
                row(
                    Div(
                        text=f"""
                            <div class="screenshot">
                                <img
                                    src="{image_utf8}"
                                    style="
                                        aspect-ratio: {width}/{height};
                                    "
                                >
                            </div>
                            <table>
                                {dict2html(row2dict(action_event.window_event), None)}
                            </table>
                        """,
                    ),
                    Div(
                        text=f"""
                            <table>
                                {dict2html(window_info)}
                            </table>
                        """
                    ),
                ),
            ])
            # flush curr_action_events
            curr_action_events = []
    # TODO: change the one at the bottom

    # display data
    title = f"Productivity metrics for recording-{recording.id}"
    fname_out = f"productivity-{recording.id}.html"
    logger.info(f"{fname_out=}")
    output_file(fname_out, title=title)

    result = show(
        layout(
            rows,
        )
    )

    def cleanup():
        os.remove(fname_out)
        removed = not os.path.exists(fname_out)
        logger.info(f"{removed=}")

    Timer(1, cleanup).start()


if __name__ == "__main__":
    calculate_productivity()
