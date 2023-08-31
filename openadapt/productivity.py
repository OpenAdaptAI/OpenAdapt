"""This module generates an HTML page.

The page has information about the productivity of the user in the latest recording.

Usage:

    $ python -m openadapt.productivity
"""

from pprint import pformat
from threading import Timer
from typing import Optional, Tuple
import os
import string

from bokeh.io import output_file, show
from bokeh.layouts import layout, row
from bokeh.models.widgets import Div
from loguru import logger

from openadapt.db.crud import get_latest_recording, get_window_events
from openadapt.events import get_events
from openadapt.models import ActionEvent, WindowEvent
from openadapt.utils import (
    configure_logging,
    display_event,
    image2utf8,
    row2dict,
    rows2dicts,
)
from openadapt.visualize import IMG_WIDTH_PCT, MAX_EVENTS, dict2html

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


def find_gaps(action_events: list[ActionEvent]) -> Tuple[int, float]:
    """Find and count gaps between ActionEvents that are longer than MAX_GAP_SECONDS.

    Args:
        action_events (list[ActionEvent]): A list of ActionEvent objects.

    Returns:
        tuple: A tuple containing two elements:
            - num_gaps (int): The number of gaps found between action events.
            - time_in_gaps (float): The total time spent in the gaps (in seconds).
    """
    num_gaps = 0
    time_in_gaps = 0
    # check every pair of action events for gap length
    for i in range(0, len(action_events) - 1):
        curr_gap = action_events[i + 1].timestamp - action_events[i].timestamp
        if curr_gap > MAX_GAP_SECONDS:
            num_gaps += 1
            time_in_gaps += curr_gap
    return num_gaps, time_in_gaps


def find_clicks(action_events: list[ActionEvent]) -> int:
    """Count the number of mouse clicks in a list of ActionEvents.

    Args:
        action_events (list[ActionEvent]): A list of ActionEvent objects.

    Returns:
        int: The total number of mouse clicks found in the ActionEvents.
    """
    num_clicks = 0
    for action_event in action_events:
        if action_event.mouse_pressed:
            num_clicks += 1
    return num_clicks


def find_key_presses(action_events: list[ActionEvent]) -> int:
    """Count the number of key presses in a list of ActionEvents.

    Args:
        action_events (list[ActionEvent]): A list of ActionEvent objects.

    Returns:
        int: The total number of key presses found in the ActionEvents.
    """
    num_key_presses = 0
    for action_event in action_events:
        if action_event.name == "press":
            num_key_presses += 1
    return num_key_presses


def is_within_margin(event1: ActionEvent, event2: ActionEvent, margin: int) -> bool:
    """Check if two mouse events are within a specified pixel distance from each other.

    Args:
        event1 (ActionEvent): The first ActionEvent.
        event2 (ActionEvent): The second ActionEvent.
        margin (int): The maximum allowable distance in pixels between the mouse
        coordinates of the two events for them to be considered the same event.

    Returns:
        bool: True if the distance between the mouse coordinates
        of the events is within the specified margin, False otherwise.
    """
    return (
        abs(event1.mouse_x - event2.mouse_x) <= margin
        and abs(event1.mouse_y - event2.mouse_y) <= margin
    )


def compare_events(event1: ActionEvent, event2: ActionEvent) -> bool:
    """Compare two action events.

    To determine if they are similar enough to be considered the same.

    For mouse events, clicks must be a within some distance of each other. For
    key presses, the keys must be the same.

    Args:
        event1 (ActionEvent): The first ActionEvent object to be compared.
        event2 (ActionEvent): The second ActionEvent object to be compared.

    Returns:
        bool: True if the two events are similar, False otherwise.
    """
    if event1.name == "click" and event2.name == "click":
        if is_within_margin(event1, event2, MAX_PIXEL_DIFF):
            return True
    elif event1.name == "press" and event2.name == "press":
        if event1.key == event2.key:
            return True
    return False


