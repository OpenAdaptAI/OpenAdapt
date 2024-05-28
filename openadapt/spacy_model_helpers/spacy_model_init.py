"""Spacy model init file."""


from pathlib import Path

from spacy.language import Language
from spacy.util import get_model_meta, load_model_from_init_py

__version__ = get_model_meta(Path(__file__).parent)["version"]


def load(**overrides: dict) -> Language:
    """Load the model.

    Args:
        **overrides: Optional overrides for model settings.

    Returns:
        The loaded model.
    """
    return load_model_from_init_py(__file__, **overrides)
