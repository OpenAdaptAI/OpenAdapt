"""Tests for adapters.openai"""

from PIL import Image

from openadapt.adapters import openai


# TODO: handle failure
def test_prompt(calculator_image: Image):
    prompt = "What is this a screenshot of?"
    result = openai.prompt(prompt, images=[calculator_image])
    assert "calculator" in result.lower(), result


if __name__ == "__main__":
    pytest.main()
