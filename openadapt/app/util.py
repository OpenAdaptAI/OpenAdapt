import bz2
import os
import sys
from shutil import copyfileobj

from nicegui import ui

from openadapt import config
from openadapt.scripts.reset_db import reset_db


def clear_db(log=None):
    if log:
        log.log.clear()
        o = sys.stdout
        sys.stdout = sys.stderr

    reset_db()
    ui.notify("Cleared database.")
    sys.stdout = o


def on_import(selected_file, delete=False, src="openadapt.db"):
    with open(src, "wb") as f:
        with bz2.BZ2File(selected_file, "rb") as f2:
            copyfileobj(f2, f)

    if delete:
        os.remove(selected_file)

    ui.notify("Imported data.")


def on_export(dest):
    # TODO: add ui card for configuration
    ui.notify("Exporting data...")

    # compress db with bz2
    with open("openadapt.db", "rb") as f:
        with bz2.BZ2File("openadapt.db.bz2", "wb", compresslevel=9) as f2:
            copyfileobj(f, f2)

    # TODO: magic wormhole
    # # upload to server with requests, and keep file name
    # files = {
    #     "files": open("openadapt.db.bz2", "rb"),
    # }
    # #requests.post(dest, files=files)

    # delete compressed db
    os.remove("openadapt.db.bz2")

    ui.notify("Exported data.")


def sync_switch(switch, prop):
    switch.value = prop.value if hasattr(prop, "value") else prop


def set_scrub(value):
    if config.SCRUB_ENABLED != value:
        config.set_env("SCRUB_ENABLED", value)
        config.SCRUB_ENABLED = value
        ui.notify("Scrubbing enabled." if value else "Scrubbing disabled.")
        ui.notify("You may need to restart the app for this to take effect.")


def get_scrub():
    return config.SCRUB_ENABLED


def set_dark(dark_mode, value):
    if dark_mode.value != value:
        dark_mode.value = value
        config.set_env("DARK_MODE", value)
