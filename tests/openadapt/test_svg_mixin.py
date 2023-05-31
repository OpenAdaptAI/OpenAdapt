from io import BytesIO
from PIL import Image, ImageChops
import cairosvg as cairo
# NOTE: cairosvg may require additional dependencies
# as outlined in https://cairosvg.org/documentation/

from openadapt.strategies.demo import DemoReplayStrategy
from openadapt.models import Recording, Screenshot

RECORDING = Recording()
REPLAY = DemoReplayStrategy(RECORDING)

# TODO
# def test_no_button():
#     no_button_screenshot = Image.open("assets/no_button.png")
#     expected = ""
#     actual = REPLAY.get_svg_info(no_button_screenshot)
#     assert actual == expected


def test_one_button():
    one_button_image = Image.open("assets/one_button.png")
    one_button_screenshot = Screenshot()
    one_button_screenshot._image = one_button_image

    actual_svg_text = REPLAY.get_svg_text(one_button_screenshot)

    expected = one_button_image.convert("L")

    actual_svg_file = BytesIO(actual_svg_text)
    actual_png_file = BytesIO()
    cairo.svg2png(file_obj=actual_svg_file, write_to=actual_png_file)
    actual = Image.open(actual_png_file)

    difference = ImageChops.difference(expected, actual)
    print(difference)
    assert difference < 0.2


# TODO
# def test_multiple_buttons():
#     multiple_button_screenshot = Image.open("assets/visualize.png")
#     # expected =   # TODO
#     actual = SVGMIXIN.get_svg_info(multiple_button_screenshot)
#     print(actual)
#     # assert actual == expected
