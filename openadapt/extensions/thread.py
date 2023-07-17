"""
    This module contains a custom Thread class that allows for the return value of a thread to be accessed.
"""

from threading import Thread

from loguru import logger


class Thread(Thread):
    def __init__(self, target, args):
        super().__init__(target=target, args=args)
        self._return = None

    def run(self):
        try:
            self._return = self._target(*self._args)
        except Exception as e:
            logger.error(e)
            self._return = False

    def join(self):
        try:
            super().join()
        except RuntimeError:  # thread runs too quickly to join
            pass
        return self._return
