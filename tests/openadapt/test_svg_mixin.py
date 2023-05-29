from PIL import Image

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
    expected = "(10, 20)"
    actual = REPLAY.get_svg_info(one_button_screenshot)
    print(actual)
    assert actual == expected

# TODO
# def test_multiple_buttons():
#     multiple_button_screenshot = Image.open("assets/visualize.png")
#     # expected =   # TODO
#     actual = SVGMIXIN.get_svg_info(multiple_button_screenshot)
#     print(actual)
#     # assert actual == expected
