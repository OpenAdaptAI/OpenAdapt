"""Scrubbing module for OpenAdapt."""


from queue import Queue
from threading import Thread
import multiprocessing
import os
import time

from loguru import logger
import sqlalchemy as sa

from openadapt.db import crud
from openadapt.models import ActionEvent, Recording, Screenshot, WindowEvent
from openadapt.privacy.providers import ScrubProvider


class ScrubbingProc:
    """Process for scrubbing recordings."""

    def __init__(self) -> None:
        """Initialize the ScrubbingProc class."""
        self.reset()

    def reset(self) -> None:
        """Reset the process."""
        self._threads = []
        self._additional_data = {}
        self._on_complete = None
        self.copying_recording = False

    def add_thread(self, thread: Thread) -> None:
        """Add a thread to the process.

        Args:
            thread (Thread): The thread to add.
        """
        self._threads.append(thread)

    def add_additional_data(self, data: dict) -> None:
        """Add additional data to the process.

        Args:
            data (dict): The data to add.
        """
        self._additional_data.update(data)

    def set_on_complete(self, on_complete: callable, *args: any, **kwargs: any) -> None:
        """Set the on complete callback.

        Args:
            on_complete (callable): The callback to set.
        """
        self._on_complete = (on_complete, args, kwargs)

    def fetch_updated_data(self) -> dict[str, any]:
        """Fetch updated data from the process."""
        data = self._additional_data
        updated_data = {
            "recording": {
                "id": data["recording"].id,
                "task_description": data["recording"].task_description,
            },
            "provider": ScrubProvider.as_options()[data["provider_id"]],
            "copying_recording": self.copying_recording,
        }
        if "error" in data:
            updated_data.update({"error": data["error"]})
        if "num_action_events_scrubbed" in data:
            updated_data.update(
                {
                    "num_action_events_scrubbed": data[
                        "num_action_events_scrubbed"
                    ].value,
                    "num_screenshots_scrubbed": data["num_screenshots_scrubbed"].value,
                    "num_window_events_scrubbed": data[
                        "num_window_events_scrubbed"
                    ].value,
                    "total_action_events": data["total_action_events"],
                    "total_screenshots": data["total_screenshots"],
                    "total_window_events": data["total_window_events"],
                }
            )
        return updated_data

    def start(self) -> None:
        """Start the process."""
        scrubbing_proc.copying_recording = False
        existing_threads = [thread for thread in self._threads]

        def closing_thread() -> None:
            """Close the process."""
            for thread in existing_threads:
                thread.join()
            if self._on_complete is not None:
                on_complete, args, kwargs = self._on_complete
                on_complete(*args, **kwargs)

        closing_thread = Thread(target=closing_thread, daemon=True)
        self.add_thread(closing_thread)

        for thread in self._threads:
            thread.start()

    def is_running(self) -> bool:
        """Check if the process is running."""
        return self.copying_recording or any(
            thread.is_alive() for thread in self._threads
        )


scrubbing_proc = ScrubbingProc()


