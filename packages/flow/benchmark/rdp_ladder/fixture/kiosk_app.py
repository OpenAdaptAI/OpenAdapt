#!/usr/bin/env python3
"""Deterministic appointment-booking kiosk for the real-RDP qualification.

The application runs as the complete remote session. It has no DOM or
accessibility tree. OpenAdapt must therefore use the RDP pixels for observation
and the RDP input channel for actuation.

Booking writes one row to a SQLite system of record. The qualification process
opens that database through a separate read-only connection. The verifier does
not trust the screen or the application success message.
"""

import hashlib
import os
import signal
import sqlite3
import tkinter as tk

DB_PATH = os.environ.get(
    "RDP_FIXTURE_DB_PATH",
    "/opt/rdp_fixture/oracle/appointments.sqlite3",
)
RESET_ACK_PATH = os.environ.get(
    "RDP_FIXTURE_RESET_ACK_PATH", "/opt/rdp_fixture/reset_ack.txt"
)
THEME = os.environ.get("RDP_FIXTURE_THEME", "light")

if THEME == "dark":
    BG, FG, ROW_BG, BTN_BG = "#101418", "#e8eef4", "#1c2733", "#2d5fa8"
else:
    BG, FG, ROW_BG, BTN_BG = "#f4f6f8", "#101418", "#ffffff", "#2d6fd8"

PATIENTS = [
    ("Ada Lovelace", "MRN A1001"),
    ("Grace Hopper", "MRN B2002"),
]

state = {"active": None, "saved": False}


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
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
    connection.commit()
    return connection


def _clear_appointments() -> None:
    with _connect() as connection:
        connection.execute("DELETE FROM appointments")
        connection.commit()


