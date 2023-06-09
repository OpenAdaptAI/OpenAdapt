from io import BytesIO, StringIO
from PIL import Image
import cairosvg as cairo
import numpy as np

from openadapt.strategies.demo import DemoReplayStrategy
from openadapt.models import Recording, Screenshot

RECORDING = Recording()
REPLAY = DemoReplayStrategy(RECORDING)


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

    print("almost done")
    difference = mse(expected, actual)
    print(difference)
    assert difference < 100