def scrub(recording_id: int, provider_id: str, release_lock: bool = False) -> None:
    """Scrub a recording.

    Args:
        recording_id (int): The recording id to scrub.
        provider_id (str): The provider id to use for scrubbing.
        release_lock (bool, optional): Whether to release the db write lock.
        Defaults to False.
    """
    scrubbing_proc.reset()

    scrubbing_proc.copying_recording = True
    session = crud.get_new_session(read_only=True)
    recording = crud.get_recording_by_id(session, recording_id)
    session.close()
    scrubbing_proc.add_additional_data(
        {"recording": recording, "provider_id": provider_id}
    )

    def cleanup(error: str) -> None:
        """Cleanup function for scrubbing.

        Args:
            error (str): The error message.
        """
        scrubbing_proc.add_additional_data({"error": error})
        scrubbing_proc.copying_recording = False
        return

    def inner() -> None:
        """Inner function to scrub a recording."""
        if not crud.acquire_db_lock():
            cleanup("Failed to acquire db lock.")
            return
        write_session = crud.get_new_session(read_and_write=True)

        new_recording_id = crud.copy_recording(write_session, recording_id)
        if new_recording_id is None:
            if release_lock:
                crud.release_db_lock()
            cleanup("Failed to copy recording.")
            return

        logger.info(f"Scrubbing recording with id {new_recording_id}...")

        scrubbed_recording_id = crud.insert_scrubbed_recording(
            write_session, new_recording_id, provider_id
        )

        new_recording = crud.get_recording_by_id(write_session, new_recording_id)

        scrubber = ScrubProvider.get_scrubber(provider_id)

        crud.scrub_item(new_recording_id, Recording, scrubber)

        def scrub_item(item_id: int, table: sa.Table) -> None:
            """Scrub an item.

            Args:
                item_id (int): The item id to scrub.
                table (sa.Table): The table to scrub.
            """
            crud.scrub_item(item_id, table, scrubber)

        num_workers = 5

        action_event_q = Queue()
        for action_event in new_recording.action_events:
            if action_event.parent_id is not None:
                continue
            action_event_q.put(action_event.id)

        def scrub_action_events(action_event_id: int) -> None:
            """Scrub an action event.

            Args:
                action_event_id (int): The action event id to scrub.
            """
            scrub_item(action_event_id, ActionEvent)

        screenshot_q = Queue()
        for screenshot in new_recording.screenshots:
            screenshot_q.put(screenshot.id)

        def scrub_screenshots(screenshot_id: int) -> None:
            """Scrub a screenshot.

            Args:
                screenshot_id (int): The screenshot id to scrub.
            """
            scrub_item(screenshot_id, Screenshot)

        window_event_q = Queue()
        for window_event in new_recording.window_events:
            window_event_q.put(window_event.id)

        def scrub_window_events(window_event_id: int) -> None:
            """Scrub a window event.

            Args:
                window_event_id (int): The window event id to scrub.
            """
            scrub_item(window_event_id, WindowEvent)

        num_action_events_scrubbed = multiprocessing.Value("i", 0)
        num_screenshots_scrubbed = multiprocessing.Value("i", 0)
        num_window_events_scrubbed = multiprocessing.Value("i", 0)
        total_action_events = action_event_q.qsize()
        total_screenshots = screenshot_q.qsize()
        total_window_events = window_event_q.qsize()

        scrubbing_proc.add_additional_data(
            {
                "num_action_events_scrubbed": num_action_events_scrubbed,
                "num_screenshots_scrubbed": num_screenshots_scrubbed,
                "num_window_events_scrubbed": num_window_events_scrubbed,
                "total_action_events": total_action_events,
                "total_screenshots": total_screenshots,
                "total_window_events": total_window_events,
            }
        )

        scrubbing_proc.add_thread(
            Thread(
                target=start_workers,
                args=(
                    "scrub-action-events",
                    scrub_action_events,
                    action_event_q,
                    num_action_events_scrubbed,
                    min(num_workers, total_action_events),
                ),
            )
        )
        scrubbing_proc.add_thread(
            Thread(
                target=start_workers,
                args=(
                    "scrub-screenshots",
                    scrub_screenshots,
                    screenshot_q,
                    num_screenshots_scrubbed,
                    min(num_workers, total_screenshots),
                ),
            )
        )
        scrubbing_proc.add_thread(
            Thread(
                target=start_workers,
                args=(
                    "scrub-window-events",
                    scrub_window_events,
                    window_event_q,
                    num_window_events_scrubbed,
                    min(num_workers, total_window_events),
                ),
            )
        )

        def on_complete_scrubbing() -> None:
            """Callback for when scrubbing is complete."""
            logger.info(f"Finished scrubbing recording with id {new_recording_id}.")
            crud.mark_scrubbing_complete(write_session, scrubbed_recording_id)
            if release_lock:
                crud.release_db_lock()

        scrubbing_proc.set_on_complete(on_complete_scrubbing)

        scrubbing_proc.start()
        return

    Thread(target=inner, daemon=True).start()


def start_workers(
    name: str,
    target: callable,
    q: Queue,
    num_items_processed: multiprocessing.Value,
    num_workers: int = os.cpu_count(),  # number of CPUs
) -> None:
    """Start a pool of workers to run a target function.

    Args:
        name: The name of the worker pool.
        target (function): The function to run.
        q (Queue): The queue of items to process.
        num_items_processed (multiprocessing.Value): The number of items processed.
        num_workers (int, optional): The number of workers to start.
    """

    def thread_target(thread_name: str) -> None:
        """Target function for a worker thread.

        Args:
            thread_name (str): The name of the thread.
        """
        logger.info(f"Starting worker thread {thread_name}.")
        while True:
            if q.empty():
                break
            item = q.get()
            start = time.time()
            target(item)
            elapsed = time.time() - start
            logger.info(f"Processed item {item} in {name} in {elapsed:.2f} seconds.")
            num_items_processed.value += 1
            q.task_done()
        logger.info(f"Closing worker thread {thread_name}.")

    logger.info(f"Starting {num_workers} worker threads for {name}...")
    threads = []
    start = time.time()
    for i in range(num_workers):
        thread_name = f"{name}-{i}"
        thread = Thread(
            target=thread_target, args=(thread_name,), daemon=True, name=thread_name
        )
        thread.start()
        threads.append(thread)
    q.join()
    for thread in threads:
        thread.join()
    logger.info(f"Finished processing {name} in {time.time() - start:.2f} seconds.")


def get_scrubbing_process() -> ScrubbingProc:
    """Get the scrubbing process.

    Returns:
        ScrubbingProc: The scrubbing process.
    """
    return scrubbing_proc
