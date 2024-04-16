"""Adapters for completion and segmentation."""

from types import ModuleType

from openadapt import config
from . import anthropic
from . import openai
from . import replicate
from . import som
from . import ultralytics
from . import google


def get_default_prompt_adapter() -> ModuleType:
    """Returns the default prompt adapter module.

    Returns:
        The module corresponding to the default prompt adapter.
    """
    return {
        "openai": openai,
        "anthropic": anthropic,
        "google": google,
    }[config.DEFAULT_ADAPTER]


def get_default_segmentation_adapter() -> ModuleType:
    """Returns the default image segmentation adapter module.

    Returns:
        The module corresponding to the default segmentation adapter.
    """
    return {
        "som": som,
        "replicate": replicate,
        "ultralytics": ultralytics,
    }[config.DEFAULT_SEGMENTATION_ADAPTER]


__all__ = ["anthropic", "openai", "replicate", "som", "ultralytics", "google"]
