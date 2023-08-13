from abc import abstractmethod
from typing import Tuple
import math

from loguru import logger

from openadapt import utils
from openadapt.evaluators import fixtures
from openadapt.models import KeyAction, MouseAction, Window

LOG_LEVEL = "DEBUG"
DEFAULT_MAX_SCREEN_SIZE = (1920, 1080)
DEFAULT_MAX_INPUT_SIZE = 1024
DEFAULT_MAX_TOKENS = 1024

utils.configure_logging(logger, LOG_LEVEL)


class BaseEvaluator:
    """Base class for all evaluations"""

    def __init__(
        self,
        model_name: str = "gpt2",
        max_input_size: int = DEFAULT_MAX_INPUT_SIZE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        max_screen_size: Tuple[int, int] = DEFAULT_MAX_SCREEN_SIZE,
    ):
        self.model_name = model_name
        self.max_input_size = max_input_size
        self.max_tokens = max_tokens
        self.max_screen_size = max_screen_size

    @abstractmethod
    def init_model(self):
        """Initialize the model. This must be adapted for each model, given attribute model name"""
        self.model = None

    @abstractmethod
    def get_completion(
        self,
        prompt: str,
    ):
        """Get the completion for a given prompt"""
        return ""

    def build_prompt(
        self, ref_window: Window, action: KeyAction | MouseAction, active_window: Window
    ):
        """Build the prompt for the model. This must be adapted for each model.

        Args:
            ref_window (Window): reference window
            action (KeyAction | MouseAction): action to be performed
            active_window (Window): active window

        Returns:
            str: prompt for the model
        """
        prompt = (
            f"{ref_window.dict()=} {action.dict()=} {active_window=} # Provide valid"
            " Python3 code containing the action dicts by completing the following,"
            " and nothing else: active_action_dict="
        )
        return prompt

    def evaluate_single_action(self) -> float:
        """Evaluate the model completion on a set of fixtures

        Returns:
            float: average score
        """
        self.init_model()
        # TODO: run evaluation on different fixtures
        ref_window, action, active_window = fixtures.generate_single_mouse()
        prompt = self.build_prompt(ref_window, action, active_window)
        completion = self.get_completion(prompt)
        score = self._score_completion(completion, active_window, action)
        logger.info(f"Average score is {score=}")
        return score

    def _is_valid_position(
        self, position: tuple[int, int], active_window: Window
    ) -> bool:
        if (
            position[0] < active_window.left
            or position[0] > active_window.left + active_window.width
        ):
            return False
        if (
            position[1] < active_window.top
            or position[1] > active_window.top + active_window.height
        ):
            return False
        return True

    def _score_valid_actions(
        self,
        ref_action: KeyAction | MouseAction,
        active_window: Window,
        prediction_action: KeyAction | MouseAction,
    ) -> bool:
        """Score the validity of the predicted action

        Args:
            ref_action (KeyAction | MouseAction): reference action
            active_window (Window): active window
            prediction_action (KeyAction | MouseAction): predicted action

        Returns:
            bool: True/False score to indicate this completion is valid or not
        """
        # verify if ref_action and prediction_action are of the same object type
        if type(ref_action) != type(prediction_action):
            return False
        # if it is same key action, same button pressed, give full score
        if type(ref_action) == KeyAction:
            if (
                ref_action.name == prediction_action.name
                and ref_action.key_name == prediction_action.key_name
            ):
                return True
            return False

        if type(ref_action) == MouseAction:
            if ref_action.name != prediction_action.name:
                return False
            if ref_action.mouse_button_name != prediction_action.mouse_button_name:
                return False
            if ref_action.mouse_pressed != prediction_action.mouse_pressed:
                return False

            ref_mouse_pos = (ref_action.mouse_x, ref_action.mouse_y)
            # verify if ref_mouse_pos is within the active window
            if not self._is_valid_position(ref_mouse_pos, active_window):
                return False
            return True

    def parse_completion(self, completion: str) -> KeyAction | MouseAction:
        """Parse a completion string to a KeyAction or MouseAction

        Args:
            completion (str): completion string

        Returns:
            KeyAction | MouseAction: action parsed from the completion string
        """
        try:
            results = eval(completion)
        except Exception as e:
            logger.error(f"Failed to parse completion: {e}, {completion=}")
            return None
        # TODO: handle list of actions
        result = results[0]
        # try to parse the result to a KeyAction or MouseAction
        try:
            action = KeyAction(**result)
            return action
        except Exception as e:
            try:
                action = MouseAction(**result)
                return action
            except Exception as e:
                logger.error(f"Failed to parse completion to action: {e}")
                return None

    def _score_completion(
        self,
        completion: str,
        active_window: Window,
        ref_action: KeyAction | MouseAction,
    ) -> bool:
        """Score a completion based on the reference action

        Args:
            completion (str): completion string
            ref_action (KeyAction | MouseAction): reference action

        Returns:
            bool: True/False score to indicate this completion is valid or not
        """
        completion = completion[completion.find("[") : completion.find("]") + 1]
        action = self.parse_completion(completion)
        if not action:
            return False
        score = self._score_valid_actions(ref_action, active_window, action)
        return score
