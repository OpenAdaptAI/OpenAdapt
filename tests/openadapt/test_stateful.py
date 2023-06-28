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
    row2dict,
)

def test_active_window_no_diff():
    """
    A test using the latest user recording where the 
    reference window events are the same as the 
    active window events. The model must thus return the same 
    action events as the REFERENCE action events.

    PRECONDITION: Recording cannot be too long due to max input
    size limitations of the LLM (in this case, GPT-3.5-Turbo/GPT-4)
    """
    recording = get_latest_recording()
    test_obj = StatefulReplayStrategy(recording)

    print(recording)

    meta = {}

    action_events = get_events(recording, process=True, meta=meta)

    test_window_event = action_events[0].window_event
    test_window_dict = row2dict(test_window_event)

    test_action_dict = test_obj.get_next_action_event(Screenshot(),test_window_dict)

if __name__ == "__main__":
    test_active_window_no_diff()
