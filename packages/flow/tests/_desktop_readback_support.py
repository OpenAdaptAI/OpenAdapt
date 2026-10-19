"""Test stand-ins for the two desktop read-back fixtures.

``WinFormsPatientNotes`` is the WinForms fixture
(``scripts/desktop/patient_notes.ps1``) as a fake ``uiautomation`` tree: the
same control names, the same UI Automation shapes (an edit box with a Value
pattern, a DataGridView whose rows hold cells, a label whose Name is its
text), and the same persistence, ``scripts/desktop/pn_db.py`` run as a
subprocess exactly as the PowerShell form runs it. The real in-guest agent
server serves it and the real ``WindowsBackend`` client talks to it, so the
read-back exercises the production HTTP and UI Automation code paths.

``KioskLookupTransport`` is the RDP kiosk fixture
(``benchmark/rdp_ladder/fixture/kiosk_app.py``) drawn as pixels: the booking
labels and the look-up panel at the Tk coordinates, served as an RDP
framebuffer. The read-back reads it with the real OCR engine.

Synthetic data only.
"""

from __future__ import annotations

import base64
import io
import json
import os
import sqlite3
import subprocess
import sys
import types
from pathlib import Path
from typing import Any, Optional

from PIL import Image, ImageDraw, ImageFont

FLOW_ROOT = Path(__file__).resolve().parents[1]
PN_DB = FLOW_ROOT / "scripts" / "desktop" / "pn_db.py"


# -- WinForms patient notes ------------------------------------------------------


class _Rect:
    def __init__(self, left: int, top: int, right: int, bottom: int) -> None:
        self.left, self.top, self.right, self.bottom = left, top, right, bottom


class _ValuePattern:
    def __init__(self, control: "Ctrl") -> None:
        self._control = control

    @property
    def Value(self) -> str:  # noqa: N802 - UI Automation naming
        return self._control.value or ""


class Ctrl:
    """One fake UI Automation control."""

    _next_id = 1

    def __init__(
        self,
        control_type: str,
        *,
        automation_id: str = "",
        name: str = "",
        bounds: tuple[int, int, int, int] = (0, 0, 10, 10),
        value: Optional[str] = None,
    ) -> None:
        self.ControlTypeName = control_type
        self.AutomationId = automation_id
        self.Name = name
        self.ClassName = "WindowsForms10.Window"
        self.ProcessId = 4242
        self.NativeWindowHandle = 0
        self.BoundingRectangle = _Rect(*bounds)
        self.value = value
        self.parent: Optional[Ctrl] = None
        self.children: list[Ctrl] = []
        Ctrl._next_id += 1
        self._runtime_id = (42, Ctrl._next_id)

    def add(self, *children: "Ctrl") -> "Ctrl":
        for child in children:
            child.parent = self
            self.children.append(child)
        return self

    def GetRuntimeId(self) -> list[int]:  # noqa: N802
        return list(self._runtime_id)

    def GetParentControl(self) -> Optional["Ctrl"]:  # noqa: N802
        return self.parent

    def GetChildren(self) -> list["Ctrl"]:  # noqa: N802
        return list(self.children)

    def GetValuePattern(self) -> Optional[_ValuePattern]:  # noqa: N802
        return _ValuePattern(self) if self.value is not None else None

    def contains(self, x: int, y: int) -> bool:
        r = self.BoundingRectangle
        return r.left <= x < r.right and r.top <= y < r.bottom


class _UiaContext:
    def __enter__(self) -> "_UiaContext":
        return self

    def __exit__(self, *args: object) -> None:
        return None


