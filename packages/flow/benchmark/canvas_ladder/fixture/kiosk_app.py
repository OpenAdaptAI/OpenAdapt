#!/usr/bin/env python3
"""Deterministic Tk kiosk app for the real-RDP vision-ladder e2e fixture.

Runs as the entire RDP session (no desktop chrome) so the framebuffer is a
fixed, reproducible pixel surface for template/OCR resolution. No animation, a
solid (non-blinking) caret, and fixed DejaVu fonts keep rendering identical
run-to-run. Saving a note writes it to SAVE_PATH so an out-of-band
DocumentHashVerifier oracle can confirm the effect with no app API.
"""
import os
import signal
import tkinter as tk

SAVE_PATH = os.environ.get("RDP_FIXTURE_SAVE_PATH", "/opt/rdp_fixture/saved_note.txt")
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


def main() -> None:
    root = tk.Tk()
    root.title("OpenAdapt Clinic Fixture")
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

    tk.Label(root, text="OpenAdapt Clinic Fixture  -  Patient Notes",
             font=("DejaVu Sans", 26, "bold"), bg=BG, fg=FG).place(x=60, y=40)

    tk.Label(root, text="Roster", font=("DejaVu Sans", 16, "bold"),
             bg=BG, fg=FG).place(x=60, y=120)

    active_lbl = tk.Label(root, text="Active: (none)", font=("DejaVu Sans", 16),
                          bg=BG, fg=FG)
    active_lbl.place(x=60, y=470)

    status_lbl = tk.Label(root, text="", font=("DejaVu Sans", 16, "bold"),
                          bg=BG, fg="#1a7f37")
    status_lbl.place(x=60, y=690)

    def select(name, mrn, btn):
        state["active"] = (name, mrn)
        active_lbl.config(text=f"Active: {name}  {mrn}")
        for b in row_btns:
            b.config(relief="raised", bd=2)
        btn.config(relief="sunken", bd=4)

    row_btns = []
    for i, (name, mrn) in enumerate(PATIENTS):
        b = tk.Button(root, text=f"{name}    {mrn}",
                      font=("DejaVu Sans", 18), width=34, anchor="w",
                      bg=ROW_BG, fg=FG, activebackground=ROW_BG,
                      relief="raised", bd=2)
        b.config(command=lambda n=name, m=mrn, bb=b: select(n, m, bb))
        b.place(x=60, y=170 + i * 70)
        row_btns.append(b)

    tk.Label(root, text="Clinical note", font=("DejaVu Sans", 16, "bold"),
             bg=BG, fg=FG).place(x=60, y=530)
    note = tk.Entry(root, font=("DejaVu Sans", 18), width=44,
                    bg=ROW_BG, fg=FG, insertbackground=FG, insertofftime=0)
    note.place(x=60, y=570)
    # Clicking the field claims both the toplevel X focus and the widget focus,
    # so RDP-forwarded keystrokes land here even with no window manager.
    note.bind("<Button-1>", lambda e: (root.focus_force(), note.focus_set()))

    def save():
        active = state["active"]
        text = note.get()
        if not active or not text:
            status_lbl.config(text="Refused: select a patient and enter a note",
                              fg="#b42318")
            return
        os.makedirs(os.path.dirname(SAVE_PATH), exist_ok=True)
        with open(SAVE_PATH, "w") as f:
            f.write(f"{active[1]}\t{text}\n")
        state["saved"] = True
        status_lbl.config(text=f"Saved note for {active[0]}", fg="#1a7f37")

    tk.Button(root, text="Save Note", font=("DejaVu Sans", 18, "bold"),
              width=16, bg=BTN_BG, fg="#ffffff", activebackground=BTN_BG,
              command=save).place(x=760, y=566)

    # In-place trial reset (SIGUSR1): clear the form and delete the saved note
    # WITHOUT destroying the window -- so the RDP display never goes black and
    # keyboard-focus continuity is preserved between trials (killing the only
    # window on a WM-less display blanks the FreeRDP client and it does not
    # reliably repaint). A flag is set from the signal handler and applied on
    # the Tk thread by a periodic poll.
    reset_pending = {"v": False}

    def _apply_reset() -> None:
        state["active"] = None
        state["saved"] = False
        note.delete(0, tk.END)
        active_lbl.config(text="Active: (none)")
        status_lbl.config(text="", fg="#1a7f37")
        for b in row_btns:
            b.config(relief="raised", bd=2)
        try:
            os.remove(SAVE_PATH)
        except OSError:
            pass
        root.focus_force()

    def _poll_reset() -> None:
        if reset_pending["v"]:
            reset_pending["v"] = False
            _apply_reset()
        root.after(200, _poll_reset)

    signal.signal(signal.SIGUSR1, lambda *_: reset_pending.__setitem__("v", True))
    root.after(200, _poll_reset)

    root.mainloop()


if __name__ == "__main__":
    main()
