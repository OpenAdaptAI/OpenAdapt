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
from openadapt.strategies.vanilla import describe_recording

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


def add_browser_elements(action_events: list[models.ActionEvent]) -> None:
    """Set the ActionEvent.active_segment_description where appropriate.

    Args:
        action_events: list of ActionEvents to modify in-place.
    """
    action_browser_tups = [
        (action, action.browser_event)
        for action in action_events
        if action.browser_event
    ]
    for action, browser in action_browser_tups:
        soup, target_element = browser.parse()
        if not target_element:
            logger.warning(f"{target_element=}")
            continue
        action.active_browser_element = target_element
        action.available_browser_elements = soup
        # TODO: move this to models.BrowserEvent
        assert len(str(action.available_browser_elements)) == len(str(soup)), (
            len(str(action.available_browser_elements)), len(str(soup))
        )


class BrowserReplayStrategy(strategies.base.BaseReplayStrategy):
    """Browser playback strategy."""

    def __init__(
        self,
        recording: models.Recording,
        # copied from vanilla.py
        # TODO: refactor into vanilla.py
        instructions: str = "",
        process_events: bool = PROCESS_EVENTS,
    ) -> None:
        """Initialize the BrowserReplayStrategy.

        Args:
            recording (models.Recording): The recording object.
            instructions (str): Natural language instructions
                for how recording should be replayed.
            process_events (bool): Flag indicating whether to process the events.
              Defaults to True.
        """
        super().__init__(
            recording,
            include_a11y_data=False,
        )
        self.event_q = queue.Queue()
        self.terminate_processing = threading.Event()
        self.recent_visible_html = ""
        add_browser_elements(recording.processed_action_events)
        self.browser_event_reader = threading.Thread(
            target=run_browser_event_server,
            args=(self.event_q, self.terminate_processing),
        )
        self.browser_event_reader.start()

        self.instructions = instructions
        self.process_events = process_events
        self.action_history = []
        self.action_event_idx = 0

        self.recording_description = describe_recording(
            self.recording,
            self.process_events,
        )


    def get_recent_visible_html(self) -> str:
        """Get the most recent visible DOM from the event queue.

        Returns:
            str: The most recent visible DOM.
        """
        num_messages_read = 0
        while not self.event_q.empty():
            event = self.event_q.get()
            num_messages_read += 1
            self.recent_visible_html = event.data["message"]["visibleHTMLString"]

        if num_messages_read:
            logger.info(f"{num_messages_read=} {len(self.recent_visible_html)=}")
        return self.recent_visible_html

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
        recent_visible_html = self.get_recent_visible_html()
        #logger.info(f"{len(recent_visible_html)=}")

        # copied from vanilla
        if self.process_events:
            action_events = self.recording.processed_action_events
        else:
            action_events = self.recording.action_events
        self.action_event_idx += 1
        num_action_events = len(action_events)
        if self.action_event_idx >= num_action_events:
            raise StopIteration()
        logger.debug(f"{self.action_event_idx=} of {num_action_events=}")

        action_event = generate_action_event(
            screenshot,
            window_event,
            action_events,
            self.action_history,
            self.instructions,
            # different from vanilla.py
            recent_visible_html,
        )
        if not action_event:
            raise StopIteration()

        self.action_history.append(action_event)
        return action_event


from bs4 import BeautifulSoup
#import html2text

def get_html_prompt(html: str, convert_to_markdown: bool = False) -> str:
    """Convert an HTML string to a processed version suitable for LLM prompts.

    Args:
        html: The input HTML string.
        convert_to_markdown: If True, converts the HTML to Markdown. Defaults to False.

    Returns:
        A string with preserved semantic structure and interactable elements.
        If convert_to_markdown is True, the string is in Markdown format.
    """
    # Parse HTML with BeautifulSoup
    soup = BeautifulSoup(html, 'html.parser')

    # Remove non-interactive and unnecessary elements
    for tag in soup(['style', 'script', 'noscript', 'meta', 'head', 'iframe']):
        tag.decompose()

    assert not convert_to_markdown, "poetry add html2text"
    if convert_to_markdown:
        # Initialize html2text converter
        converter = html2text.HTML2Text()
        converter.ignore_links = False  # Keep all links
        converter.ignore_images = False  # Keep all images
        converter.body_width = 0  # Preserve original width without wrapping
        
        # Convert the cleaned HTML to Markdown
        markdown = converter.handle(str(soup))
        return markdown
    
    # Return processed HTML as a string if Markdown conversion is not required
    return str(soup)


def generate_action_event(
    current_screenshot: models.Screenshot,
    current_window_event: models.WindowEvent,
    recorded_actions: list[models.ActionEvent],
    replayed_actions: list[models.ActionEvent],
    instructions: str,
    # different from vanilla.py
    recent_visible_html: str,
) -> models.ActionEvent:
    """Modify the given ActionEvents according to the given replay instructions.

    Given the description of what happened, proposed modifications in natural language
    instructions, the current state, and the actions produced so far, produce the next
    action.

    Args:
        current_screenshot (models.Screenshot): current state screenshot
        current_window_event (models.WindowEvent): current state window data
        recorded_actions (list[models.ActionEvent]): list of action events from the
            recording
        replayed_actions (list[models.ActionEvent]): list of actions produced during
            current replay
        instructions (str): proposed modifications in natural language
            instructions

    Returns:
        (models.ActionEvent) the next action event to be played, produced by the model
    """

    recent_visible_html_prompt = get_html_prompt(recent_visible_html)

    current_image = current_screenshot.image
    current_window_dict = current_window_event.to_prompt_dict()
    recorded_action_dicts = [action.to_prompt_dict() for action in recorded_actions]
    replayed_action_dicts = [action.to_prompt_dict() for action in replayed_actions]

    system_prompt = utils.render_template_from_file(
        "prompts/system.j2",
    )
    prompt = utils.render_template_from_file(
        "prompts/generate_action_event.j2",
        current_window=current_window_dict,
        recorded_actions=recorded_action_dicts,
        replayed_actions=replayed_action_dicts,
        replay_instructions=instructions,



        # XXX TODO: incorporate this
        recent_visible_html=recent_visible_html_prompt,



    )
    prompt_adapter = adapters.get_default_prompt_adapter()
    content = prompt_adapter.prompt(
        prompt,
        system_prompt,
        [current_image],
    )
    action_dict = utils.parse_code_snippet(content)
    logger.info(f"{action_dict=}")
    if not action_dict:
        # allow early stopping
        return None
    action = models.ActionEvent.from_dict(action_dict)
    logger.info(f"{action=}")
    return action
