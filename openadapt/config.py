from dotenv import load_dotenv
from loguru import logger
import multiprocessing
import os
import pathlib
 

ROOT_DIRPATH = pathlib.Path(__file__).parent.parent.resolve()
ZIPPED_RECORDING_FOLDER_PATH = ROOT_DIRPATH / "data" / "zipped"
UNZIPPED_RECORDING_FOLDER_PATH = ROOT_DIRPATH / "data" / "unzipped"

DB_FNAME = "openadapt.db"


_DEFAULTS = {
    "CACHE_DIR_PATH": ".cache",
    "CACHE_ENABLED": True,
    "CACHE_VERBOSITY": 0,
    "DB_ECHO": False,
    "DB_FNAME": "openadapt.db",
    "OPENAI_API_KEY": "<set your api key in .env>",
    #"OPENAI_MODEL_NAME": "gpt-4",
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

if multiprocessing.current_process().name == "MainProcess":
    for key, val in locals().items():
        if not key.startswith("_") and key.isupper():
            logger.info(f"{key}={val}")

DB_FPATH = ROOT_DIRPATH / DB_FNAME
DB_URL = f"sqlite:///{DB_FPATH}"
DB_ECHO = False

DT_FMT = "%Y-%m-%d_%H-%M-%S"


ENV_FILE_PATH = (ROOT_DIRPATH / ".env").resolve()
logger.info(f"{ENV_FILE_PATH=}")
load_dotenv(ENV_FILE_PATH)

def set_db_fname(db_fname):
    global DB_FNAME
    DB_FNAME = db_fname
    set_db_fpath(DB_FNAME)
    logger.info(f"{DB_FNAME=}")


def set_db_fpath(db_fname):
    global DB_FPATH
    DB_FPATH = ROOT_DIRPATH / db_fname
    logger.info(f"{DB_FPATH=}")


def set_db_url(db_fpath):
    global DB_URL
    DB_URL = f"sqlite:///{db_fpath}"
    logger.info(f"{DB_URL=}")

set_db_fname(DB_FNAME)
