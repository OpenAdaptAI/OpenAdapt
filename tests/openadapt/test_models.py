"""Tests for openadapt.models."""

from openadapt import models


def test_action_from_dict() -> None:
    """Test ActionEvent.from_dict()"""

    texts = [
        # standard
        "<backspace>-<backspace>-<backspace>",
        # mal-formed
        "<backspace><backspace><backspace>",
        # mixed
        "<backspace>-<backspace><backspace>",
        "<backspace><backspace>-<backspace>",
    ]

    for text in texts:
        action_dict = {
            "name": "type",
            "text": text,
            "canonical_text": text,
        }
        print(f"{text=}")
        action_event = models.ActionEvent.from_dict(action_dict)
        assert action_event.text == "<backspace>-<backspace>-<backspace>", action_event
