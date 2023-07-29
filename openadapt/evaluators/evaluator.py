from abc import abstractmethod
from typing import Tuple
import math

from loguru import logger

from openadapt import utils
from openadapt.evaluators import fixtures
from openadapt.evaluators.data_models import KeyAction, MouseAction, Window

LOG_LEVEL = "DEBUG"
DEFAULT_MAX_SCREEN_SIZE = (1920, 1080)
DEFAULT_MAX_INPUT_SIZE = 1024
DEFAULT_MAX_TOKENS = 1024

utils.configure_logging(logger, LOG_LEVEL)


class BaseEvaluation:
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
        # [TODO] run evaluation on different fixtures
        ref_window, action, active_window = fixtures.generate_single_mouse()
        prompt = self.build_prompt(ref_window, action, active_window)
        completion = self.get_completion(prompt)
        score = self._score_completion(completion, action)
        logger.info(f"Average score is {score=}")
        return score

    def _euclidean_distance(
        self, point1: Tuple[float, float], point2: Tuple[float, float]
    ):
        """Calculate the euclidean distance between two points

        Args:
            point1 (Tuple[float, float]): first point
            point2 (Tuple[float, float]): second point

        Raises:
            ValueError: if either point is not 2D

        Returns:
            float: euclidean distance between the two points
        """
        if len(point1) != 2 or len(point2) != 2:
            raise ValueError("Both points must be 2D coordinates (x, y)")

        x1, y1 = point1
        x2, y2 = point2
        distance = math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)
        return distance

    def _score_valid_actions(
        self,
        ref_action: KeyAction | MouseAction,
        prediction_action: KeyAction | MouseAction,
    ) -> float:
        """Score the validity of the predicted action

        Args:
            ref_action (KeyAction | MouseAction): reference action
            prediction_action (KeyAction | MouseAction): predicted action

        Returns:
            float: score of the predicted action
        """
        score = 0
        # verify if ref_action and prediction_action are of the same object type
        if type(ref_action) != type(prediction_action):
            # if the actions are not of the same type, return 0.1, as a correct action
            # is already worth the score
            return score + 0.1
        # if it is same key action, same button pressed, give full score
        if type(ref_action) == KeyAction:
            score += 0.5
            if ref_action.name == prediction_action.name:
                score += 0.25
            if ref_action.key_name == prediction_action.key_name:
                score += 0.25
            return score

        # for mouse actions, give score for each attribute
        # the point click is compared by Euclidean distance to see how close the
        # predicted point is to the reference point
        if type(ref_action) == MouseAction:
            score += 0.5

            if ref_action.name == prediction_action.name:
                score += 0.10
            if ref_action.mouse_button_name == prediction_action.mouse_button_name:
                score += 0.10
            if ref_action.mouse_pressed == prediction_action.mouse_pressed:
                score += 0.10

            ref_mouse_pos = (ref_action.mouse_x, ref_action.mouse_y)
            prediction_mouse_pos = (
                prediction_action.mouse_x,
                prediction_action.mouse_y,
            )
            distance = self._euclidean_distance(ref_mouse_pos, prediction_mouse_pos)
            normalized_distance = distance / self.max_screen_size[0]
            # the smaller the distance, the better the score
            additional_score = 0.1 - normalized_distance
            score += additional_score
            return score

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
        # [TODO] handle list of actions
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
        self, completion: str, ref_action: KeyAction | MouseAction
    ) -> float:
        """Score a completion based on the reference action

        Args:
            completion (str): completion string
            ref_action (KeyAction | MouseAction): reference action

        Returns:
            float: score to represent the similarity between the completion and the reference action
        """
        completion = completion[completion.find("[") : completion.find("]") + 1]
        action = self.parse_completion(completion)
        if not action:
            return 0
        score = self._score_valid_actions(ref_action, action)
        return score
