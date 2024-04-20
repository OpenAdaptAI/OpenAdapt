"""openadapt.app.main module.

This module provides the main entry point for running the OpenAdapt application.

Example usage:
    from openadapt.app import run_app

    run_app()
"""

from functools import partial
from subprocess import Popen
import base64
import os
import pathlib

from nicegui import app, ui

from openadapt import replay
from openadapt.app.cards import recording_prompt, select_import, settings
from openadapt.app.objects.console import Console
from openadapt.app.util import clear_db, on_export, on_import
from openadapt.config import config

SERVER = "127.0.0.1:8000/upload"
FPATH = pathlib.Path(__file__).parent

# Recording description autocomplete
OPTIONS = ["test"]

app.native.start_args["debug"] = False

dark = ui.dark_mode()
dark.value = config.APP_DARK_MODE

logger = None


def start(fullscreen: bool = False) -> None:
    """Start the OpenAdapt application."""
    with ui.row().classes("w-full justify-right"):
        with ui.avatar(color="white" if dark else "black", size=128):
            logo_base64 = base64.b64encode(
                open(os.path.join(FPATH, "assets/logo.png"), "rb").read()
            )
            img = bytes(
                f"data:image/png;base64,{(logo_base64.decode('utf-8'))}",
                encoding="utf-8",
            )
            ui.image(img.decode("utf-8"))
        ui.icon("settings").tooltip("Settings").on("click", lambda: settings(dark))
        ui.icon("delete").on("click", lambda: clear_db(log=logger)).tooltip(
            "Clear all recorded data"
        )
        ui.icon("upload").tooltip("Export Data").on("click", lambda: on_export(SERVER))
        ui.icon("download").tooltip("Import Data").on(
            "click", lambda: select_import(on_import)
        )
        ui.icon("share").tooltip("Share").on(
            "click", lambda: (_ for _ in ()).throw(Exception(NotImplementedError))
        )

        with ui.splitter(value=20) as splitter:
            splitter.classes("w-full h-full")
            with splitter.before:
                with ui.column().classes("w-full h-full"):
                    record_button = (
                        ui.icon("radio_button_checked", size="64px")
                        .on(
                            "click",
                            lambda: recording_prompt(OPTIONS, record_button),
                        )
                        .tooltip("Record a new replay / Stop recording")
                    )
                    ui.icon("visibility", size="64px").on(
                        "click", partial(Popen, ["python", "-m", "openadapt.visualize"])
                    ).tooltip("Visualize the latest replay")

                    ui.icon("play_arrow", size="64px").on(
                        "click",
                        lambda: replay.replay("NaiveReplayStrategy"),
                    ).tooltip("Play the latest replay")
            with splitter.after:
                logger = Console()
                logger.log.style("height: 250px;, width: 300px;")

            splitter.enabled = False

    ui.run(
        title="OpenAdapt Client",
        native=True,
        window_size=(400, 400),
        fullscreen=fullscreen,
        reload=False,
        show=False,
    )


if __name__ == "__main__":
    start()
