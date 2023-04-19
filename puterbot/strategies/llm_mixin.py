"""
Implements a ReplayStrategy mixin for generating LLM completions.
"""


from loguru import logger
import transformers as tf  # RIP TensorFlow

from puterbot.models import Recording
from puterbot.strategies.base import BaseReplayStrategy


MODEL_NAME = "gpt2-xl"  # gpt2-xl
MODEL_MAX_LENGTH = 1024


class LLMReplayStrategyMixin(BaseReplayStrategy):

    def __init__(
        self,
        recording: Recording,
        model_name=MODEL_NAME,
    ):
        super().__init__(recording)

        logger.info(f"{model_name=}")
        self.tokenizer = tf.AutoTokenizer.from_pretrained(model_name)
        self.model = tf.AutoModelForCausalLM.from_pretrained(model_name)

    def generate_completion(self, prompt, max_tokens):
        if MODEL_MAX_LENGTH and len(prompt) > MODEL_MAX_LENGTH:
            logger.warning(
                f"Truncating from {len(prompt)=} to {MODEL_MAX_LENGTH=}"
            )
            prompt = prompt[:MODEL_MAX_LENGTH]
        logger.info(f"{prompt=} {max_tokens=}")
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
        logger.info(f"{completion=}")

        return completion
