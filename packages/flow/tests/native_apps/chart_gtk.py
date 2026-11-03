#!/usr/bin/env python3
"""A small GTK3 chart app for repair qualification on Linux (AT-SPI).

Usage: chart_gtk.py <db.sqlite> <window title> <patient id> <patient banner> <mode>

The Linux counterpart of ``Chart.swift``. The window shows a patient banner
and a "New note" button. "New note" opens a note field and a "Save note"
button; "Save note" inserts one row into the SQLite table
notes(patient_id, note). With mode "update_notice", the first "New note" shows
an in-window "Update available" notice instead: a bordered panel over the
window, as many desktop apps draw their notices. "Later" dismisses it and
opens the note form. Nothing else writes to the database.
"""

from __future__ import annotations

import sqlite3
import sys

import gi

gi.require_version("Atk", "1.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Atk, GLib, Gtk  # noqa: E402

APP_NAME = "chart"

CSS = b"""
.notice-backdrop { background-color: #ececec; }
.notice { background-color: #ffffff; border: 2px solid #404040; }
.banner { font-size: 18px; font-weight: bold; }
"""


def insert_note(db_path: str, patient_id: str, note: str) -> bool:
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS notes "
                "(id INTEGER PRIMARY KEY, patient_id TEXT, note TEXT)"
            )
            conn.execute(
                "INSERT INTO notes (patient_id, note) VALUES (?, ?)",
                (patient_id, note),
            )
        return True
    except sqlite3.Error:
        return False


class Chart:
    def __init__(self, db: str, title: str, patient_id: str, banner: str, mode: str):
        self.db, self.patient_id, self.banner = db, patient_id, banner
        self.update_notice = mode == "update_notice"
        self.notice_shown = False

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            self.window_screen(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        self.window = Gtk.Window(title=title)
        self.window.set_default_size(560, 360)
        self.window.set_resizable(False)
        self.window.move(160, 120)
        self.window.connect("destroy", Gtk.main_quit)

        self.overlay = Gtk.Overlay()
        self.window.add(self.overlay)
        content = Gtk.Fixed()
        self.overlay.add(content)

        banner_label = Gtk.Label(label=banner)
        banner_label.get_style_context().add_class("banner")
        content.put(banner_label, 24, 28)
        new_note = Gtk.Button(label="New note")
        new_note.set_size_request(140, 32)
        new_note.connect("clicked", self.new_note)
        content.put(new_note, 24, 88)

        self.field = Gtk.Entry()
        self.field.set_size_request(420, 32)
        self.field.set_placeholder_text("Note")
        # Centred, so the label a person clicks is the field's structural
        # center too (benchmark/repair_every_backend, finding 7).
        self.field.set_alignment(0.5)
        self.field.get_accessible().set_name("Note")
        self.field.set_no_show_all(True)
        content.put(self.field, 24, 150)

        self.save = Gtk.Button(label="Save note")
        self.save.set_size_request(140, 32)
        self.save.connect("clicked", self.save_note)
        self.save.set_no_show_all(True)
        content.put(self.save, 24, 210)

        self.status = Gtk.Label(label="")
        content.put(self.status, 24, 300)
        self.notice: Gtk.Widget | None = None

    @staticmethod
    def window_screen():
        from gi.repository import Gdk

        return Gdk.Screen.get_default()

    def show_form(self) -> None:
        self.field.show()
        self.save.show()
        self.field.grab_focus()
        self.status.set_text("New note for " + self.banner)

    def new_note(self, _button: Gtk.Button) -> None:
        if self.update_notice and not self.notice_shown:
            self.notice_shown = True
            backdrop = Gtk.EventBox()
            backdrop.get_style_context().add_class("notice-backdrop")
            holder = Gtk.Fixed()
            backdrop.add(holder)
            panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
            panel.get_style_context().add_class("notice")
            panel.set_size_request(420, 220)
            panel.set_border_width(24)
            # Announced as a dialog, as Chart.swift gives its panel the AX
            # sheet role.
            panel.get_accessible().set_name("Update available")
            panel.get_accessible().set_role(Atk.Role.DIALOG)
            heading = Gtk.Label(label="Update available", xalign=0)
            heading.get_style_context().add_class("banner")
            body = Gtk.Label(label="Version 2.1 is ready to install.", xalign=0)
            buttons = Gtk.Box(spacing=20)
            later = Gtk.Button(label="Later")
            later.set_size_request(120, 32)
            later.connect("clicked", self.dismiss_later)
            install = Gtk.Button(label="Install now")
            install.set_size_request(140, 32)
            install.connect("clicked", self.dismiss_install)
            buttons.pack_start(later, False, False, 0)
            buttons.pack_start(install, False, False, 0)
            for child in (heading, body, buttons):
                panel.pack_start(child, False, False, 0)
            holder.put(panel, 70, 70)
            self.overlay.add_overlay(backdrop)
            backdrop.show_all()
            self.notice = backdrop
            return
        self.show_form()

    def _close_notice(self) -> None:
        if self.notice is not None:
            self.overlay.remove(self.notice)
            self.notice.destroy()
            self.notice = None

    def dismiss_later(self, _button: Gtk.Button) -> None:
        self._close_notice()
        self.show_form()

    def dismiss_install(self, _button: Gtk.Button) -> None:
        self._close_notice()
        self.status.set_text("Installing")

    def save_note(self, _button: Gtk.Button) -> None:
        saved = insert_note(self.db, self.patient_id, self.field.get_text())
        self.status.set_text("Note saved" if saved else "Save failed")
        self.field.set_text("")
        self.field.hide()
        self.save.hide()


def main(argv: list[str]) -> int:
    db, title, patient_id, banner = argv[1:5]
    mode = argv[5] if len(argv) > 5 else "plain"
    # AT-SPI names the application by the GLib program name.
    GLib.set_prgname(APP_NAME)
    GLib.set_application_name(APP_NAME)
    root = Atk.get_root()
    if root is not None:
        root.set_name(APP_NAME)
    settings = Gtk.Settings.get_default()
    if settings is not None:
        # A steady caret: a blinking one changes the frame between the
        # runtime's frame lease and the save click (#1713).
        settings.set_property("gtk-cursor-blink", False)
    chart = Chart(db, title, patient_id, banner, mode)
    chart.window.show_all()
    chart.window.present()
    Gtk.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
