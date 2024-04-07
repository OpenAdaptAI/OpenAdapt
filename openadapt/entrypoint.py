import multiprocessing
import pathlib

if __name__ == "__main__":
    multiprocessing.freeze_support()

from openadapt.config import print_config
from openadapt.app import tray # noqa
from openadapt.alembic.context_loader import load_context

OPENADAPT_DIR = pathlib.Path(__file__).parent / "openadapt"


if __name__ == "__main__":
    print_config()
    load_context()
    tray._run()

