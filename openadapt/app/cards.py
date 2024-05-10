"""openadapt.app.cards module.

This module provides functions for managing UI cards in the OpenAdapt application.
"""

from datetime import datetime
import multiprocessing
import sys
import time

from nicegui import ui

from openadapt.app.objects.local_file_picker import LocalFilePicker
from openadapt.app.util import get_scrub, set_dark, set_scrub, sync_switch
from openadapt.db.crud import new_session
from openadapt.record import record


class RecordProc:
    """Class to manage the recording process."""

    def __init__(self) -> None:
        """Initialize the RecordProc class."""
        self.terminate_processing = multiprocessing.Event()
        self.terminate_recording = multiprocessing.Event()
        self.record_proc: multiprocessing.Process = None
        self.has_initiated_stop = False

    def set_terminate_processing(self) -> multiprocessing.Event:
        """Set the terminate event."""
        return self.terminate_processing.set()

    def terminate(self) -> None:
        """Terminate the recording process."""
        self.record_proc.terminate()

    def reset(self) -> None:
        """Reset the recording process."""
        self.terminate_processing.clear()
        self.terminate_recording.clear()
        self.record_proc = None
        record_proc.has_initiated_stop = False

    def wait(self) -> None:
        """Wait for the recording process to finish."""
        if sys.platform == "win32":
            # a bug on windows for when `record` runs for a long time, where even when
            # the `record` function returns a value, `record_proc` refuses to join.
            # this is a workaround to ensure the process is terminated
            while True:
                if self.terminate_recording.is_set():
                    self.record_proc.terminate()
                    return
                time.sleep(0.1)
        else:
            self.record_proc.join()

    def is_running(self) -> bool:
        """Check if the recording process is running."""
        if self.record_proc is not None and not self.record_proc.is_alive():
            self.reset()
        return self.record_proc is not None

    def start(self, func: callable, args: tuple, kwargs: dict) -> None:
        """Start the recording process."""
        self.record_proc = multiprocessing.Process(
            target=func,
            args=args,
            kwargs=kwargs,
        )
        self.record_proc.start()


record_proc = RecordProc()


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
    if record_proc.is_running() and not record_proc.has_initiated_stop:
        record_proc.set_terminate_processing()

        # wait for process to terminate
        record_proc.wait()
        record_proc.reset()


def is_recording() -> bool:
    """Check if a recording session is currently active."""
    global record_proc
    return record_proc.is_running()


def quick_record(
    task_description: str | None = None,
    status_pipe: multiprocessing.connection.Connection | None = None,
) -> None:
    """Run a recording session."""
    global record_proc
    new_session()
    task_description = task_description or datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    record_proc.start(
        record,
        (
            task_description,
            record_proc.terminate_processing,
            record_proc.terminate_recording,
            status_pipe,
        ),
        {
            "log_memory": False,
        },
    )


def recording_prompt(options: list[str], record_button: ui.button) -> None:
    """Display the recording prompt dialog.

    Args:
        options (list): List of autocomplete options.
        record_button (nicegui.widgets.Button): Record button widget.
    """
    if not record_proc.is_running():
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
        record_proc.set_terminate_processing()

        # wait for process to terminate
        record_proc.wait()
        ui.notify("Stopped recording")
        record_button._props["name"] = "radio_button_checked"
        record_button.on("click", lambda: recording_prompt(options, record_button))

        record_proc.reset()

    def begin() -> None:
        name = result.text.__getattribute__("value")

        ui.notify(
            f"Recording {name}... Press CTRL + C in terminal window to cancel",
        )
        new_session()
        global record_proc
        record_proc.start(
            record,
            (name, record_proc.terminate_processing, record_proc.terminate_recording),
        )
        record_button._props["name"] = "stop"
        record_button.on("click", lambda: terminate())
        record_button.update()

    def on_record() -> None:
        global record_proc
        dialog.close()
        begin()
