"""Test openadapt.utils."""

from openadapt import utils


def test_get_scale_ratios() -> None:
    """Tests utils.get_scale_ratios."""
    # TODO: pass in action event
    width, height = utils.get_scale_ratios()

    assert isinstance(
        width, (int, float)
    ), f"Expected width to be int or float, got {type(width).__name__}"
    assert isinstance(
        height, (int, float)
    ), f"Expected height to be int or float, got {type(height).__name__}"
