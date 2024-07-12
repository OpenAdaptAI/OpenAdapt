import pytest
import time
from loguru import logger
import logging
from openadapt.db.crud import (
    get_recordings_by_desc,
    get_new_session,
)
from openadapt.replay import replay
from openadapt.window import (
    get_active_window,
)

# logging to a txt file
logging.basicConfig(
    level=logging.INFO,
    filename="test_results.txt",
    filemode="w",
    format="%(asctime)s | %(levelname)s | %(message)s",
)


# parametrized tests
@pytest.mark.parametrize(
    "task_description, replay_strategy, expected_value, instructions",
    [
        ("test_calculator", "VisualReplayStrategy", "6", " "),
        ("test_calculator", "VisualReplayStrategy", "8", "calculate 9-8+7"),
        # ("test_spreadsheet", "NaiveReplayStrategy"),
        # ("test_powerpoint", "NaiveReplayStrategy")
    ],
)
def test_replay(task_description, replay_strategy, expected_value, instructions):
    # Get recordings which contain the string "test_calculator"
    session = get_new_session(read_only=True)
    recordings = get_recordings_by_desc(session, task_description)

    assert (
        len(recordings) > 0
    ), f"No recordings found with task description: {task_description}"
    recording = recordings[0]

    result = replay(
        strategy_name=replay_strategy,
        recording=recording,
        instructions=instructions,
    )
    assert result is True, f"Replay failed for recording: {recording.id}"

    def find_display_element(element, timeout=10):
        """Find the display element within the specified timeout.

        Args:
            element: The parent element to search within.
            timeout: The maximum time to wait for the element (default is 10 seconds).

        Returns:
            The found element.

        Raises:
            TimeoutError: If the element is not found within the specified timeout.
        """
        end_time = time.time() + timeout
        while time.time() < end_time:
            elements = element.descendants(control_type="Text")
            for elem in elements:
                if elem.element_info.name.startswith(
                    "Display is"
                ):  # Target the display element
                    return elem
            time.sleep(0.5)
        raise TimeoutError("Display element not found within the specified timeout")

    active_window = get_active_window()
    element = find_display_element(active_window)
    value = element.element_info.name[-1]

    element_value = value
    assert (
        element_value == expected_value
    ), f"Value mismatch: expected '{expected_value}', got '{element_value}'"

    result_message = f"Value match: '{element_value}' == '{expected_value}'"
    logging.info(result_message)


if __name__ == "__main__":
    pytest.main()
