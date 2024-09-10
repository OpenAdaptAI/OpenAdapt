"""Utilities for working with BrowserEvents."""

from statistics import mean, median, stdev

from bs4 import BeautifulSoup
from copy import deepcopy
from dtaidistance import dtw, dtw_ndim
from loguru import logger
from tqdm import tqdm
import numpy as np

from openadapt import models, utils
from openadapt.db import crud

# action to browser
MOUSE_BUTTON_MAPPING = {
    "left": 0,
    "right": 2,
    "middle": 1
}

# action to browser
EVENT_TYPE_MAPPING = {
    "click": "click",
    "press": "keydown",
    "release": "keyup"
}

SPATIAL = True

# TODO: read from pynput
KEYBOARD_KEYS = [
    'a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i', 'j', 'k', 'l', 'm', 'n', 'o', 'p',
    'q', 'r', 's', 't', 'u', 'v', 'w', 'x', 'y', 'z',
    '0', '1', '2', '3', '4', '5', '6', '7', '8', '9',
    'tab', 'enter', 'shift', 'ctrl', 'alt', 'esc', 'space', 'backspace',
    'delete', 'home', 'end', 'pageup', 'pagedown',
    'arrowup', 'arrowdown', 'arrowleft', 'arrowright'
]


def add_screen_tlbr(browser_events: list[models.BrowserEvent]) -> None:
    """
    Computes and adds the 'data-tlbr-screen' attribute for each element based on the
    coordMappings provided by JavaScript events. If 'data-tlbr-screen' already exists,
    compute the values again and assert equality. Reuse the most recent valid mappings
    if none exist for the current event by iterating over the events in reverse order.

    Args:
        browser_events (list[models.BrowserEvent]): List of browser events to process.
    """
    # Initialize variables to store the most recent valid mappings
    latest_valid_x_mappings = None
    latest_valid_y_mappings = None

    # Iterate over the events in reverse order
    for event in reversed(browser_events):
        message = event.message

        event_type = message.get('eventType')
        if event_type != "click":
            continue

        visible_html_string = message.get('visibleHtmlString')
        if not visible_html_string:
            logger.warning("No visible HTML data available for event.")
            continue

        # Parse the visible HTML using BeautifulSoup
        soup = BeautifulSoup(visible_html_string, 'html.parser')

        # Fetch the target element using its data-id
        target_id = message.get('targetId')
        target_element = soup.find(attrs={"data-id": target_id})

        if not target_element:
            logger.warning(f"No target element found for targetId: {target_id}")
            continue

        # Extract coordMappings from the message
        coord_mappings = message.get('coordMappings', {})
        x_mappings = coord_mappings.get('x', {})
        y_mappings = coord_mappings.get('y', {})

        # Check if there are sufficient data points; if not, reuse the latest valid mappings
        if 'client' in x_mappings and len(x_mappings['client']) >= 2 and len(y_mappings['client']) >= 2:
            # Update the latest valid mappings
            latest_valid_x_mappings = x_mappings
            latest_valid_y_mappings = y_mappings
        else:
            # Reuse the most recent valid mappings
            if latest_valid_x_mappings is None or latest_valid_y_mappings is None:
                logger.warning(f"No valid coordinate mappings available for element: {target_id}")
                continue  # No valid mappings available, skip this event

            x_mappings = latest_valid_x_mappings
            y_mappings = latest_valid_y_mappings

        # Compute the scale and offset for both x and y axes
        sx_scale, sx_offset = fit_linear_transformation(x_mappings['client'], x_mappings['screen'])
        sy_scale, sy_offset = fit_linear_transformation(y_mappings['client'], y_mappings['screen'])

        # Only process the screen coordinates
        tlbr_attr = "data-tlbr-screen"
        try:
            # Get existing screen coordinates if present
            existing_screen_coords = target_element[tlbr_attr] if tlbr_attr in target_element.attrs else None
        except KeyError:
            existing_screen_coords = None

        # Compute screen coordinates
        client_coords = target_element.get('data-tlbr-client')
        if not client_coords:
            logger.warning(f"Missing client coordinates for element with id: {target_id}")
            continue

        # Extract client coordinates
        client_top, client_left, client_bottom, client_right = map(float, client_coords.split(','))

        # Calculate screen coordinates using the computed scale and offset
        screen_top = sy_scale * client_top + sy_offset
        screen_left = sx_scale * client_left + sx_offset
        screen_bottom = sy_scale * client_bottom + sy_offset
        screen_right = sx_scale * client_right + sx_offset

        # New computed screen coordinates
        new_screen_coords = f"{screen_top},{screen_left},{screen_bottom},{screen_right}"
        logger.info(f"{client_coords=} {existing_screen_coords=} {new_screen_coords=}")

        # Check for existing data-tlbr-screen attribute
        if existing_screen_coords:
            assert existing_screen_coords == new_screen_coords, (
                f"Mismatch in computed and existing screen coordinates: {existing_screen_coords} != {new_screen_coords}"
            )

        # Update the attribute with the new value
        target_element['data-tlbr-screen'] = new_screen_coords

        # Write the updated element back to the message
        message['visibleHtmlString'] = str(soup)

    logger.info("Finished processing all browser events for screen coordinates.")


