"""Module to test events.py."""

from functools import partial
from pprint import pformat
from typing import Any, Callable, List, Optional
import itertools
import pytest

from deepdiff import DeepDiff
from loguru import logger

from openadapt.models import ActionEvent, WindowEvent
from openadapt.utils import (
    get_double_click_interval_seconds,
    rows2dicts,
    override_double_click_interval_seconds,
)
from openadapt.events import (
    discard_unused_events,
    merge_consecutive_keyboard_events,
    merge_consecutive_mouse_click_events,
    merge_consecutive_mouse_move_events,
    merge_consecutive_mouse_scroll_events,
    remove_redundant_mouse_move_events,
)


# default duration between consecutive events
# this needs to be small enough such that dt_short + DEFAULT < x,
# where x is the double click interval in seconds
# (see test_merge_consecutive_mouse_click_events() for definition of dt_short)
DEFAULT_DT = get_double_click_interval_seconds() / 2
# set to 10 to improve output readability
OVERRIDE_DOUBLE_CLICK_INTERVAL_SECONDS = None
NUM_TIMESTAMP_DIGITS = 6


rows2dicts = partial(rows2dicts, num_digits=NUM_TIMESTAMP_DIGITS)
_start_time = 0
timestamp = _start_time
timestamp_raw = _start_time


def reset_timestamp() -> None:
    """Reset the timestamp counters to their initial values.

    Returns:
        None
    """
    global timestamp
    global timestamp_raw
    timestamp = _start_time
    timestamp_raw = _start_time


@pytest.fixture(autouse=True)
def _reset_timestamp() -> None:
    """Fixture that automatically resets the timestamp counters before each test.

    Returns:
        None
    """
    reset_timestamp()


def make_action_event(
    event_dict: dict,
    dt: float = None,
    get_pre_children: Callable[[], List[ActionEvent]] = None,
    get_post_children: Callable[[], List[ActionEvent]] = None,
) -> ActionEvent:
    """Create an ActionEvent instance with the given attributes.

    Args:
        event_dict (dict): Dictionary containing the event attributes.
        dt (float, optional): Time difference in seconds from the previous event.
        get_pre_children (function, optional): returns the list of pre-children events.
        get_post_children (function, optional): returns the list of post-children events.

    Returns:
        ActionEvent: An instance of the ActionEvent class.
    """
    assert "chidren" not in event_dict, event_dict["children"]
    children = get_children_with_timestamps(get_pre_children)
    event_dict["children"] = children

    dt = dt if dt is not None else DEFAULT_DT
    if "timestamp" not in event_dict:
        global timestamp
        global timestamp_raw
        event_dict["timestamp"] = float(timestamp)
        prev_timestamp = timestamp
        prev_timestamp_raw = timestamp_raw
        timestamp += dt
        timestamp_raw += dt
        logger.debug(
            f"{dt=} "
            f"{prev_timestamp=} "
            f"{prev_timestamp_raw=} "
            f"{timestamp=} "
            f"{timestamp_raw=} "
            f"name={event_dict.get('name')}"
        )

    event = ActionEvent(**event_dict)

    if get_pre_children:
        # correct off by one error
        timestamp_raw -= dt

    if get_post_children:
        event.children += get_children_with_timestamps(get_post_children)

    return event


def get_children_with_timestamps(get_children: Callable[[], List[Any]]) -> List[Any]:
    """Get the list of children events with timestamps.

    Args:
        get_children (Callable[[], List[Any]]): Function that returns the list of children events.

    Returns:
        List[Any]: List of children events with timestamps.
    """
    if not get_children:
        return []

    global timestamp
    global timestamp_raw

    # save the current timestamp
    prev_timestamp = timestamp

    # set the current timestamp to the raw timestamp to assign it to children
    timestamp = timestamp_raw
    children = get_children()

    # reset current timestamp to original value to assign it to parent
    timestamp = prev_timestamp

    return children


def make_move_event(
    x: int = 0,
    y: int = 0,
    get_pre_children: Optional[Callable[[], List[ActionEvent]]] = None,
    get_post_children: Optional[Callable[[], List[ActionEvent]]] = None,
) -> ActionEvent:
    """Create a move event with the given attributes.

    Args:
        x (int, optional): X-coordinate of the mouse.
        y (int, optional): Y-coordinate of the mouse.
        get_pre_children (function, optional): returns list of pre-children events.
        get_post_children (function, optional): returns the list of post-children events.

    Returns:
        ActionEvent: An instance of the ActionEvent class representing the move event.
    """
    return make_action_event(
        {"name": "move", "mouse_x": x, "mouse_y": y},
        get_pre_children=get_pre_children,
        get_post_children=get_post_children,
    )


