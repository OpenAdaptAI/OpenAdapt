"""
Implements a ReplayStrategy mixin using Pypotrace to convert an image to SVG text.

NOTE: additional libraries on the system are needed,
        see https://github.com/flupke/pypotrace#installation

Usage:

    class MyReplayStrategy(SVGReplayStrategyMixin):
        ...
"""
import subprocess
import numpy as np
import drawsvg as draw

from openadapt.models import Recording, Screenshot
from openadapt.strategies.base import BaseReplayStrategy


class SVGReplayStrategyMixin(BaseReplayStrategy):
    """ReplayStrategy mixin that uses Pypotrace to convert an image to SVG text.

    Attributes:
        recording: the recording to be played back

    """
    def __init__(self, recording: Recording):
        super().__init__(recording)

    def get_svg_text(self, screenshot: Screenshot) -> str:
        input_img = screenshot.image
        input_img.save("input.png")

        subprocess.run(["vtracer", "--input", "input.png", "--output", "output.svg"])

        with open("output.svg", "r") as file:
            svg_text = file.read()

        # clean-up
        subprocess.run(["rm", "input.png"])
        subprocess.run(["rm", "output.svg"])

        return svg_text
