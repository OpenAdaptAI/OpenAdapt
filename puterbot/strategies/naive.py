"""
Implements a naive playback strategy wherein the ActionEvents are replayed
directly, without considering any screenshots.
"""

from pprint import pformat
import time

from loguru import logger
import mss.base

from puterbot import events, utils, models, strategies


DISPLAY_EVENTS = False
PROCESS_EVENTS = True
REPLAY_EVENTS = True
SLEEP = True


class NaiveReplayStrategy(strategies.base.BaseReplayStrategy):

    def __init__(
        self,
        recording: models.Recording,
        display_events=DISPLAY_EVENTS,
        replay_events=REPLAY_EVENTS,
        sleep=SLEEP,
    ):
        super().__init__(recording)
        self.display_events = display_events
        self.replay_events = replay_events
        self.sleep = sleep
        self.prev_timestamp = None
        self.action_event_idx = -1
        self.processed_action_events = events.get_events(
            recording,
            process=PROCESS_EVENTS,
        )
        event_dicts = utils.rows2dicts(self.processed_action_events)
        logger.info(f"event_dicts=\n{pformat(event_dicts)}")

    def get_next_action_event(
        self,
        screenshot: models.Screenshot,
        window_event: models.WindowEvent,
    ):
        self.action_event_idx += 1
        num_action_events = len(self.processed_action_events)
        if self.action_event_idx >= num_action_events:
            # TODO: refactor
            raise StopIteration()
        action_event = self.processed_action_events[self.action_event_idx]
        logger.info(
            f"{self.action_event_idx=} of {num_action_events=}: {action_event=}"
        )
        if self.display_events:
            image = utils.display_event(action_event)
            image.show()
        if self.replay_events:
            if self.sleep and self.prev_timestamp:
                # TODO: subtract processing time
                sleep_time = (
                    action_event.timestamp - self.prev_timestamp
                ).total_seconds()
                logger.debug(f"{sleep_time=}")
                time.sleep(sleep_time)
            self.prev_timestamp = action_event.timestamp
            return action_event
        else:
            return None
