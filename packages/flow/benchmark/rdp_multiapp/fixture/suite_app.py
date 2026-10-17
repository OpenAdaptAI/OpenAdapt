#!/usr/bin/env python3
"""Deterministic, pixel-only back-office suite for the RDP vision campaign.

The fixture presents three separate task windows and a small launcher.  Flow
can observe only the RDP-decoded pixels and can act only through the RDP input
channel.  The independent result surfaces are SQLite, CSV, and Maildir files.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import sqlite3
import tkinter as tk
from email.message import EmailMessage
from functools import partial
from pathlib import Path

ROOT = Path(os.environ.get("RDP_MULTIAPP_ORACLE_ROOT", "/opt/rdp_multiapp/oracle"))
DB_PATH = ROOT / "appointments.sqlite3"
CSV_PATH = ROOT / "worklist.csv"
MAILDIR = ROOT / "outbox"
CONTROL_PATH = ROOT / "control.json"
ACK_PATH = ROOT / "reset_ack.txt"
FAULT_ACK_PATH = ROOT / "fault_ack.json"
INPUT_LEDGER_PATH = ROOT / "input-ledger.jsonl"

BG = "#eef2f6"
PANEL = "#ffffff"
FG = "#142033"
BLUE = "#2457d6"
GREEN = "#16803c"
RED = "#b42318"
ROW_HEIGHT = 52
TARGET_REQUEST = "REQ-LIVE-2048"
TARGET_NAME = "Jordan Lee"
TARGET_RECORD = "REC-2048"


def _connect() -> sqlite3.Connection:
    ROOT.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS appointments (
            appointment_id TEXT PRIMARY KEY,
            request_id TEXT NOT NULL UNIQUE,
            record_id TEXT NOT NULL,
            entity_name TEXT NOT NULL,
            appointment_slot TEXT NOT NULL,
            appointment_type TEXT NOT NULL,
            status TEXT NOT NULL
        )
        """
    )
    connection.commit()
    return connection


def _scenario() -> str:
    try:
        return str(json.loads(CONTROL_PATH.read_text()).get("scenario", "healthy"))
    except (OSError, ValueError, TypeError):
        return "healthy"


def _reset_token() -> str | None:
    try:
        value = json.loads(CONTROL_PATH.read_text()).get("reset_token")
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, str) and value else None


def _fault_control() -> tuple[str | None, str | None]:
    try:
        control = json.loads(CONTROL_PATH.read_text())
    except (OSError, ValueError, TypeError):
        return None, None
    fault = control.get("fault")
    token = control.get("fault_token")
    return (
        fault if isinstance(fault, str) and fault else None,
        token if isinstance(token, str) and token else None,
    )


def _fault_ack_payload() -> dict[str, object]:
    """Describe the exact synthetic visual fault armed by the control token."""

    scenario = _scenario()
    fault, token = _fault_control()
    partial = fault == "partial_render"
    return {
        "reset_token": _reset_token(),
        "scenario": scenario,
        "fault": fault,
        "fault_token": token,
        "scheduler_ready_visible": not partial,
        "active_identity_visible": not partial,
        "save_control_count": 2 if scenario == "duplicate_save_control" else 1,
    }


def _rows() -> list[dict[str, str]]:
    rows = [
        {
            "request_id": f"REQ-{1000 + i}",
            "record_id": f"REC-{1000 + i}",
            "name": f"Sample Person {i:02d}",
            "status": "New",
        }
        for i in range(18)
    ]
    target = {
        "request_id": TARGET_REQUEST,
        "record_id": TARGET_RECORD,
        "name": TARGET_NAME,
        "status": "New",
    }
    # One scroll keeps the target inside the viewport in both layouts. The
    # reordered case moves it to a different row so replay must re-resolve it.
    insert_at = 11 if _scenario() == "row_reordered" else 13
    rows.insert(insert_at, target)
    return rows


def _write_rows(rows: list[dict[str, str]]) -> None:
    with CSV_PATH.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=["request_id", "record_id", "name", "status"]
        )
        writer.writeheader()
        writer.writerows(rows)


