import signal
from datetime import datetime
from subprocess import Popen

from nicegui import ui

from openadapt.app.objects.local_file_picker import LocalFilePicker
from openadapt.app.util import set_dark, sync_switch
from openadapt.crud import newSession

record_proc = None


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


def stop_record():
    global record_proc
    if record_proc is not None:
        record_proc.send_signal(signal.SIGINT)

        # wait for process to terminate
        record_proc.wait()
        record_proc = None


def quick_record():
    global record_proc
    newSession()
    now = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    record_proc = Popen(
        f"python -m openadapt.record '{now}'",
        shell=True,
    )


def recording_prompt(options, record_button):
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

    def terminate():
        global record_proc
        record_proc.send_signal(signal.SIGINT)

        # wait for process to terminate
        record_proc.wait()
        ui.notify("Stopped recording")
        record_button._props["name"] = "radio_button_checked"
        record_button.on("click", lambda: recording_prompt(options, record_button))

        record_proc = None

    def begin():
        name = result.text.__getattribute__("value")

        ui.notify(
            f"Recording {name}... Press CTRL + C in terminal window to cancel",
        )
        newSession()
        proc = Popen(
            "python3 -m openadapt.record " + name,
            shell=True,
        )
        record_button._props["name"] = "stop"
        record_button.on("click", lambda: terminate())
        record_button.update()
        return proc

    def on_record():
        global record_proc
        dialog.close()
        record_proc = begin()
