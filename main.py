import sys
import os
import bz2
import requests
from shutil import copyfileobj
from nicegui import app, ui
from openadapt import visualize, replay
from openadapt.app.local_file_picker import local_file_picker

SERVER = "127.0.0.1:8000/upload"

app.native.window_args["resizable"] = False
app.native.start_args["debug"] = True
dark = ui.dark_mode()

with ui.dialog() as settings, ui.card():
    ui.switch("Dark mode", on_change=lambda: dark.toggle())

    ui.button("Close", on_click=lambda: settings.close())

# Add logo
# right align icon
with ui.row().classes("w-full justify-right"):
    # settings

    with ui.avatar(color="white" if dark else "black", size=128):
        # TODO: remove base64 and add dark mode version of icon
        ui.image(
            "data:image/png;base64,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",
        )
    ui.icon("settings").tooltip("Settings").on("click", lambda: settings.open())
    ui.icon("share").tooltip("Share").on(
        "click", lambda: (_ for _ in ()).throw(Exception(NotImplementedError))
    )
    ui.icon("upload").tooltip("Export Data").on("click", lambda: on_export())
    ui.icon("download").tooltip("Import Data").on("click", lambda: import_dialog.open())


# Record button + popup

options = ["test"]
result = ""
logger = None

with ui.dialog() as dialog, ui.card():
    ui.label("Enter a name for the recording: ")
    ui.input(
        label="Name",
        placeholder="test",
        autocomplete=options,
        on_change=lambda e: result.set_text(e),
    )
    result = ui.label()

    def cancel_recording():
        dialog.close()
        print("Recording cancelled.", file=sys.stderr)

    def on_record():
        dialog.close()
        if result.text:
            # fails
            # record.record(name)
            # works
            print(
                "Recording {}... Press CTRL + C in log window to cancel".format(
                    result.text
                ),
                file=sys.stderr,
            )
            os.system('python3 -m openadapt.record "{}"'.format(result.text))
            print("Recording complete.", file=sys.stderr)

        else:
            print("Recording cancelled.", file=sys.stderr)

    with ui.row():
        ui.button("Cancel", on_click=cancel_recording)
        ui.button("Enter", on_click=on_record)


# Add buttons and log text element in a row container | split ui for console
with ui.splitter() as splitter:
    with splitter.before:
        with ui.column():
            ui.button("Record", on_click=dialog.open).tooltip("Record a new replay")

            ui.button(
                "Visualize",
                on_click=lambda: __import__("threading")
                .Thread(target=visualize.main)
                .start(),
            ).tooltip("Visualize the latest replay")
            ui.button(
                "Replay", on_click=lambda: replay.replay("NaiveReplayStrategy")
            ).tooltip("Play the latest replay")
            ui.button("Clear Data", on_click=lambda: clear_db()).tooltip(
                "Clear all data"
            )
    with splitter.after:

        class Console(object):
            def __init__(self):
                self.log = ui.log().classes("w-full h-20")
                self.old_stderr = sys.stderr
                sys.stderr = self

            def write(self, data):
                self.log.push(data[:-1])
                self.log.update()

            def flush(self):
                self.log.update()

            def reset(self):
                self.log.clear()
                sys.stderr = self.old_stderr

        logger = Console()
        logger.log.style("height: 200px;, width: 300px;")
        # logger.log.style("height: 200px;, width: 300px; font-size: 10px;")

    splitter.disable()


def clear_db():
    os.remove("openadapt.db")
    os.system("alembic upgrade head")
    logger.log.clear()
    print("Database cleared.", file=sys.stderr)


def on_export():
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

    requests.post(SERVER, files=files)

    # delete compressed db
    os.remove("openadapt.db.bz2")

    ui.notify("Exported data.")


async def pick_file():
    result = await local_file_picker(".")
    ui.notify(f"Selected {result[0]}" if result else "No file selected.")
    selected_file.text = result[0]
    import_button.enabled = True if result else False


def on_import(selected_file, delete=False):
    with open("openadapt.db", "wb") as f:
        with bz2.BZ2File(selected_file, "rb") as f2:
            copyfileobj(f2, f)

    if delete:
        os.remove(selected_file)

    ui.notify("Imported data from server.")


with ui.dialog() as import_dialog, ui.card():
    with ui.column():
        ui.button("Select File", on_click=pick_file).props("icon=folder")
        selected_file = ui.label("")
        selected_file.visible = False
        import_button = ui.button(
            "Import", on_click=lambda: on_import(selected_file.text, delete.value)
        )
        import_button.enabled = False
        delete = ui.checkbox("Delete file after import")


ui.run(title="OpenAdapt Client", native=True, window_size=(400, 350), fullscreen=False)
