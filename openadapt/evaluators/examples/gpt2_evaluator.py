from typing import Tuple

from loguru import logger
import fire
import transformers as tf

from openadapt import utils
from openadapt.evaluators.evaluator import BaseEvaluator
from openadapt.models import KeyAction, MouseAction, Window

LOG_LEVEL = "DEBUG"
MAX_SCREEN_SIZE = (1920, 1080)
MAX_INPUT_SIZE = 1024
MAX_TOKENS = 1024

utils.configure_logging(logger, LOG_LEVEL)


class GPT2Evaluator(BaseEvaluator):
    def __init__(
        self,
        model_name: str = "gpt2",
        max_input_size: int = MAX_INPUT_SIZE,
        max_tokens: int = MAX_TOKENS,
        max_screen_size: Tuple[int, int] = MAX_SCREEN_SIZE,
    ):
        super().__init__(
            model_name=model_name,
            max_input_size=max_input_size,
            max_tokens=max_tokens,
            max_screen_size=max_screen_size,
        )

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


def main():
    evaluation = GPT2Evaluator()
    evaluation.evaluate_single_action()


def start():
    fire.Fire(main)


if __name__ == "__main__":
    fire.Fire(main)
