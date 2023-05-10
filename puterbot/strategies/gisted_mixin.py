"""
Implements an ReplayStrategy mixin for generating LLM completions
augmented with gisting on inputs.
"""

from loguru import logger
import transformers as tf

from puterbot.models import Recording
from puterbot.strategies.base import BaseReplayStrategy

from gisting.src import compress

MAX_INPUT_SIZE = 512
# 
MODEL_NAME = "t5"

class GistedLLMReplayStrategyMixin(BaseReplayStrategy):
    
    def __init__(self, recording: Recording, model_name: str = MODEL_NAME, max_input_size: int = MAX_INPUT_SIZE):
        super.__init__(Recording)

        logger.info(f"{model_name=}")
        self.max_input_size = max_input_size


    def get_completion(self, prompt: str):

        max_input_size = self.max_input_size
        if max_input_size and len(prompt) > max_input_size:
            logger.warning(
                f"Truncating from {len(prompt)=} to {max_input_size=}"
            )
            prompt = prompt[-max_input_size:]
            logger.warning(
                f"Truncated {len(prompt)=}"
            )
        
        logger.debug(f"{prompt=}")

        gisted_output = compress.compress(MODEL_NAME, prompt)
        logger.debug(f"{gisted_output=}")
        return gisted_output






