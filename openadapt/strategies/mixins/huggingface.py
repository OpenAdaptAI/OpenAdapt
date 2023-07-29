"""Implements a ReplayStrategy mixin for generating completions with HuggingFace.

Usage:

    class MyReplayStrategy(HuggingFaceReplayStrategyMixin):
        ...
"""

from loguru import logger
import transformers as tf  # RIP TensorFlow

from openadapt.models import Recording
from openadapt.strategies.base import BaseReplayStrategy

MODEL_NAME = "gpt2"  # gpt2-xl is bigger and slower
MAX_INPUT_SIZE = 1024


class HuggingFaceReplayStrategyMixin(BaseReplayStrategy):
    """ReplayStrategy mixin for generating completions with HuggingFace."""

    def __init__(
        self,
        recording: Recording,
        model_name: str = MODEL_NAME,
        max_input_size: int = MAX_INPUT_SIZE,
    ) -> None:
        """Initialize the HuggingFaceReplayStrategyMixin.

        Args:
            recording (Recording): The recording to replay.
            model_name (str): The name of the HuggingFace model to use
              (default: "gpt2").
            max_input_size (int): The maximum input size for the model
              (default: 1024).
        """
        super().__init__(recording)

        logger.info(f"{model_name=}")
        self.tokenizer = tf.AutoTokenizer.from_pretrained(model_name)
        self.model = tf.AutoModelForCausalLM.from_pretrained(model_name)
        self.max_input_size = max_input_size

    def get_completion(
        self,
        prompt: str,
        max_tokens: int,
    ) -> str:
        """Generate completion for a given prompt using the HuggingFace model.

        Args:
            prompt (str): The prompt for generating completion.
            max_tokens (int): The maximum number of tokens to generate.

        Returns:
            str: The generated completion.
        """
        max_input_size = self.max_input_size
        if max_input_size and len(prompt) > max_input_size:
            logger.warning(f"Truncating from {len(prompt) =} to {max_input_size=}")
            prompt = prompt[-max_input_size:]
            logger.warning(f"Truncated {len(prompt)=}")

        logger.debug(f"{prompt=} {max_tokens=}")
        input_tokens = self.tokenizer(prompt, return_tensors="pt")
        pad_token_id = self.tokenizer.eos_token_id
        attention_mask = input_tokens["attention_mask"]
        output_tokens = self.model.generate(
            input_ids=input_tokens["input_ids"],
            attention_mask=attention_mask,
            max_length=input_tokens["input_ids"].shape[-1] + max_tokens,
            pad_token_id=pad_token_id,
            num_return_sequences=1,
        )
        N = input_tokens["input_ids"].shape[-1]
        completion = self.tokenizer.decode(
            output_tokens[:, N:][0],
            clean_up_tokenization_spaces=True,
        )
        logger.debug(f"{completion=}")
        return completion
