import os
import sys
from multiprocessing import Process

from nicegui import ui

from openadapt.app.objects.local_file_picker import local_file_picker


def settings(dark_mode):
    with ui.dialog() as settings, ui.card():
        ui.switch("Dark mode", on_change=lambda: dark_mode.toggle())

        ui.button("Close", on_click=lambda: settings.close())

    settings.open()


def select_import(f):
    async def pick_file():
        result = await local_file_picker(".")
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


def recording_prompt(logger, options):
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

        def on_stop(proc):
            dialog.close()
            proc.terminate()

        def create_proc_record(desc):
            os.system(f'python3 -m openadapt.record "{desc}"')
            # record.record(desc)

        async def on_record():
            if result.text:
                name = result.text.__getattribute__("value")
                # fails
                # record.record(name)
                # works
                print(
                    f"Recording {name}... Press CTRL + C in log window to cancel",
                    file=sys.stderr,
                )
                logger.reset()
                Process(target=create_proc_record(desc=name)).start()

            else:
                print("Recording cancelled.", file=sys.stderr)

        with ui.row():
            ui.button(
                "Close", on_click=cancel_recording if not result.text else dialog.close
            )
            ui.button("Enter", on_click=on_record)

        dialog.open()
