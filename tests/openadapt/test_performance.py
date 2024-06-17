import pytest
from loguru import logger
from openadapt.db.crud import get_recordings_by_desc, get_new_session, get_latest_recording
from openadapt.replay import replay 
from openadapt.a11y import find_application, get_main_window, get_element_value


# parametrized tests
@pytest.mark.parametrize("task_description, replay_strategy", [
    ("test_calculator", "NaiveReplayStrategy"),
    # ("test_spreadsheet", "NaiveReplayStrategy"),
    # ("test_powerpoint", "NaiveReplayStrategy")
])

def test_replay(task_description, replay_strategy):
    # Get recordings which containn the string "test_calculator"
    session = get_new_session(read_only=True)
    recordings = get_recordings_by_desc(session, task_description)

    assert len(recordings) > 0, f"No recordings found with task description: {task_description}"
    recording = recordings[0]

    result = replay(strategy_name=replay_strategy, recording=recording)
    assert result == True, f"Replay failed for recording: {recording.id}"


    app_name = "Calculator"
    role = "AXStaticText"
    expected_value = "42"  

    app_element = find_application(app_name)
    assert app_element is not None, f"{app_name} application is not running."

    main_window = get_main_window(app_element)
    assert main_window is not None, f"Main window not found for {app_name}."

    element_value = get_element_value(main_window, role)
    assert element_value == expected_value, f"Value mismatch: expected '{expected_value}', got '{element_value}'"

    logger.info(f"Value match: '{element_value}' == '{expected_value}'")




