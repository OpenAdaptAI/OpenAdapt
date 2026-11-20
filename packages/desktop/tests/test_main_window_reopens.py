"""Every way back to the app shows the main window.

The app keeps running after its window is closed. Before Desktop 0.30.5 closing
destroyed the window, so a second launch from the Start menu, "Open OpenAdapt"
in the tray menu and a click on the macOS Dock icon all did nothing.

``src-tauri/src/window.rs`` now hides the window on close and owns the one
function that shows it again. These checks hold the wiring in place; the
behaviour itself was verified on Windows 11 and macOS with a branch build.
"""

from __future__ import annotations

import re
from pathlib import Path

SHELL = Path(__file__).resolve().parents[1] / "src-tauri" / "src"


def _source(name: str) -> str:
    return (SHELL / name).read_text(encoding="utf-8")


def test_closing_the_main_window_hides_it_instead_of_destroying_it() -> None:
    window = _source("window.rs")
    handler = window[window.index("pub fn on_window_event") : window.index("pub fn show_main")]
    assert "WindowEvent::CloseRequested" in handler
    assert "api.prevent_close()" in handler
    assert "window.hide()" in handler
    assert ".on_window_event(window::on_window_event)" in _source("main.rs")


def test_a_second_launch_shows_the_running_apps_window() -> None:
    main = _source("main.rs")
    start = main.index("tauri_plugin_single_instance::init(")
    handler = main[start : main.index("}))", start)]
    assert "window::show_main(app)" in handler


def test_the_tray_menu_and_the_dock_show_the_window_too() -> None:
    assert "crate::window::show_main(app)" in _source("tray.rs")
    main = _source("main.rs")
    assert re.search(r"RunEvent::Reopen\s*\{\s*\.\.\s*\}\s*=>\s*window::show_main\(app\)", main)


def test_show_main_restores_shows_and_focuses() -> None:
    window = _source("window.rs")
    body = window[window.index("pub fn show_main") : window.index("#[cfg(test)]")]
    for call in ("window.unminimize()", "window.show()", "window.set_focus()"):
        assert call in body
