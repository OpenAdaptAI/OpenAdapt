"""
Implements a ReplayStrategy mixin for generating completions with HuggingFace.

Usage:

    class MyReplayStrategy(LLMReplayStrategyMixin):
        ...
"""

import guidance
from loguru import logger
import transformers as tf  # RIP TensorFlow

from openadapt.models import Recording
from openadapt.strategies.base import BaseReplayStrategy


MODEL_NAME = "stabilityai/stablelm-base-alpha-3b"  # smallest model available
MAX_INPUT_SIZE = 1024
VALID_ACTIONS = ["character", "click"]
PROGRAM = guidance(""" {{description}} Given the above description, 
    fill in the following (with N/A where unapplicable) as a valid json
    ```json
    {
        "medium": "{{select 'medium' options=valid_medium}}",
        "mouse x-location": {{gen 'location' pattern='[0-9]+[0-9]+[0-9]+' stop=')'}},
        "mouse y-location": {{gen 'location' pattern='[0-9]+[0-9]+[0-9]+' stop=')'}},
        "character": {{gen 'character' pattern='[a-z]+' stop=','}}
        ]
    }```""")


class HuggingFaceReplayStrategyMixin(BaseReplayStrategy):
    """ReplayStrategy using HuggingFace Transformers.

    The prompts will be formatted using Microsoft Guidance.
    
    Attributes:
        tokenizer: the tokenizer associated with the model
        llm: the model to be prompted
        max_input_size: the max number of tokens the model can take as a prompt
    """

    def __init__(
        self,
        recording: Recording,
        model_name: str = MODEL_NAME,
        max_input_size: int = MAX_INPUT_SIZE,
    ):
        """Initialized the instance based on the recording and model.

        Args:
            recording: the recording to be played back
            model_name: the name of the Hugging Face model to be used
            max_input_size: the max number of tokens the model can take
        """
        super().__init__(recording)

        logger.info(f"{model_name=}")
        self.tokenizer = tf.AutoTokenizer.from_pretrained(model_name)
        self.llm = guidance.llms.Transformers("stabilityai/stablelm-base-alpha-3b")
        self.max_input_size = max_input_size

    def get_completion(
        self,
        prompt: str,
        max_tokens: int,
    ):
        max_input_size = self.max_input_size
        if max_input_size and len(prompt) > max_input_size:
            logger.warning(
                f"Truncating from {len(prompt) =} to {max_input_size=}"
            )
            prompt = prompt[-max_input_size:]
            logger.warning(
                f"Truncated {len(prompt)=}"
            )

        logger.debug(f"{prompt=} {max_tokens=}")
        input_tokens = self.tokenizer(prompt, return_tensors="pt")
        pad_token_id = self.tokenizer.eos_token_id
        attention_mask = input_tokens["attention_mask"]
        output_tokens = self.model.generate(
            input_ids=input_tokens["input_ids"],
            attention_mask=attention_mask,
            max_length=input_tokens["input_ids"].shape[-1] + max_tokens,
            pad_token_id=pad_token_id,
            num_return_sequences=1
        )
        N = input_tokens["input_ids"].shape[-1]
        completion = self.tokenizer.decode(
            output_tokens[:, N:][0],
            clean_up_tokenization_spaces=True,
        )

        completion = PROGRAM(description=prompt, valid_actions=VALID_ACTIONS)
        logger.debug(f"{completion=}")

        return completion
