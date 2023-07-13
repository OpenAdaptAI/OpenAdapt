""" ADD DOCUMENTATION, FIX STYLING AND LINT"""

import lmql
from loguru import logger
from pynput.keyboard import Key

from openadapt.strategies.base import BaseReplayStrategy
from openadapt.models import Recording, ActionEvent

DEFAULT_MODEL = "openai/text-davinci-003"

class LMQLReplayStrategyMixin(BaseReplayStrategy):

    def __init__(
        self,
        recording: Recording,
        model_name: str = DEFAULT_MODEL,
    ):
        super().__init__(recording)

        logger.info(f"{model_name=}")
        self.model_name = model_name
        # TODO: check if model_name is valid ?! Should be able to work with all OpenAI models

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
        "Summarize {description} as a json: {{ "
        "'action': [ACTION], "
        if ACTION == "type":
            "'character': [CHAR] }}"
        if ACTION == "move":
            "'x': [X], 'y': [Y] }}"
        if ACTION == "scroll":
            "'change in x': [X], 'change in y': [Y] }}"
        if ACTION == "click":
            "'x': [X], 'y': [Y], 'click': [CLICK], 'num clicks': [NUM] }}"
    from
        model
    where
        ACTION in set(["move", "scroll", "type", "click"]) 
        and len(TOKENS(CHAR)) < 5 and STOPS_BEFORE(CHAR, " }") 
        and INT(X) and INT(Y) and len(TOKENS(X)) < 5 and len(TOKENS(Y)) < 5 
        and CLICK in set(["left", "right"]) and NUM in set(["doubleclick", "singleclick"])
    '''

def parse_action(result_dict: dict) -> ActionEvent:
    name = result_dict["ACTION"]
    logger.info(f"The action is: {name}")
    action_event = ActionEvent(name=name)

    if name == "type":
        key_with_whitespace = result_dict["CHAR"]
        key = key_with_whitespace.strip()
        if key[0] == "<" and key [-1] == ">" and (getattr(Key, key[1:-2], None) != None):
            action_event.key_name = key
            # what is the canonical_key_name? A number? The same thing w <>s ?!
        if len(key) == 1 or (getattr(Key, key, None) != None):
            action_event.key_name = key
            action_event.canonical_key_name = key
        else:
            # in case of error, return empty ActionEvent ?
            return ActionEvent()
    elif name == "move":
        action_event.mouse_x = result_dict["X"]
        action_event.mouse_y = result_dict["Y"]
    elif name == "scroll":
        action_event.mouse_dx = result_dict["X"]
        action_event.mouse_dy = result_dict["Y"]
    elif name == "click":
        action_event.name = result_dict["NUM"]
        action_event.mouse_x = result_dict["X"]
        action_event.mouse_y = result_dict["Y"]
        action_event.mouse_button_name = result_dict["CLICK"]
    
    return action_event
