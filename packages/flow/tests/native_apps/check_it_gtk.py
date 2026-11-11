"""Synthetic prefilled GTK row with observable Save and autosave effects."""

from __future__ import annotations

import sqlite3
import sys

import gi

gi.require_version("Atk", "1.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Atk, GLib, Gtk  # noqa: E402

APP_NAME = "OpenAdapt Check It"


def main() -> None:
    database, title, record = sys.argv[1:4]
    GLib.set_prgname(APP_NAME)
    GLib.set_application_name(APP_NAME)
    Atk.get_root().set_name(APP_NAME)
    window = Gtk.Window(title=title)
    window.set_default_size(640, 240)
    window.connect("destroy", Gtk.main_quit)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    window.add(box)
    row = Gtk.ListBoxRow()
    content = Gtk.Box(spacing=12)
    # Expose the record's cells at the row itself. A GtkListBoxRow is a
    # single-child bin; excluding its whole box branch loses all identity.
    content.get_accessible().set_role(Atk.Role.TABLE_ROW)
    row.add(content)
    label = Gtk.Label(label="Record " + record)
    content.pack_start(label, False, False, 0)
    entry = Gtk.Entry()
    entry.set_text("prefilled")
    entry.get_accessible().set_name("Note")
    content.pack_start(entry, True, True, 0)
    listing = Gtk.ListBox()
    listing.add(row)
    box.pack_start(listing, False, False, 0)

    def write(operation: str) -> None:
        with sqlite3.connect(database) as connection:
            connection.execute(
                "INSERT INTO effects(operation, record_id, value) VALUES (?, ?, ?)",
                (operation, record, entry.get_text()),
            )

    entry.connect("changed", lambda *_: write("autosave"))
    save = Gtk.Button(label="Save")
    save.connect("clicked", lambda *_: write("save"))
    content.pack_start(save, False, False, 0)
    navigate = Gtk.Button(label="Navigate")
    navigate.connect("clicked", lambda *_: write("navigation"))
    box.pack_start(navigate, False, False, 0)
    notice = Gtk.Label()
    notice.set_markup("<span font_desc='Sans 32'>Ready</span>")
    box.pack_start(notice, False, False, 0)
    window.connect("key-press-event", lambda *_: write("key") or False)
    window.show_all()
    window.present()
    Gtk.main()


if __name__ == "__main__":
    main()
