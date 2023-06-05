from dotenv import find_dotenv, load_dotenv
from loguru import logger
import multiprocessing
import os
import pathlib

ROOT_DIRPATH = pathlib.Path(__file__).parent.parent.resolve()
ZIPPED_RECORDING_FOLDER_PATH = ROOT_DIRPATH / "data" / "zipped"
UNZIPPED_RECORDING_FOLDER_PATH = ROOT_DIRPATH / "data" / "unzipped"

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

if multiprocessing.current_process().name == "MainProcess":
    for key, val in locals().items():
        if not key.startswith("_") and key.isupper():
            logger.info(f"{key}={val}")

DB_FPATH = ROOT_DIRPATH / DB_FNAME
DB_URL = f"sqlite:///{DB_FPATH}"
DB_ECHO = False

DT_FMT = "%Y-%m-%d_%H-%M-%S"
