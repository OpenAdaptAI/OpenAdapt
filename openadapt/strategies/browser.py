"""Implements a replay strategy for browser recordings."""

import json
import queue
import threading
import websockets.sync.server

from openadapt import models, strategies, utils
from openadapt.browser import set_browser_mode
from openadapt.config import config
from openadapt.custom_logger import logger
from openadapt.record import Event

ws_server_instance = None


def read_browser_events(
    websocket: websockets.sync.server.ServerConnection,
    event_q: queue.Queue,
    terminate_processing: threading.Event,
) -> None:
    """Read browser events and add them to the event queue.

    Params:
        websocket: The websocket object.
        event_q: A queue for adding browser events.
        terminate_processing: An event to signal the termination of the process.

    Returns:
        None
    """
    set_browser_mode("replay", websocket)
    utils.set_start_time()

    logger.info("Starting Reading Browser Events ...")

    try:
        while not terminate_processing.is_set():
            try:
                message = websocket.recv(0.01)
            except TimeoutError:
                continue
            timestamp = utils.get_timestamp()
            logger.info(f"{len(message)=}")
            data = json.loads(message)
            assert data["type"] == "DOM_EVENT", data.type
            event_q.put(
                # TODO: create BrowserEvent?
                Event(
                    timestamp,
                    "browser",
                    {"message": data},
                )
            )
    finally:
        # XXX this is never called when quitting via ctrl+c
        set_browser_mode("idle", websocket)


def run_browser_event_server(
    event_q: queue.Queue,
    terminate_processing: threading.Event,
) -> None:
    """Run the browser event server.

    Params:
        event_q: A queue for adding browser events.
        terminate_processing: An event to signal the termination of the process.

    Returns:
        None
    """
    global ws_server_instance

    # Function to run the server in a separate thread
    def run_server() -> None:
        global ws_server_instance
        with websockets.sync.server.serve(
            lambda ws: read_browser_events(
                ws,
                event_q,
                terminate_processing,
            ),
            config.BROWSER_WEBSOCKET_SERVER_IP,
            config.BROWSER_WEBSOCKET_PORT,
            max_size=config.BROWSER_WEBSOCKET_MAX_SIZE,
        ) as server:
            ws_server_instance = server
            logger.info("WebSocket server started")
            server.serve_forever()

    # Start the server in a separate thread
    server_thread = threading.Thread(target=run_server)
    server_thread.start()

    # Wait for a termination signal
    terminate_processing.wait()
    logger.info("Termination signal received, shutting down server")

    if ws_server_instance:
        ws_server_instance.shutdown()

    # Ensure the server thread is terminated cleanly
    server_thread.join()


class BrowserReplayStrategy(strategies.base.BaseReplayStrategy):
    """Browser playback strategy."""

    def __init__(
        self,
        recording: models.Recording,
    ) -> None:
        """Initialize the BrowserReplayStrategy.

        Args:
            recording (models.Recording): The recording object.
        """
        super().__init__(recording)
        self.event_q = queue.Queue()
        self.terminate_processing = threading.Event()
        self.recent_visible_dom = ""
        self.browser_event_reader = threading.Thread(
            target=run_browser_event_server,
            args=(self.event_q, self.terminate_processing),
        )
        self.browser_event_reader.start()

    def get_most_recent_dom(self) -> str:
        """Get the most recent visible DOM from the event queue.

        Returns:
            str: The most recent visible DOM.
        """
        num_messages_read = 0
        while not self.event_q.empty():
            event = self.event_q.get()
            num_messages_read += 1
            self.recent_visible_dom = event.data["message"]["visibleHTMLString"]

        if num_messages_read:
            logger.info(f"{num_messages_read=} {len(self.recent_visible_dom)=}")
        return self.recent_visible_dom

    def get_next_action_event(
        self,
        screenshot: models.Screenshot,
        window_event: models.WindowEvent,
    ) -> models.ActionEvent | None:
        """Get the next ActionEvent for replay.

        Args:
            screenshot (models.Screenshot): The screenshot object.
            window_event (models.WindowEvent): The window event object.

        Returns:
            models.ActionEvent or None: The next ActionEvent for replay or None
              if there are no more events.
        """
        # Get the most recent DOM
        most_recent_dom = self.get_most_recent_dom()
        logger.info(f"{len(most_recent_dom)=}")
        # TODO XXX
        return None
