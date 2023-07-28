from typing import Tuple
import math

from loguru import logger
import fire
import transformers as tf

from openadapt import utils
from openadapt.models_evaluation import fixtures
from openadapt.models_evaluation.data_models import KeyAction, MouseAction, Window

LOG_LEVEL = "DEBUG"
MAX_SCREEN_SIZE = (1920, 1080)
MAX_INPUT_SIZE = 1024
MAX_TOKENS = 1024

utils.configure_logging(logger, LOG_LEVEL)


class BaseEvaluation:
    def __init__(
        self,
        model_name: str = "gpt2",
        max_input_size: int = MAX_INPUT_SIZE,
        max_tokens: int = MAX_TOKENS,
    ):
        self.model_name = model_name
        self.max_input_size = max_input_size
        self.max_tokens = max_tokens

    def init_model(self):
        logger.info(f"{self.model_name=}")
        self.tokenizer = tf.AutoTokenizer.from_pretrained(self.model_name)
        self.model = tf.AutoModelForCausalLM.from_pretrained(self.model_name)

    def get_completion(
        self,
        prompt: str,
    ):
        if len(prompt) > self.max_input_size:
            logger.warning(f"Truncating from {len(prompt) =} to {self.max_input_size=}")
            prompt = prompt[-self.max_input_size :]
            logger.warning(f"Truncated {len(prompt)=}")
        input_tokens = self.tokenizer(prompt, return_tensors="pt")
        pad_token_id = self.tokenizer.eos_token_id
        attention_mask = input_tokens["attention_mask"]
        output_tokens = self.model.generate(
            input_ids=input_tokens["input_ids"],
            attention_mask=attention_mask,
            pad_token_id=pad_token_id,
            max_length=1000,
            num_return_sequences=1,
        )
        N = input_tokens["input_ids"].shape[-1]
        completion = self.tokenizer.decode(
            output_tokens[:, N:][0],
            clean_up_tokenization_spaces=True,
        )
        completion = self.tokenizer.decode(output_tokens[0])
        return completion

    def build_prompt(
        self, ref_window: Window, action: KeyAction | MouseAction, active_window: Window
    ):
        prompt = (
            f"{ref_window.dict()=} {action.dict()=} {active_window=} # Provide valid"
            " Python3 code containing the action dicts by completing the following,"
            " and nothing else: active_action_dict="
        )
        return prompt

    def evaluate(self):
        # [TODO] run evaluation on different fixtures
        ref_window, action, active_window = fixtures.generate_single_mouse()
        prompt = self.build_prompt(ref_window, action, active_window)
        completion = self.get_completion(prompt)
        score = self._score_completion(completion, action)
        logger.info(f"Average score is {score=}")

    def _euclidean_distance(
        self, point1: Tuple[float, float], point2: Tuple[float, float]
    ):
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
        score = 0
        # verify if ref_action and prediction_action are of the same object type
        if type(ref_action) != type(prediction_action):
            return score + 0
        if type(ref_action) == KeyAction:
            score += 0.5

            if ref_action.name == prediction_action.name:
                score += 0.25
            if ref_action.key_name == prediction_action.key_name:
                score += 0.25
            return score

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
            normalized_distance = distance / MAX_SCREEN_SIZE[0]
            # the smaller the distance, the better the score
            additional_score = 0.1 - normalized_distance
            score += additional_score
            return score

    def _parse_completion(self, completion: str) -> KeyAction | MouseAction:
        results = completion[completion.find("[") : completion.find("]") + 1]
        try:
            results = eval(results)
        except Exception as e:
            logger.error(f"Failed to parse completion: {e}, {results=}")
            return None
        for result in results:
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
        # try to parse the completion to a list of action dicts
        completion = completion[completion.find("[") : completion.find("]") + 1]
        action = self._parse_completion(completion)
        if not action:
            return 0
        score = self._score_valid_actions(ref_action, action)
        return score


def main():
    evaluation = BaseEvaluation()
    evaluation.init_model()
    evaluation.evaluate()


# entry point
def start():
    fire.Fire(main)


if __name__ == "__main__":
    fire.Fire(main)
