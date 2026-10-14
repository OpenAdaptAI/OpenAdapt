"""Shared fixtures for openadapt-evals tests."""

import pytest

from openadapt_evals.adapters import (
    BenchmarkAction,
    BenchmarkObservation,
    BenchmarkResult,
    BenchmarkTask,
    UIElement,
    WAAMockAdapter,
)
from openadapt_evals.agents import (
    ScriptedAgent,
    SmartMockAgent,
)


@pytest.fixture
def mock_adapter():
    """Create a WAAMockAdapter instance for testing."""
    return WAAMockAdapter(num_tasks=10, domains=["browser", "office"])


@pytest.fixture
def sample_task():
    """Create a sample BenchmarkTask for testing."""
    return BenchmarkTask(
        task_id="test_task_1",
        instruction="Click the Submit button",
        domain="browser",
        time_limit_steps=15,
        raw_config={"mock": True},
    )


@pytest.fixture
def sample_observation():
    """Create a sample BenchmarkObservation for testing."""
    return BenchmarkObservation(
        screenshot=None,
        screenshot_path=None,
        viewport=(1920, 1200),
        accessibility_tree={
            "role": "window",
            "name": "Test Window",
            "children": [
                {"role": "button", "name": "OK", "id": "1"},
                {"role": "textfield", "name": "Input", "id": "2"},
                {"role": "button", "name": "Submit", "id": "4"},
            ],
        },
        window_title="Test Window",
    )


@pytest.fixture
def sample_click_action():
    """Create a sample click action."""
    return BenchmarkAction(
        type="click",
        x=0.5,
        y=0.5,
        target_node_id="4",
    )


@pytest.fixture
def sample_type_action():
    """Create a sample type action."""
    return BenchmarkAction(
        type="type",
        text="Hello, World!",
    )


@pytest.fixture
def sample_done_action():
    """Create a sample done action."""
    return BenchmarkAction(type="done")


@pytest.fixture
def sample_result():
    """Create a sample BenchmarkResult for testing."""
    return BenchmarkResult(
        task_id="test_task_1",
        success=True,
        score=1.0,
        num_steps=3,
        total_time_seconds=2.5,
    )


@pytest.fixture
def scripted_agent(sample_click_action, sample_done_action):
    """Create a scripted agent with predefined actions."""
    return ScriptedAgent([sample_click_action, sample_done_action])


@pytest.fixture
def smart_mock_agent():
    """Create a SmartMockAgent instance."""
    return SmartMockAgent()


@pytest.fixture
def sample_ui_element():
    """Create a sample UIElement for testing."""
    return UIElement(
        node_id="1",
        role="button",
        name="Submit",
        bbox=(100, 200, 200, 250),
        text="Submit",
    )
