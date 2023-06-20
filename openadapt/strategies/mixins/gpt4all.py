"""
Implements a ReplayStrategy mixin using GPT4All.

Usage:

    class MyReplayStrategy(GPT4ALLReplayStrategyMixin):
        ...
"""

from gpt4all import GPT4All
from transformers import AutoTokenizer
from loguru import logger

from openadapt.models import Recording
from openadapt.strategies.base import BaseReplayStrategy

MODEL_NAME = "ggml-gpt4all-j-v1.3-groovy.bin"
# the following keys are the names of the model on GPT4All,
#   and the value is a list with the 1st element being the name of the model on huggingface,
#   and the 2nd element being the revision run name
MODEL_DICTIONARY = {
    "ggml-gpt4all-j-v1.3-groovy.bin": "gpt4all-j",
    "ggml-replit-code-v1-3b.bin": "ggml-replit-code-v1-3b",
    "ggml-mpt-7b-chat.bin": "gpt4all-mpt",
    "ggml-mpt-7b-base.bin": "gpt4all-mpt",
    "ggml-mpt-7b-instruct.bin": "gpt4all-mpt",
    "GPT4All-13B-snoozy.ggmlv3.q4_0.bin": "gpt4all-13b-snoozy",
}
ROLE = "user"


class GPT4ALLReplayStrategyMixin(BaseReplayStrategy):
    """ReplayStrategy mixin that uses GPT4All.

    Attributes:
        recording: the recording to be played back
        model: the name of the GPT4All model to be prompted
        tokenizer: the name of the tokenizer associated with the model
    """

    def __init__(
        self, recording: Recording, model_name: str = MODEL_NAME, tokenizer: str = None
    ):
        super().__init__(recording)
        self.GPT4All_model = GPT4All(model_name)
        if tokenizer is None:
            if model_name in MODEL_DICTIONARY:
                tokenizer = "nomic-ai/" + MODEL_DICTIONARY[model_name]
            else:
                logger.info("There is no known tokenizer for %s", model_name)
                return
        self.tokenizer = AutoTokenizer.from_pretrained(tokenizer)

    def get_gpt4all_completion(self, prompt: str, role: str = ROLE) -> str:
        """
        Returns the GPT4All model's response to the prompt as a string.

        Args:
            prompt: the instruction given to the model
            role: a string from ["system", "assistant", "user"]
                    that describes how the message should be organized
                    (default is "user")

        NOTE: As indicated in logger msg, for computers without GPU support, it is recommended that
        less than 750 tokens are used for the prompt and response.
        """
        encoded_dict = self.tokenizer.encode(prompt)
        num_tokens = len(encoded_dict[0])
        if num_tokens > 750:
            logger.info(
                "For computers without GPU support, \
                        it is recommended that <750 tokens are used for the prompt and response. \
                         There are %s tokens in the prompt",
                num_tokens,
            )

        messages = [{"role": role, "content": prompt}]

        response_dict = self.GPT4All_model.chat_completion(
            messages, default_prompt_header=False, verbose=False
        )
        return response_dict["choices"][0]["message"]["content"]