def make_click_event(
    pressed: bool,
    mouse_x: int = 0,
    mouse_y: int = 0,
    dt: float = None,
    button_name: str = "left",
    get_pre_children: Optional[Callable[[], List[ActionEvent]]] = None,
    get_post_children: Optional[Callable[[], List[ActionEvent]]] = None,
) -> ActionEvent:
    """Create a click event with the given attributes.

    Args:
        pressed (bool): True if the mouse button is pressed, False otherwise.
        mouse_x (int, optional): X-coordinate of the mouse.
        mouse_y (int, optional): Y-coordinate of the mouse.
        dt (float, optional): Time difference in seconds from the previous event.
        button_name (str, optional): Name of the mouse button. Defaults to "left".
        get_pre_children (function, optional): Function that returns the list of pre-children events.
        get_post_children (function, optional): Function that returns the list of post-children events.

    Returns:
        ActionEvent: An instance of the ActionEvent class representing the click event.
    """
    return make_action_event(
        {
            "name": "click",
            "mouse_button_name": button_name,
            "mouse_pressed": pressed,
            "mouse_x": mouse_x,
            "mouse_y": mouse_y,
        },
        dt=dt,
        get_pre_children=get_pre_children,
        get_post_children=get_post_children,
    )


def make_scroll_event(dy: int = 0, dx: int = 0) -> ActionEvent:
    """Create a scroll event with the given attributes.

    Args:
        dy (int, optional): Vertical scroll amount. Defaults to 0.
        dx (int, optional): Horizontal scroll amount. Defaults to 0.

    Returns:
        ActionEvent: An instance of the ActionEvent class representing the scroll event.
    """
    return make_action_event({"name": "scroll", "mouse_dx": dx, "mouse_dy": dy})


def make_click_events(
    dt_released: float, dt_pressed: float = None, button_name: str = "left"
) -> tuple[ActionEvent, ActionEvent]:
    """Create a pair of click events with the given time differences and button name.

    Args:
        dt_released (float): Time difference in seconds between the press and release events.
        dt_pressed (float, optional): Time difference in seconds from the previous event for the press event.
        button_name (str, optional): Name of the mouse button. Defaults to "left".

    Returns:
        tuple: A tuple containing the press and release events.
    """
    return (
        make_click_event(True, dt=dt_pressed, button_name=button_name),
        make_click_event(False, dt=dt_released, button_name=button_name),
    )


def make_processed_click_event(
    name: str,
    dt: float,
    get_children: Optional[Callable[[], List[ActionEvent]]],
    mouse_x: int = 0,
    mouse_y: int = 0,
    button_name: str = "left",
) -> ActionEvent:
    """Create a processed click event with the given attributes.

    Args:
        name (str): Name of the processed click event.
        dt (float): Time difference in seconds from the previous event.
        get_children (function): Function that returns the list of children events.
        mouse_x (int, optional): X-coordinate of the mouse. Defaults to 0.
        mouse_y (int, optional): Y-coordinate of the mouse. Defaults to 0.
        button_name (str, optional): Name of the mouse button. Defaults to "left".

    Returns:
        ActionEvent: An instance of the ActionEvent class representing the processed click event.
    """
    return make_action_event(
        {
            "name": name,
            "mouse_button_name": button_name,
            "mouse_x": mouse_x,
            "mouse_y": mouse_y,
        },
        dt=dt,
        get_pre_children=get_children,
    )


def make_singleclick_event(
    dt: float,
    get_children: Optional[Callable[[], List[ActionEvent]]],
    mouse_x: int = 0,
    mouse_y: int = 0,
    button_name: str = "left",
) -> ActionEvent:
    """Create a single click event with the given attributes.

    Args:
        dt (float): Time difference in seconds from the previous event.
        get_children (function): Function that returns the list of children events.
        mouse_x (int, optional): X-coordinate of the mouse. Defaults to 0.
        mouse_y (int, optional): Y-coordinate of the mouse. Defaults to 0.
        button_name (str, optional): Name of the mouse button. Defaults to "left".

    Returns:
        ActionEvent: An instance of the ActionEvent class representing the single click event.
    """
    return make_processed_click_event(
        "singleclick",
        dt,
        get_children,
        mouse_x,
        mouse_y,
        button_name,
    )


