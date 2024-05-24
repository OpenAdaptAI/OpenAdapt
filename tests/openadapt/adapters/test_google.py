"""Tests for adapters.google"""

from google.api_core.exceptions import DeadlineExceeded, InvalidArgument
from PIL import Image
import pytest

from openadapt.adapters import google
from openadapt.utils import image2utf8


def test_prompt(calculator_image: Image):
    try:
        prompt = "What is this a screenshot of?"
        result = google.prompt(prompt, base64_images=[image2utf8(calculator_image)])
        assert "calculator" in result.lower(), result
    except InvalidArgument:
        pytest.xfail("Invalid API key, expected failure.")
    except DeadlineExceeded:
        pytest.xfail("Request timeout, expected failure.")


if __name__ == "__main__":
    pytest.main()
