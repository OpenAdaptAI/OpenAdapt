"""Adapters for completion and segmentation."""

from types import ModuleType

from openadapt.config import config

# Lazy imports when required instead of importing everything
# Add omniparser which is needed for OmniMCP
from . import omniparser


# TODO: remove
def get_default_prompt_adapter() -> ModuleType:
    """Returns the default prompt adapter module.

    Returns:
        The module corresponding to the default prompt adapter.
    """
    return prompt


# TODO: refactor to follow adapters.prompt
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