def _read_rows() -> list[dict[str, str]]:
    with CSV_PATH.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def _reset_persisted_state() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    with _connect() as connection:
        connection.execute("DELETE FROM appointments")
        connection.commit()
    _write_rows(_rows())
    INPUT_LEDGER_PATH.write_text("", encoding="utf-8")
    for subdir in ("cur", "new", "tmp"):
        path = MAILDIR / subdir
        path.mkdir(parents=True, exist_ok=True)
        for child in path.iterdir():
            if child.is_file():
                child.unlink()


def _record_input(action: str, **details: str | None) -> None:
    entry = {"action": action, **details}
    with INPUT_LEDGER_PATH.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, sort_keys=True) + "\n")


def _label(parent: tk.Misc, text: str, x: int, y: int, **kwargs) -> tk.Label:
    label = tk.Label(parent, text=text, bg=BG, fg=FG, **kwargs)
    label.place(x=x, y=y)
    return label


class Suite:
    def __init__(self) -> None:
        _reset_persisted_state()
        self.root = tk.Tk()
        # The remote display has no window manager. Keep the root mapped so
        # its toplevels can own the X focus used by RDP keyboard delivery.
        self.root.geometry("1x1+0+0")
        self.root.overrideredirect(True)
        self.windows: dict[str, tk.Toplevel] = {}
        self.selected_request: str | None = None
        self.active_record: tuple[str, str] | None = None
        self.last_reset_token = _reset_token()
        self.last_fault_token: str | None = None
        self.partial_render_latched = False
        self._build_inbox()
        self._build_worklist()
        self._build_scheduler()
        self._build_launcher()
        self._build_environment_banner()
        self.reset()
        self.root.after(250, lambda: self.show("Inbox"))
        self.root.after(100, self._poll_control)

    def _window(self, title: str) -> tk.Toplevel:
        window = tk.Toplevel(self.root)
        window.title(title)
        window.geometry("1280x740+0+0")
        window.configure(bg=BG)
        window.overrideredirect(True)
        window.resizable(False, False)
        self.windows[title] = window
        return window

    def show(self, title: str) -> None:
        window = self.windows[title]
        if title == "Worklist":
            self.work_canvas.yview_moveto(0)
        window.deiconify()
        window.lift()
        window.focus_force()
        self.launcher.lift()
        self.environment_banner.lift()

    def _build_launcher(self) -> None:
        launcher = tk.Toplevel(self.root)
        launcher.title("OpenAdapt Fixture Launcher")
        launcher.geometry("1280x60+0+740")
        launcher.configure(bg="#172033")
        launcher.overrideredirect(True)
        launcher.attributes("-topmost", True)
        self.launcher = launcher
        tk.Label(
            launcher,
            text="OpenAdapt RDP Fixture",
            bg="#172033",
            fg="white",
            font=("DejaVu Sans", 12, "bold"),
        ).place(x=28, y=8)
        tk.Label(
            launcher,
            text="app oa-rdp-fixture",
            bg="#172033",
            fg="#bfdbfe",
            font=("DejaVu Sans", 10, "bold"),
        ).place(x=28, y=34)
        tk.Label(
            launcher,
            text="oa-fixture-v1",
            bg="#172033",
            fg="#bfdbfe",
            font=("DejaVu Sans", 9, "bold"),
        ).place(x=850, y=4)
        tk.Label(
            launcher,
            text="oa-rdp-env",
            bg="#172033",
            fg="#bfdbfe",
            font=("DejaVu Sans", 9, "bold"),
        ).place(x=850, y=21)
        self.session_label = tk.Label(
            launcher,
            text="",
            bg="#172033",
            fg="#bfdbfe",
            font=("DejaVu Sans", 9, "bold"),
        )
        self.session_label.place(x=850, y=38)
        for index, title in enumerate(("Inbox", "Worklist", "Scheduler")):
            tk.Button(
                launcher,
                text=title,
                command=partial(self.show, title),
                bg="#2d3d5e",
                fg="white",
                activebackground=BLUE,
                activeforeground="white",
                font=("DejaVu Sans", 13, "bold"),
                relief="flat",
            ).place(x=285 + index * 190, y=8, width=165, height=44)

    def _build_environment_banner(self) -> None:
        """Render PHI-free environment identity in a stable visible band.

        The bottom launcher can sit outside a client viewport when a remote
        client adds window chrome.  Keep the qualification-only markers in an
        otherwise unused top-right band so the decoded RDP frame can always
        prove the application, version, and fixture environment.
        """

        banner = tk.Toplevel(self.root)
        banner.geometry("320x72+960+0")
        banner.configure(bg="#172033")
        banner.overrideredirect(True)
        banner.attributes("-topmost", True)
        self.environment_banner = banner
        tk.Label(
            banner,
            text="app oa-rdp-fixture",
            bg="#172033",
            fg="#bfdbfe",
            font=("DejaVu Sans", 14, "bold"),
        ).place(x=14, y=2)
        tk.Label(
            banner,
            text="oa-fixture-v1",
            bg="#172033",
            fg="#bfdbfe",
            font=("DejaVu Sans", 12, "bold"),
        ).place(x=14, y=28)
        tk.Label(
            banner,
            text="oa-rdp-env",
            bg="#172033",
            fg="#bfdbfe",
            font=("DejaVu Sans", 12, "bold"),
        ).place(x=14, y=49)

    def _build_inbox(self) -> None:
        window = self._window("Inbox")
        _label(window, "Referral inbox", 42, 30, font=("DejaVu Sans", 26, "bold"))
        _label(
            window,
            "Incoming requests that need a scheduled appointment",
            42,
            76,
            font=("DejaVu Sans", 14),
        )
        self.inbox_row = tk.Button(
            window,
            text=(
                f"{TARGET_NAME}    {TARGET_RECORD}    {TARGET_REQUEST}\n"
                "Cardiology referral  ·  requested this week"
            ),
            command=self._select_inbox,
            anchor="w",
            justify="left",
            bg=PANEL,
            fg=FG,
            activebackground="#dce7ff",
            font=("DejaVu Sans", 16),
            relief="solid",
            bd=1,
        )
        self.inbox_row.place(x=42, y=130, width=870, height=86)
        self.inbox_detail = _label(
            window,
            "Select the request to review its structured details.",
            42,
            250,
            font=("DejaVu Sans", 15),
        )
        self.send_button = tk.Button(
            window,
            text="Send confirmation",
            command=self._send_confirmation,
            state="disabled",
            bg=BLUE,
            fg="white",
            disabledforeground="#7b879a",
            font=("DejaVu Sans", 16, "bold"),
        )
        self.send_button.place(x=42, y=420, width=250, height=54)
        self.inbox_status = _label(
            window, "", 42, 500, font=("DejaVu Sans", 16, "bold")
        )

    def _select_inbox(self) -> None:
        self.selected_request = TARGET_REQUEST
        self.inbox_detail.config(
            text=(
                f"Request: {TARGET_REQUEST}\n"
                f"Record: {TARGET_RECORD}  ·  Name: {TARGET_NAME}\n"
                "Requested type: Cardiology follow-up"
            )
        )
        self.send_button.config(state="normal")

    def _send_confirmation(self) -> None:
        _record_input(
            "send_confirmation",
            selected_request=self.selected_request,
        )
        rows = _read_rows()
        status = next(
            (row["status"] for row in rows if row["request_id"] == TARGET_REQUEST),
            None,
        )
        with _connect() as connection:
            appointment = connection.execute(
                "SELECT appointment_id, appointment_slot FROM appointments "
                "WHERE request_id = ?",
                (TARGET_REQUEST,),
            ).fetchone()
        if (
            self.selected_request != TARGET_REQUEST
            or status != "Scheduled"
            or not appointment
        ):
            self.inbox_status.config(
                text="Refused: schedule and reconcile this request first", fg=RED
            )
            return
        message = EmailMessage()
        message["From"] = "scheduling@example.test"
        message["To"] = "referrals@example.test"
        message["Subject"] = f"Scheduled {TARGET_REQUEST}"
        message["X-Request-ID"] = TARGET_REQUEST
        message.set_content(
            f"{TARGET_NAME} is scheduled for {appointment[1]}. "
            f"Appointment {appointment[0]}."
        )
        name = hashlib.sha256(TARGET_REQUEST.encode()).hexdigest()[:16] + ".eml"
        path = MAILDIR / "new" / name
        if not path.exists():
            path.write_bytes(message.as_bytes())
        self.inbox_status.config(text="Confirmation queued", fg=GREEN)

    def _build_worklist(self) -> None:
        window = self._window("Worklist")
        _label(window, "Scheduling worklist", 42, 30, font=("DejaVu Sans", 26, "bold"))
        _label(
            window,
            "Scroll to the request, select it, then update its persisted status.",
            42,
            76,
            font=("DejaVu Sans", 14),
        )
        canvas = tk.Canvas(
            window,
            bg=PANEL,
            highlightthickness=1,
            highlightbackground="#bdc7d6",
            yscrollincrement=ROW_HEIGHT,
        )
        canvas.place(x=42, y=125, width=900, height=460)
        scrollbar = tk.Scrollbar(window, orient="vertical", command=canvas.yview)
        scrollbar.place(x=942, y=125, width=22, height=460)
        canvas.configure(yscrollcommand=scrollbar.set)
        self.work_canvas = canvas
        self.work_inner = tk.Frame(canvas, bg=PANEL)
        canvas.create_window((0, 0), window=self.work_inner, anchor="nw")
        self.work_inner.bind(
            "<Configure>",
            lambda _event: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        for widget in (canvas, self.work_inner):
            widget.bind("<Button-4>", lambda _event: canvas.yview_scroll(-1, "units"))
            widget.bind("<Button-5>", lambda _event: canvas.yview_scroll(1, "units"))
        self.work_focus_button = tk.Button(
            window,
            text="Scroll request list",
            command=canvas.focus_set,
            bg="#dce7ff",
            fg=FG,
            font=("DejaVu Sans", 13, "bold"),
        )
        self.work_focus_button.place(x=990, y=125, width=240, height=44)
        self.work_focus_button.bind(
            "<Button-4>", lambda _event: canvas.yview_scroll(-1, "units")
        )
        self.work_focus_button.bind(
            "<Button-5>", lambda _event: canvas.yview_scroll(1, "units")
        )
        self.work_status = _label(
            window, "No request selected", 42, 610, font=("DejaVu Sans", 15, "bold")
        )
        self.mark_button = tk.Button(
            window,
            text="Mark scheduled",
            command=self._mark_scheduled,
            bg=BLUE,
            fg="white",
            font=("DejaVu Sans", 16, "bold"),
            state="disabled",
        )
        self.mark_button.place(x=990, y=190, width=240, height=54)

    def _render_worklist(self) -> None:
        for child in self.work_inner.winfo_children():
            child.destroy()
        self.work_canvas.yview_moveto(0)
        self.work_selected: str | None = None
        for index, row in enumerate(_read_rows()):
            text = (
                f"{row['request_id']}    {row['record_id']}    "
                f"{row['name']}    {row['status']}"
            )
            button = tk.Button(
                self.work_inner,
                text=text,
                command=partial(self._select_work, row["request_id"]),
                anchor="w",
                bg=PANEL,
                fg=FG,
                activebackground="#dce7ff",
                font=("DejaVu Sans", 13),
                relief="solid",
                bd=1,
            )
            button.grid(row=index, column=0, sticky="ew")
            button.configure(width=88, height=2)
            button.bind(
                "<Button-4>", lambda _event: self.work_canvas.yview_scroll(-1, "units")
            )
            button.bind(
                "<Button-5>", lambda _event: self.work_canvas.yview_scroll(1, "units")
            )

    def _select_work(self, request_id: str) -> None:
        self.work_selected = request_id
        self.work_status.config(text=f"Selected request: {request_id}", fg=FG)
        self.mark_button.config(state="normal")

    def _mark_scheduled(self) -> None:
        _record_input("mark_scheduled", selected_request=self.work_selected)
        if self.work_selected != TARGET_REQUEST:
            self.work_status.config(text="Refused: wrong request selected", fg=RED)
            return
        with _connect() as connection:
            appointment = connection.execute(
                "SELECT 1 FROM appointments WHERE request_id = ?", (TARGET_REQUEST,)
            ).fetchone()
        if not appointment:
            self.work_status.config(
                text="Refused: appointment is not persisted", fg=RED
            )
            return
        rows = _read_rows()
        for row in rows:
            if row["request_id"] == TARGET_REQUEST:
                row["status"] = "Scheduled"
        _write_rows(rows)
        self.work_status.config(text=f"Reconciled {TARGET_REQUEST}", fg=GREEN)
        self._render_worklist()

    def _build_scheduler(self) -> None:
        window = self._window("Scheduler")
        _label(
            window, "Appointment scheduler", 42, 30, font=("DejaVu Sans", 26, "bold")
        )
        _label(window, "Record list", 42, 92, font=("DejaVu Sans", 15, "bold"))
        self.record_buttons: list[tk.Button] = []
        for index, (name, record_id) in enumerate(
            ((TARGET_NAME, TARGET_RECORD), ("Morgan Reed", "REC-3099"))
        ):
            button = tk.Button(
                window,
                text=f"{name}    {record_id}",
                command=partial(self._select_record, name, record_id),
                anchor="w",
                bg=PANEL,
                fg=FG,
                font=("DejaVu Sans", 15),
            )
            button.place(x=42, y=130 + index * 68, width=410, height=54)
            self.record_buttons.append(button)
        self.active_label = _label(
            window, "Active record: (none)", 520, 94, font=("DejaVu Sans", 16, "bold")
        )
        self.identity_loading = tk.Label(
            window,
            text="Loading record identity…",
            bg="#d8dee8",
            fg="#667085",
            font=("DejaVu Sans", 16, "bold"),
            anchor="w",
        )
        self.slot, self.slot_label = self._entry(
            window, "Appointment date and time", 520, 165
        )
        self.kind, self.kind_label = self._entry(window, "Appointment type", 520, 285)
        self.request, self.request_label = self._entry(window, "Request ID", 520, 405)
        self.save_button = tk.Button(
            window,
            text="Save appointment",
            command=self._save_appointment,
            bg=BLUE,
            fg="white",
            font=("DejaVu Sans", 16, "bold"),
        )
        self.save_button.place(x=520, y=540, width=260, height=56)
        self.duplicate_save_button = tk.Button(
            window,
            text="Save appointment",
            command=self._save_appointment,
            bg=BLUE,
            fg="white",
            font=("DejaVu Sans", 16, "bold"),
        )
        self.scheduler_status = _label(
            window, "", 520, 625, font=("DejaVu Sans", 16, "bold")
        )

    def _entry(
        self, parent: tk.Misc, title: str, x: int, y: int
    ) -> tuple[tk.Entry, tk.Label]:
        label = _label(parent, title, x, y, font=("DejaVu Sans", 14, "bold"))
        entry = tk.Entry(
            parent,
            font=("DejaVu Sans", 17),
            bg=PANEL,
            fg=FG,
            insertbackground=FG,
            insertofftime=0,
        )
        entry.place(x=x, y=y + 36, width=590, height=42)
        return entry, label

    def _select_record(self, name: str, record_id: str) -> None:
        self.active_record = (name, record_id)
        self.active_label.config(text=f"Active record: {name}  {record_id}")

    def _save_appointment(self) -> None:
        slot = self.slot.get().strip()
        kind = self.kind.get().strip()
        request_id = self.request.get().strip()
        _record_input(
            "save_appointment",
            active_record_id=(self.active_record or (None, None))[1],
            appointment_slot=slot,
            appointment_type=kind,
            request_id=request_id,
        )
        if self.active_record != (TARGET_NAME, TARGET_RECORD):
            self.scheduler_status.config(text="Refused: wrong active record", fg=RED)
            return
        if not slot or not kind or request_id != TARGET_REQUEST:
            self.scheduler_status.config(text="Refused: incomplete request", fg=RED)
            return
        appointment_id = (
            "APT-" + hashlib.sha256(request_id.encode()).hexdigest()[:8].upper()
        )
        with _connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO appointments (
                    appointment_id, request_id, record_id, entity_name,
                    appointment_slot, appointment_type, status
                ) VALUES (?, ?, ?, ?, ?, ?, 'scheduled')
                """,
                (appointment_id, request_id, TARGET_RECORD, TARGET_NAME, slot, kind),
            )
            connection.commit()
        self.scheduler_status.config(
            text=f"Appointment saved  ·  {appointment_id}", fg=GREEN
        )

    def _poll_control(self) -> None:
        token = _reset_token()
        if token is not None and token != self.last_reset_token:
            self.last_reset_token = token
            self.reset()
        fault, fault_token = _fault_control()
        if (
            fault == "partial_render"
            and fault_token is not None
            and fault_token != self.last_fault_token
        ):
            self.last_fault_token = fault_token
            self.partial_render_latched = True
            self._apply_partial_render()
            self._write_fault_ack()
        self.root.after(100, self._poll_control)

    def _apply_partial_render(self) -> None:
        """Latch the incomplete scheduler frame without changing saved state."""

        self.active_label.place_forget()
        self.identity_loading.place(x=520, y=94, width=740, height=44)

    def _write_fault_ack(self) -> None:
        payload = _fault_ack_payload()
        payload["scheduler_ready_visible"] = not self.partial_render_latched
        payload["active_identity_visible"] = not self.partial_render_latched
        payload["identity_surface"] = (
            "loading_skeleton" if self.partial_render_latched else "active_record"
        )
        FAULT_ACK_PATH.write_text(
            json.dumps(payload, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def reset(self) -> None:
        _reset_persisted_state()
        self.partial_render_latched = False
        self.last_fault_token = None
        self.selected_request = None
        self.active_record = None
        token = self.last_reset_token
        self.session_label.config(
            text=f"oa-session-{token[:8]}" if token else "oa-session-pending"
        )
        self.inbox_detail.config(
            text="Select the request to review its structured details."
        )
        self.inbox_status.config(text="")
        self.send_button.config(state="disabled")
        self._render_worklist()
        self.work_status.config(text="No request selected", fg=FG)
        self.mark_button.config(state="disabled")
        self.active_label.config(text="Active record: (none)")
        self.identity_loading.place_forget()
        self.active_label.place(x=520, y=94)
        self.slot_label.place(x=520, y=165)
        self.slot.place(x=520, y=201, width=590, height=42)
        self.kind_label.place(x=520, y=285)
        self.kind.place(x=520, y=321, width=590, height=42)
        self.request.place(x=520, y=441, width=590, height=42)
        self.request_label.place(x=520, y=405)
        if _scenario() == "duplicate_save_control":
            # Neither same-label candidate remains at the recorded target
            # origin. The pixel resolver must therefore refuse ambiguity
            # instead of preferring the original coordinate.
            self.save_button.place(x=260, y=540, width=260, height=56)
        else:
            self.save_button.place(x=520, y=540, width=260, height=56)
        if _scenario() == "duplicate_save_control":
            self.duplicate_save_button.place(x=840, y=540, width=260, height=56)
        else:
            self.duplicate_save_button.place_forget()
        for entry in (self.slot, self.kind, self.request):
            entry.delete(0, "end")
        self.scheduler_status.config(text="")
        ACK_PATH.write_text(self.last_reset_token or "startup", encoding="utf-8")
        self._write_fault_ack()
        self.show("Inbox")

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    Suite().run()
