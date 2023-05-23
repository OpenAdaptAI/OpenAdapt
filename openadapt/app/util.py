import bz2
import os
import sys
from shutil import copyfileobj

import requests
from nicegui import ui


def clear_db():
    if os.path.exists("openadapt.db"):
        os.remove("openadapt.db")
    os.system("alembic upgrade head")
    print("Database cleared.", file=sys.stderr)


def on_import(selected_file, delete=False, src="openadapt.db"):
    with open(src, "wb") as f:
        with bz2.BZ2File(selected_file, "rb") as f2:
            copyfileobj(f2, f)

    if delete:
        os.remove(selected_file)

    ui.notify("Imported data.")


def on_export(dest):
    # TODO: add ui card for configuration
    ui.notify("Exporting data to server...")

    # compress db with bz2
    with open("openadapt.db", "rb") as f:
        with bz2.BZ2File("openadapt.db.bz2", "wb", compresslevel=9) as f2:
            copyfileobj(f, f2)

    # upload to server with requests, and keep file name
    files = {
        "files": open("openadapt.db.bz2", "rb"),
    }

    requests.post(dest, files=files)

    # delete compressed db
    os.remove("openadapt.db.bz2")

    ui.notify("Exported data.")
