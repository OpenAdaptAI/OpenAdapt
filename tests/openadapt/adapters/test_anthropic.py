"""Tests for adapters.anthropic."""

from PIL import Image
import pytest

import anthropic

from openadapt import adapters


def test_prompt(calculator_image: Image) -> None:
    """Test image prompt."""
    prompt = "What is this a screenshot of?"
    try:
        result = adapters.anthropic.prompt(prompt, images=[calculator_image])
        assert "calculator" in result.lower(), result
    except anthropic.AuthenticationError as e:
        pytest.xfail(f"Anthropic AuthenticationError occurred: {e}")


if __name__ == "__main__":
    pytest.main()
