from pathlib import Path

from spacy.util import get_model_meta, load_model_from_init_py

__version__ = get_model_meta(Path(__file__).parent)["version"]


def load(**overrides):
    return load_model_from_init_py(__file__, **overrides)