class WinFormsPatientNotes:
    """The patient-notes WinForms fixture, as UI Automation sees it."""

    def __init__(self, db_path: Path, *, drift: str = "none") -> None:
        self.db_path = db_path
        self._db("seed", "--drift", drift)
        self.window = Ctrl(
            "WindowControl",
            name="Patient Notes - Benchmark Harness",
            bounds=(80, 80, 840, 640),
        )
        self.search_box = Ctrl(
            "EditControl",
            automation_id="searchBox",
            name="searchBox",
            bounds=(100, 100, 580, 128),
            value="",
        )
        self.search_button = Ctrl(
            "ButtonControl",
            automation_id="searchButton",
            name="searchButton",
            bounds=(590, 98, 690, 128),
        )
        self.grid = Ctrl(
            "TableControl",
            automation_id="patientGrid",
            name="patientGrid",
            bounds=(100, 140, 690, 360),
        )
        self.header = Ctrl(
            "TextControl",
            automation_id="patientHeader",
            name="",
            bounds=(310, 375, 690, 397),
        )
        self.note_box = Ctrl(
            "EditControl",
            automation_id="noteBox",
            name="noteBox",
            bounds=(100, 400, 690, 520),
            value="",
        )
        self.save_button = Ctrl(
            "ButtonControl",
            automation_id="saveButton",
            name="saveButton",
            bounds=(590, 530, 690, 564),
        )
        self.status = Ctrl(
            "TextControl",
            automation_id="statusLabel",
            name="statusLabel",
            bounds=(100, 538, 570, 560),
        )
        self.window.add(
            self.search_box,
            self.search_button,
            self.grid,
            self.header,
            self.note_box,
            self.save_button,
            self.status,
        )
        self.root = Ctrl("PaneControl", name="Desktop", bounds=(0, 0, 1280, 800))
        self.root.add(self.window)
        self.focus: Optional[Ctrl] = None
        self.selected_all = False
        self.selected_id: Optional[int] = None
        self.row_ids: dict[int, int] = {}
        self.clicked: list[str] = []
        self.saves = 0
        self._load_patients("")

    # -- persistence (exactly the form's Invoke-Db) --------------------------------
    def _db(self, *args: str) -> str:
        env = {**os.environ, "PN_DB": str(self.db_path)}
        return subprocess.run(
            [sys.executable, str(PN_DB), *args],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        ).stdout

    def save_note(self, patient_id: int, note: str) -> None:
        """What the run under test does: the form's Save Note handler."""

        b64 = base64.b64encode(note.encode("utf-8")).decode("ascii")
        self._db("save", str(patient_id), b64)

    def all_patients(self) -> list[dict[str, Any]]:
        return json.loads(self._db("all"))

    # -- form behaviour ---------------------------------------------------------------
    def _load_patients(self, filt: str) -> None:
        rows = json.loads(self._db("list", filt) or "[]")
        self.grid.children = []
        self.row_ids = {}
        top = 180
        for index, row in enumerate(rows):
            line = Ctrl(
                "CustomControl",
                name=f"Row {index}",
                bounds=(100, top + index * 44, 690, top + (index + 1) * 44),
            )
            for column_index, column in enumerate(("first", "last", "dob")):
                left = 100 + column_index * 197
                line.add(
                    Ctrl(
                        "DataItemControl",
                        name=f"{column} Row {index}",
                        bounds=(
                            left,
                            top + index * 44,
                            left + 197,
                            top + (index + 1) * 44,
                        ),
                        value=str(row[column]),
                    )
                )
            self.grid.add(line)
            self.row_ids[id(line)] = int(row["id"])
        self.status.value = None

    def _select(self, row: Ctrl) -> None:
        patient_id = self.row_ids[id(row)]
        self.selected_id = patient_id
        patient = json.loads(self._db("get", str(patient_id)))
        self.note_box.value = patient["note"]
        self.header.Name = (
            f"Patient: {patient['first']} {patient['last']} ({patient['dob']})"
        )

    def click(self, x: int, y: int) -> None:
        for row in self.grid.children:
            if row.contains(x, y):
                self.clicked.append(row.Name)
                self._select(row)
                return
        for control in (self.search_box, self.note_box):
            if control.contains(x, y):
                self.clicked.append(control.AutomationId)
                self.focus = control
                self.selected_all = False
                return
        if self.search_button.contains(x, y):
            self.clicked.append("searchButton")
            self._load_patients(self.search_box.value or "")
            return
        if self.save_button.contains(x, y):
            self.clicked.append("saveButton")
            self.saves += 1
            if self.selected_id is not None:
                self.save_note(self.selected_id, self.note_box.value or "")

    def type_text(self, text: str) -> None:
        if self.focus is None:
            return
        if self.selected_all:
            self.focus.value = ""
            self.selected_all = False
        self.focus.value = (self.focus.value or "") + text

    def press(self, keys: list[str]) -> None:
        lowered = [k.lower() for k in keys]
        if lowered in (["ctrl", "a"], ["control", "a"]):
            self.selected_all = True
        elif lowered == ["enter"] and self.focus is self.search_box:
            self._load_patients(self.search_box.value or "")

    # -- agent seams --------------------------------------------------------------------
    def uia_module(self) -> types.SimpleNamespace:
        def from_point(x: int, y: int) -> Optional[Ctrl]:
            stack = [self.root]
            hit = None
            while stack:
                node = stack.pop()
                if node.contains(x, y):
                    hit = node
                    stack.extend(node.children)
            return hit

        return types.SimpleNamespace(
            GetRootControl=lambda: self.root,
            ControlFromPoint=from_point,
            GetFocusedControl=lambda: self.focus,
            UIAutomationInitializerInThread=_UiaContext,
        )

    def input_fn(self, payload: dict[str, Any]) -> dict[str, Any]:
        action = payload.get("action")
        if action == "click":
            self.click(int(payload["x"]), int(payload["y"]))
            if payload.get("double"):
                self.click(int(payload["x"]), int(payload["y"]))
        elif action == "type_text":
            self.type_text(str(payload["text"]))
        elif action == "press":
            self.press(list(payload["keys"]))
        operation = {
            "click": "physical_double_click"
            if payload.get("double")
            else "physical_click",
            "type_text": "physical_type_text",
            "press": "physical_press",
        }.get(str(action), f"physical_{action}")
        return {
            "status": "delivered",
            "receipt_id": f"input-{len(self.clicked)}",
            "operation": operation,
            "native": False,
            "target_fingerprint": None,
            "delivered_at": "2026-10-18T00:00:00+00:00",
            "outcome_verified": False,
        }


