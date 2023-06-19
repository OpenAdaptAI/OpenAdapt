"""
Implements a ReplayStrategy mixin using VTracer to convert an image to SVG text.

Usage:

    class MyReplayStrategy(SVGReplayStrategyMixin):
        ...
"""
import subprocess
import tempfile

from openadapt.models import Recording, Screenshot
from openadapt.strategies.base import BaseReplayStrategy


class SVGReplayStrategyMixin(BaseReplayStrategy):
    """ReplayStrategy mixin that uses VTracer to convert an image to SVG text.

    Attributes:
        recording: the recording to be played back

    """
    def __init__(self, recording: Recording):
        super().__init__(recording)

    def get_svg_text(self, screenshot: Screenshot) -> str:
        input_img = screenshot.image

        temp_output = tempfile.NamedTemporaryFile(delete=False, suffix='.svg')

        # Create a temporary png file with the image
        #   delete=False so the file is deleted after the function returns
        with tempfile.NamedTemporaryFile(suffix='.png') as temp_input:
            # Save the image to the temporary file
            input_img.save(temp_input, 'PNG')
            subprocess.run(["vtracer", "--input", temp_input.name, "--output", temp_output.name])

        with open(temp_output.name, "r") as file:
            svg_text = file.read()

        return svg_text
