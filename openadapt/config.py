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
from presidio_analyzer import AnalyzerEngine
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_anonymizer import AnonymizerEngine
from presidio_image_redactor import (
    ImageRedactorEngine,
    ImageAnalyzerEngine,
)


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


# ACTION EVENT CONFIGURATIONS
TEXT_SEP = "-"
TEXT_NAME_PREFIX = "<"
TEXT_NAME_SUFFIX = ">"




# SCRUBBING CONFIGURATIONS

SCRUB_CHAR = "*"
SCRUB_LANGUAGE = "en"
SCRUB_CONFIG_TRF = {
    "nlp_engine_name": "spacy",
    "models": [{"lang_code": "en", "model_name": "en_core_web_trf"}],
}
SCRUB_PROVIDER_TRF = NlpEngineProvider(nlp_configuration=SCRUB_CONFIG_TRF)
NLP_ENGINE_TRF = SCRUB_PROVIDER_TRF.create_engine()
ANALYZER_TRF = AnalyzerEngine(
    nlp_engine=NLP_ENGINE_TRF, supported_languages=["en"]
)
ANONYMIZER = AnonymizerEngine()
IMAGE_REDACTOR = ImageRedactorEngine(ImageAnalyzerEngine(ANALYZER_TRF))
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
SCRUBBING_ENTITIES = [
    entity
    for entity in ANALYZER_TRF.get_supported_entities()
    if entity not in SCRUB_IGNORE_ENTITIES
]
SCRUB_KEYS_HTML = [
    "text",
    "canonical_text",
    "title",
    "state",
    "task_description",
]
DEFAULT_SCRUB_FILL_COLOR = (255,)
