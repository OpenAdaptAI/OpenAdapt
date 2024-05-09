"""Custom Thread class."""
# TODO: remove

from threading import Thread

from loguru import logger


class Thread(Thread):
    """Thread class that allows for the return value of a thread to be accessed."""

    def __init__(self, daemon: bool, target: callable, args: tuple | list) -> None:
        """Initialize the Thread class."""
        super().__init__(target=target, daemon=daemon, args=args)
        self._return = None

    def run(self) -> None:
        """Run the thread."""
        try:
            self._return = self._target(*self._args)
        except Exception as e:
            logger.error(e)
            self._return = False

    def join(self) -> bool:
        """Join the thread.

        Returns:
            bool: The return value of the thread.
        """
        try:
            super().join()
        except RuntimeError:  # thread runs too quickly to join
            pass
        return self._return
