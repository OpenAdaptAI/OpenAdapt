"""Implements the LMQL library to restrict model outputs.

Typical usage example:
    class MyReplayStrategy(SummaryReplayStrategyMixin):
        ...
"""
import json
import re
import string

import lmql
from loguru import logger
from pynput.keyboard import Key

from openadapt import j2
from openadapt.models import Recording, ActionEvent
from openadapt.record import trace
from openadapt.strategies.base import BaseReplayStrategy

DEFAULT_MODEL = "openai/text-davinci-003"
REGEX = re.compile("[%s]" % re.escape(string.punctuation))


class LMQLReplayStrategyMixin(BaseReplayStrategy):
    """ReplayStrategy Mixin which restricts model output.

    Attributes:
        - recording: the recording to be played back
        - model_name: the model to be prompted
    """

    def __init__(
        self,
        recording: Recording,
        model_name: str = DEFAULT_MODEL,
    ):
        """
        See base class.

        Additional Attribute:
            - model_name: the model to be prompted
        """
        super().__init__(recording)

        logger.info(f"{model_name=}")
        self.model_name = model_name

    def get_valid_json(self, prompt: str, template_file_path: str) -> dict:
        """Return the ActionEvent the model returns from the prompt."""

        # NOTE: the model parameter of the lmql_json specifies which model to prompt
        result = lmql_json(prompt, template_file_path, model=self.model_name)

        assert len(result) == 1, result  # by default LMQL uses argmax which should return 1 result
        json_result = result[0]
        logger.info(f"The json is {json_result}")

        return json_result


@lmql.query()
def lmql_json(description: str, template_file_path: str) -> list[lmql.LMQLResult]:
    # This function uses LMQL to constrain the output to a valid json
    # NOTE: openai/text-davinci-003 is the default model to prompt, but the model
    #        can be changed by calling lmql_json with an additional model parameter

    '''lmql
    prompt = j2.load_template(template_fname=template_file_path, description=description)
    """{prompt}"""
    """{{ 
            "name": [ACTION],
            "mouse_x": [INT_VALUE],
            "mouse_y": [INT_VALUE],
            "mouse_dx": [INT_VALUE],
            "mouse_dy": [INT_VALUE],
            "mouse_button_name": [CLICK],
            "mouse_pressed": [STRING_VALUE],
            "key_name": [STRING_VALUE],
            "key_char": [STRING_VALUE],
            "key_vk": [STRING_VALUE] 
        }}
    """ where ACTION in set(['"move"', '"scroll"', '"press"', '"release"', '"click"']) \
    and STOPS_BEFORE(STRING_VALUE, ',') and INT(INT_VALUE) and len(TOKENS(INT_VALUE)) < 4 \
    and CLICK in set(['"left"', '"right"', '"null"'])

    return json.loads(context.prompt.split(prompt,1)[1])
    '''
