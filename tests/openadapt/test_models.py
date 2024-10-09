"""Tests for openadapt.models."""

from openadapt import models


def test_action_from_dict() -> None:
    """Test ActionEvent.from_dict()."""
    input_variations_by_expected_output = {
        ## all named keys
        "<backspace>-<backspace>-<backspace>": [
            # standard
            "<backspace>-<backspace>-<backspace>",
            # mal-formed
            "<backspace><backspace><backspace>",
            # mixed
            "<backspace>-<backspace><backspace>",
            "<backspace><backspace>-<backspace>",
        ],
        # TODO: support malformed configurations below
        ## all char keys
        "a-b-c": [
            # standard
            "a-b-c",
            # malformed
            #"abc",
            # mixed
            #"a-bc",
            #"ab-c",
        ],
        ## mixed named and char
        "<cmd>-t": [
            # standard
            "<cmd>-t",
            # malformed
            #"<cmd>t",
        ],
    }

    for expected_output, input_variations in input_variations_by_expected_output.items():
        for input_variation in input_variations:
            action_dict = {
                "name": "type",
                "text": input_variation,
                "canonical_text": input_variation,
            }
            print(f"{input_variation=}")
            action_event = models.ActionEvent.from_dict(action_dict)
            assert action_event.text == expected_output, action_event
