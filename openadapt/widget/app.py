import tkinter as tk
import signal
from subprocess import Popen
from openadapt.record import record
import os

# Initialize the current state
current_state = "default"

PROC = None

root = None
def handle_click():
    global current_state
    global button
    global PROC
    global root

    if current_state == "default":
        current_state = "recording_in_progress"
        button["image"] = states[current_state]
        button["command"] = stop_recording
        start_recording()

    elif current_state == "recording_in_progress":
        current_state = "replay_available"
        button["image"] = states[current_state]
        button["command"] = handle_replay

    elif current_state == "replay_available":
        current_state = "replaying_in_progress"
        button["image"] = states[current_state]
        button["command"] = None

    elif current_state == "replaying_in_progress":
        current_state = "replaying_paused"
        button["image"] = states[current_state]
        button["command"] = None

def start_recording():
     global PROC
     PROC = Popen(
            "python -m openadapt.record " + "test",
            shell=True,
        )
    

def stop_recording():
    global PROC
    PROC.send_signal(signal.SIGINT)

    # Wait for the process to terminate
    PROC.wait()
    PROC = None

def handle_exit():
    global PROC
    if PROC:
        stop_recording()
    root.destroy()

def handle_replay() :
    pass
def run_widget():
    global button
    global states
    root = tk.Tk()
    root.title("OpenAdapt")
    root.geometry("300x300")
    root.wm_attributes("-topmost", True) # make the window always on top
    root.wm_attributes("-transparentcolor", root["bg"])
    root.overrideredirect(True)

    # Define the states
    states = {
        "default": tk.PhotoImage(file="./widget/assets/logo.png"),
        "recording_in_progress": tk.PhotoImage(file="./widget/assets/noun-recording.png"),
        "replay_available": tk.PhotoImage(file="./widget/assets/noun-replay-available.png"),
        "replaying_in_progress": tk.PhotoImage(file="./widget/assets/noun-replaying-inprogress.png"),
        "replaying_paused": tk.PhotoImage(file="./widget/assets/noun-pause.png")
    }

    # Create the button
    button = tk.Button(
        root,
        image=states[current_state],
        text=states[current_state],
        command=handle_click
    )
    button.pack(ipadx=5, ipady=5, expand=True)

    root.protocol("WM_DELETE_WINDOW", handle_exit)
    root.mainloop()

if __name__ == "__main__":
    run_widget()
