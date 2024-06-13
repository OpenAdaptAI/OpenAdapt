import pytest
from openadapt.db.crud import get_recordings_by_desc, get_new_session
from openadapt.replay import replay 


# parametrized tests
@pytest.mark.parametrize("task_description, replay_strategy", [
    ("calculator_test", "NaiveReplayStrategy"),
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


    # TO DO: use openadapt.window_accessibility_data to assert that the value is as we expect

    




