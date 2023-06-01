import threading
import base64
import os

from nicegui import app, ui

from openadapt import replay, visualize
from openadapt.app.cards import recording_prompt, select_import, settings
from openadapt.app.objects.console import Console
from openadapt.app.util import clear_db, on_export, on_import

SERVER = "127.0.0.1:8000/upload"


def run_app():
    file = os.path.dirname(__file__)
    app.native.window_args["resizable"] = False
    app.native.start_args["debug"] = True
    dark = ui.dark_mode()

    # Add logo
    # right align icon
    with ui.row().classes("w-full justify-right"):
        # settings

        # alignment trick
        with ui.avatar(color="white" if dark else "black", size=128):
            logo_base64 = base64.b64encode(open(f"{file}/assets/logo.png", "rb").read())
            img = bytes(
                f"data:image/png;base64,{(logo_base64.decode('utf-8'))}",
                encoding="utf-8",
            )
            ui.image(img.decode("utf-8"))
        ui.icon("settings").tooltip("Settings").on("click", lambda: settings(dark))
        ui.icon("share").tooltip("Share").on(
            "click", lambda: (_ for _ in ()).throw(Exception(NotImplementedError))
        )
        ui.icon("upload").tooltip("Export Data").on("click", lambda: on_export(SERVER))
        ui.icon("download").tooltip("Import Data").on(
            "click", lambda: select_import(on_import)
        )

    # Record button + popup

    options = ["test"]
    logger = None

    # Add buttons and log text element in a row container | split ui for console
    with ui.splitter() as splitter:
        with splitter.before:
            with ui.column():
                ui.button(
                    "Record", on_click=lambda: recording_prompt(logger, options)
                ).tooltip("Record a new replay")

                ui.button(
                    "Visualize",
                    on_click=lambda: threading.Thread(target=visualize.main).start(),
                ).tooltip("Visualize the latest replay")
                ui.button(
                    "Replay", on_click=lambda: replay.replay("NaiveReplayStrategy")
                ).tooltip("Play the latest replay")
                ui.button("Clear Data", on_click=lambda: clear_db()).tooltip(
                    "Clear all data"
                )
        with splitter.after:
            logger = Console()
            logger.log.style("height: 200px;, width: 300px;")

        splitter.disable()

    ui.run(
        title="OpenAdapt Client",
        native=True,
        window_size=(400, 350),
        fullscreen=False,
        reload=False,
    )


if __name__ == "__main__":
    run_app()