def fit_linear_transformation(client_coords: list[float], screen_coords: list[float]) -> tuple[float, float]:
    """
    Fits a linear transformation (scale and offset) from client coordinates to screen coordinates.

    Args:
        client_coords (list[float]): The client coordinates (x or y).
        screen_coords (list[float]): The corresponding screen coordinates (x or y).

    Returns:
        tuple[float, float]: The scale and offset values for the linear transformation.
    """
    n = len(client_coords)
    sum_client = sum(client_coords)
    sum_screen = sum(screen_coords)
    sum_client_squared = sum(c ** 2 for c in client_coords)
    sum_client_screen = sum(c * s for c, s in zip(client_coords, screen_coords))

    # Calculate scale and offset using least squares fitting
    scale = (n * sum_client_screen - sum_client * sum_screen) / (n * sum_client_squared - sum_client ** 2)
    offset = (sum_screen - scale * sum_client) / n

    return scale, offset


def identify_and_log_smallest_clicked_element(browser_event) -> None:
    """
    Identifies the smallest DOM element that was clicked on for a given click event 
    and logs it.

    Args:
        browser_event: The browser event containing the click details.
    """
    visible_html_string = browser_event.message.get('visibleHtmlString')
    message_id = browser_event.message.get('id')
    logger.info("*" * 10)
    logger.info(f"{message_id=}")
    target_id = browser_event.message.get('targetId')
    logger.info(f"{target_id=}")

    if not visible_html_string:
        logger.warning("No visible HTML data available for click event.")
        return

    # Parse the visible HTML using BeautifulSoup
    soup = BeautifulSoup(visible_html_string, 'html.parser')
    target_element = soup.find(attrs={"data-id": target_id})
    target_area = None
    if not target_element:
        logger.warning(f"{target_element=}")
    else:
        for coord_type in ('client', 'screen'):
            x = browser_event.message.get(f"{coord_type}X")
            y = browser_event.message.get(f"{coord_type}Y")
            tlbr = f"data-tlbr-{coord_type}"
            try:
                target_element_tlbr = target_element[tlbr]
            except KeyError as exc:
                logger.warning(f"{tlbr=} not in {target_element=}")
                continue
            top, left, bottom, right = map(float, target_element_tlbr.split(','))
            logger.info(f"{tlbr=} {x=} {y=} {top=} {left=} {bottom=} {right=}")
            if not (left <= x <= right and top <= y <= bottom):
                logger.warning(f"outside")

        # Calculate the area for target_element
        if 'data-tlbr-client' in target_element.attrs:
            target_top, target_left, target_bottom, target_right = map(float, target_element['data-tlbr-client'].split(','))
            target_area = (target_right - target_left) * (target_bottom - target_top)

    elements = soup.find_all(attrs={'data-tlbr-client': True})
    
    smallest_element = None
    smallest_area = float('inf')

    for elem in elements:
        data_tlbr = elem['data-tlbr-client']
        top, left, bottom, right = map(float, data_tlbr.split(','))
        client_x = browser_event.message.get("clientX")
        client_y = browser_event.message.get("clientY")

        if left <= client_x <= right and top <= client_y <= bottom:
            area = (right - left) * (bottom - top)
            if area < smallest_area:
                smallest_area = area
                smallest_element = elem

    if smallest_element is not None:
        smallest_element_str = utils.truncate_html(str(smallest_element), 100)
        logger.info(f"Smallest clicked element found: {smallest_element_str}")
        smallest_element_id = smallest_element["data-id"]
        smallest_element_type = smallest_element.name
        target_element_type = target_element.name if target_element else 'Unknown'
        smallest_element_area = smallest_area

        # Check if smallest_element is a descendant or ancestor of target_element
        is_descendant = False
        is_ancestor = False

        if target_element:
            is_descendant = target_element in smallest_element.parents
            is_ancestor = smallest_element in target_element.parents

        # Log a warning if the smallest element is not the target, or a descendant/ancestor of the target
        if not (
            smallest_element_id == target_id or
            is_descendant or
            is_ancestor
        ):
            logger.warning(
                f"{smallest_element_id=} {smallest_element_type=} {smallest_element_area=} does not match "
                f"{target_id=} {target_element_type=} {target_area=} is_descendant={is_descendant} is_ancestor={is_ancestor}"
            )
    else:
        logger.warning("No element found matching the click coordinates.")


