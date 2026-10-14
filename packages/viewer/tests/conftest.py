"""Shared pytest fixtures for openadapt-viewer tests."""

import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from openadapt_viewer.core.types import (
    BenchmarkRun,
    BenchmarkTask,
    ExecutionStep,
    TaskExecution,
)

# Import Playwright fixtures via pytest_playwright
# The plugin is automatically loaded when pytest-playwright is installed


@pytest.fixture
def sample_execution_step():
    """Create a sample ExecutionStep for testing."""
    return ExecutionStep(
        step_number=0,
        timestamp=datetime.now().astimezone(),
        screenshot_path="tasks/task_001/screenshots/step_000.png",
        action_type="click",
        action_details={"x": 500, "y": 300},
        reasoning="Clicking on the target button",
        raw_output="Action: CLICK(500, 300)",
    )


@pytest.fixture
def sample_task_execution(sample_execution_step):
    """Create a sample TaskExecution for testing."""
    steps = [
        sample_execution_step,
        ExecutionStep(
            step_number=1,
            timestamp=datetime.now().astimezone() + timedelta(seconds=2),
            action_type="type",
            action_details={"text": "Hello World"},
            reasoning="Typing the required text",
        ),
        ExecutionStep(
            step_number=2,
            timestamp=datetime.now().astimezone() + timedelta(seconds=4),
            action_type="click",
            action_details={"x": 800, "y": 600},
            reasoning="Clicking the submit button",
        ),
    ]
    return TaskExecution(
        task_id="task_001",
        start_time=datetime.now().astimezone(),
        end_time=datetime.now().astimezone() + timedelta(seconds=10),
        steps=steps,
        success=True,
        error=None,
    )


@pytest.fixture
def failed_task_execution():
    """Create a failed TaskExecution for testing."""
    return TaskExecution(
        task_id="task_002",
        start_time=datetime.now().astimezone(),
        end_time=datetime.now().astimezone() + timedelta(seconds=5),
        steps=[
            ExecutionStep(
                step_number=0,
                action_type="click",
                action_details={"x": 100, "y": 100},
                reasoning="Attempting to click target",
            )
        ],
        success=False,
        error="Task failed: Could not locate target element",
    )


@pytest.fixture
def sample_benchmark_task():
    """Create a sample BenchmarkTask for testing."""
    return BenchmarkTask(
        task_id="task_001",
        instruction="Open Notepad and type 'Hello World'",
        domain="office",
        difficulty="easy",
        time_limit=300,
        metadata={"source": "test"},
    )


@pytest.fixture
def sample_benchmark_run(sample_benchmark_task, sample_task_execution, failed_task_execution):
    """Create a sample BenchmarkRun with multiple tasks for testing."""
    tasks = [
        sample_benchmark_task,
        BenchmarkTask(
            task_id="task_002",
            instruction="Navigate to google.com in Chrome",
            domain="browser",
            difficulty="easy",
            time_limit=300,
        ),
        BenchmarkTask(
            task_id="task_003",
            instruction="Create a new folder on Desktop",
            domain="file_management",
            difficulty="medium",
            time_limit=300,
        ),
    ]

    # Add a third successful execution
    third_execution = TaskExecution(
        task_id="task_003",
        start_time=datetime.now().astimezone(),
        end_time=datetime.now().astimezone() + timedelta(seconds=8),
        steps=[
            ExecutionStep(
                step_number=0,
                action_type="click",
                action_details={"x": 200, "y": 400},
            ),
            ExecutionStep(
                step_number=1,
                action_type="type",
                action_details={"text": "New Folder"},
            ),
        ],
        success=True,
    )

    executions = [sample_task_execution, failed_task_execution, third_execution]

    return BenchmarkRun(
        run_id="test_run_001",
        benchmark_name="Test Benchmark",
        model_id="test-model-v1",
        start_time=datetime.now().astimezone() - timedelta(hours=1),
        end_time=datetime.now().astimezone(),
        tasks=tasks,
        executions=executions,
        config={"max_steps": 10, "timeout": 300},
    )


@pytest.fixture
def temp_dir():
    """Create a temporary directory for test files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def benchmark_data_dir(temp_dir):
    """Create a mock benchmark data directory with test data."""
    # Create directory structure
    tasks_dir = temp_dir / "tasks"
    task_001_dir = tasks_dir / "task_001"
    screenshots_dir = task_001_dir / "screenshots"
    screenshots_dir.mkdir(parents=True)

    # Create metadata.json
    metadata = {
        "run_id": "test_run",
        "benchmark_name": "Test Benchmark",
        "model_id": "test-model",
        "start_time": datetime.now().astimezone().isoformat(),
        "config": {"max_steps": 10},
    }
    with open(temp_dir / "metadata.json", "w") as f:
        json.dump(metadata, f)

    # Create task.json
    task_data = {
        "task_id": "task_001",
        "instruction": "Test task instruction",
        "domain": "test",
        "difficulty": "easy",
    }
    with open(task_001_dir / "task.json", "w") as f:
        json.dump(task_data, f)

    # Create execution.json
    execution_data = {
        "task_id": "task_001",
        "start_time": datetime.now().astimezone().isoformat(),
        "steps": [
            {
                "action_type": "click",
                "action_details": {"x": 100, "y": 200},
                "reasoning": "Test reasoning",
            }
        ],
        "success": True,
    }
    with open(task_001_dir / "execution.json", "w") as f:
        json.dump(execution_data, f)

    return temp_dir
