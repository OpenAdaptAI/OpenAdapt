"""openadapt.app.objects.console module.

This module provides the Console class for redirecting stderr to a NiceGUI log.

Example usage:
    logger = Console()
    logger.write("Error message")
"""

import sys

from nicegui import ui


class Console(object):
    """Console class for redirecting stderr to a NiceGUI log."""

    def __init__(self) -> None:
        """Initialize the Console object."""
        self.log = ui.log().classes("w-full h-20")
        self.old_stderr = sys.stderr
        self.buffer = ""

    def write(self, data: str) -> None:
        """Write data to the log.

        Args:
            data (str): Data to be written.
        """
        self.log.push(data[:-1])
        self.log.update()
        self.old_stderr.write(data)

    def flush(self) -> None:
        """Flush the log."""
        self.log.update()
        self.old_stderr.flush()

    def reset(self) -> None:
        """Reset the log and restore stderr."""
        self.log.clear()
        sys.stderr = self.old_stderr
