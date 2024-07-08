import pytest
import time
from openadapt import window


def test_get_active_window_state():

    state = window.get_active_window_state(True)

    # Ensure state is a dictionary
    assert isinstance(state, dict), "State should be a dictionary"

    # Check for required keys
    required_keys = {
        "title",
        "left",
        "top",
        "width",
        "height",
        "meta",
        "data",
        "window_id",
    }
    for key in required_keys:
        assert key in state, f"Missing key in state: {key}"

    # Check the type of each key
    assert isinstance(state["title"], str), "Title should be a string"
    assert isinstance(state["left"], int), "Left should be an integer"
    assert isinstance(state["top"], int), "Top should be an integer"
    assert isinstance(state["width"], int), "Width should be an integer"
    assert isinstance(state["height"], int), "Height should be an integer"
    assert isinstance(state["meta"], dict), "Meta should be a dictionary"
    assert isinstance(state["data"], dict), "Data should be a dictionary"
    assert isinstance(state["window_id"], int), "Window ID should be an integer"


def test_get_active_window_state_pgw():
    state = window.get_active_window_state_pgw(True)

    # Ensure state is a dictionary
    assert isinstance(state, dict), "State should be a dictionary"

    # Check for required keys
    required_keys = {
        "title",
        "left",
        "top",
        "width",
        "height",
        "meta",
        "data",
        "window_id",
    }
    for key in required_keys:
        assert key in state, f"Missing key in state: {key}"

    # Check the type of each key
    assert isinstance(state["title"], str), "Title should be a string"
    assert isinstance(state["left"], int), "Left should be an integer"
    assert isinstance(state["top"], int), "Top should be an integer"
    assert isinstance(state["width"], int), "Width should be an integer"
    assert isinstance(state["height"], int), "Height should be an integer"
    assert isinstance(state["meta"], dict), "Meta should be a dictionary"
    assert isinstance(state["data"], dict), "Data should be a dictionary"
    assert isinstance(state["window_id"], int), "Window ID should be an integer"


def test_execution_time():
    # Measure execution time for pywinauto-based function
    start_time = time.perf_counter()
    window.get_active_window_state(True)
    pywinauto_duration = time.perf_counter() - start_time

    # Measure execution time for pygetwindow-based function
    start_time = time.perf_counter()
    window.get_active_window_state_pgw(True)
    pygetwindow_duration = time.perf_counter() - start_time

    print(f"pygetwindow duration: {pygetwindow_duration:.10f}")
    print(f"pywinauto duration: {pywinauto_duration:.10f}")

    assert (
        pygetwindow_duration < pywinauto_duration
    ), "pygetwindow should be faster than pywinauto"


if __name__ == "__main__":
    pytest.main()
