import pathlib

ROOT_DIRPATH = pathlib.Path(__file__).parent.parent.resolve()
ZIPPED_RECORDING_FOLDER_PATH = ROOT_DIRPATH / "data" / "zipped"
UNZIPPED_RECORDING_FOLDER_PATH = ROOT_DIRPATH / "data" / "unzipped"

DB_FNAME = "openadapt.db"

DB_FPATH = ROOT_DIRPATH / DB_FNAME
DB_URL = f"sqlite:///{DB_FPATH}"
DB_ECHO = False

DT_FMT = "%Y-%m-%d_%H-%M-%S"