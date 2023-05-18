import pathlib

ROOT_DIRPATH = pathlib.Path(__file__).parent.parent.resolve()
RECORDING_DIR_PATH = ROOT_DIRPATH / "puterbot" / "recordings"
DB_FNAME = "puterbot.db"

DB_FPATH = ROOT_DIRPATH / DB_FNAME
DB_URL = f"sqlite:///{DB_FPATH}"
DB_ECHO = False

DT_FMT = "%Y-%m-%d_%H-%M-%S"