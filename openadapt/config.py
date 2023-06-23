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

from dotenv import find_dotenv, load_dotenv
from loguru import logger

ROOT_DIRPATH = pathlib.Path(__file__).parent.parent.resolve()
ZIPPED_RECORDING_FOLDER_PATH = ROOT_DIRPATH / "data" / "zipped"

ENV_FILE_PATH = (ROOT_DIRPATH / ".env").resolve()
logger.info(f"{ENV_FILE_PATH=}")
dotenv_file = find_dotenv()
load_dotenv(dotenv_file)


def read_env_file(file_path):
    """Read the contents of an environment file.

    Args:
        file_path (str): The path to the environment file.

    Returns:
        dict: A dictionary containing the environment variables and their values.
    """
    env_vars = {}
    with open(file_path, "r") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                key, value = line.split("=", 1)
                env_vars[key] = value.strip('"')
    return env_vars


env_file_path = ".env"
env_vars = read_env_file(env_file_path)

DB_FNAME = env_vars.get("DB_FNAME")


def set_db_url(db_fname):
    """Set the database URL based on the given database file name.

    Args:
        db_fname (str): The database file name.
    """
    global DB_FNAME, DB_FPATH, DB_URL
    DB_FNAME = db_fname
    DB_FPATH = ROOT_DIRPATH / DB_FNAME
    DB_URL = f"sqlite:///{DB_FPATH}"
    logger.info(f"{DB_URL=}")


_DEFAULTS = {
    "CACHE_DIR_PATH": ".cache",
    "CACHE_ENABLED": True,
    "CACHE_VERBOSITY": 0,
    "DB_ECHO": False,
    "DB_FNAME": DB_FNAME,
    "OPENAI_API_KEY": "<set your api key in .env>",
    "OPENAI_MODEL_NAME": "gpt-3.5-turbo",
    "RECORD_READ_ACTIVE_ELEMENT_STATE": False,
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
    "SCRUB_FILL_COLOR": 0x0000FF, # BGR format
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
    """Get the value of an environment variable with fallback to default.

    Args:
        var_name (str): The name of the environment variable.

    Returns:
        str: The value of the environment variable or the default value if not found.

    Raises:
        ValueError: If the environment variable is not defined.
    """
    if var_name == "DB_FNAME":
        rval = _DEFAULTS.get(var_name)
    else:
        rval = os.getenv(var_name) or _DEFAULTS.get(var_name)
    if rval is None:
        raise ValueError(f"{var_name=} not defined")
    return rval


for key in _DEFAULTS:
    val = getenv_fallback(key)
    locals()[key] = val


ROOT_DIRPATH = pathlib.Path(__file__).parent.parent.resolve()
DB_FPATH = ROOT_DIRPATH / DB_FNAME  # type: ignore # noqa
DB_URL = f"sqlite:///{DB_FPATH}"
DIRNAME_PERFORMANCE_PLOTS = "performance"
DB_ECHO = False
DT_FMT = "%Y-%m-%d_%H-%M-%S"

if multiprocessing.current_process().name == "MainProcess":
    for key, val in locals().items():
        if not key.startswith("_") and key.isupper():
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