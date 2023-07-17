from io import BytesIO, StringIO
import os
import platform
import subprocess

from PIL import Image
import cairosvg as cairo
import numpy as np
from loguru import logger

from openadapt.strategies.mixins.svg import SVGReplayStrategyMixin, is_dependency_installed
from openadapt.models import Recording, Screenshot


RECORDING = Recording()


class SVGReplayStrategy(SVGReplayStrategyMixin):
    """Custom Replay Strategy to solely test the SVG Mixin."""
    def __init__(self, recording: Recording, cairo_executable_path: str = "cairosvg"):
        if platform.system() == "Darwin" and not is_dependency_installed(cairo_executable_path):
            cpu = subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"])
            if "Apple" in cpu:
                subprocess.run(["arch", "-arm64", "brew", "install", "cairo"])
            else:
                subprocess.run(["brew", "install", "cairo"])
            root_directory = os.path.expanduser('~')
            self.path_to_cairosvg = os.path.join(root_directory, ".cargo/bin")
        else:
            self.path_to_cairosvg = cairo_executable_path
        super().__init__(recording)

    def get_next_action_event(self):
        pass


REPLAY = SVGReplayStrategy(RECORDING)


def mse(image1, image2):
    """Calculate the Mean Squared Error (MSE) between two images
    """
    array1 = np.array(image1)
    array2 = np.array(image2)

    err = np.sum((array1.astype('float') - array2.astype('float')) ** 2)
    err /= float(array1.shape[0] * array1.shape[1])
    return err


def test_one_button():
    # set up the screenshot
    expected = Image.open("assets/one_button.png")

    one_button_screenshot = Screenshot()
    one_button_screenshot._image = expected

    # get the actual image
    actual_svg_text = REPLAY.get_svg_text(one_button_screenshot)
    actual_svg_file = StringIO(actual_svg_text)
    actual_png_file = BytesIO()
    cairo.svg2png(file_obj=actual_svg_file, write_to=actual_png_file)
    actual = Image.open(actual_png_file)

    logger.info("saved test images")
    difference = mse(expected, actual)
    logger.info(f"{difference=}")
    assert difference < 100

