"""
Implements a ReplayStrategy mixin using GPT4All.

Usage:

    class MyReplayStrategy(GPT4ALLReplayStrategyMixin):
        ...
"""
from gpt4all import GPT4All
import transformers as tf

from openadapt.models import Recording, Screenshot
from openadapt.strategies.base import BaseReplayStrategy

MODEL_NAME = "ggml-gpt4all-j-v1.3-groovy.bin"
ROLE = "user"


class GPT4ALLReplayStrategyMixin(BaseReplayStrategy):
    """ReplayStrategy mixin that uses GPT4All.

    Attributes:
        recording: the recording to be played back
        model: the GPT4All model to be prompted
        tokenizer: the tokenizer associated with the model
    """

    def __init__(self, recording: Recording, model_name: str = MODEL_NAME):
        super().__init__(recording)
        self.GPT4All_model = GPT4All(model_name)

    def get_gpt4all_completion(self, prompt: str, role: str = ROLE) -> str:
        """
        Returns the GPT4All model's response to the prompt as a string.

        Args:
            prompt: the instruction given to the model
            role: a string from ["system", "assistant", "user"]
                    that describes how the message should be organized
                    (default is "user")

        NOTE: for computers without GPU support, it is recommended that
        less than 750 tokens are used for the prompt and response.
        """
        messages = [{"role": role, "content": prompt}]
        response_dict = self.GPT4All_model.chat_completion(
            messages, default_prompt_header=False, verbose=False
        )
        return response_dict["choices"][0]["message"]["content"]