def is_action_event(event, action_name: str, key_or_button: str) -> bool:
    """
    Determine if the event matches the given action name and key/button.

    Args:
        event: The action event to check.
        action_name: The type of action (e.g., "click", "press", "release").
        key_or_button: The key or button associated with the action.

    Returns:
        bool: True if the event matches the action name and key/button, False otherwise.
    """
    if action_name == "click":
        return event.name == action_name and event.mouse_button_name == key_or_button
    elif action_name in {"press", "release"}:
        raw_action_text = event._text(name_prefix="", name_suffix="")
        return event.name == action_name and raw_action_text == key_or_button
    else:
        return False


def is_browser_event(event, action_name: str, key_or_button: str) -> bool:
    if action_name == "click":
        return (event.message.get("eventType") == EVENT_TYPE_MAPPING[action_name] and
                event.message.get("button") == MOUSE_BUTTON_MAPPING[key_or_button])
    elif action_name in {"press", "release"}:
        return (event.message.get("eventType") == EVENT_TYPE_MAPPING[action_name] and
                event.message.get("key").lower() == key_or_button)
    else:
        return False


def align_and_print(
    event_type: str,
    action_events: list,
    browser_events: list,
    spatial: bool = SPATIAL,
) -> tuple[int, list[float], list[float], list[float], list[float]]:
    # Only log if there are any action or browser events of each type
    if not action_events and not browser_events:
        return 0, [], [], [], []

    # Convert series of events to timestamps
    action_timestamps = [e.timestamp for e in action_events]
    browser_timestamps = [e.message["timestamp"] for e in browser_events]
    #browser_timestamps = [e.timestamp for e in browser_events]

    if spatial:
        # Prepare sequences for multidimensional DTW
        action_sequence = np.array([
            [e.timestamp, e.mouse_x or 0.0, e.mouse_y or 0.0]
            for e in action_events
        ], dtype=np.double)

        browser_sequence = np.array([
            [e.timestamp, e.message.get('screenX', 0.0), e.message.get('screenY', 0.0)]
            for e in browser_events
        ], dtype=np.double)

        # Compute the alignment using multidimensional DTW
        path = dtw_ndim.warping_path(action_sequence, browser_sequence)
    else:
        # Compute the alignment using DTW
        path = dtw.warping_path(action_timestamps, browser_timestamps)

    # Enforce a one-to-one correspondence by selecting the closest matches
    filtered_path = enforce_one_to_one_mapping(path, action_timestamps, browser_timestamps)

    match_count = 0
    mismatch_count = 0
    error_count = 0  # Initialize error counter
    remote_time_differences = []  # To store differences in local/remote timestamps for matching events
    local_time_differences = []  # As above but for local/local
    mouse_x_differences = []  # To store differences in mouse X positions for matching events
    mouse_y_differences = []  # To store differences in mouse Y positions for matching events

    logger.info(f"Alignment for {event_type} Events")
    for i, j in filtered_path:
        action_event = action_events[i]
        browser_event = deepcopy(browser_events[j])

        action_event_type = action_event.name.lower()
        browser_event_type = browser_event.message['eventType'].lower()

        if action_event_type in EVENT_TYPE_MAPPING and browser_event_type == EVENT_TYPE_MAPPING[action_event_type]:
            match_count += 1
            remote_time_difference = action_event.timestamp - browser_event.message['timestamp']
            remote_time_differences.append(remote_time_difference)
            local_time_difference = action_event.timestamp - browser_event.timestamp
            local_time_differences.append(local_time_difference)

            # Compute differences between mouse positions
            if action_event.mouse_x is not None:
                mouse_x_difference = action_event.mouse_x - browser_event.message['screenX']
                mouse_y_difference = action_event.mouse_y - browser_event.message['screenY']
                if mouse_x_difference > 1:
                    logger.warning(f"{mouse_x_difference=} {action_event.mouse_x=} {browser_event.message['screenX']=}")
                if mouse_y_difference > 1:
                    logger.warning(f"{mouse_y_difference=} {action_event.mouse_y=} {browser_event.message['screenY']=}")
                mouse_x_differences.append(mouse_x_difference)
                mouse_y_differences.append(mouse_y_difference)
        else:
            mismatch_count += 1
            logger.warning(f"Event type mismatch: Action({action_event_type}) does not match Browser({browser_event_type})")

        logger.info(
            f"\nAction Event:\n"
            f"  - Type: {action_event.name}\n"
            f"  - Timestamp: {action_event.timestamp}\n"
            f"  - Details: {action_event}\n"
            f"Browser Event:\n"
            f"  - Type: {browser_event.message['eventType']}\n"
            f"  - Timestamp: {browser_event.message['timestamp']}\n"
            f"  - Details: {browser_event}\n"
            f"{'-'*80}"
        )

    logger.info(f"Total Matches: {match_count}")
    logger.info(f"Total Mismatches: {mismatch_count}")

    # Log unmatched browser events
    matched_browser_indices = {j for _, j in filtered_path}
    unmatched_browser_events = [e for idx, e in enumerate(browser_events) if idx not in matched_browser_indices]

    if unmatched_browser_events:
        logger.warning(f"Unmatched Browser Events: {len(unmatched_browser_events)}")
        for browser_event in unmatched_browser_events:
            logger.warning(
                f"Unmatched Browser Event:\n"
                f"  - Type: {browser_event.message['eventType']}\n"
                f"  - Timestamp: {browser_event.message['timestamp']}\n"
                f"  - Details: {browser_event}\n"
            )
            error_count += 1  # Increment error count for each unmatched browser event
    
    try:
        assert len(browser_events) == match_count, "Every BrowserEvent should have a corresponding ActionEvent."
    except Exception as exc:
        error_count += 1  # Increment error count for assertion error
        logger.warning(exc)
    
    return error_count, remote_time_differences, local_time_differences, mouse_x_differences, mouse_y_differences


