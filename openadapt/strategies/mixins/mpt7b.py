"""
Implements a ReplayStrategy mixin which gets completions from a 4-bit quantized
version of MPT-7b, for inference on CPU.


Usage:

    class MyReplayStrategy(MPT7BReplayStrategy):
        ...
"""

from ctransformers import AutoModelForCausalLM
from openadapt.models import Recording
from openadapt.strategies.base import BaseReplayStrategy


class MPT7BReplayStrategy(BaseReplayStrategy):
    """
    MPT-7B Replay Strategy for inference on CPU.
    """

    def __init__(
        self,
        recording: Recording
    ):
        super.__init__(recording)
        self.model_path = 'TheBloke/MPT-7B-GGML'
        self.model_file = 'mpt-7b.ggmlv3.q4_0.bin'

    def get_completion(
        self,
        completion: str
    ) -> str:
        llm = AutoModelForCausalLM.from_pretrained(
        self.model_path, model_type='mpt',
        model_file=self.model_file)

        return llm(completion)
