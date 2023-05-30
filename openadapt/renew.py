import os
from subprocess import run, PIPE
from openadapt.config import getenv_fallback


def clear_db():
    db = getenv_fallback("DB_FNAME")
    if os.path.exists(db):
        os.remove(db)
    result = run(["alembic", "upgrade", "head"], stderr=PIPE, text=True)
    print(result.stderr[result.stderr.find("INFO  [alembic") :])
    if result.returncode != 0:
        raise RuntimeError("Database migration failed.")
    else:
        print("Database cleared successfully.")


if __name__ == "__main__":
    clear_db()
