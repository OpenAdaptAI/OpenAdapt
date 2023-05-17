"""
LLM with window states.

Usage:

    $ python -m puterbot.replay StatefulReplayStrategy
"""

from loguru import logger
import deepdiff
import numpy as np

from puterbot import events, models, strategies
from puterbot.strategies.mixins.openai import OpenAIReplayStrategyMixin


class StatefulReplayStrategy(
    OpenAIReplayStrategyMixin,
    strategies.base.BaseReplayStrategy,
):

    def __init__(
        self,
        recording: models.Recording,
    ):
        super().__init__(recording)
        self.result_history = []

    def get_next_action_event(
        self,
        screenshot: models.Screenshot,
        window_event: models.WindowEvent,
    ):
        event_strs = [
            f"<{event}>"
            for event in self.recording.action_events
        ]
        history_strs = [
            f"<{completion}>"
            for completion in self.result_history
        ]

        state_diffs = get_state_diffs(self.processed_action_events)
        # TODO XXX

        """
        prompt = " ".join(event_strs + history_strs)
        N = max(0, len(prompt) - MAX_INPUT_SIZE)
        prompt = prompt[N:]
        logger.info(f"{prompt=}")
        max_tokens = 10
        completion = self.get_completion(prompt, max_tokens)
        logger.info(f"{completion=}")

        # only take the first <...>
        result = completion.split(">")[0].strip(" <>")
        logger.info(f"{result=}")
        self.result_history.append(result)

        # TODO: parse result into ActionEvent(s)

        return None
        """


def get_state_diffs(action_events):
    window_events = [
        action_event.window_event
        for action_event in action_events
    ]
    diffs = [
        deepdiff.DeepDiff(prev_window_event.state, window_event.state)
        for prev_window_event, window_event in zip(
            window_events, window_events[1:]
        )
    ]
    return diffs
    import ipdb; ipdb.set_trace()