def make_doubleclick_event(
    dt: float,
    get_children: Optional[Callable[[], List[ActionEvent]]],
    mouse_x: int = 0,
    mouse_y: int = 0,
    button_name: str = "left",
) -> ActionEvent:
    """Create a double click event with the given attributes.

    Args:
        dt (float): Time difference in seconds from the previous event.
        get_children (function): Function that returns the list of children events.
        mouse_x (int, optional): X-coordinate of the mouse. Defaults to 0.
        mouse_y (int, optional): Y-coordinate of the mouse. Defaults to 0.
        button_name (str, optional): Name of the mouse button. Defaults to "left".

    Returns:
        ActionEvent: An instance of the ActionEvent class representing the double click event.
    """
    return make_processed_click_event(
        "doubleclick",
        dt,
        get_children,
        mouse_x,
        mouse_y,
        button_name,
    )


def test_merge_consecutive_mouse_click_events() -> None:
    """Test case for merging consecutive mouse click events.

    Returns:
        None
    """
    if OVERRIDE_DOUBLE_CLICK_INTERVAL_SECONDS:
        override_double_click_interval_seconds(OVERRIDE_DOUBLE_CLICK_INTERVAL_SECONDS)
    double_click_interval_seconds = get_double_click_interval_seconds()
    dt_short = double_click_interval_seconds / 10
    dt_long = double_click_interval_seconds * 10
    raw_events = [
        *make_click_events(dt_long, button_name="right"),
        # doubleclick
        *make_click_events(dt_short),
        *make_click_events(dt_long),
        # doubleclick
        *make_click_events(dt_short),
        *make_click_events(dt_long),
        *make_click_events(dt_long, button_name="right"),
        # singleclick
        *make_click_events(dt_long),
    ]
    logger.info(f"raw_events=\n{pformat(rows2dicts(raw_events))}")
    reset_timestamp()
    expected_events = rows2dicts(
        [
            *make_click_events(dt_long, button_name="right"),
            make_doubleclick_event(
                dt_long,
                lambda: [*make_click_events(dt_short), *make_click_events(dt_long)],
            ),
            make_doubleclick_event(
                dt_long,
                lambda: [*make_click_events(dt_short), *make_click_events(dt_long)],
            ),
            *make_click_events(dt_long, button_name="right"),
            make_singleclick_event(dt_long, lambda: [*make_click_events(dt_long)]),
        ]
    )
    logger.info(f"expected_events=\n{pformat(expected_events)}")
    actual_events = rows2dicts(merge_consecutive_mouse_click_events(raw_events))
    logger.info(f"actual_events=\n{pformat(actual_events)}")
    assert expected_events == actual_events


def test_merge_consecutive_mouse_move_events() -> None:
    """Test case for merging consecutive mouse move events.

    Returns:
        None
    """
    raw_events = [
        make_scroll_event(),
        make_move_event(0),
        make_move_event(1),
        make_move_event(2),
        make_scroll_event(),
        make_move_event(3),
        make_move_event(4),
    ]
    logger.info(f"raw_events=\n{pformat(rows2dicts(raw_events))}")
    reset_timestamp()
    expected_events = rows2dicts(
        [
            make_scroll_event(),
            make_move_event(
                2,
                get_pre_children=lambda: [
                    make_move_event(0),
                    make_move_event(1),
                    make_move_event(2),
                ],
            ),
            make_scroll_event(),
            make_move_event(
                4,
                get_pre_children=lambda: [make_move_event(3), make_move_event(4)],
            ),
        ]
    )
    logger.info(f"expected_events=\n{pformat(expected_events)}")
    actual_events = rows2dicts(
        merge_consecutive_mouse_move_events(
            raw_events,
            # TODO XXX: test with by_diff_distance=True
            by_diff_distance=False,
        )
    )
    logger.info(f"actual_events=\n{pformat(actual_events)}")
    assert expected_events == actual_events


