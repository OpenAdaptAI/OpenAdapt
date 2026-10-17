#!/usr/bin/env python3
"""Minimal GTK fixture for the native Linux AT-SPI CI qualification."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import gi

gi.require_version("Atk", "1.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Atk, GLib, Gtk  # noqa: E402

APP_NAME = "OpenAdapt Linux Qualification"
ENTRY_NAME = "Effect value"
BUTTON_NAME = "Write effect"


def _accessible_name(widget: Gtk.Widget, name: str) -> None:
    accessible = widget.get_accessible()
    if accessible is None:
        raise RuntimeError(f"GTK did not expose an accessible for {name!r}")
    accessible.set_name(name)


class QualificationWindow:
    def __init__(
        self,
        *,
        title: str,
        effect_path: Path,
        control_path: Path,
        mode: str,
    ) -> None:
        self._effect_path = effect_path
        self._control_path = control_path
        self._mode = mode
        self._replaced = False

        self.window = Gtk.Window(title=title)
        _accessible_name(self.window, title)
        self.window.set_default_size(460, 190)
        self.window.set_border_width(18)
        self.window.connect("destroy", Gtk.main_quit)

        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.window.add(self.box)

        label = Gtk.Label(label="Value committed by the independent file oracle")
        label.set_xalign(0)
        self.box.pack_start(label, False, False, 0)

        self.entry = Gtk.Entry()
        self.entry.set_text("not-yet-qualified")
        _accessible_name(self.entry, ENTRY_NAME)
        self.box.pack_start(self.entry, False, False, 0)

        self.button = self._new_button()
        self.box.pack_start(self.button, False, False, 0)
        if mode == "ambiguous":
            duplicate = self._new_button()
            self.box.pack_start(duplicate, False, False, 0)

        if mode == "stale":
            GLib.timeout_add(25, self._poll_control)

    def _new_button(self) -> Gtk.Button:
        button = Gtk.Button(label=BUTTON_NAME)
        _accessible_name(button, BUTTON_NAME)
        button.connect("clicked", self._write_effect)
        return button

    def _write_effect(self, _button: Gtk.Button) -> None:
        value = self.entry.get_text().encode("utf-8")
        self._effect_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._effect_path.with_suffix(".tmp")
        temporary.write_bytes(value)
        os.replace(temporary, self._effect_path)

    def _poll_control(self) -> bool:
        if self._replaced or not self._control_path.exists():
            return True
        try:
            command = self._control_path.read_text(encoding="utf-8").strip()
        except OSError:
            return True
        if command != "replace":
            return True

        self._control_path.unlink(missing_ok=True)
        self.box.remove(self.button)
        self.button.destroy()

        # Inserting a spacer before the replacement changes the native tree path.
        # The replacement also has different bounds. Either change must invalidate
        # the handle resolved before this controlled UI drift.
        spacer = Gtk.Label(label="interface updated")
        spacer.set_xalign(0)
        self.box.pack_start(spacer, False, False, 0)
        self.button = self._new_button()
        self.button.set_margin_start(70)
        self.box.pack_start(self.button, False, False, 0)
        self.box.show_all()
        self._replaced = True
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--title", required=True)
    parser.add_argument("--effect-path", type=Path, required=True)
    parser.add_argument("--control-path", type=Path, required=True)
    parser.add_argument(
        "--mode",
        choices=("clean", "ambiguous", "stale"),
        required=True,
    )
    args = parser.parse_args()

    # AT-SPI exposes the GLib program name as the application accessible name.
    # Keep it identical to the qualifier's exact application selector.
    GLib.set_prgname(APP_NAME)
    GLib.set_application_name(APP_NAME)
    root = Atk.get_root()
    if root is None:
        raise RuntimeError("GTK did not expose an ATK application root")
    root.set_name(APP_NAME)
    window = QualificationWindow(
        title=args.title,
        effect_path=args.effect_path,
        control_path=args.control_path,
        mode=args.mode,
    )
    window.window.show_all()
    window.window.present()
    Gtk.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
