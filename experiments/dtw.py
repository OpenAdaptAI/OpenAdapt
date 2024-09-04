from dtaidistance import dtw
from loguru import logger

from openadapt.db import crud

MOUSE_BUTTON_MAPPING = {
    "left": 0,
    "right": 2,
    "middle": 1
}

EVENT_TYPE_MAPPING = {
    "click": "click",
    "press": "keydown",
    "release": "keyup"
}

KEYBOARD_KEYS = [
    'a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i', 'j', 'k', 'l', 'm', 'n', 'o', 'p', 'q', 'r', 's', 't', 'u', 'v', 'w', 'x', 'y', 'z',
    '0', '1', '2', '3', '4', '5', '6', '7', '8', '9',
    'tab', 'enter', 'shift', 'ctrl', 'alt', 'esc', 'space', 'backspace', 'delete', 'home', 'end', 'pageup', 'pagedown', 'arrowup', 'arrowdown', 'arrowleft', 'arrowright'
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

    # Process each event pair
    for event_type, action_name, key_or_button in event_pairs:
        action_filtered_events = list(filter(lambda e: is_action_event(e, action_name, key_or_button), action_events))
        browser_filtered_events = list(filter(lambda e: is_browser_event(e, action_name, key_or_button), browser_events))

        logger.info(f"{event_type}: {len(action_filtered_events)} action events, {len(browser_filtered_events)} browser events")

        # Convert series of events to timestamps
        action_timestamps = [e.timestamp for e in action_filtered_events]
        browser_timestamps = [e.message["timestamp"] for e in browser_filtered_events]

        # Align and print events for the specific type
        align_and_print(event_type, action_timestamps, browser_timestamps, action_filtered_events, browser_filtered_events)


def is_action_event(event, action_name: str, key_or_button: str) -> bool:
    if action_name == "click":
        return event.name == action_name and event.mouse_button_name == key_or_button
    elif action_name in {"press", "release"}:
        return event.name == action_name and event.key == key_or_button
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


def align_and_print(event_type: str, action_timestamps: list, browser_timestamps: list, action_events: list, browser_events: list) -> None:
    # Compute the alignment using DTW
    path = dtw.warping_path(action_timestamps, browser_timestamps)

    # Enforce a one-to-one correspondence by selecting the closest matches
    filtered_path = enforce_one_to_one_mapping(path, action_timestamps, browser_timestamps)

    match_count = 0
    mismatch_count = 0

    print(f"\nAlignment for {event_type} Events")
    for i, j in filtered_path:
        action_event = action_events[i]
        browser_event = browser_events[j]

        action_event_type = action_event.name.lower()
        browser_event_type = browser_event.message['eventType'].lower()

        if action_event_type in EVENT_TYPE_MAPPING and browser_event_type == EVENT_TYPE_MAPPING[action_event_type]:
            match_count += 1
        else:
            mismatch_count += 1
            print(f"Event type mismatch: Action({action_event_type}) does not match Browser({browser_event_type})")

        print(
            f"Action Event:\n"
            f"  - Type: {action_event.name}\n"
            f"  - Timestamp: {action_event.timestamp}\n"
            f"  - Details: {action_event}\n"
            f"Browser Event:\n"
            f"  - Type: {browser_event.message['eventType']}\n"
            f"  - Timestamp: {browser_event.message['timestamp']}\n"
            f"  - Details: {browser_event}\n"
            f"{'-'*80}"
        )

    print(f"\nTotal Matches: {match_count}")
    print(f"Total Mismatches: {mismatch_count}")

    assert len(browser_events) == match_count, "Every BrowserEvent should have a corresponding ActionEvent."


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