def test_merge_consecutive_mouse_scroll_events() -> None:
    """Test case for merging consecutive mouse scroll events.

    Returns:
        None
    """
    raw_events = [
        make_move_event(),
        make_scroll_event(dx=2),
        make_scroll_event(dx=1),
        make_scroll_event(dx=-1),
        make_move_event(),
        make_scroll_event(dy=1),
        make_scroll_event(dx=1),
    ]
    logger.info(f"raw_events=\n{pformat(rows2dicts(raw_events))}")
    reset_timestamp()
    expected_events = rows2dicts(
        [
            make_move_event(),
            make_scroll_event(dx=2),
            make_move_event(),
            make_scroll_event(dx=1, dy=1),
        ]
    )
    logger.info(f"expected_events=\n{pformat(expected_events)}")
    actual_events = rows2dicts(merge_consecutive_mouse_scroll_events(raw_events))
    logger.info(f"actual_events=\n{pformat(actual_events)}")
    assert expected_events == actual_events


def test_remove_redundant_mouse_move_events() -> None:
    """Test case for removing redundant mouse move events.

    Returns:
        None
    """
    # certain failure modes only appear in longer event chains
    raw_events = list(
        itertools.chain(
            *[
                [
                    make_move_event(1),
                    make_click_event(True, 1),
                    make_move_event(1),
                    make_click_event(False, 1),
                    make_move_event(2),
                    make_click_event(True, 2),
                    make_move_event(3),
                    make_click_event(False, 3),
                    make_move_event(3),
                ]
                for _ in range(2)
            ]
        )
    )
    logger.info(f"raw_events=\n{pformat(rows2dicts(raw_events))}")
    reset_timestamp()
    expected_events = rows2dicts(
        [
            make_click_event(True, 1, get_pre_children=lambda: [make_move_event(1)]),
            make_click_event(
                False,
                1,
                get_post_children=lambda: [make_move_event(1)],
            ),
            make_click_event(
                True,
                2,
                get_post_children=lambda: [make_move_event(2)],
            ),
            make_click_event(
                False,
                3,
                get_post_children=lambda: [make_move_event(3)],
            ),
            make_click_event(
                True,
                1,
                get_post_children=lambda: [make_move_event(3), make_move_event(1)],
            ),
            make_click_event(
                False,
                1,
                get_post_children=lambda: [make_move_event(1)],
            ),
            make_click_event(True, 2, get_post_children=lambda: [make_move_event(2)]),
            make_click_event(
                False,
                3,
                get_post_children=lambda: [make_move_event(3)],
            ),
        ]
    )
    logger.info(f"expected_events=\n{pformat(expected_events)}")
    actual_events = rows2dicts(
        remove_redundant_mouse_move_events(raw_events),
    )
    logger.info(f"actual_events=\n{pformat(actual_events)}")
    assert expected_events == actual_events


def make_press_event(char: str = None, name: str = None) -> ActionEvent:
    """Create a press event with the given character or key name.

    Args:
        char (str, optional): Character corresponding to the pressed key. Defaults to None.
        name (str, optional): Name of the pressed key. Defaults to None.

    Returns:
        ActionEvent: An instance of the ActionEvent class representing the press event.
    """
    assert (char or name) and not (char and name), (char, name)
    return make_action_event({"name": "press", "key_char": char, "key_name": name})


def make_release_event(char: str = None, name: str = None) -> ActionEvent:
    """Create a release event with the given character or key name.

    Args:
        char (str, optional): Character corresponding to the released key. Defaults to None.
        name (str, optional): Name of the released key. Defaults to None.

    Returns:
        ActionEvent: An instance of the ActionEvent class representing the release event.
    """
    assert (char or name) and not (char and name), (char, name)
    return make_action_event({"name": "release", "key_char": char, "key_name": name})


def make_type_event(
    get_children: Optional[Callable[[], List[ActionEvent]]]
) -> ActionEvent:
    """Create a type event with the given children events.

    Args:
        get_children (function): Function that returns the list of children events.

    Returns:
        ActionEvent: An instance of the ActionEvent class representing the type event.
    """
    return make_action_event({"name": "type"}, get_pre_children=get_children)


def make_key_events(char: str) -> tuple[ActionEvent, ActionEvent]:
    """Create a pair of press and release events for the given character.

    Args:
        char (str): Character corresponding to the key.

    Returns:
        tuple: A tuple containing the press and release events.
    """
    return make_press_event(char), make_release_event(char)


