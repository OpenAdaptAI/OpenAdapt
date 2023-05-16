"""
LLM with window states.

Usage:

    $ python -m puterbot.replay StatefulReplayStrategy
"""

from loguru import logger
import numpy as np

from puterbot import events, models, strategies
)


class StatefulReplayStrategy(
    strategies.mixins.openai.OpenAIReplayStrategyMixin,
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
    ):
        event_strs = [
            f"<{event}>"
            for event in self.recording.action_events
        ]
        history_strs = [
            f"<{completion}>"
            for completion in self.result_history
        ]
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
