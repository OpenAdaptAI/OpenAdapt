"""openadapt.app.util module.

This module provides utility functions for the OpenAdapt application.

Example usage:
    from openadapt.app.util import clear_db, on_import, on_export, sync_switch, set_dark

    clear_db()
    on_import(selected_file, delete=False, src="openadapt.db")
    on_export(dest)
    sync_switch(switch, prop)
    set_dark(dark_mode, value)
"""

from shutil import copyfileobj
import bz2
import os
import sys

from nicegui import elements, ui

from openadapt.app.objects import console
from openadapt.config import config
from openadapt.scripts.reset_db import reset_db


def clear_db(log: console.Console) -> None:
    """Clear the database.

    Args:
        log: Optional NiceGUI log object.
    """
    if log:
        log.log.clear()
        o = sys.stdout
        sys.stdout = sys.stderr

    reset_db()
    ui.notify("Cleared database.")
    sys.stdout = o


def on_import(
    selected_file: str,
    delete: bool = False,
    src: str = "openadapt.db",
) -> None:
    """Import data from a selected file.

    Args:
        selected_file (str): The path of the selected file.
        delete (bool): Whether to delete the selected file after import.
        src (str): The source file name to save the imported data.
    """
    with open(src, "wb") as f:
        with bz2.BZ2File(selected_file, "rb") as f2:
            copyfileobj(f2, f)

    if delete:
        os.remove(selected_file)

    ui.notify("Imported data.")


def on_export(dest: str) -> None:
    """Export data to a destination.

    Args:
        dest (str): The destination to export the data to.
    """
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


def sync_switch(
    switch: elements.switch.Switch, prop: elements.mixins.value_element.ValueElement
) -> None:
    """Synchronize the value of a switch with a property.

    Args:
        switch: The switch object.
        prop: The property object.
    """
    switch.value = prop.value if hasattr(prop, "value") else prop


def set_scrub(value: bool) -> None:
    """Set the scrubbing value.

    Args:
        value: The value to set.
    """
    if config.SCRUB_ENABLED != value:
        config.SCRUB_ENABLED = value
        ui.notify("Scrubbing enabled." if value else "Scrubbing disabled.")
        ui.notify("You may need to restart the app for this to take effect.")


def get_scrub() -> bool:
    """Get the scrubbing value.

    Returns:
        bool: The scrubbing value.
    """
    return config.SCRUB_ENABLED


def set_dark(dark_mode: ui.dark_mode, value: bool) -> None:
    """Set the dark mode.

    Args:
        dark_mode: The dark mode object.
        value: The value to set.
    """
    if dark_mode.value != value:
        dark_mode.value = value
        config.APP_DARK_MODE = value
