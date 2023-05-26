"""
Implements a naive playback strategy wherein the ActionEvents are replayed
directly, without considering any screenshots.
"""

from pprint import pformat
import time

from loguru import logger
import mss.base

from openadapt import config, events, utils, models, strategies


DISPLAY_EVENTS = False
PROCESS_EVENTS = True
REPLAY_EVENTS = True
SLEEP = False


class NaiveReplayStrategy(strategies.base.BaseReplayStrategy):

    def __init__(
        self,
        recording: models.Recording,
        display_events=DISPLAY_EVENTS,
        replay_events=REPLAY_EVENTS,
        process_events=PROCESS_EVENTS,
        sleep=SLEEP,
    ):
        super().__init__(recording)
        self.display_events = display_events
        self.replay_events = replay_events
        self.process_events = process_events
        self.sleep = sleep
        self.prev_timestamp = None
        self.action_event_idx = -1
        #event_dicts = utils.rows2dicts(self.processed_action_events)
        #logger.info(f"event_dicts=\n{pformat(event_dicts)}")

    def get_next_action_event(
        self,
        screenshot: models.Screenshot,
        window_event: models.WindowEvent,
    ):
        if self.process_events:
            action_events = self.recording.processed_action_events
        else:
            action_events = self.recording.action_events
        self.action_event_idx += 1
        num_action_events = len(action_events)
        if self.action_event_idx >= num_action_events:
            # TODO: refactor
            raise StopIteration()
        action_event = action_events[self.action_event_idx]
        
        if config.REPLAY_STRIP_ELEMENT_STATE:
            action_event = utils.strip_element_state(action_event)
        logger.info(
            f"{self.action_event_idx=} of {num_action_events=}: {action_event=}"
        )
        if self.display_events:
            image = utils.display_event(action_event)
            image.show()
        if self.replay_events:
            if self.sleep and self.prev_timestamp:
                # TODO: subtract processing time
                sleep_time = action_event.timestamp - self.prev_timestamp
                logger.debug(f"{sleep_time=}")
                time.sleep(sleep_time)
            self.prev_timestamp = action_event.timestamp
            return action_event
        else:
            return None
