"""openadapt.deprecated.app.cards module.

This module provides functions for managing UI cards in the OpenAdapt application.
"""

from datetime import datetime
import multiprocessing
import time

from openadapt.record import record
from openadapt.utils import WrapStdout


class RecordProc:
    """Class to manage the recording process."""

    def __init__(self) -> None:
        """Initialize the RecordProc class."""
        self.terminate_processing = multiprocessing.Event()
        self.terminate_recording = multiprocessing.Event()
        self.record_proc: multiprocessing.Process = None
        self.has_initiated_stop = False

    def set_terminate_processing(self) -> multiprocessing.Event:
        """Set the terminate event."""
        return self.terminate_processing.set()

    def terminate(self) -> None:
        """Terminate the recording process."""
        self.record_proc.terminate()

    def reset(self) -> None:
        """Reset the recording process."""
        self.terminate_processing.clear()
        self.terminate_recording.clear()
        self.record_proc = None
        record_proc.has_initiated_stop = False

    def wait(self) -> None:
        """Wait for the recording process to finish."""
        while True:
            if self.terminate_recording.is_set():
                self.record_proc.terminate()
                return
            time.sleep(0.1)

    def is_running(self) -> bool:
        """Check if the recording process is running."""
        if self.record_proc is not None and not self.record_proc.is_alive():
            self.reset()
        return self.record_proc is not None

    def start(self, func: callable, args: tuple, kwargs: dict) -> None:
        """Start the recording process."""
        self.record_proc = multiprocessing.Process(
            target=WrapStdout(func),
            args=args,
            kwargs=kwargs,
        )
        self.record_proc.start()


record_proc = RecordProc()


def stop_record() -> None:
    """Stop the current recording session."""
    global record_proc
    if record_proc.is_running() and not record_proc.has_initiated_stop:
        record_proc.set_terminate_processing()

        # wait for process to terminate
        record_proc.wait()
        record_proc.reset()


def is_recording() -> bool:
    """Check if a recording session is currently active."""
    global record_proc
    return record_proc.is_running()


def quick_record(
    task_description: str | None = None,
    status_pipe: multiprocessing.connection.Connection | None = None,
) -> None:
    """Run a recording session."""
    global record_proc
    task_description = task_description or datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    record_proc.start(
        record,
        (
            task_description,
            record_proc.terminate_processing,
            record_proc.terminate_recording,
            status_pipe,
        ),
        {
            "log_memory": False,
        },
    )