def enforce_one_to_one_mapping(path: list, action_timestamps: list, browser_timestamps: list) -> list:
    """
    Enforces a one-to-one mapping between BrowserEvents and ActionEvents by selecting the closest match.
    
    Args:
        path: List of tuples representing the DTW path.
        action_timestamps: List of timestamps for action events.
        browser_timestamps: List of timestamps for browser events.
        
    Returns:
        filtered_path: List of tuples representing the filtered DTW path with one-to-one mapping.
    """
    used_action_indices = set()
    filtered_path = []

    # Create a dictionary to store the closest match for each browser event
    closest_matches = {}

    for i, j in path:
        if j not in closest_matches:
            closest_matches[j] = (i, abs(action_timestamps[i] - browser_timestamps[j]))
        else:
            # If a closer match is found, update the closest match for this browser event
            current_diff = abs(action_timestamps[i] - browser_timestamps[j])
            if current_diff < closest_matches[j][1]:
                closest_matches[j] = (i, current_diff)

    # Collect the closest matches while ensuring each action event is used only once
    for j, (i, _) in closest_matches.items():
        if i not in used_action_indices:
            filtered_path.append((i, j))
            used_action_indices.add(i)

    return filtered_path


def main() -> None:
    session = crud.get_new_session(read_only=True)
    recording = crud.get_latest_recording(session)
    action_events = crud.get_action_events(session=session, recording=recording)
    browser_events = crud.get_browser_events(session=session, recording=recording)

    # Filter BrowserEvents for 'USER_EVENT' type
    browser_events = [
        browser_event
        for browser_event in browser_events
        if browser_event.message["type"] == "USER_EVENT"
    ]

    add_screen_tlbr(browser_events)

    # Define event pairs dynamically for mouse events
    event_pairs = [
        ("Left Click", "click", "left"),
        ("Right Click", "click", "right"),
        ("Middle Click", "click", "middle"),
    ]

    # Add keyboard events dynamically
    event_pairs.extend(
        [(f"Key Press {key.upper()}", "press", key) for key in KEYBOARD_KEYS] +
        [(f"Key Release {key.upper()}", "release", key) for key in KEYBOARD_KEYS]
    )

    total_errors = 0  # Initialize total errors counter
    total_remote_time_differences = []
    total_local_time_differences = []
    total_mouse_x_differences = []
    total_mouse_y_differences = []

    # Process each event pair
    for event_type, action_name, key_or_button in tqdm(event_pairs):
        action_filtered_events = list(filter(lambda e: is_action_event(e, action_name, key_or_button), action_events))
        browser_filtered_events = list(filter(lambda e: is_browser_event(e, action_name, key_or_button), browser_events))

        # Only log if there are any action or browser events
        if action_filtered_events or browser_filtered_events:
            logger.info(f"{event_type}: {len(action_filtered_events)} action events, {len(browser_filtered_events)} browser events")

            # Align and print events for the specific type
            errors, remote_time_differences, local_time_differences, mouse_x_differences, mouse_y_differences = align_and_print(
                event_type, action_filtered_events, browser_filtered_events
            )
            total_errors += errors  # Accumulate errors
            total_remote_time_differences += remote_time_differences
            total_local_time_differences += local_time_differences
            total_mouse_x_differences += mouse_x_differences
            total_mouse_y_differences += mouse_y_differences

            # Identify and log the smallest clicked DOM element
            for browser_event in browser_filtered_events:
                if browser_event.message["eventType"] == "click":
                    identify_and_log_smallest_clicked_element(browser_event)

    # Calculate and log statistics for timestamp differences
    for name, time_differences in (
        ('Remote', total_remote_time_differences),
        ('Local', total_local_time_differences),
    ):
        if not time_differences:
            logger.warning(f"{name=} {time_differences=}")
            continue
        min_diff = min(time_differences, key=abs)
        max_diff = max(time_differences, key=abs)
        mean_diff = mean(time_differences)
        median_diff = median(time_differences)
        stddev_diff = stdev(time_differences) if len(time_differences) > 1 else 0
        logger.info(
            f"{name} Timestamp Differences - Min: {min_diff:.4f}, Max: {max_diff:.4f}, "
            f"Mean: {mean_diff:.4f}, Median: {median_diff:.4f}, Std Dev: {stddev_diff:.4f}"
        )

    # Calculate and log statistics for mouse position differences
    for axis, mouse_differences in (("X", total_mouse_x_differences), ("Y", total_mouse_y_differences)):
        if not mouse_differences:
            logger.warning(f"{axis=} {mouse_differences=}")
            continue
        min_mouse_diff = min(mouse_differences, key=abs)
        max_mouse_diff = max(mouse_differences, key=abs)
        num_mouse_errors = sum([abs(diff) >= 1 for diff in mouse_differences])
        num_mouse_correct = sum([abs(diff) < 1 for diff in mouse_differences])
        logger.info(f"{num_mouse_errors=} {num_mouse_correct=}")
        if abs(max_mouse_diff) >= 1:
            logger.warning(f"abs({max_mouse_diff=}) > 1")
        mean_mouse_diff = mean(mouse_differences)
        median_mouse_diff = median(mouse_differences)
        stddev_mouse_diff = stdev(mouse_differences) if len(mouse_differences) > 1 else 0
        logger.info(
            f"Mouse {axis} Position Differences - Min: {min_mouse_diff:.4f}, Max: {max_mouse_diff:.4f}, "
            f"Mean: {mean_mouse_diff:.4f}, Median: {median_mouse_diff:.4f}, Std Dev: {stddev_mouse_diff:.4f}"
        )

    # Report total errors at the end
    logger.info(f"Total Errors Across All Events: {total_errors}")


if __name__ == "__main__":
    main()
