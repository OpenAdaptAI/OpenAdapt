import pytest
from loguru import logger
from pprint import pformat
from openadapt.models import Recording, Screenshot, WindowEvent
from openadapt.strategies.stateful import StatefulReplayStrategy
from openadapt.crud import (
    get_latest_recording,
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

def test_active_window_diff():
    # boilerplate code, check db and create synthetic
    # instances of each object to feed into stateful.

    # TODO
    test_recording = Recording()


    test_strategy = StatefulReplayStrategy(test_recording)

    expected_action_events = {}

    test_screenshot = Screenshot()
    test_window = WindowEvent()

    actual_action_events = test_strategy.get_next_action_event(
        active_screenshot=test_screenshot, active_window=test_window
    )

    print(actual_action_events)

    assert actual_action_events == expected_action_events


def dict_form_testing():
    recording = get_latest_recording()

    meta = {}
    action_events = get_events(recording, process=True, meta=meta)
    event_dicts = rows2dicts(action_events)
    
    for events in event_dicts:
        print(events.keys())


    recording_dict = row2dict(recording)

    #print(f"This is the recording dict{recording_dict=}")

def output_example():
    recording = get_latest_recording()
    test_obj = StatefulReplayStrategy(recording)

    # test out on the same window
    # recording can't be too large, obviously due to context length limitations
    windowevent = WindowEvent()

    meta = {}

    action_events = get_events(recording, process=True, meta=meta)

    for action in action_events:
        window_event_dict = row2dict(action.window_event)
        active_action_dict = test_obj.get_next_action_event(Screenshot(),window_event_dict)
        print(active_action_dict)
        break


if __name__ == "__main__":
    output_example()
