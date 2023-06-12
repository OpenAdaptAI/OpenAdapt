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
    # ACTION EVENT CONFIGURATIONS
    "ACTION_TEXT_SEP": "-",
    "ACTION_TEXT_NAME_PREFIX": "<",
    "ACTION_TEXT_NAME_SUFFIX": ">",
    "IGNORE_WARNINGS": False,
}


def getenv_fallback(var_name):
    rval = os.getenv(var_name) or _DEFAULTS.get(var_name)
    if rval is None:
        raise ValueError(f"{var_name=} not defined")
    return rval


load_dotenv()

for key in _DEFAULTS:
    val = getenv_fallback(key)
    locals()[key] = val

ROOT_DIRPATH = pathlib.Path(__file__).parent.parent.resolve()
DB_FPATH = ROOT_DIRPATH / DB_FNAME
DB_URL = f"sqlite:///{DB_FPATH}"
DIRNAME_PERFORMANCE_PLOTS = "performance"

if multiprocessing.current_process().name == "MainProcess":
    for key, val in locals().items():
        if not key.startswith("_") and key.isupper():
            logger.info(f"{key}={val}")


# SCRUBBING CONFIGURATIONS
SCRUB_ENABLED = True
SCRUB_CHAR = "*"
SCRUB_LANGUAGE = "en"
SCRUB_CONFIG_TRF = {
    "nlp_engine_name": "spacy",
    "models": [{"lang_code": "en", "model_name": "en_core_web_trf"}],
}
DEFAULT_SCRUB_FILL_COLOR = (255,0,0)
SCRUB_IGNORE_ENTITIES = [
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
]
SCRUB_KEYS_HTML = [
    "text",
    "canonical_text",
    "title",
    "state",
    "task_description",
    "key_char",
    "canonical_key_char",
    "key_vk",
    "children",
]
