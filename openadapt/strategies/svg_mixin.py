"""
Implements a ReplayStrategy mixin using the SVG image format and GPT4All.

NOTE: additional libraries on the system are needed,
        see https://github.com/flupke/pypotrace#installation

Usage:

    class MyReplayStrategy(SVGReplayStrategyMixin):
        ...
"""
from openadapt.pypotrace import potrace
from PIL import Image
from gpt4all import GPT4All
import numpy as np

from openadapt.models import Recording, Screenshot
from openadapt.strategies.base import BaseReplayStrategy

PROMPT = "This a screenshot of a computer's window screen. Return the centre of the button from the following svg code, " \
         "with the centre represented as one list of 2 numbers and only return that list inside a larger list. " \
         "I want to parse the text, so the entire answer " \
         "needs to be one list with one sublist with exactly 2 numbers."


class SVGReplayStrategyMixin(BaseReplayStrategy):
    """ReplayStrategy mixin that uses Potrace and GPT4All to detect information.

    Attributes:
        recording: the recording to be played back
        model: the GPT4All model used

    """
    def __init__(self, recording: Recording):
        super().__init__(recording)
        self.GPT4All_model = GPT4All("ggml-gpt4all-j-v1.3-groovy.bin")

    def get_svg_info(self, screenshot: Screenshot) -> str:
        """
        Returns the location of the buttons.
        """
        # convert png to pbm
        png_img = screenshot.image
        pbm_img = png_img.convert("1")

        # convert pbm to svg
        image_array = np.array(pbm_img)
        bitmap = potrace.Bitmap(image_array)
        path = bitmap.trace()
        svg_string = str(path)

        # give svg to GPT4ALL and ask it to return the location of the buttons
        messages = [{"role": "user", "content": PROMPT + svg_string}]
        response_dict = self.GPT4All_model.chat_completion(
            messages, default_prompt_header=False, verbose=False
        )
        return response_dict["choices"][0]["message"]["content"]
