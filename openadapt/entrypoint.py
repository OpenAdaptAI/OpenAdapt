"""Entrypoint for OpenAdapt."""

import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()


from openadapt.alembic.context_loader import load_alembic_context
from openadapt.app import tray  # noqa
from openadapt.build_utils import override_stdout_stderr
from openadapt.config import print_config


def run_openadapt() -> None:
    """Run OpenAdapt."""
    print_config()
    load_alembic_context()
    tray._run()


if __name__ == "__main__":
    with override_stdout_stderr():
        run_openadapt()
