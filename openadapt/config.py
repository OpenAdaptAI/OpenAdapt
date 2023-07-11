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
    # APP CONFIGURATIONS
    "DARK_MODE": True,
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
    "PLOT_PERFORMANCE": True,
}

# each string in STOP_STRS should only contain strings that don't contain special characters
STOP_STRS = [
    "oa.stop",
    # TODO:
    # "<ctrl>+c,<ctrl>+c,<ctrl>+c"
]
# each list in SPECIAL_CHAR_STOP_SEQUENCES should contain sequences
# containing special chars, separated by keys
SPECIAL_CHAR_STOP_SEQUENCES = [["ctrl", "ctrl", "ctrl"]]
# sequences that when typed, will stop the recording of ActionEvents in record.py
STOP_SEQUENCES = [
    list(stop_str) for stop_str in STOP_STRS
] + SPECIAL_CHAR_STOP_SEQUENCES


def getenv_fallback(var_name):
    rval = os.getenv(var_name) or _DEFAULTS.get(var_name)
    if type(rval) is str and rval.lower() in ("true", "false", "1", "0"):
        rval = rval.lower() == "true" or rval == "1"
    if rval is None:
        raise ValueError(f"{var_name=} not defined")
    return rval


def set_env(var_name, val):
    if not os.path.exists(".env"):
        with open(".env", "w") as f:
            f.write(f"{var_name}={val}")
    else:
        # find and replace
        with open(".env", "r") as f:
            lines = f.readlines()
        for i, line in enumerate(lines):
            if line.startswith(f"{var_name}="):
                lines[i] = f"{var_name}={val}\n"
                break
        else:
            if lines[-1][-1] != "\n":
                lines.append(f"\n{var_name}={val}")
            else:
                lines.append(f"{var_name}={val}")
        with open(".env", "w") as f:
            f.writelines(lines)


load_dotenv()

for key in _DEFAULTS:
    val = getenv_fallback(key)
    locals()[key] = val

ROOT_DIRPATH = pathlib.Path(__file__).parent.parent.resolve()
DB_FPATH = ROOT_DIRPATH / DB_FNAME  # type: ignore # noqa
DB_URL = f"sqlite:///{DB_FPATH}"
DIRNAME_PERFORMANCE_PLOTS = "performance"


def obfuscate(val, pct_reveal=0.1, char="*"):
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
    This function filters log messages by ignoring any message that contains a specific string.

    Args:
      data: The input parameter "data" is expected to be data from a loguru logger.

    Returns:
      a boolean value indicating whether the message in the input data should be ignored or not. If the
    message contains any of the messages in the `messages_to_ignore` list, the function returns `False`
    indicating that the message should be ignored. Otherwise, it returns `True` indicating that the
    message should not be ignored.
    """
    # TODO: ultimately, we want to fix the underlying issues, but for now, we can ignore these messages
    messages_to_ignore = [
        "Cannot pickle Objective-C objects",
    ]
    return not any(msg in data["message"] for msg in messages_to_ignore)
