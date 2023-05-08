"""
Implements a naive playback strategy wherein the Actions are replayed
directly, without considering any screenshots.
"""

from pprint import pformat
import time

from loguru import logger
import mss.base

from puterbot.events import get_events
from puterbot.utils import display_event, rows2dicts
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
        self.display_events = display_events
        self.replay_events = replay_events
        self.sleep = sleep
        self.prev_timestamp = None
        self.action_idx = -1
        self.processed_actions = get_events(recording, process=True) 
        event_dicts = rows2dicts(self.processed_actions)
        logger.info(f"event_dicts=\n{pformat(event_dicts)}")

    def get_next_action(
        self,
        screenshot: mss.base.ScreenShot,
    ):
        self.action_idx += 1
        num_actions = len(self.processed_actions)
        if self.action_idx >= num_actions:
            # TODO: refactor
            raise StopIteration()
        action = self.processed_actions[self.action_idx]
        logger.info(
            f"{self.action_idx=} of {num_actions=}: {action=}"
        )
        if self.display_events:
            image = display_event(action)
            image.show()
        if self.replay_events:
            if self.sleep and self.prev_timestamp:
                sleep_time = action.timestamp - self.prev_timestamp
                logger.debug(f"{sleep_time=}")
                time.sleep(sleep_time)
            self.prev_timestamp = action.timestamp
            return action
        else:
            return None