def find_num_tasks(
    action_events: list[ActionEvent],
    start: ActionEvent,
    length: int,
    task: Optional[list[ActionEvent]] = None,
) -> Tuple[list[ActionEvent], int, float]:
    """Find the num of times a task is repeated and how much time is spent on the task.

    Given a list of ActionEvents, the start of a repeating task,
    the length of the task, and optionally the identified task,
    verify that the task repeats (and if not, find the correct repeating task),
    find how many times the task is repeated,
    and how much time in total is spent repeating the task.

    Args:
        action_events (List[ActionEvent]): A list of ActionEvents.
        start (ActionEvent): The starting ActionEvent of the task.
        length (int): The number of ActionEvents in the identified task.
        task (Optional[ActionEvent]):
        An optional task identified by the search algorithm.

    Returns:
        list[ActionEvent]: The final verified task.
        int: The number of repetitions.
        float: The total time in seconds spent on repeating the task.
    """
    if start is None:
        return [], 0, 0

    if task is None:
        task = []
        for i in range(0, length):
            task.append(action_events[start + i])

    if len(task) < MIN_TASK_LENGTH:
        return [], 0, 0

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
        if task_index == 1:
            # start of a task
            start_time = action_events[j].timestamp
        if task_index == len(task):
            # TODO: check if there's more that is the same after the last ActionEvent
            # completed another task
            num_repetitions += 1
            task_index = 0
            end_time = action_events[j].timestamp
            total_time = total_time + end_time - start_time

    if num_repetitions == 1:
        num_repetitions = 0
        task_index = 0
        curr_len = len(task)

        # find the real task
        for j in range(0, len(action_events)):
            if compare_events(action_events[j], task[task_index]):
                task_index += 1
            else:
                if task_index > MIN_TASK_LENGTH:
                    # tasks are the same up to task_index - 1
                    num_to_pop = len(task) - task_index
                    for _ in range(0, num_to_pop):
                        task.pop()
                task_index = 0
            if task_index == len(task):
                # completed another task
                num_repetitions += 1
                task_index = 0

        if len(task) == curr_len:
            # nothing changed, so couldn't find a long enough task
            return [], 0, 0

        # find time with new task
        task_index = 0
        total_time = 0
        start_time = 0
        end_time = 0
        for j in range(0, len(action_events)):
            if compare_events(action_events[j], task[task_index]):
                task_index += 1
            else:
                task_index = 0
            if task_index == 1:
                # start of a task
                start_time = action_events[j].timestamp
            if task_index == len(task):
                # completed another task
                task_index = 0
                end_time = action_events[j].timestamp
                total_time = total_time + end_time - start_time

    return task, num_repetitions, total_time


def rec_lrs(
    action_events: list[ActionEvent],
) -> Tuple[list[ActionEvent], Optional[ActionEvent], int]:
    """A function to find the longest repeating non-overlapping task of ActionEvents.

    Caller function that calls longest_repeated_substring recursively to find the
    longest repeating non-overlapping task of ActionEvents
    from the original action_events.

    Args:
        action_events (List[ActionEvent]): A list of ActionEvents.

    Returns:
        list[ActionEvent]: identified task.
        ActionEvent: start of task.
        int: length of task.
    """
    len_orig = len(action_events)
    while True:
        task, start, length = longest_repeated_substring(action_events)
        # length is either 0 or the length of a repeating task
        if length < MIN_TASK_LENGTH:
            if len(action_events) != len_orig:
                # a task shorter than len_orig was found in the last iteration
                return action_events, start, length
            else:
                # no tasks longer than MIN_TASK_LENGTH exist
                return [], None, 0
        action_events = task


