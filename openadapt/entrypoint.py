"""Entrypoint for OpenAdapt."""

import multiprocessing

if __name__ == "__main__":
    # This needs to be called before any code that uses multiprocessing
    multiprocessing.freeze_support()


from openadapt.alembic.context_loader import load_alembic_context
from openadapt.app import tray
from openadapt.build_utils import redirect_stdout_stderr
from openadapt.config import print_config


def run_openadapt() -> None:
    """Run OpenAdapt."""
    print_config()
    load_alembic_context()
    tray._run()


if __name__ == "__main__":
    with redirect_stdout_stderr():
        run_openadapt()
