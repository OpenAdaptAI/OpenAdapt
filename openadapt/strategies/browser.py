"""
TODO:
- re-use approach from visual.py: segment each screenshot, prompt for descriptions
"""

from pprint import pformat
from threading import Event, Thread
import json
import queue

from bs4 import BeautifulSoup
from websockets.sync.server import ServerConnection

from openadapt import adapters, config, models, utils, strategies
from openadapt.custom_logger import logger

# Define ws_server_instance at the top scope
ws_server_instance = None


def read_browser_events(
    websocket: ServerConnection,
    event_q: queue.Queue,
    terminate_processing: Event,
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
            assert data["type"] == "DOM_EVENT", data["type"]
            event_q.put(
                models.BrowserEvent(
                    timestamp=timestamp,
                    message=data,
                )
            )
    finally:
        set_browser_mode("idle", websocket)


def run_browser_event_server(
    event_q: queue.Queue,
    terminate_processing: Event,
) -> None:
    """Run the browser event server.

    Params:
        event_q: A queue for adding browser events.
        terminate_processing: An event to signal the termination of the process.

    Returns:
        None
    """
    global ws_server_instance

    def run_server() -> None:
        global ws_server_instance
        with ServerConnection(
            lambda ws: read_browser_events(ws, event_q, terminate_processing),
            config.BROWSER_WEBSOCKET_SERVER_IP,
            config.BROWSER_WEBSOCKET_PORT,
            max_size=config.BROWSER_WEBSOCKET_MAX_SIZE,
        ) as server:
            ws_server_instance = server
            logger.info("WebSocket server started")
            server.serve_forever()

    server_thread = Thread(target=run_server)
    server_thread.start()
    terminate_processing.wait()
    logger.info("Termination signal received, shutting down server")

    if ws_server_instance:
        ws_server_instance.shutdown()

    server_thread.join()


def add_browser_elements(
    action_events: list[models.ActionEvent],
    filter_html: bool = False,
) -> None:
    """Set the ActionEvent.active_browser_element where appropriate and log actions.

    Args:
        action_events: list of ActionEvents to modify in-place.
        filter_html: whether to try to remove unnecessary elements
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

        if filter_html:
            # Convert BeautifulSoup object to cleaned HTML strings
            action.active_browser_element = clean_html_attributes(target_element)
            action.available_browser_elements = filter_and_clean_html(soup)

        # Verify the cleaned elements
        # (TODO: move this to setters)
        assert action.active_browser_element, action.active_browser_element
        assert action.available_browser_elements, action.available_browser_elements


class BrowserReplayStrategy(strategies.BaseReplayStrategy):
    """ReplayStrategy using HTML and replay instructions."""

    def __init__(
        self,
        recording: models.Recording,
        instructions: str,
    ) -> None:
        """Initialize the BrowserReplayStrategy.

        Args:
            recording (models.Recording): The recording object.
            instructions (str): Natural language instructions for how recording
                should be replayed.
        """
        super().__init__(recording, include_a11y_data=False)
        self.event_q = queue.Queue()
        self.terminate_processing = Event()
        self.recent_visible_html = ""
        add_browser_elements(recording.processed_action_events)
        self.browser_event_reader = Thread(
            target=run_browser_event_server,
            args=(self.event_q, self.terminate_processing),
        )
        self.browser_event_reader.start()

        self.instructions = instructions
        self.action_history = []
        self.modified_actions = self.apply_replay_instructions(
            recording.processed_action_events, instructions
        )
        self.action_event_idx = 0

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
        # First, try the direct approach based on planned sequence.
        try:
            action = self._execute_planned_action(
                screenshot=screenshot, current_window_event=window_event
            )
            if action:
                return action
        except Exception as e:
            logger.warning(f"Direct generation approach failed: {e}")

        # Fallback to the planning approach if the direct approach fails.
        try:
            action = self._generate_next_action_plan(
                screenshot=screenshot,
                window_event=window_event,
                recorded_actions=self.recording.processed_action_events,
                replayed_actions=self.action_history,
                instructions=self.instructions,
            )
            return action
        except Exception as e:
            logger.error(f"Planning approach also failed: {e}")
            return None

    def _execute_planned_action(
        self,
        screenshot: models.Screenshot,
        current_window_event: models.WindowEvent,
    ) -> models.ActionEvent | None:
        """Try to execute the next planned action assuming it matches reality.

        Args:
            screenshot (models.Screenshot): The current state screenshot.
            current_window_event (models.WindowEvent): The current state window data.

        Returns:
            models.ActionEvent or None: The next action event if the planned target exists.
        """
        if self.action_event_idx >= len(self.modified_actions):
            return None  # No more actions to replay.

        planned_action = self.modified_actions[self.action_event_idx]
        self.action_event_idx += 1

        # Find target element in the current DOM.
        recent_visible_html = self.get_recent_visible_html()
        soup, target_element = self._find_element_in_dom(
            planned_action, recent_visible_html
        )

        if target_element:
            planned_action.active_browser_element = target_element
            self.action_history.append(planned_action)
            return planned_action
        else:
            raise ValueError("Target element not found in the current DOM.")

    def _generate_next_action_plan(
        self,
        screenshot: models.Screenshot,
        window_event: models.WindowEvent,
        recorded_actions: list[models.ActionEvent],
        replayed_actions: list[models.ActionEvent],
        instructions: str,
    ) -> models.ActionEvent | None:
        """Fallback method to dynamically plan the next action event.

        Args:
            screenshot (models.Screenshot): The current state screenshot.
            window_event (models.WindowEvent): The current state window data.
            recorded_actions (list[models.ActionEvent]): List of action events from the recording.
            replayed_actions (list[models.ActionEvent]): List of actions produced during current replay.
            instructions (str): Proposed modifications in natural language instructions.

        Returns:
            models.ActionEvent or None: The next action event if successful, otherwise None.
        """
        prompt_adapter = adapters.get_default_prompt_adapter()
        system_prompt = utils.render_template_from_file("prompts/system.j2")
        prompt = utils.render_template_from_file(
            "prompts/generate_action_event--browser.j2",  # Updated template file name
            current_window=window_event.to_prompt_dict(),
            recorded_actions=[action.to_prompt_dict() for action in recorded_actions],
            replayed_actions=[action.to_prompt_dict() for action in replayed_actions],
            replay_instructions=instructions,
        )

        content = prompt_adapter.prompt(
            prompt,
            system_prompt=system_prompt,
            images=[screenshot.image],
        )
        action_dict = utils.parse_code_snippet(content)
        logger.info(f"{action_dict=}")
        if not action_dict:
            return None

        return models.ActionEvent.from_dict(action_dict)

    def apply_replay_instructions(
        self,
        action_events: list[models.ActionEvent],
        replay_instructions: str,
    ) -> list[models.ActionEvent]:
        """Modify the given ActionEvents according to the given replay instructions.

        Args:
            action_events: list of action events to be modified in place.
            replay_instructions: instructions for how action events should be modified.

        Returns:
            list[models.ActionEvent]: The modified list of action events.
        """
        action_dicts = [action.to_prompt_dict() for action in action_events]
        actions_dict = {"actions": action_dicts}
        system_prompt = utils.render_template_from_file("prompts/system.j2")
        prompt = utils.render_template_from_file(
            "prompts/apply_replay_instructions--browser.j2",  # Updated template file name
            actions=actions_dict,
            replay_instructions=replay_instructions,
            # TODO: remove
            exceptions=[],
        )
        print(prompt)
        # XXX
        import ipdb

        ipdb.set_trace()
        prompt_adapter = adapters.get_default_prompt_adapter()
        content = prompt_adapter.prompt(prompt, system_prompt=system_prompt)
        content_dict = utils.parse_code_snippet(content)

        try:
            action_dicts = content_dict["actions"]
        except TypeError as exc:
            logger.warning(exc)
            action_dicts = (
                content_dict  # OpenAI sometimes returns a list of dicts directly.
            )

        modified_actions = []
        for action_dict in action_dicts:
            action = models.ActionEvent.from_dict(action_dict)
            modified_actions.append(action)
        return modified_actions

    def _find_element_in_dom(self, planned_action: models.ActionEvent, html: str):
        """Locate the target element in the current HTML DOM.

        Args:
            planned_action (models.ActionEvent): The planned action with target element info.
            html (str): The current HTML content.

        Returns:
            Tuple[BeautifulSoup, Tag or None]: Parsed HTML and the target element or None.
        """
        soup = BeautifulSoup(html, "html.parser")
        target_selector = (
            planned_action.active_browser_element
        )  # Assuming selector or similar identifier is used.
        target_element = soup.select_one(target_selector)  # Simplify finding elements.

        return soup, target_element

    def __del__(self) -> None:
        """Clean up resources and log action history."""
        self.terminate_processing.set()
        action_history_dicts = [
            action.to_prompt_dict() for action in self.action_history
        ]
        logger.info(f"action_history=\n{pformat(action_history_dicts)}")


# Define a whitelist of essential attributes
WHITELIST_ATTRIBUTES = [
    "id",
    "class",
    "href",
    "src",
    "alt",
    "name",
    "type",
    "value",
    "title",
    "data-*",
    "aria-*",
]


def clean_html_attributes(element: BeautifulSoup) -> str:
    """Retain only essential attributes from an HTML element based on a whitelist.

    Args:
        element: A BeautifulSoup tag element.

    Returns:
        A string representing the cleaned HTML element.
    """
    whitelist_attrs = []

    # Go through each attribute in the element and keep only whitelisted ones
    for attr_name, attr_value in element.attrs.items():
        if (
            attr_name in WHITELIST_ATTRIBUTES
            or attr_name.startswith("data-")
            or attr_name.startswith("aria-")
        ):
            whitelist_attrs.append((attr_name, attr_value))
        else:
            logger.debug(
                f"Removing attribute from <{element.name}>: {attr_name}='{attr_value}'"
            )

    # Update the element with only whitelisted attributes
    element.attrs = dict(whitelist_attrs)
    return str(element)


def filter_and_clean_html(soup: BeautifulSoup) -> str:
    """Filter out irrelevant elements, clean attributes, and log removed elements.

    Args:
        soup: BeautifulSoup object of the parsed HTML.

    Returns:
        A string representing the cleaned HTML.
    """
    # Define relevant elements for action replay
    relevant_tags = ["a", "button", "div", "span", "input", "img", "form", "iframe"]
    relevant_elements = []

    # Find relevant elements and log removal of irrelevant ones
    for el in soup.find_all():
        if el.name in relevant_tags:
            relevant_elements.append(el)
        else:
            logger.debug(f"Removing element <{el.name}> with attributes: {el.attrs}")

    # Clean each relevant element
    cleaned_elements = [clean_html_attributes(el) for el in relevant_elements]

    # Recreate a simplified HTML structure with only the cleaned elements
    return "".join(cleaned_elements)
