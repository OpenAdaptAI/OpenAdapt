"""
Implements a naive playback strategy wherein the InputEvents are replayed
directly, without considering any screenshots.
"""

from pprint import pformat
import time

from loguru import logger
import mss.base

from puterbot.events import (
    get_events,
)
from puterbot.utils import (
    display_event,
    rows2dicts,
)
from puterbot.models import Recording
from puterbot.strategies.base import BaseReplayStrategy


DISPLAY_EVENTS = False
REPLAY_EVENTS = True
SLEEP = True


class NaiveReplayStrategy(BaseReplayStrategy):

    def __init__(
        self,
        recording: Recording,
        display_events=DISPLAY_EVENTS,
        replay_events=REPLAY_EVENTS,
        sleep=SLEEP,
    ):
        super().__init__(recording)
        self.prev_timestamp = None
        self.input_event_idx = -1
        self.processed_input_events = get_events(recording, process=True) 
        event_dicts = rows2dicts(self.processed_input_events)
        logger.info(f"event_dicts=\n{pformat(event_dicts)}")

    def get_next_input_event(
        self,
        screenshot: mss.base.ScreenShot,
    ):
        self.input_event_idx += 1
        num_input_events = len(self.processed_input_events)
        if self.input_event_idx >= num_input_events:
            # TODO: refactor
            raise StopIteration()
        input_event = self.processed_input_events[self.input_event_idx]
        logger.info(
            f"{self.input_event_idx=} of {num_input_events=}: {input_event=}"
        )
        if DISPLAY_EVENTS:
            image = display_event(input_event)
            image.show()
        if REPLAY_EVENTS:
            if SLEEP and self.prev_timestamp:
                sleep_time = input_event.timestamp - self.prev_timestamp
                logger.debug(f"{sleep_time=}")
                time.sleep(sleep_time)
            self.prev_timestamp = input_event.timestamp
            return input_event
        else:
            return None
