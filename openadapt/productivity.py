from loguru import logger

from openadapt.crud import (
    get_latest_recording,
)

from openadapt.events import (
    get_events,
)

from openadapt.utils import (
configure_logging
)

MAX_GAP_SECONDS = 30
PROCESS_EVENTS = True
LOG_LEVEL = "INFO"


def find_gaps(action_events):
    num_gaps = 0
    # check every pair of action events for gap length
    for i in range(0, len(action_events) - 1):
        if action_events[i + 1].screenshot_timestamp - action_events[i].screenshot_timestamp > MAX_GAP_SECONDS:
            num_gaps += 1
    return num_gaps


def calculate_productivity():
    configure_logging(logger, LOG_LEVEL)

    recording = get_latest_recording()

    action_events = get_events(recording, process=PROCESS_EVENTS)

    gaps = find_gaps(action_events)
    # other productivity metrics

    # display data somehow
    # for now just printing it
    logger.info(f"Number of gaps larger than {MAX_GAP_SECONDS} seconds: {gaps}")


if __name__ == "__main__":
    calculate_productivity()