def test_merge_consecutive_keyboard_events() -> None:
    """Test case for merging consecutive keyboard events.

    Returns:
        None
    """
    raw_events = [
        make_click_event(True),
        *make_key_events("a"),
        *make_key_events("b"),
        *make_key_events("c"),
        *make_key_events("d"),
        *make_key_events("e"),
        make_click_event(False),
        make_press_event("f"),
        make_press_event("g"),
        make_release_event("f"),
        make_press_event("h"),
        make_release_event("g"),
        make_release_event("h"),
        make_scroll_event(1),
    ]
    logger.info(f"raw_events=\n{pformat(rows2dicts(raw_events))}")
    reset_timestamp()
    expected_events = rows2dicts(
        [
            make_click_event(True),
            make_type_event(
                lambda: [
                    *make_key_events("a"),
                    *make_key_events("b"),
                    *make_key_events("c"),
                    *make_key_events("d"),
                    *make_key_events("e"),
                ]
            ),
            make_click_event(False),
            make_type_event(
                lambda: [
                    make_press_event("f"),
                    make_press_event("g"),
                    make_release_event("f"),
                    make_press_event("h"),
                    make_release_event("g"),
                    make_release_event("h"),
                ]
            ),
            make_scroll_event(1),
        ]
    )
    logger.info(f"expected_events=\n{pformat(expected_events)}")
    actual_events = rows2dicts(merge_consecutive_keyboard_events(raw_events))
    logger.info(f"actual_events=\n{pformat(actual_events)}")
    diff = DeepDiff(expected_events, actual_events)
    assert not diff, pformat(diff)


def test_merge_consecutive_keyboard_events__grouped() -> None:
    """Test case for merging consecutive keyboard events with grouping.

    Returns:
        None
    """
    raw_events = [
        make_click_event(True),
        *make_key_events("a"),
        *make_key_events("b"),
        make_press_event(name="ctrl"),
        *make_key_events("c"),
        make_press_event(name="alt"),
        *make_key_events("d"),
        make_release_event(name="ctrl"),
        *make_key_events("e"),
        make_release_event(name="alt"),
        *make_key_events("f"),
        *make_key_events("g"),
        make_click_event(False),
    ]
    logger.info(f"raw_events=\n{pformat(rows2dicts(raw_events))}")
    reset_timestamp()
    expected_events = rows2dicts(
        [
            make_click_event(True),
            make_type_event(lambda: [*make_key_events("a"), *make_key_events("b")]),
            make_type_event(
                lambda: [
                    make_press_event(name="ctrl"),
                    *make_key_events("c"),
                    make_press_event(name="alt"),
                    *make_key_events("d"),
                    make_release_event(name="ctrl"),
                    *make_key_events("e"),
                    make_release_event(name="alt"),
                ]
            ),
            make_type_event(lambda: [*make_key_events("f"), *make_key_events("g")]),
            make_click_event(False),
        ]
    )
    logger.info(f"expected_events=\n{pformat(expected_events)}")
    actual_events = rows2dicts(
        merge_consecutive_keyboard_events(raw_events, group_named_keys=True)
    )
    logger.info(f"actual_events=\n{pformat(actual_events)}")
    diff = DeepDiff(expected_events, actual_events)
    assert not diff, pformat(diff)


def make_window_event(event_dict: dict) -> WindowEvent:
    """Create a WindowEvent with the given attributes.

    Args:
        event_dict (dict): Dictionary containing the attributes of the WindowEvent.

    Returns:
        WindowEvent: An instance of the WindowEvent class with the specified attributes.
    """
    return WindowEvent(**event_dict)


def test_discard_unused_events() -> None:
    """Test case for discarding unused events.

    Tests that the discard_unused_events function discards unused events based on specified timestamp.

    Returns:
        None
    """
    window_events = [
        make_window_event({"timestamp": 0}),
        make_window_event({"timestamp": 1}),
        make_window_event({"timestamp": 2}),
    ]
    action_events = [
        make_action_event({"window_event_timestamp": 0}),
        make_action_event({"window_event_timestamp": 2}),
    ]
    expected_filtered_window_events = rows2dicts(
        [make_window_event({"timestamp": 0}), make_window_event({"timestamp": 2})]
    )
    actual_filtered_window_events = rows2dicts(
        discard_unused_events(
            window_events,
            action_events,
            "window_event_timestamp",
        )
    )
    assert expected_filtered_window_events == actual_filtered_window_events