def longest_repeated_substring(
    action_events: list[ActionEvent],
) -> Tuple[list[ActionEvent], Optional[ActionEvent], int]:
    """A function to find the longest repeating non-overlapping task of ActionEvents.

    Recursive function to find the longest repeating non-overlapping task of
    ActionEvents from the original action_events. Based on algorithm found at
    this link:
    https://www.geeksforgeeks.org/longest-repeating-and-non-overlapping-substring/

    Args:
        action_events (List[ActionEvent]): A list of ActionEvents.

    Returns:
        list[ActionEvent]: identified task.
        ActionEvent: start of task.
        int: length of task.
    """
    n = len(action_events)

    # table_of_max_lengths[i][j] stores length of the matching and
    # non-overlapping substrings ending with i'th and j'th events
    table_of_max_lengths = [[0 for _ in range(n + 1)] for _ in range(n + 1)]

    result = []  # To store result
    res_length = 0  # To store length of result

    # building table in bottom-up manner
    index = 0
    for i in range(1, n + 1):
        for j in range(i + 1, n + 1):
            if compare_events(
                action_events[i - 1], action_events[j - 1]
            ) and table_of_max_lengths[i - 1][j - 1] < (j - i):
                table_of_max_lengths[i][j] = table_of_max_lengths[i - 1][j - 1] + 1

                # updating maximum length of the
                # substring and updating the finishing
                # index of the suffix
                if table_of_max_lengths[i][j] > res_length:
                    res_length = table_of_max_lengths[i][j]
                    index = max(i, index)

            else:
                table_of_max_lengths[i][j] = 0

    # If we have non-empty result, then insert
    # all characters from first character to
    # last character of string
    if res_length > 0:
        for i in range(index - res_length + 1, index + 1):
            result.append(action_events[i - 1])
    else:
        return [], None, 0

    return result, index - res_length + 1, res_length


def filter_move_release(action_events: list[ActionEvent]) -> list[ActionEvent]:
    """Filter out any events that aren't clicks and key presses.

    Args:
        action_events (list[ActionEvent]): list of ActionEvents to be filtered

    Returns:
        list[ActionEvent]: list of ActionEvents containing only clicks and key presses
    """
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


def find_errors(action_events: list[ActionEvent]) -> int:
    """Currently unused as there is no good way to find errors.

    Args:
        action_events (list[ActionEvent]): list of ActionEvents.

    Returns:
        int: number of errors.
    """
    # TODO: how to find click errors
    errors = 0
    for i in range(0, len(action_events)):
        if (
            action_events[i].canonical_key_name == "backspace"
            or action_events[i].canonical_key_name == "delete"
        ):
            errors += 1
        elif action_events[i].canonical_key_name == "ctrl":
            if (
                i < len(action_events) - 1
                and action_events[i + 1].canonical_key_char == "z"
            ):
                errors += 1
    return errors


def find_num_window_tab_changes(window_events: list[WindowEvent]) -> int:
    """Find the number of times a user switches between tabs or applications.

    Args:
        window_events (list[WindowEvent]): list of WindowEvents.

    Return:
        int: number of tab/application switches.
    """
    num_window_tab_changes = 0

    if len(window_events) < 2:
        return 0

    for i in range(0, len(window_events) - 1):
        curr = window_events[i]
        next = window_events[i + 1]
        if curr.title != "" and curr.title != next.title:
            num_window_tab_changes += 1

    if window_events[-1].title != "":
        num_window_tab_changes += 1

    return num_window_tab_changes - 1


