from dotenv import load_dotenv
from loguru import logger
import pathlib
 

ROOT_DIRPATH = pathlib.Path(__file__).parent.parent.resolve()
ZIPPED_RECORDING_FOLDER_PATH = ROOT_DIRPATH / "data" / "zipped"
UNZIPPED_RECORDING_FOLDER_PATH = ROOT_DIRPATH / "data" / "unzipped"

DB_FNAME = "openadapt.db"

DB_FPATH = ROOT_DIRPATH / DB_FNAME
DB_URL = f"sqlite:///{DB_FPATH}"
DB_ECHO = False

DT_FMT = "%Y-%m-%d_%H-%M-%S"


ENV_FILE_PATH = (ROOT_DIRPATH / ".." / ".env").resolve()
logger.info(f"{ENV_FILE_PATH=}")
load_dotenv(ENV_FILE_PATH)

def set_db_fname(db_fname):
    global DB_FNAME
    DB_FNAME = db_fname
    set_db_fpath()

def set_db_fpath():
    global DB_FPATH
    DB_FPATH = ROOT_DIRPATH / DB_FNAME

def set_db_url():
    global DB_URL
    DB_URL = f"sqlite:///{DB_FPATH}"
    logger.info(f"{DB_URL=}")

set_db_url()