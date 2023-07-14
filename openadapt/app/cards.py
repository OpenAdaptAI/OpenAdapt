from subprocess import Popen
import signal

from nicegui import ui

from openadapt.app.objects.local_file_picker import LocalFilePicker
from openadapt.app.util import set_dark, sync_switch

PROC = None


def settings(dark_mode):
    with ui.dialog() as settings, ui.card():
        s = ui.switch("Dark mode", on_change=lambda: set_dark(dark_mode, s.value))
        sync_switch(s, dark_mode)
        ui.button("Close", on_click=lambda: settings.close())

    settings.open()


def select_import(f):
    async def pick_file():
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
                "Import", on_click=lambda: f(selected_file.text, delete.value)
            )
            import_button.enabled = False
            delete = ui.checkbox("Delete file after import")

    import_dialog.open()


def recording_prompt(options, record_button):
    if PROC is None:
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

    def terminate():
        global PROC
        PROC.send_signal(signal.SIGINT)

        # wait for process to terminate
        PROC.wait()
        ui.notify("Stopped recording")
        record_button._props["name"] = "radio_button_checked"
        record_button.on("click", lambda: recording_prompt(options, record_button))

        PROC = None

    def begin():
        name = result.text.__getattribute__("value")

        ui.notify(
            f"Recording {name}... Press CTRL + C in terminal window to cancel",
        )
        PROC = Popen(
            "python3 -m openadapt.record " + name,
            shell=True,
        )
        record_button._props["name"] = "stop"
        record_button.on("click", lambda: terminate())
        record_button.update()
        return PROC

    def on_record():
        global PROC
        dialog.close()
        PROC = begin()
