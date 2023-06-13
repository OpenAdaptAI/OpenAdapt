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
MAX_PIXEL_DIFF = 50
# define task to be at least 4 actions
MIN_TASK_LENGTH = 4


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


def compare_events(event1, event2):
    if event1.name == "click" and event2.name == "click":
        if is_within_margin(event1, event2, MAX_PIXEL_DIFF):
            return True
    elif event1.name == "press" and event2.name == "press":
        if event1.key == event2.key:
            return True
    return False


def find_tasks_brute_force(action_events):
    configure_logging(logger, LOG_LEVEL)
    logger.info("{} ActionEvents".format(len(action_events)))
    repetitions = {}
    # find candidates for start of a sequence that repeat within some error
    # store list of indices of repetitions
    logger.info("Finding candidates...")
    for i in range(0, len(action_events)):
        is_repeated = False
        for stored_event in repetitions:
            if compare_events(stored_event, action_events[i]):
                repetitions[stored_event].append(i)
                is_repeated = True
        if not is_repeated:
            repetitions[action_events[i]] = [i]

    candidates = [event for event, indices in repetitions.items() if len(indices) >= 2]

    logger.info("Found {} candidates".format(len(candidates)))

    tasks = []

    # compare candidate sequences
    # TODO: alternatively try hashing method again
    # TODO: optimize!!! O(n^4)
    logger.info("Finding sequences")
    for candidate in candidates:
        indices = repetitions[candidate]
        for i in range(0, len(indices)):
            # compare indices[i] with each next index
            # want to find largest parts that repeat
            index1 = indices[i]
            skip_to_next_j = False
            # TODO: if any found tasks are subtasks, only include the larger tasks
            # don't have to compare unless they're farther than MIN_TASK_LENGTH
            for j in range(i + 1, len(indices)):
                index2 = indices[j]
                if index2 < index1 + MIN_TASK_LENGTH:
                    continue
                possible_task = []
                if len(action_events) - index1 < MIN_TASK_LENGTH or \
                        len(action_events) - index2 < MIN_TASK_LENGTH:
                    break
                while index1 < len(action_events) and index2 < len(action_events):
                    if compare_events(action_events[index1], action_events[index2]):
                        possible_task.append(action_events[index1])
                    else:
                        skip_to_next_j = True
                        break
                if skip_to_next_j:
                    continue
                if len(possible_task) > MIN_TASK_LENGTH:
                    tasks.append(possible_task)
                    logger.info("Found potential task")

    return len(tasks)


def brents_algo(action_events):
    tortoise_index = 0
    hare_index = 1
    power = 1
    length = 1

    # Phase 1: Finding a cycle
    while hare_index < len(action_events) and \
            not compare_events(action_events[tortoise_index], action_events[hare_index]):
        if length == power:
            tortoise_index = hare_index
            power *= 2
            length = 0
        hare_index += 1
        length += 1

    if hare_index >= len(action_events):
        # No repeating subsequence found
        return None, 0

    # cycle detected
    # length = length of cycle
    # hare = tortoise
    tortoise_index = 0
    hare_index = length
    # Now move both pointers
    # at same speed so that
    # they meet at the
    # beginning of loop.
    while not compare_events(action_events[tortoise_index], action_events[hare_index]):
        tortoise_index += 1
        hare_index += 1

    # tortoise = hare = beginning of cycle

    return tortoise_index, length


def brents_count_repetitions(action_events, start, length):
    if start is None:
        return 0

    task = []
    for i in range(0, length):
        task.append(action_events[start + i])

    num_repetitions = 0
    task_index = 0
    for j in range(0, len(action_events)):
        if compare_events(action_events[j], task[task_index]):
            task_index += 1
        else:
            task_index = 0
        if task_index == len(task):
            num_repetitions += 1
            task_index = 0

    return num_repetitions


def brents_numbers(sequence):
    # just for testing purposes :)
    if len(sequence) == 0:
        return False

    first_p = 0
    second_p = 1
    power = 1
    length = 1

    while second_p < len(sequence) and sequence[second_p] != sequence[first_p]:
        if length == power:
            power *= 2
            length = 0
            first_p = second_p

        second_p += 1
        length += 1

    if second_p == len(sequence):
        print("No cycle")
        return None

    first_p = 0
    second_p = length

    while sequence[second_p] != sequence[first_p]:
        second_p += 1
        first_p += 1

    print("cycle found")
    return first_p, length


def find_tasks_cydets(action_events):
    # TODO: outdated, remove imports
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

    return len(cycles)


def filter_move_release(action_events):
    filtered_action_events = []
    for action_event in action_events:
        if action_event.name == "move":
            pass
        elif action_event.name == "release":
            pass
        elif action_event.name == "click" and not action_event.mouse_pressed:
            pass
        else:
            filtered_action_events.append(action_event)
    return filtered_action_events


def calculate_productivity():
    configure_logging(logger, LOG_LEVEL)

    recording = get_latest_recording()
    logger.debug(f"{recording=}")

    action_events = get_events(recording, process=PROCESS_EVENTS)
    event_dicts = rows2dicts(action_events)
    logger.info(f"event_dicts=\n{pformat(event_dicts)}")
    window_events = get_window_events(recording)
    filtered_action_events = filter_move_release(action_events)

    # overall info first
    gaps, time_in_gaps = find_gaps(action_events)
    num_clicks = find_clicks(action_events)
    num_key_presses = find_key_presses(action_events)
    duration = action_events[-1].timestamp - action_events[0].timestamp

    start, length = brents_algo(filtered_action_events)
    tasks = brents_count_repetitions(filtered_action_events, start, length)

    prod_info = {f"Number of pauses longer than {MAX_GAP_SECONDS} seconds": gaps,
                 "Total time spent during pauses": time_in_gaps,
                 "Total number of mouse clicks": num_clicks,
                 "Total number of key presses": num_key_presses,
                 "Number of windows/tabs used": len(window_events),
                 "Recording length": duration,
                 f"Number of repetitive tasks longer than {MIN_TASK_LENGTH} actions": tasks
                 }

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
                           "Time spent on this window/tab": window_duration,
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
    # sequence = [1, 2, 3, 4]
    # start, length = brents_numbers(sequence)
    # for i in range(0, length):
    #     print(sequence[start + i])
    calculate_productivity()
