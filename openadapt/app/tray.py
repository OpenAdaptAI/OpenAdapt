import os
import threading
from PIL import Image
from pystray import Icon, Menu, MenuItem
from openadapt.app.cards import quick_record, stop_record
from openadapt.app.main import start, FPATH
from openadapt import visualize

app_thread = None
g_tray = None


def new_tray():
    global g_tray

    def on_exit():
        os._exit(0)

    def show_app():
        global app_thread
        app_thread = threading.Thread(target=start)
        app_thread.daemon = True
        app_thread.start()

    image = Image.open(f"{FPATH}/assets/logo_dark.png")
    menu = Menu(
        MenuItem("Record", quick_record),
        MenuItem("Stop Recording", lambda: stop_record(g_tray)),
        MenuItem("Visualize", lambda: threading.Thread(target=visualize.main).start()),
        MenuItem("Show App", show_app),
        MenuItem("Exit", on_exit),
    )

    g_tray = Icon("Pystray", icon=image, menu=menu)
    g_tray.notify("OpenAdapt", "OpenAdapt is running in the background.")
    return g_tray


def run_app():
    tray = new_tray()
    # TODO: get tray.run_detached() working for non-blocking solution
    tray.run()


if __name__ == "__main__":
    run_app()
