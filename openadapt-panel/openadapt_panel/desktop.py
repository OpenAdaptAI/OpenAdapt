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


def _data_dir() -> Path:
    d = Path.home() / ".openadapt"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _setup_logging():
    """Log to ~/.openadapt/panel.log so failures aren't invisible under pythonw
    (which has no console)."""
    import logging

    logger = logging.getLogger("openadapt_panel")
    if not logger.handlers:
        handler = logging.FileHandler(_data_dir() / "panel.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


def _open_url(url: str, log) -> None:
    """Open the URL in the default browser as robustly as possible."""
    import webbrowser

    try:
        if webbrowser.open(url):
            return
    except Exception as exc:  # noqa: BLE001
        log.warning("webbrowser.open failed: %s", exc)
    try:
        os.startfile(url)  # noqa: S606 - Windows default handler
    except Exception as exc:  # noqa: BLE001
        log.warning("os.startfile failed: %s", exc)


def _open_when_ready(url: str, host: str, port: int, log) -> None:
    """Wait until the server actually answers, then open the browser.

    Fixes the race where the browser opened before uvicorn was listening and
    showed a "can't reach this page" error. Also writes the URL (with token) to
    a file so the user always has a way in even if auto-open is blocked.
    """
    import time
    import urllib.request

    ready = False
    for _ in range(60):  # up to ~30s
        try:
            urllib.request.urlopen(f"http://{host}:{port}/api/health", timeout=1)
            ready = True
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.5)

    try:
        (_data_dir() / "panel-url.txt").write_text(url, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        log.warning("could not write panel-url.txt: %s", exc)

    if ready:
        log.info("Panel ready; opening %s", url)
        _open_url(url, log)
    else:
        log.error("Server did not become ready on %s:%s (port in use?)", host, port)


def run_app(*, host: str = "127.0.0.1", port: int = 8080) -> int:
    """Run the panel as a tray-resident desktop app.

    Falls back to the plain foreground server if the tray dependencies are
    missing, so the command still works. Logs to ~/.openadapt/panel.log and
    writes the launch URL to ~/.openadapt/panel-url.txt (it carries the session
    token, which is otherwise invisible when launched via pythonw).
    """
    log = _setup_logging()
    try:
        # Under pythonw (the desktop shortcut) sys.stdout/stderr are None, which
        # crashes uvicorn's logging setup (it calls sys.stdout.isatty()) and
        # made the app die silently on double-click. Point missing streams at a
        # file so the whole app is safe.
        if sys.stdout is None or sys.stderr is None:
            stream = open(
                _data_dir() / "panel-console.log", "a", encoding="utf-8", buffering=1
            )
            if sys.stdout is None:
                sys.stdout = stream
            if sys.stderr is None:
                sys.stderr = stream

        import threading

        import uvicorn

        from .app import create_app
        from .auth import generate_token

        try:
            import pystray
        except ImportError:
            log.warning("pystray/pillow missing; running plain server instead.")
            from .server import run_panel

            return run_panel(host=host, port=port, open_browser=True)

        token = generate_token()
        url = f"http://{host}:{port}/?token={token}"
        log.info("Starting panel on %s:%s", host, port)
        server = uvicorn.Server(
            uvicorn.Config(
                create_app(token=token), host=host, port=port, log_level="warning"
            )
        )
        # uvicorn skips signal handlers off the main thread, so this is safe.
        threading.Thread(target=server.run, daemon=True).start()
        threading.Thread(
            target=_open_when_ready, args=(url, host, port, log), daemon=True
        ).start()

        def open_browser(icon=None, item=None):
            _open_url(url, log)

        def quit_app(icon, item):
            server.should_exit = True
            icon.stop()

        menu = pystray.Menu(
            pystray.MenuItem("Open Control Panel", open_browser, default=True),
            pystray.MenuItem("Quit", quit_app),
        )
        icon = pystray.Icon(
            "openadapt-panel", render_icon(64), "OpenAdapt Control Panel", menu
        )

        def _notify():
            try:
                icon.notify(f"Running at http://{host}:{port}", "OpenAdapt Panel")
            except Exception:  # noqa: BLE001 - notifications are best-effort
                pass

        threading.Timer(2.5, _notify).start()
        icon.run()  # blocks until Quit
        return 0
    except Exception:
        log.exception("Panel failed to start")
        raise


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
