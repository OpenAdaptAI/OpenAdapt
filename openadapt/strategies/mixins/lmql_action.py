import re
import string

import lmql
from loguru import logger
from pynput.keyboard import Key

from openadapt.strategies.base import BaseReplayStrategy
from openadapt.models import Recording, ActionEvent
from openadapt.config import ACTION_TEXT_NAME_PREFIX, ACTION_TEXT_NAME_SUFFIX

DEFAULT_MODEL = "openai/text-davinci-003"
REGEX = re.compile('[%s]' % re.escape(string.punctuation))

class LMQLReplayStrategyMixin(BaseReplayStrategy):

    def __init__(
        self,
        recording: Recording,
        model_name: str = DEFAULT_MODEL,
    ):
        super().__init__(recording)

        logger.info(f"{model_name=}")
        self.model_name = model_name

    def get_valid_action_event(self, prompt: str) -> ActionEvent:
        list_of_results = lmql_call(description=prompt, model=self.model_name)
        result = list_of_results[0]
        logger.info(f"The filled in prompt is: {result.prompt}")
        result_dict = result.variables

        action = parse_action(result_dict)
        return action

@lmql.query
def lmql_call(description: str, model: str) -> list:
    '''lmql
    argmax
        "Summarize the following description as a json: {description} {{ "
        "'action': [ACTION], "
        if ACTION == "press" or ACTION == "release":
            "'key': [KEY] }}"
            output_key = KEY.strip()
            if type_of_key(output_key) == "invalid":
                "The previously entered key is incorrect, the correct key is [KEY2]"
                output_key2 = KEY2.strip()
                if type_of_key(output_key2) == "invalid":
                    raise ValueError(f"invalid {output_key=}, invalid {output_key2=}")
        if ACTION == "move":
            "'x': [X], 'y': [Y] }}"
        if ACTION == "scroll":
            "'change in x': [X], 'change in y': [Y] }}"
        if ACTION == "click":
            "'x': [X], 'y': [Y], 'click': [CLICK] }}"
    from
        model
    where
        ACTION in set(["move", "scroll", "press", "release", "click"]) 
        and len(TOKENS(KEY)) < 5 and STOPS_BEFORE(KEY, " }") 
        and len(TOKENS(KEY2)) < 5 and STOPS_BEFORE(KEY2, " }") 
        and INT(X) and INT(Y) and len(TOKENS(X)) < 5 and len(TOKENS(Y)) < 5 
        and CLICK in set(["left", "right"])
    '''

def parse_action(result_dict: dict) -> ActionEvent:
    name = result_dict["ACTION"]
    logger.info(f"The action is: {name}")
    action_event = ActionEvent(name=name)

    if name == "press" or name == "release":
        output = result_dict["KEY"]
        key = clean_output(output)
        if type_of_key(key) == "invalid":
            output2 = result_dict["KEY2"]
            key = clean_output(output2)
        if key[0] == ACTION_TEXT_NAME_PREFIX and key [-1] == ACTION_TEXT_NAME_SUFFIX:
            key_name = key[1:-1]
            action_event.key_name = key_name
        if len(key) == 1:
            action_event.key_char = key
            action_event.canonical_key_char = key
        if hasattr(Key, key):
            action_event.key_name = key
            action_event.canonical_key_name = key
        else:
            key_vk = int(key)
            action_event.key_vk = key_vk
            action_event.canonical_key_vk = key_vk
    elif name == "move":
        action_event.mouse_x = result_dict["X"]
        action_event.mouse_y = result_dict["Y"]
    elif name == "scroll":
        action_event.mouse_dx = result_dict["X"]
        action_event.mouse_dy = result_dict["Y"]
    elif name == "click":
        action_event.mouse_x = result_dict["X"]
        action_event.mouse_y = result_dict["Y"]
        action_event.mouse_button_name = result_dict["CLICK"]
    
    return action_event

def type_of_key(key: str) -> str:
    """Return a string representing what type of key the input is.
    
    Types of keys:
        - char: a single character
        - key_name: a multicharacter key
        - vk: a virtual key
        - invalid: not a valid key
    """
    if key[0] == ACTION_TEXT_NAME_PREFIX and key [-1] == ACTION_TEXT_NAME_SUFFIX and hasattr(Key, key[1:-1]):
         return "key_name"
    elif len(key) == 1:
        return "char"
    elif hasattr(Key, key):
        return "key_name"
    else:
        # in case of error
        try:
            key_vk = int(key)
        except:
            return "invalid"
        return "vk"

def clean_output(output: str) -> str:
    """Remove the whitespace and punctuation."""
    output_without_whitespace = output.strip()
    clean_output = REGEX.sub('', output_without_whitespace)
    return clean_output
