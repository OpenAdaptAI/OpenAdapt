from statistics import mean, median, stdev

from dtaidistance import dtw
from loguru import logger

from openadapt import utils
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

# TODO: read from pynput
KEYBOARD_KEYS = [
    'a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i', 'j', 'k', 'l', 'm', 'n', 'o', 'p',
    'q', 'r', 's', 't', 'u', 'v', 'w', 'x', 'y', 'z',
    '0', '1', '2', '3', '4', '5', '6', '7', '8', '9',
    'tab', 'enter', 'shift', 'ctrl', 'alt', 'esc', 'space', 'backspace',
    'delete', 'home', 'end', 'pageup', 'pagedown',
    'arrowup', 'arrowdown', 'arrowleft', 'arrowright'
]


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

    # Process each event pair
    for event_type, action_name, key_or_button in event_pairs:
        action_filtered_events = list(filter(lambda e: is_action_event(e, action_name, key_or_button), action_events))
        browser_filtered_events = list(filter(lambda e: is_browser_event(e, action_name, key_or_button), browser_events))

        # Only log if there are any action or browser events
        if action_filtered_events or browser_filtered_events:
            logger.info(f"{event_type}: {len(action_filtered_events)} action events, {len(browser_filtered_events)} browser events")
            # Convert series of events to timestamps
            action_timestamps = [e.timestamp for e in action_filtered_events]
            browser_timestamps = [e.message["timestamp"] for e in browser_filtered_events]

            # Align and print events for the specific type
            errors, remote_time_differences, local_time_differences  = align_and_print(
                event_type, action_timestamps, browser_timestamps, action_filtered_events, browser_filtered_events
            )
            total_errors += errors  # Accumulate errors
            total_remote_time_differences += remote_time_differences
            total_local_time_differences += local_time_differences

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
    # Report total errors at the end
    logger.info(f"Total Errors Across All Events: {total_errors}")


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
        stripped_action_text = event._text(name_prefix="", name_suffix="")
        return event.name == action_name and stripped_action_text == key_or_button
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
    action_timestamps: list,
    browser_timestamps: list,
    action_events: list,
    browser_events: list,
) -> tuple[int, list[float], list[float]]:
    # Only log if there are any action or browser events of each type
    if not action_events and not browser_events:
        return 0

    # Compute the alignment using DTW
    path = dtw.warping_path(action_timestamps, browser_timestamps)

    # Enforce a one-to-one correspondence by selecting the closest matches
    filtered_path = enforce_one_to_one_mapping(path, action_timestamps, browser_timestamps)

    match_count = 0
    mismatch_count = 0
    error_count = 0  # Initialize error counter
    remote_time_differences = []  # To store differences in local/remote timestamps for matching events
    local_time_differences = []  # As above but for local/local

    logger.info(f"Alignment for {event_type} Events")
    for i, j in filtered_path:
        action_event = action_events[i]
        browser_event = browser_events[j]

        action_event_type = action_event.name.lower()
        browser_event_type = browser_event.message['eventType'].lower()

        if action_event_type in EVENT_TYPE_MAPPING and browser_event_type == EVENT_TYPE_MAPPING[action_event_type]:
            match_count += 1
            remote_time_difference = action_event.timestamp - browser_event.message['timestamp']
            remote_time_differences.append(remote_time_difference)
            local_time_difference = action_event.timestamp - browser_event.timestamp
            local_time_differences.append(local_time_difference)
        else:
            mismatch_count += 1
            logger.warning(f"Event type mismatch: Action({action_event_type}) does not match Browser({browser_event_type})")

        browser_event.message["visibleHtmlData"] = utils.truncate_html(browser_event.message["visibleHtmlData"], max_len=100)
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
    
    return error_count, remote_time_differences, local_time_differences


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


if __name__ == "__main__":
    main()
