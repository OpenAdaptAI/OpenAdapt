"""Tests for adapters.openai."""

import pytest

from PIL import Image

from openadapt.adapters import openai


def test_prompt(calculator_image: Image) -> None:
    """Test image prompt."""
    prompt = "What is this a screenshot of?"
    try:
        result = openai.prompt(prompt, images=[calculator_image])
        assert "calculator" in result.lower(), result
    except ValueError as e:
        if "Incorrect API key" in str(e):
            pytest.xfail(f"ValueError due to incorrect API key: {e}")
        else:
            raise


if __name__ == "__main__":
    pytest.main()
