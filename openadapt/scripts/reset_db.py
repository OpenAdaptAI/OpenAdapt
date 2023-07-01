"""Reset Database Script.

This script clears the database by removing the database file and
running a database migration using Alembic.

Module: reset_db.py
"""

import os
from subprocess import run, PIPE
from openadapt import config


def reset_db() -> None:
    """Clears the database by removing the db file and running a db migration using Alembic."""
    if os.path.exists(config.DB_FPATH):
        os.remove(config.DB_FPATH)

    # Prevents duplicate logging of config values by piping stderr and filtering the output.
    result = run(["alembic", "upgrade", "head"], stderr=PIPE, text=True)
    print(result.stderr[result.stderr.find("INFO  [alembic") :])  # noqa: E203
    if result.returncode != 0:
        raise RuntimeError("Database migration failed.")
    else:
        print("Database cleared successfully.")


if __name__ == "__main__":
    reset_db()
