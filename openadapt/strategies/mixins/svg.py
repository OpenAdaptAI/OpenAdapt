"""
Implements a ReplayStrategy mixin using VTracer to convert an image to SVG text.

Usage:

    class MyReplayStrategy(SVGReplayStrategyMixin):
        ...
"""
import os
import platform
import subprocess
import tempfile

from openadapt.dependencies import ensure_dependency, is_dependency_installed
from openadapt.models import Recording, Screenshot
from openadapt.strategies.base import BaseReplayStrategy


class SVGReplayStrategyMixin(BaseReplayStrategy):
    """ReplayStrategy mixin that uses VTracer to convert an image to SVG text.

    Attributes:
        recording: the recording to be played back
        path_to_vtracer: the absolute path to the cargo command
    """

    def __init__(self, recording: Recording, vtracer_executable_path: str = "vtracer"):
        if not is_dependency_installed(vtracer_executable_path):
            path_to_cargo = ensure_dependency("cargo")
            subprocess.run(["cargo", "install", "vtracer"])
            head, _ = os.path.split(path_to_cargo)
            self.path_to_vtracer = os.path.join(head, "vtracer")
        else:
            self.path_to_vtracer = vtracer_executable_path

        super().__init__(recording)

    def get_svg_text(self, screenshot: Screenshot) -> str:
        input_img = screenshot.image

        temp_output = tempfile.NamedTemporaryFile(delete=False, suffix=".svg")

        # Create a temporary png file with the image
        #   delete=False so the file is deleted after the function returns
        with tempfile.NamedTemporaryFile(suffix=".png") as temp_input:
            # Save the image to the temporary file
            input_img.save(temp_input, "PNG")
            subprocess.run(
                [
                    self.path_to_vtracer,
                    "--input",
                    temp_input.name,
                    "--output",
                    temp_output.name,
                ]
            )

        with open(temp_output.name, "r") as file:
            svg_text = file.read()

        return svg_text
