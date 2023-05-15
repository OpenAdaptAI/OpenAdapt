from dotenv import load_dotenv
from loguru import logger
import pathlib


_DEFAULTS = {
    "OPENAI_API_KEY": None,
    "DB_FNAME": "openadapt.db",
    "DB_ECHO": False,
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

for key, val in locals().items():
    if not key.startswith("_") and key.isupper():
        logger.info(f"{key}={val}")
