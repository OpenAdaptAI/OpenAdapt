"""Module for log message filtering, excluding strings & limiting warnings."""

from collections import defaultdict
import time

from openadapt.config import config

MESSAGE_TIMESTAMPS = defaultdict(list)

# TODO: move utils.configure_logging to here


def filter_log_messages(data: dict) -> bool:
    """Filter log messages based on the defined criteria.

    Args:
        data: The log message data from a loguru logger.

    Returns:
        bool: True if the log message should not be ignored, False otherwise.
    """
    # TODO: ultimately, we want to fix the underlying issues, but for now,
    # we can ignore these messages
    for msg in config.MESSAGES_TO_FILTER:
        if msg in data["message"]:
            if config.MAX_NUM_WARNINGS_PER_SECOND > 0:
                current_timestamp = time.time()
                MESSAGE_TIMESTAMPS[msg].append(current_timestamp)
                timestamps = MESSAGE_TIMESTAMPS[msg]

                # Remove timestamps older than 1 second
                timestamps = [
                    ts
                    for ts in timestamps
                    if current_timestamp - ts <= config.WARNING_SUPPRESSION_PERIOD
                ]

                if len(timestamps) > config.MAX_NUM_WARNINGS_PER_SECOND:
                    return False

                MESSAGE_TIMESTAMPS[msg] = timestamps

    return True
