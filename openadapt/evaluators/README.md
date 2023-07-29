### How does the evaluator work ?

The `BaseEvaluator` class perform the following action:

- For a given model, generate a single action (either mouse action or key press action)
- Give a single window reference and action reference, run the completion to get the predicted action.
- Evaluate the predicted action with the refrence action.


The `BaseEvaluator` class does NOT implement code related to model prompts and completion. It only peform the evaluation based on a basic single mouse action or key board action.

The score is given as following:

- As long as the completion can be parsed to a valid action, a score is given.
- A maximum score of 0.9-1 is given to the prediction which predicts the exact same action as the reference.
- If it is a key action, then compare the type of action : `press` or `release` and the key used.
- If it is a mouse action, then compare the type of action: `click` or `move`, then calculate the normalized `Euclidean distance` between the reference point and the predicted point.



### How to use the `evaluator` module

1. For generation of simple action fixtures:

```python
from openadapt.evaluators import fixtures
ref_window, action, active_window = fixtures.generate_single_mouse()
ref_window, action, active_window = fixtures.generate_multi_click()
ref_window, action, active_window = fixtures.generate_multi_action_sequence()
```

2. For evaluation of a model

Refer to `examples` for a simple examples. In order to evaluate, for example, a `fine-tuned-model`, we need to add the a class which inherits from `BaseEvaluator`
and implement the following methods

- `init_model`: how to init model and tokenizer
- `get_completion`: how to get completion from model
- `build_prompt`: how to build the prompt
- `parse_completion`: how to parse a completion to a valid Action


As these methods are model specific, it is not implemented inside `BaseEvaluator`

```python
from typing import Tuple

from loguru import logger
import fire
import transformers as tf

from openadapt import utils
from openadapt.evaluators.data_models import KeyAction, MouseAction, Window
from openadapt.evaluators.evaluator import BaseEvaluation

LOG_LEVEL = "DEBUG"
MAX_SCREEN_SIZE = (1920, 1080)
MAX_INPUT_SIZE = 1024
MAX_TOKENS = 1024

utils.configure_logging(logger, LOG_LEVEL)


class MyFineTunedModelEvaluator(BaseEvaluation):
    def __init__(
        self,
        model_name: str = "my-fine-tuned-model",
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





```
