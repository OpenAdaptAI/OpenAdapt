import pytest
from openadapt.models import Recording, Screenshot, WindowEvent
from openadapt.strategies.stateful import StatefulReplayStrategy


def test_active_window_diff():
    # boilerplate code, check db and create synthetic
    # instances of each object to feed into stateful.

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
