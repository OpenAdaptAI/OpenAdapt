"""
openadapt.app.objects.console module.

This module provides the Console class for redirecting stderr to a NiceGUI log.

Example usage:
    logger = Console()
    logger.write("Error message")
"""

import sys

from nicegui import ui


class Console(object):
    """
    Console class for redirecting stderr to a NiceGUI log.
    """

    def __init__(self):
        """
        Initialize the Console object.
        """
        self.log = ui.log().classes("w-full h-20")
        self.old_stderr = sys.stderr
        sys.stderr = self

    def write(self, data):
        """
        Write data to the log.

        Args:
            data (str): Data to be written.
        """
        self.log.push(data[:-1])
        self.log.update()

    def flush(self):
        """
        Flush the log.
        """
        self.log.update()

    def reset(self):
        """
        Reset the log and restore stderr.
        """
        self.log.clear()
        sys.stderr = self.old_stderr
