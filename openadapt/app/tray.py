import os
import sys
import threading

from PIL import Image
from pystray import Icon, Menu, MenuItem

from openadapt import visualize
from openadapt.app.cards import quick_record, stop_record
from openadapt.app.main import FPATH, start
from openadapt.crud import get_all_recordings

# hide dock icon on macos
if sys.platform == "darwin":
    import AppKit

    info = AppKit.NSBundle.mainBundle().infoDictionary()
    info["LSBackgroundOnly"] = "1"

app_thread = None
g_tray = None


def get_visualize_menu():
    recordings = get_all_recordings()
    menu_items = [MenuItem("latest", lambda: visualize.main())]
    for i in range(1, len(recordings)):
        menu_items.append(
            MenuItem(
                f"{recordings[i].task_description}",
                lambda: visualize.main(recording=recordings[i]),
            )
        )
    return Menu(*menu_items)


def new_tray():
    global g_tray

    def on_exit():
        os._exit(0)

    def show_app():
        global app_thread

        # check if app is already running
        if app_thread is None or not app_thread.is_alive():
            app_thread = threading.Thread(target=start, daemon=True)
            app_thread.start()

    image = Image.open(f"{FPATH}/assets/logo_inverted.png")
    menu = Menu(
        MenuItem("Record", quick_record),
        MenuItem("Stop Recording", lambda: stop_record(g_tray)),
        MenuItem(
            "Visualize",
            get_visualize_menu(),
        ),
        MenuItem("Show App", show_app),
        MenuItem("Exit", on_exit),
    )

    g_tray = Icon("OpenAdapt", icon=image, menu=menu)
    g_tray.notify("OpenAdapt", "OpenAdapt is running in the background.")
    return g_tray


def run_tray():
    tray = new_tray()
    # TODO: get tray.run_detached() working for non-blocking solution
    tray.run()


if __name__ == "__main__":
    run_tray()
