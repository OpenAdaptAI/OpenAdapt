"""openadapt.app.cards module.

This module provides functions for managing UI cards in the OpenAdapt application.
"""

from datetime import datetime
from subprocess import Popen
import signal

from nicegui import ui

from openadapt.app.objects.local_file_picker import LocalFilePicker
from openadapt.app.util import get_scrub, set_dark, set_scrub, sync_switch
from openadapt.crud import newSession

record_proc = None


def settings(dark_mode: bool) -> None:
    """Display the settings dialog.

    Args:
        dark_mode (bool): Current dark mode setting.
    """
    with ui.dialog() as settings, ui.card():
        dark_switch = ui.switch(
            "Dark mode", on_change=lambda: set_dark(dark_mode, dark_switch.value)
        )
        sync_switch(dark_switch, dark_mode)

        scrub_switch = ui.switch(
            "Scrubbing", on_change=lambda: set_scrub(scrub_switch.value)
        )
        sync_switch(scrub_switch, get_scrub())

        ui.button("Close", on_click=lambda: settings.close())
    settings.open()


def select_import(f: callable) -> None:
    """Display the import file selection dialog.

    Args:
        f (callable): Function to call when import button is clicked.
    """

    async def pick_file() -> None:
        result = await LocalFilePicker(".")
        ui.notify(f"Selected {result[0]}" if result else "No file selected.")
        selected_file.text = result[0] if result else ""
        import_button.enabled = True if result else False

    with ui.dialog() as import_dialog, ui.card():
        with ui.column():
            ui.button("Select File", on_click=pick_file).props("icon=folder")
            selected_file = ui.label("")
            selected_file.visible = False
            import_button = ui.button(
                "Import",
                on_click=lambda: f(selected_file.text, delete.value),
            )
            import_button.enabled = False
            delete = ui.checkbox("Delete file after import")

    import_dialog.open()


def stop_record() -> None:
    """Stop the current recording session."""
    global record_proc
    if record_proc is not None:
        record_proc.send_signal(signal.SIGINT)

        # wait for process to terminate
        record_proc.wait()
        record_proc = None


def quick_record() -> None:
    """Run a recording session with no option for recording name (uses date instead)."""
    global record_proc
    newSession()
    now = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    record_proc = Popen(
        f"python -m openadapt.record '{now}'",
        shell=True,
    )


def recording_prompt(options: list[str], record_button: ui.button) -> None:
    """Display the recording prompt dialog.

    Args:
        options (list): List of autocomplete options.
        record_button (nicegui.widgets.Button): Record button widget.
    """
    if record_proc is None:
        with ui.dialog() as dialog, ui.card():
            ui.label("Enter a name for the recording: ")
            ui.input(
                label="Name",
                placeholder="test",
                autocomplete=options,
                on_change=lambda e: result.set_text(e),
            )
            result = ui.label()

            with ui.row():
                ui.button("Close", on_click=dialog.close)
                ui.button("Enter", on_click=lambda: on_record())

            dialog.open()

    def terminate() -> None:
        global record_proc
        record_proc.send_signal(signal.SIGINT)

        # wait for process to terminate
        record_proc.wait()
        ui.notify("Stopped recording")
        record_button._props["name"] = "radio_button_checked"
        record_button.on("click", lambda: recording_prompt(options, record_button))

        record_proc = None

    def begin() -> None:
        name = result.text.__getattribute__("value")

        ui.notify(
            f"Recording {name}... Press CTRL + C in terminal window to cancel",
        )
        newSession()
        proc = Popen(
            "python -m openadapt.record " + name,
            shell=True,
        )
        record_button._props["name"] = "stop"
        record_button.on("click", lambda: terminate())
        record_button.update()
        return proc

    def on_record() -> None:
        global record_proc
        dialog.close()
        record_proc = begin()
