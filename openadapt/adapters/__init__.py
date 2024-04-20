"""Adapters for completion and segmentation."""

from types import ModuleType

from openadapt.config import config

from . import anthropic, google, openai, replicate, som, ultralytics


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
