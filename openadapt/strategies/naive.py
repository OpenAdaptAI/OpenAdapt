"""Implements a naive playback strategy, replaying ActionEvents without screenshots."""

import time

from loguru import logger

from openadapt import models, strategies, utils
from openadapt.config import config

DISPLAY_EVENTS = False
PROCESS_EVENTS = True
REPLAY_EVENTS = True
SLEEP = False
assert PROCESS_EVENTS or not SLEEP, "invalid configuration"


class NaiveReplayStrategy(strategies.base.BaseReplayStrategy):
    """Naive playback strategy that replays ActionEvents directly."""

    def __init__(
        self,
        recording: models.Recording,
        display_events: bool = DISPLAY_EVENTS,
        replay_events: bool = REPLAY_EVENTS,
        process_events: bool = PROCESS_EVENTS,
        sleep: bool = SLEEP,
    ) -> None:
        """Initialize the NaiveReplayStrategy.

        Args:
            recording (models.Recording): The recording object.
            replay_instructions (str): Natural language instructions
                for how recording should be replayed.
            display_events (bool): Flag indicating whether to display the events.
              Defaults to False.
            replay_events (bool): Flag indicating whether to replay the events.
              Defaults to True.
            process_events (bool): Flag indicating whether to process the events.
              Defaults to True.
            sleep (bool): Flag indicating whether to add sleep time between events.
              Defaults to False.
        """
        super().__init__(recording)
        self.display_events = display_events
        self.replay_events = replay_events
        self.process_events = process_events
        self.sleep = sleep
        self.prev_timestamp = None
        self.action_event_idx = -1
        # event_dicts = utils.rows2dicts(self.processed_action_events)
        # logger.info(f"event_dicts=\n{pformat(event_dicts)}")

    def get_next_action_event(
        self,
        screenshot: models.Screenshot,
        window_event: models.WindowEvent,
    ) -> models.ActionEvent | None:
        """Get the next ActionEvent for replay.

        Args:
            screenshot (models.Screenshot): The screenshot object.
            window_event (models.WindowEvent): The window event object.

        Returns:
            models.ActionEvent or None: The next ActionEvent for replay or None
              if there are no more events.
        """
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