def tiny_png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (4, 2), "white").save(buffer, format="PNG")
    return buffer.getvalue()


# -- RDP kiosk look-up panel --------------------------------------------------------

#: Tk geometry from kiosk_app.py.
VIEWPORT = (1280, 800)
LOOKUP_ENTRY = (50, 370, 330, 408)
FIND_BUTTON = (380, 366, 480, 406)
LOOKUP_REGION = (40, 320, 470, 360)


def _font(size: int) -> Any:
    return ImageFont.load_default(size=size)


class KioskLookupTransport:
    """The kiosk's screen as an RDP framebuffer, with its look-up behaviour.

    Booking rows live in a SQLite file, as in the fixture. Input follows the
    Tk widgets: a click on the look-up entry focuses it, typed characters
    append (or replace a Ctrl+A selection), Enter in the entry or a click on
    Find runs the read-only query. Clicks on the booking form's Save button
    are recorded so a test can prove the read-back never pressed it.
    """

    supports_hwheel = False

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.connected = False
        self.focus: Optional[str] = None
        self.lookup = ""
        self.selected_all = False
        self.found: Optional[str] = None
        self.held: set[str] = set()
        self.save_clicks = 0
        self.booking_typed = ""
        #: What the booking form shows, per field (the form keeps its text
        #: after a save, as the Tk kiosk does).
        self.booking = {"slot": "", "visit": "", "request": ""}
        self.inputs: list[str] = []
        #: Read-back sessions opened on this kiosk's database (tests).
        self.sessions: list["KioskLookupTransport"] = []
        with sqlite3.connect(db_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS appointments (
                    appointment_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE,
                    patient_mrn TEXT NOT NULL,
                    patient_name TEXT NOT NULL,
                    appointment_slot TEXT NOT NULL,
                    visit_type TEXT NOT NULL,
                    status TEXT NOT NULL
                )
                """
            )

    def book(self, request_id: str, mrn: str, name: str, slot: str, visit: str) -> None:
        """What the run under test does: the kiosk's Save appointment handler."""

        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                "INSERT INTO appointments VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    f"APT-{request_id[-4:]}",
                    request_id,
                    mrn,
                    name,
                    slot,
                    visit,
                    "scheduled",
                ),
            )

    # -- RDPTransport ------------------------------------------------------------------
    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def framebuffer(self) -> tuple[Image.Image, int, int]:
        image = self.render()
        return image, image.width, image.height

    def pointer(self, x: int, y: int, button: str, down: bool) -> None:
        if down:
            return
        self.inputs.append(f"click {x},{y}")
        ex0, ey0, ex1, ey1 = LOOKUP_ENTRY
        bx0, by0, bx1, by1 = FIND_BUTTON
        if ex0 <= x <= ex1 and ey0 <= y <= ey1:
            self.focus = "lookup"
            self.selected_all = False
        elif bx0 <= x <= bx1 and by0 <= y <= by1:
            self._find()
        elif 550 <= x <= 830 and 570 <= y <= 615:
            self.save_clicks += 1
        elif 550 <= x <= 1000 and 250 <= y <= 288:
            self.focus = "slot"
        elif 550 <= x <= 1000 and 370 <= y <= 408:
            self.focus = "visit"
        elif 550 <= x <= 1000 and 490 <= y <= 528:
            self.focus = "request"
        else:
            self.focus = None

    def key(self, keysym_or_char: str, down: bool) -> None:
        if not down:
            self.held.discard(keysym_or_char)
            return
        self.held.add(keysym_or_char)
        self.inputs.append(f"key {keysym_or_char}")
        if keysym_or_char == "a" and "ctrl" in self.held:
            self.selected_all = self.focus == "lookup"
            return
        if keysym_or_char in ("ctrl", "shift", "alt", "meta"):
            return
        if keysym_or_char == "enter":
            if self.focus == "lookup":
                self._find()
            return
        if len(keysym_or_char) != 1:
            return
        if self.focus == "lookup":
            if self.selected_all:
                self.lookup = ""
                self.selected_all = False
            self.lookup += keysym_or_char
        elif self.focus in self.booking:
            self.booking_typed += keysym_or_char
            self.booking[self.focus] += keysym_or_char

    def wheel(self, dx: int, dy: int) -> None:
        return None

    # -- the kiosk --------------------------------------------------------------------
    def _find(self) -> None:
        request = self.lookup.strip()
        if not request:
            self.found = ""
            return
        connection = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            row = connection.execute(
                "SELECT request_id, patient_name, patient_mrn, appointment_slot, "
                "visit_type, status FROM appointments WHERE request_id = ?",
                (request,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            self.found = "No appointment found"
            return
        self.found = "\n".join(
            [
                f"Request ID: {row[0]}",
                f"Patient: {row[1]}",
                f"MRN: {row[2]}",
                f"Slot: {row[3]}",
                f"Visit type: {row[4]}",
                f"Status: {row[5]}",
            ]
        )

    def render(self) -> Image.Image:
        image = Image.new("RGB", VIEWPORT, "#f4f6f8")
        draw = ImageDraw.Draw(image)
        ink = "#101418"
        draw.text(
            (50, 34),
            "Northstar Clinic - Appointment Scheduling",
            fill=ink,
            font=_font(30),
        )
        draw.text((50, 116), "Patient roster", fill=ink, font=_font(20))
        for index, row in enumerate(
            ("Ada Lovelace    MRN A1001", "Grace Hopper    MRN B2002")
        ):
            top = 166 + index * 70
            draw.rectangle((50, top, 480, top + 54), fill="#ffffff", outline="#888888")
            draw.text((60, top + 14), row, fill=ink, font=_font(22))
        # The booking form (the read-back must never touch it).
        draw.text((550, 130), "Active record: (none)", fill=ink, font=_font(20))
        for label, top, key in (
            ("Appointment slot", 210, "slot"),
            ("Visit type", 330, "visit"),
            ("Request ID", 450, "request"),
        ):
            draw.text((550, top), label, fill=ink, font=_font(20))
            draw.rectangle(
                (550, top + 40, 1000, top + 78), fill="#ffffff", outline="#888888"
            )
            if self.booking[key]:
                draw.text((558, top + 48), self.booking[key], fill=ink, font=_font(22))
        draw.rectangle((550, 570, 830, 615), fill="#2d6fd8")
        draw.text((570, 580), "Save appointment", fill="#ffffff", font=_font(22))
        # The look-up panel.
        draw.text((50, 330), "Find appointment", fill=ink, font=_font(20))
        draw.rectangle(LOOKUP_ENTRY, fill="#ffffff", outline="#888888")
        if self.lookup:
            draw.text((58, 378), self.lookup, fill=ink, font=_font(22))
        draw.rectangle(FIND_BUTTON, fill="#ffffff", outline="#888888")
        draw.text((404, 376), "Find", fill=ink, font=_font(20))
        if self.found:
            for index, line in enumerate(self.found.split("\n")):
                draw.text((50, 436 + index * 36), line, fill=ink, font=_font(20))
        return image
