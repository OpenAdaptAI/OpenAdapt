"""Tests for adapters.openai"""

from PIL import Image

from openadapt.adapters import openai
from openadapt.utils import image2utf8


# TODO: handle failure
def test_prompt(calculator_image: Image):
    prompt = "What is this a screenshot of?"
    result = openai.prompt(prompt, base64_images=[image2utf8(calculator_image)])
    assert "calculator" in result.lower(), result


if __name__ == "__main__":
    pytest.main()
