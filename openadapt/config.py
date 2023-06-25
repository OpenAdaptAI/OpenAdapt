"""Script containing configurations for the openadapt application.

Usage:

    from openadapt import config
    ...
    config.<setting>
    ...

"""

import multiprocessing
import os
import pathlib

from dotenv import load_dotenv
from loguru import logger


_DEFAULTS = {
    "CACHE_DIR_PATH": ".cache",
    "CACHE_ENABLED": True,
    "CACHE_VERBOSITY": 0,
    "DB_ECHO": False,
    "DB_FNAME": "openadapt.db",
    "OPENAI_API_KEY": "<set your api key in .env>",
    # "OPENAI_MODEL_NAME": "gpt-4",
    "OPENAI_MODEL_NAME": "gpt-3.5-turbo",
    # may incur significant performance penalty
    "RECORD_READ_ACTIVE_ELEMENT_STATE": False,
    # TODO: remove?
    "REPLAY_STRIP_ELEMENT_STATE": True,
    # IGNORES WARNINGS (PICKLING, ETC.)
    # TODO: ignore warnings by default on GUI
    "IGNORE_WARNINGS": False,
    # ACTION EVENT CONFIGURATIONS
    "ACTION_TEXT_SEP": "-",
    "ACTION_TEXT_NAME_PREFIX": "<",
    "ACTION_TEXT_NAME_SUFFIX": ">",
    # SCRUBBING CONFIGURATIONS
    "SCRUB_ENABLED": True,
    "SCRUB_CHAR": "*",
    "SCRUB_LANGUAGE": "en",
    # TODO support lists in getenv_fallback
    "SCRUB_FILL_COLOR": 0x0000FF,  # BGR format
    "SCRUB_CONFIG_TRF": {
        "nlp_engine_name": "spacy",
        "models": [{"lang_code": "en", "model_name": "en_core_web_trf"}],
    },
    "SCRUB_IGNORE_ENTITIES": [
        # 'US_PASSPORT',
        # 'US_DRIVER_LICENSE',
        # 'CRYPTO',
        # 'UK_NHS',
        # 'PERSON',
        # 'CREDIT_CARD',
        # 'US_BANK_NUMBER',
        # 'PHONE_NUMBER',
        # 'US_ITIN',
        # 'AU_ABN',
        "DATE_TIME",
        # 'NRP',
        # 'SG_NRIC_FIN',
        # 'AU_ACN',
        # 'IP_ADDRESS',
        # 'EMAIL_ADDRESS',
        "URL",
        # 'IBAN_CODE',
        # 'AU_TFN',
        # 'LOCATION',
        # 'AU_MEDICARE',
        # 'US_SSN',
        # 'MEDICAL_LICENSE'
    ],
    "SCRUB_KEYS_HTML": [
        "text",
        "canonical_text",
        "title",
        "state",
        "task_description",
        "key_char",
        "canonical_key_char",
        "key_vk",
        "children",
    ],
}


def getenv_fallback(var_name):
    """
    Get the value of an environment variable or fallback to the default value.

    Args:
        var_name (str): The name of the environment variable.

    Returns:
        The value of the environment variable or the fallback default value.

    Raises:
        ValueError: If the environment variable is not defined.
    """
    rval = os.getenv(var_name) or _DEFAULTS.get(var_name)
    if rval is None:
        raise ValueError(f"{var_name=} not defined")
    return rval


load_dotenv()

for key in _DEFAULTS:
    val = getenv_fallback(key)
    locals()[key] = val

ROOT_DIRPATH = pathlib.Path(__file__).parent.parent.resolve()
DB_FPATH = ROOT_DIRPATH / DB_FNAME  # type: ignore # noqa
DB_URL = f"sqlite:///{DB_FPATH}"
DIRNAME_PERFORMANCE_PLOTS = "performance"


def obfuscate(val, pct_reveal=0.1, char="*"):
    """
    Obfuscates a value by replacing a portion of characters.

    Args:
        val (str): The value to obfuscate.
        pct_reveal (float, optional): Percentage of characters to reveal (default: 0.1).
        char (str, optional): Obfuscation character (default: "*").

    Returns:
        str: Obfuscated value with a portion of characters replaced.

    Raises:
        AssertionError: If length of obfuscated value does not match original value.
    """
    num_reveal = int(len(val) * pct_reveal)
    num_obfuscate = len(val) - num_reveal
    obfuscated = char * num_obfuscate
    revealed = val[-num_reveal:]
    rval = f"{obfuscated}{revealed}"
    assert len(rval) == len(val), (val, rval)
    return rval


_OBFUSCATE_KEY_PARTS = ("KEY", "PASSWORD", "TOKEN")
if multiprocessing.current_process().name == "MainProcess":
    for key, val in dict(locals()).items():
        if not key.startswith("_") and key.isupper():
            parts = key.split("_")
            if (
                any([part in parts for part in _OBFUSCATE_KEY_PARTS])
                and val != _DEFAULTS[key]
            ):
                val = obfuscate(val)
            logger.info(f"{key}={val}")


def filter_log_messages(data):
    """
    Filter log messages by ignoring specific strings.

    Args:
        data (dict): Data from a loguru logger.

    Returns:
        bool: True if the message should not be ignored, False if it should be ignored.
    """
    # TODO: Implement log message filtering logic
    messages_to_ignore = [
        "Cannot pickle Objective-C objects",
    ]
    return not any(msg in data["message"] for msg in messages_to_ignore)
