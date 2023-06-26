from pprint import pformat
from threading import Timer
import html
import os
import string

from bokeh.io import output_file, show
from bokeh.layouts import layout, row
from bokeh.models.widgets import Div
from loguru import logger

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
MAX_PIXEL_DIFF = 75
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


def brents_algo(action_events):
    tortoise_index = 0
    hare_index = 1
    power = 1
    length = 1

    # Phase 1: Finding a cycle
    while hare_index < len(action_events) - 1:
        if compare_events(action_events[tortoise_index], action_events[hare_index]) and \
                compare_events(action_events[tortoise_index + 1], action_events[hare_index + 1]):
            break
        if length == power:
            tortoise_index = hare_index
            power *= 2
            length = 0
        hare_index += 1
        length += 1

    if hare_index >= len(action_events) - 1 and not compare_events(action_events[tortoise_index],
                                                                   action_events[hare_index]):
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


def find_num_tasks(action_events, start, length, task=None):
    if start is None:
        return 0, 0, 0

    if task is None:
        task = []
        for i in range(0, length):
            task.append(action_events[start + i])

    num_repetitions = 0
    task_index = 0
    total_time = 0
    start_time = 0
    end_time = 0
    for j in range(0, len(action_events)):
        if compare_events(action_events[j], task[task_index]):
            task_index += 1
        else:
            task_index = 0
        if task_index == len(task):
            # check if there's more that is the same after the last ActionEvent
            # completed another task
            num_repetitions += 1
            task_index = 0
            end_time = action_events[j].timestamp
            total_time += end_time - start_time
        if task_index == 1:
            # start of a task
            start_time = action_events[j].timestamp

    return task, num_repetitions, total_time


def floyds_algo(action_events):
    slow_index = 0
    fast_index = 0

    while slow_index < len(action_events) and fast_index < len(action_events) - 1:
        slow_index += 1
        fast_index += 2
        if compare_events(action_events[slow_index], action_events[fast_index]):
            break

    # if no loop exists
    if not compare_events(action_events[slow_index], action_events[fast_index]):
        return None, 0

    # meeting point can't be last event
    fast_index = slow_index

    # reset slow index to 0
    # and traverse again
    slow_index = 0
    # for i in range(0, meeting_point):
    #     if compare_events(action_events[i], action_events[meeting_point]):
    #         if compare_events(action_events[i + 1], action_events[meeting_point + 1]):
    #             fast_index = i

    while not compare_events(action_events[slow_index], action_events[fast_index]):
        slow_index += 1
        fast_index += 1

    # slow_index is start of cycle
    # find length/overestimate
    length = 1
    j = slow_index + 1
    while j < len(action_events) - 1:
        if compare_events(action_events[j], action_events[slow_index]) and \
                compare_events(action_events[j + 1], action_events[slow_index + 1]):
            break
        j += 1
        length += 1

    return slow_index, length


def longest_repeated_substring(action_events):
    n = len(action_events)
    # TODO: rename LCSRe
    LCSRe = [[0 for _ in range(n + 1)]
             for _ in range(n + 1)]

    result = []  # To store result
    res_length = 0  # To store length of result

    # building table in bottom-up manner
    index = 0
    for i in range(1, n + 1):
        for j in range(i + 1, n + 1):

            # (j-i) > LCSRe[i-1][j-1] to remove
            # overlapping
            if (compare_events(action_events[i - 1], action_events[j - 1]) and
                    LCSRe[i - 1][j - 1] < (j - i)):
                LCSRe[i][j] = LCSRe[i - 1][j - 1] + 1

                # updating maximum length of the
                # substring and updating the finishing
                # index of the suffix
                if LCSRe[i][j] > res_length:
                    res_length = LCSRe[i][j]
                    index = max(i, index)

            else:
                LCSRe[i][j] = 0

    # If we have non-empty result, then insert
    # all characters from first character to
    # last character of string
    if res_length > 0:
        for i in range(index - res_length + 1,
                       index + 1):
            result.append(action_events[i - 1])
    else:
        return result, None, res_length

    return result, index - res_length + 1, res_length


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


def find_errors(action_events):
    # TODO: how to find click errors
    errors = 0
    for i in range(0, len(action_events)):
        if action_events[i].canonical_key_name == 'backspace' or \
                action_events[i].canonical_key_name == 'delete':
            errors += 1
        elif action_events[i].canonical_key_name == 'ctrl':
            if i < len(action_events) - 1 and action_events[i + 1].canonical_key_char == 'z':
                errors += 1
    return errors


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

    logger.info("searching for tasks")
    task, start, length = longest_repeated_substring(filtered_action_events)
    _, num_tasks, total_task_time = find_num_tasks(filtered_action_events, start, length, task)
    logger.info("finished searching for tasks")
    if num_tasks != 0:
        ave_task_time = total_task_time / num_tasks
    else:
        ave_task_time = 0
    errors = find_errors(filtered_action_events)

    prod_info = {f"Number of pauses longer than {MAX_GAP_SECONDS} seconds": gaps,
                 "Total time spent during pauses": time_in_gaps,
                 "Total number of mouse clicks": num_clicks,
                 "Total number of key presses": num_key_presses,
                 "Number of windows/tabs used": len(window_events),
                 "Recording length": duration,
                 f"Number of repetitive tasks longer than {MIN_TASK_LENGTH} actions": num_tasks,
                 "Total time spent on repetitive tasks": total_task_time,
                 "Average time spent per repetitive task": ave_task_time,
                 "Number of errors": errors
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

    # task screenshots
    if num_tasks > 0:
        if len(task) > 0:
            rows.append([
                row(
                    Div(
                        text="<hr><h1>Identified Task</h1><hr>"
                    )
                )
            ])

        for event in task:
            screenshot = display_event(event)
            screenshot_utf8 = image2utf8(screenshot)
            width, height = screenshot.size

            rows.append([
                row(
                    Div(
                        text=f"""
                            <div class="screenshot">
                                <img
                                    src="{screenshot_utf8}"
                                    style="
                                        aspect-ratio: {width}/{height};
                                    "
                                >
                            </div>
                        """,
                    ),
                ),
            ])

    # window by window
    if len(window_events) > 0:
        rows.append([
            row(
                Div(
                    text="<hr><h1>Windows/Tabs</h1><hr>"
                )
            )
        ])

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
