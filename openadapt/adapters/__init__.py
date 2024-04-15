"""Adapters for completion and segmentation."""

from . import anthropic
from . import openai
from . import replicate
from . import som
from . import ultralytics
from . import google

__all__ = ['anthropic', 'openai', 'replicate', 'som', 'ultralytics', 'google']
