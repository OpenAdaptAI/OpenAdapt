import pytest
from loguru import logger
from openadapt.db.crud import (
    get_recordings_by_desc,
    get_new_session,
)
from openadapt.replay import replay
from openadapt.a11y import (
    get_active_window,
    get_element_value,
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

    active_window = get_active_window()
    element_value = get_element_value(active_window)
    logger.info(element_value)

    assert (
        element_value == expected_value
    ), f"Value mismatch: expected '{expected_value}', got '{element_value}'"

    result_message = f"Value match: '{element_value}' == '{expected_value}'"
    logger.info(result_message)


if __name__ == "__main__":
    pytest.main()
