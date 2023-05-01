import pathlib

ROOT_DIRPATH = pathlib.Path(__file__).parent.parent.resolve()
DB_FNAME = "puterbot.db"
DB_FPATH = ROOT_DIRPATH / DB_FNAME
DB_URL = f"sqlite:///{DB_FPATH}"
DB_ECHO = False
