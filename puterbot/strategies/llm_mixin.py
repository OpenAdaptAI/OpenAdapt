"""
Implements a ReplayStrategy mixin for generating LLM completions.

Usage:

    class MyReplayStrategy(LLMReplayStrategyMixin):
        ...
"""

from loguru import logger
import transformers as tf  # RIP TensorFlow
import openai

import puterbot.config as config
from puterbot.models import Recording
from puterbot.strategies.base import BaseReplayStrategy

# Model options:
# hugging_face: gpt2, gpt2-xl is bigger and slower
# openai: gpt-3.5-turbo, gpt-4, gpt-4-32k
MODEL_NAME = "gpt-3.5-turbo"

# Platform options: 
# huggingface or openai
# Note that OPENAI_APIKEY and OPENAI_SYSTEM_MESSAGE must be set in config.py for OpenAI platform
PLATFORM_NAME = "openai"

# general input parsing
MAX_INPUT_SIZE = 1024

class LLMReplayStrategyMixin(BaseReplayStrategy):
    def __init__(
        self,
        recording: Recording,
        model_name: str = MODEL_NAME,
        platform: str = PLATFORM_NAME,
        max_input_size: str = MAX_INPUT_SIZE,
    ):
        super().__init__(recording)

        logger.info(f"{model_name=}")
        self.model_name = model_name
        self.platform = platform
        self.max_input_size = max_input_size

        if self.platform == "huggingface":
            self.tokenizer = tf.AutoTokenizer.from_pretrained(self.model_name)
            self.model = tf.AutoModelForCausalLM.from_pretrained(self.model_name)
        else:
            openai.api_key = config.OPENAI_APIKEY

    def get_completion(
        self,
        prompt: str,
        max_tokens: int,
    ):
        max_input_size = self.max_input_size
        if max_input_size and len(prompt) > max_input_size:
            logger.warning(
                f"Truncating from {len(prompt)=} to {max_input_size=}"
            )
            prompt = prompt[-max_input_size:]
            logger.warning(
                f"Truncated {len(prompt)=}"
            )

        logger.debug(f"{prompt=} {max_tokens=}")

        if self.platform == "huggingface":
            # Hugging face platform
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
            logger.debug(f"{completion=}")
        else:
            # Default to OpenAI as the platform

            # Build the messages first.
            messages = []
            sys_msg = config.OPENAI_SYSTEM_MESSAGE
            if sys_msg:
                messages.append({"role": "system", "content": sys_msg})
            messages.append({"role":"user", "content": prompt})

            # Get an OpenAI Chat completion
            response = openai.ChatCompletion.create(
                model=self.model_name,
                temperature=0,
                frequency_penalty=0.0,
                presence_penalty=0.0,
                messages=messages
            )
            completion = response.to_dict_recursive()['choices'][0]['message']['content']
        logger.debug(f"{completion=}")
        return completion