def main() -> None:
    _clear_appointments()
    root = tk.Tk()
    root.title("OpenAdapt RDP Fixture")
    root.geometry("1280x800+0+0")
    root.configure(bg=BG)
    root.resizable(False, False)
    # No window manager runs on the fixture display, so the toplevel must claim
    # keyboard focus itself: override-redirect makes it borderless and
    # WM-independent (the app fills the framebuffer at 0,0 so its hardcoded
    # geometry stays stable), and focus_force gives it the X input focus that
    # RDP-forwarded keystrokes need.
    root.overrideredirect(True)
    root.after(200, root.focus_force)

    tk.Label(
        root,
        text="Northstar Clinic  ·  Appointment Scheduling",
        font=("DejaVu Sans", 26, "bold"),
        bg=BG,
        fg=FG,
    ).place(x=50, y=34)

    tk.Label(
        root,
        text="Patient roster",
        font=("DejaVu Sans", 16, "bold"),
        bg=BG,
        fg=FG,
    ).place(x=50, y=116)

    active_lbl = tk.Label(
        root,
        text="Active record: (none)",
        font=("DejaVu Sans", 16, "bold"),
        bg=BG,
        fg=FG,
    )
    active_lbl.place(x=550, y=130)

    status_lbl = tk.Label(
        root, text="", font=("DejaVu Sans", 16, "bold"), bg=BG, fg="#1a7f37"
    )
    status_lbl.place(x=550, y=660)

    def select(name, mrn, btn):
        state["active"] = (name, mrn)
        active_lbl.config(text=f"Active record: {name}  {mrn}")
        for b in row_btns:
            b.config(relief="raised", bd=2)
        btn.config(relief="sunken", bd=4)

    row_btns = []
    for i, (name, mrn) in enumerate(PATIENTS):
        b = tk.Button(
            root,
            text=f"{name}    {mrn}",
            font=("DejaVu Sans", 18),
            width=34,
            anchor="w",
            bg=ROW_BG,
            fg=FG,
            activebackground=ROW_BG,
            relief="raised",
            bd=2,
        )
        b.config(command=lambda n=name, m=mrn, bb=b: select(n, m, bb))
        b.place(x=50, y=166 + i * 70)
        row_btns.append(b)

    tk.Label(
        root,
        text="Appointment slot",
        font=("DejaVu Sans", 16, "bold"),
        bg=BG,
        fg=FG,
    ).place(x=550, y=210)
    slot = tk.Entry(
        root,
        font=("DejaVu Sans", 18),
        width=35,
        bg=ROW_BG,
        fg=FG,
        insertbackground=FG,
        insertofftime=0,
    )
    slot.place(x=550, y=250)

    tk.Label(
        root,
        text="Visit type",
        font=("DejaVu Sans", 16, "bold"),
        bg=BG,
        fg=FG,
    ).place(x=550, y=330)
    visit_type = tk.Entry(
        root,
        font=("DejaVu Sans", 18),
        width=35,
        bg=ROW_BG,
        fg=FG,
        insertbackground=FG,
        insertofftime=0,
    )
    visit_type.place(x=550, y=370)

    tk.Label(
        root,
        text="Request ID",
        font=("DejaVu Sans", 16, "bold"),
        bg=BG,
        fg=FG,
    ).place(x=550, y=450)
    request_id = tk.Entry(
        root,
        font=("DejaVu Sans", 18),
        width=35,
        bg=ROW_BG,
        fg=FG,
        insertbackground=FG,
        insertofftime=0,
    )
    request_id.place(x=550, y=490)

    for field in (slot, visit_type, request_id):
        # The complete remote session has no window manager. A click must claim
        # the X focus and the exact field focus before RDP text delivery.
        field.bind(
            "<Button-1>",
            lambda _event, target=field: (root.focus_force(), target.focus_set()),
        )

    def save():
        active = state["active"]
        appointment_slot = slot.get().strip()
        appointment_type = visit_type.get().strip()
        request = request_id.get().strip()
        if not active or not appointment_slot or not appointment_type or not request:
            status_lbl.config(
                text="Refused: select a patient and complete all fields",
                fg="#b42318",
            )
            return
        appointment_id = (
            "APT-" + hashlib.sha256(request.encode()).hexdigest()[:8].upper()
        )
        with _connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO appointments (
                        appointment_id,
                        request_id,
                        patient_mrn,
                        patient_name,
                        appointment_slot,
                        visit_type,
                        status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        appointment_id,
                        request,
                        active[1],
                        active[0],
                        appointment_slot,
                        appointment_type,
                        "scheduled",
                    ),
                )
                connection.commit()
            except sqlite3.IntegrityError:
                existing = connection.execute(
                    """
                    SELECT patient_mrn, appointment_slot, visit_type
                    FROM appointments
                    WHERE request_id = ?
                    """,
                    (request,),
                ).fetchone()
                if existing != (active[1], appointment_slot, appointment_type):
                    status_lbl.config(
                        text="Refused: request ID already belongs to another booking",
                        fg="#b42318",
                    )
                    return
        state["saved"] = True
        status_lbl.config(
            text=f"Appointment booked for {active[0]}  ·  {appointment_id}",
            fg="#1a7f37",
        )

    tk.Button(
        root,
        text="Save appointment",
        font=("DejaVu Sans", 18, "bold"),
        width=20,
        bg=BTN_BG,
        fg="#ffffff",
        activebackground=BTN_BG,
        command=save,
    ).place(x=550, y=570)

    # In-place trial reset (SIGUSR1): clear the form and delete the saved note
    # WITHOUT destroying the window -- so the RDP display never goes black and
    # keyboard-focus continuity is preserved between trials (killing the only
    # window on a WM-less display blanks the FreeRDP client and it does not
    # reliably repaint). A flag is set from the signal handler and applied on
    # the Tk thread by a periodic poll.
    reset_pending = {"v": False}
    reset_sequence = {"v": 0}

    def _ack_reset() -> None:
        """Atomically acknowledge that the full reset reached the Tk thread."""
        os.makedirs(os.path.dirname(RESET_ACK_PATH), exist_ok=True)
        temporary = f"{RESET_ACK_PATH}.tmp"
        with open(temporary, "w", encoding="utf-8") as stream:
            stream.write(f"{reset_sequence['v']}\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, RESET_ACK_PATH)

    def _apply_reset() -> None:
        state["active"] = None
        state["saved"] = False
        for field in (slot, visit_type, request_id):
            field.delete(0, tk.END)
        active_lbl.config(text="Active record: (none)")
        status_lbl.config(text="", fg="#1a7f37")
        for b in row_btns:
            b.config(relief="raised", bd=2)
        _clear_appointments()
        root.focus_force()
        reset_sequence["v"] += 1
        _ack_reset()

    def _poll_reset() -> None:
        if reset_pending["v"]:
            reset_pending["v"] = False
            _apply_reset()
        root.after(200, _poll_reset)

    signal.signal(signal.SIGUSR1, lambda *_: reset_pending.__setitem__("v", True))
    _ack_reset()
    root.after(200, _poll_reset)

    root.mainloop()


if __name__ == "__main__":
    main()
