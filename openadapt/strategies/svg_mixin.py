"""
Implements a ReplayStrategy mixin for getting information from images
using the Pypotrace and GPT4All libraries.

Usage:

    class MyReplayStrategy(SVGReplayStrategyMixin):
        ...
"""
from openadapt.pypotrace import potrace
from PIL import Image
from gpt4all import GPT4All
import numpy as np
from loguru import logger

from openadapt.models import Recording, Screenshot
from openadapt.strategies.base import BaseReplayStrategy

PROMPT = "return the location of the buttons in the following svg string in a tuple without any english"


class SVGReplayStrategyMixin(BaseReplayStrategy):
    def __init__(self, recording: Recording):
        super().__init__(recording)
        self.GPT4All_model = GPT4All("ggml-gpt4all-j-v1.3-groovy.bin")

    def get_svg_info(self, screenshot: Screenshot) -> str:
        # convert png to pbm (etc.)
        png_img = screenshot.image
        pbm_img = png_img.convert("1")

        # convert pbm to svg (potrace)
        image_array = np.array(pbm_img)
        bitmap = potrace.Bitmap(image_array)
        path = bitmap.trace()
        svg_string = str(path)
        print("this is the svg string" + svg_string)

        # give svg to GPT4ALL (for example) and ask it to return the location of the buttons (as a tuple ?! tbd)
        messages = [{"role": "user", "content": PROMPT + svg_string}]
        response = self.GPT4All_model.chat_completion(
            messages, default_prompt_header=False, verbose=False
        )
        return response
