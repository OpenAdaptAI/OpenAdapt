"""Desktop app experience: a system-tray launcher + a desktop shortcut.

The panel is a local web app, so "an app you click on the desktop" is a shortcut
that launches a **tray-resident** server (no console window) which opens the
browser and stays in the system tray with an Open/Quit menu. This keeps the
pure-Python, no-native-toolchain approach the project chose.

- ``run_app``   — start the server in a background thread, open the browser, and
  block on a tray icon until the user picks Quit. Invoked by ``openadapt panel
  --app`` (which the shortcut runs via ``pythonw`` so there's no console).
- ``install_desktop_shortcut`` — create a Desktop shortcut that runs the above.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_ACCENT = (76, 139, 245, 255)
_WHITE = (255, 255, 255, 255)


def render_icon(size: int = 64):
    """A simple OpenAdapt tray/app icon (blue rounded square with a ring)."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([1, 1, size - 2, size - 2], radius=size // 5, fill=_ACCENT)
    m = size // 4
    d.ellipse([m, m, size - m, size - m], outline=_WHITE, width=max(2, size // 12))
    return img


def _icon_file() -> Path:
    """Render the app icon to a stable .ico the shortcut can point at."""
    path = Path.home() / ".openadapt" / "panel-icon.ico"
    path.parent.mkdir(parents=True, exist_ok=True)
    render_icon(256).save(
        path, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (256, 256)]
    )
    return path


def run_app(*, host: str = "127.0.0.1", port: int = 8080) -> int:
    """Run the panel as a tray-resident desktop app.

    Falls back to the plain foreground server if the tray dependencies are
    missing, so the command still works.
    """
    import threading
    import webbrowser

    import uvicorn

    from .app import create_app
    from .auth import generate_token

    try:
        import pystray
    except ImportError:
        print("Tray deps (pystray, pillow) missing; running plain server instead.")
        from .server import run_panel

        return run_panel(host=host, port=port, open_browser=True)

    token = generate_token()
    url = f"http://{host}:{port}/?token={token}"
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(token=token), host=host, port=port, log_level="warning"
        )
    )
    # uvicorn skips signal handlers off the main thread, so this is safe.
    threading.Thread(target=server.run, daemon=True).start()

    def open_browser(icon=None, item=None):
        webbrowser.open(url)

    def quit_app(icon, item):
        server.should_exit = True
        icon.stop()

    threading.Timer(1.2, open_browser).start()

    menu = pystray.Menu(
        pystray.MenuItem("Open Control Panel", open_browser, default=True),
        pystray.MenuItem("Quit", quit_app),
    )
    icon = pystray.Icon(
        "openadapt-panel", render_icon(64), "OpenAdapt Control Panel", menu
    )
    icon.run()  # blocks until Quit
    return 0


def install_desktop_shortcut(*, port: int = 8080, name: str = "OpenAdapt Panel") -> str:
    """Create a Desktop shortcut that launches the tray app (Windows only).

    The shortcut runs ``pythonw -m openadapt.cli panel --app`` so there's no
    console window. Returns the shortcut path.
    """
    if os.name != "nt":
        raise NotImplementedError(
            "Desktop shortcut creation is currently Windows-only. On other "
            "platforms run `openadapt panel --app`."
        )
    import subprocess

    # Prefer pythonw.exe (no console); fall back to python.exe.
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    launcher = pythonw if pythonw.exists() else Path(sys.executable)
    icon = _icon_file()
    desktop = Path(os.path.expanduser("~")) / "Desktop"
    desktop.mkdir(parents=True, exist_ok=True)
    lnk = desktop / f"{name}.lnk"

    ps = (
        "$s = New-Object -ComObject WScript.Shell; "
        f"$l = $s.CreateShortcut('{lnk}'); "
        f"$l.TargetPath = '{launcher}'; "
        f"$l.Arguments = '-m openadapt.cli panel --app --port {port}'; "
        f"$l.IconLocation = '{icon}'; "
        f"$l.WorkingDirectory = '{Path.home()}'; "
        "$l.Description = 'OpenAdapt Control Panel'; "
        "$l.Save()"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], check=True
    )
    return str(lnk)
