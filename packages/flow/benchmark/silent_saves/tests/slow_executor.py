"""A Python executor for the tests that outlives the item timeout."""

import time

from silent_saves.executors import Report, Task


class Slow:
    name = "slow"

    def run(self, task: Task) -> Report:
        time.sleep(1.5)
        return Report(True, final_text="took too long")