def calculate_productivity() -> None:
    """A function to calculate productivity metrics.

    Calculate any relevant information
    about the productivity of a user in the latest recording.
    Display this information in an HTML page and open the page.

    Args:
        None

    Returns:
        None
    """
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
    tab_changes = find_num_window_tab_changes(window_events)

    task, start, length = rec_lrs(filtered_action_events)
    final_task, num_tasks, total_task_time = find_num_tasks(
        filtered_action_events, start, length, task
    )
    if num_tasks != 0:
        ave_task_time = total_task_time / num_tasks
    else:
        ave_task_time = 0
    # errors = find_errors(filtered_action_events)

    prod_info = {
        f"Number of pauses longer than {MAX_GAP_SECONDS} seconds": gaps,
        "Total time spent during pauses": time_in_gaps,
        "Total number of mouse clicks": num_clicks,
        "Total number of key presses": num_key_presses,
        "Number of window/tab switches": tab_changes,
        "Recording length": duration,
        f"Number of repetitive tasks longer than {MIN_TASK_LENGTH} actions": num_tasks,
        "Number of key presses and mouse clicks in identified task": len(final_task),
        "Total time spent on repetitive tasks": total_task_time,
        "Average time spent per repetitive task": ave_task_time,
        # "Number of errors": errors
    }

    rows = [
        row(
            Div(
                text=f"<style>{CSS}</style>",
            ),
        ),
        row(
            Div(
                text="<hr><h1>Productivity Metrics</h1><hr>",
            ),
        ),
        row(
            Div(
                text=f"{dict2html(row2dict(recording))}",
            ),
        ),
        row(Div(text=f"{dict2html(prod_info)}")),
    ]

    # task screenshots
    if num_tasks > 0:
        if len(task) > 0:
            rows.append([row(Div(text="<hr><h1>Identified Task</h1><hr>"))])

        for event in final_task:
            screenshot = display_event(event)
            screenshot_utf8 = image2utf8(screenshot)
            width, height = screenshot.size

            rows.append(
                [
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
                ]
            )

    # window by window
    if len(window_events) > 0:
        rows.append([row(Div(text="<hr><h1>Windows/Tabs</h1><hr>"))])

        last_event = action_events[0]
        curr_action_events = [last_event]
        for i in range(1, len(action_events)):
            # TODO:
            if i == MAX_EVENTS:
                break
            curr_event = action_events[i]
            if (
                curr_event.window_event_timestamp != last_event.window_event_timestamp
                and curr_event.window_event.title != ""
                and curr_event.window_event.title != last_event.window_event.title
            ):
                if len(curr_action_events) > 2:
                    # sometimes the app isn't loaded yet on the first action event
                    event_to_display = curr_action_events[1]
                else:
                    event_to_display = curr_action_events[0]
                image = display_event(event_to_display)
                image_utf8 = image2utf8(image)
                width, height = image.size

                gaps, time_in_gaps = find_gaps(curr_action_events)
                num_clicks = find_clicks(curr_action_events)
                num_key_presses = find_key_presses(curr_action_events)
                window_duration = (
                    curr_event.window_event_timestamp
                    - last_event.window_event_timestamp
                )

                window_info = {
                    f"Number of pauses longer than {MAX_GAP_SECONDS} seconds": gaps,
                    "Total time spent during pauses": time_in_gaps,
                    "Total number of mouse clicks": num_clicks,
                    "Total number of key presses": num_key_presses,
                    "Time spent on this window/tab": window_duration,
                }

                rows.append(
                    [
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
                                    {dict2html(row2dict(last_event.window_event))}
                                </table>
                            """,
                            ),
                            Div(text=f"""
                                <table>
                                    {dict2html(window_info)}
                                </table>
                            """),
                        ),
                    ]
                )
                # flush curr_action_events
                curr_action_events = []
            last_event = curr_event
            curr_action_events.append(curr_event)

        # info for the time between start of last window event and last action event
        image = display_event(action_events[-1])
        image_utf8 = image2utf8(image)
        width, height = image.size

        last_action_events = window_events[-1].action_events
        gaps, time_in_gaps = find_gaps(last_action_events)
        num_clicks = find_clicks(last_action_events)
        num_key_presses = find_key_presses(last_action_events)
        window_duration = (
            last_action_events[-1].timestamp - last_action_events[0].timestamp
        )
        window_info = {
            f"Number of pauses longer than {MAX_GAP_SECONDS} seconds": gaps,
            "Total time spent during pauses": time_in_gaps,
            "Total number of mouse clicks": num_clicks,
            "Total number of key presses": num_key_presses,
            "Time spent on this window/tab": window_duration,
        }

        rows.append(
            [
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
                                        {dict2html(row2dict(window_events[-1]))}
                                    </table>
                                """,
                    ),
                    Div(text=f"""
                                    <table>
                                        {dict2html(window_info)}
                                    </table>
                                """),
                ),
            ]
        )

    # display data
    title = f"Productivity metrics for recording-{recording.id}"
    fname_out = f"productivity-{recording.id}.html"
    logger.info(f"{fname_out=}")
    output_file(fname_out, title=title)

    result = show(  # noqa: F841
        layout(
            rows,
        )
    )

    def cleanup() -> None:
        os.remove(fname_out)
        removed = not os.path.exists(fname_out)
        logger.info(f"{removed=}")

    Timer(1, cleanup).start()


if __name__ == "__main__":
    calculate_productivity()
