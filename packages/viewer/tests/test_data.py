"""Tests for data models and data loading functionality."""

from datetime import datetime

from openadapt_viewer.core.data_loader import DataLoader
from openadapt_viewer.core.types import (
    BenchmarkRun,
    BenchmarkTask,
    ExecutionStep,
    TaskExecution,
)
from openadapt_viewer.viewers.benchmark.data import create_sample_data, load_benchmark_data


class TestExecutionStep:
    """Tests for the ExecutionStep model."""

    def test_execution_step_creation(self):
        """Test creating an ExecutionStep with all fields."""
        step = ExecutionStep(
            step_number=0,
            timestamp=datetime.now().astimezone(),
            screenshot_path="path/to/screenshot.png",
            action_type="click",
            action_details={"x": 100, "y": 200},
            reasoning="Test reasoning",
            raw_output="Raw output text",
        )
        assert step.step_number == 0
        assert step.action_type == "click"
        assert step.action_details == {"x": 100, "y": 200}
        assert step.reasoning == "Test reasoning"
        assert step.raw_output == "Raw output text"

    def test_execution_step_minimal(self):
        """Test creating an ExecutionStep with minimal required fields."""
        step = ExecutionStep(
            step_number=1,
            action_type="type",
        )
        assert step.step_number == 1
        assert step.action_type == "type"
        assert step.timestamp is None
        assert step.screenshot_path is None
        assert step.action_details == {}
        assert step.reasoning is None
        assert step.raw_output is None

    def test_execution_step_from_fixture(self, sample_execution_step):
        """Test that the fixture creates a valid ExecutionStep."""
        assert sample_execution_step.step_number == 0
        assert sample_execution_step.action_type == "click"
        assert "x" in sample_execution_step.action_details
        assert "y" in sample_execution_step.action_details


class TestTaskExecution:
    """Tests for the TaskExecution model."""

    def test_task_execution_creation(self, sample_task_execution):
        """Test creating a TaskExecution."""
        assert sample_task_execution.task_id == "task_001"
        assert sample_task_execution.success is True
        assert sample_task_execution.error is None
        assert len(sample_task_execution.steps) == 3

    def test_task_execution_with_failure(self, failed_task_execution):
        """Test a failed TaskExecution."""
        assert failed_task_execution.task_id == "task_002"
        assert failed_task_execution.success is False
        assert failed_task_execution.error is not None
        assert "Could not locate target element" in failed_task_execution.error

    def test_task_execution_empty_steps(self):
        """Test TaskExecution with no steps."""
        execution = TaskExecution(
            task_id="empty_task",
            success=False,
        )
        assert execution.steps == []
        assert execution.start_time is None
        assert execution.end_time is None


class TestBenchmarkTask:
    """Tests for the BenchmarkTask model."""

    def test_benchmark_task_creation(self, sample_benchmark_task):
        """Test creating a BenchmarkTask."""
        assert sample_benchmark_task.task_id == "task_001"
        assert sample_benchmark_task.instruction == "Open Notepad and type 'Hello World'"
        assert sample_benchmark_task.domain == "office"
        assert sample_benchmark_task.difficulty == "easy"
        assert sample_benchmark_task.time_limit == 300
        assert sample_benchmark_task.metadata == {"source": "test"}

    def test_benchmark_task_minimal(self):
        """Test BenchmarkTask with minimal required fields."""
        task = BenchmarkTask(
            task_id="minimal_task",
            instruction="Do something",
        )
        assert task.task_id == "minimal_task"
        assert task.instruction == "Do something"
        assert task.domain is None
        assert task.difficulty is None
        assert task.time_limit is None
        assert task.metadata == {}


class TestBenchmarkRun:
    """Tests for the BenchmarkRun model."""

    def test_benchmark_run_creation(self, sample_benchmark_run):
        """Test creating a BenchmarkRun."""
        assert sample_benchmark_run.run_id == "test_run_001"
        assert sample_benchmark_run.benchmark_name == "Test Benchmark"
        assert sample_benchmark_run.model_id == "test-model-v1"
        assert len(sample_benchmark_run.tasks) == 3
        assert len(sample_benchmark_run.executions) == 3

    def test_benchmark_run_success_rate(self, sample_benchmark_run):
        """Test success rate calculation."""
        # 2 successes out of 3 = ~66.7%
        success_rate = sample_benchmark_run.success_rate
        assert 0.66 <= success_rate <= 0.67

    def test_benchmark_run_total_tasks(self, sample_benchmark_run):
        """Test total tasks property."""
        assert sample_benchmark_run.total_tasks == 3

    def test_benchmark_run_passed_tasks(self, sample_benchmark_run):
        """Test passed tasks property."""
        assert sample_benchmark_run.passed_tasks == 2

    def test_benchmark_run_failed_tasks(self, sample_benchmark_run):
        """Test failed tasks property."""
        assert sample_benchmark_run.failed_tasks == 1

    def test_benchmark_run_domain_stats(self, sample_benchmark_run):
        """Test domain statistics calculation."""
        stats = sample_benchmark_run.get_domain_stats()

        # We have office (passed), browser (failed), file_management (passed)
        assert "office" in stats
        assert "browser" in stats
        assert "file_management" in stats

        assert stats["office"]["passed"] == 1
        assert stats["office"]["failed"] == 0
        assert stats["office"]["total"] == 1

        assert stats["browser"]["passed"] == 0
        assert stats["browser"]["failed"] == 1
        assert stats["browser"]["total"] == 1

        assert stats["file_management"]["passed"] == 1
        assert stats["file_management"]["failed"] == 0
        assert stats["file_management"]["total"] == 1

    def test_benchmark_run_empty(self):
        """Test BenchmarkRun with no tasks/executions."""
        run = BenchmarkRun(
            run_id="empty_run",
            benchmark_name="Empty",
            model_id="none",
        )
        assert run.total_tasks == 0
        assert run.passed_tasks == 0
        assert run.failed_tasks == 0
        assert run.success_rate == 0.0
        assert run.get_domain_stats() == {}


class TestDataLoader:
    """Tests for the DataLoader class."""

    def test_parse_datetime_iso(self):
        """Test parsing ISO format datetime."""
        dt = DataLoader.parse_datetime("2024-01-15T10:30:00")
        assert dt is not None
        assert dt.year == 2024
        assert dt.month == 1
        assert dt.day == 15
        assert dt.hour == 10
        assert dt.minute == 30

    def test_parse_datetime_with_z(self):
        """Test parsing ISO datetime with Z suffix."""
        dt = DataLoader.parse_datetime("2024-01-15T10:30:00Z")
        assert dt is not None
        assert dt.year == 2024

    def test_parse_datetime_timestamp(self):
        """Test parsing Unix timestamp."""
        # Use a specific timestamp
        timestamp = 1704067200  # 2024-01-01 00:00:00 UTC
        dt = DataLoader.parse_datetime(timestamp)
        assert dt is not None

    def test_parse_datetime_none(self):
        """Test parsing None returns None."""
        assert DataLoader.parse_datetime(None) is None

    def test_parse_datetime_invalid(self):
        """Test parsing invalid string returns None."""
        assert DataLoader.parse_datetime("not a date") is None

    def test_parse_datetime_object(self):
        """Test parsing datetime object returns the same object."""
        original = datetime.now().astimezone()
        result = DataLoader.parse_datetime(original)
        assert result == original

    def test_load_benchmark_run_from_directory(self, benchmark_data_dir):
        """Test loading a BenchmarkRun from a directory."""
        run = DataLoader.load_benchmark_run(benchmark_data_dir)

        assert run.run_id == "test_run"
        assert run.benchmark_name == "Test Benchmark"
        assert run.model_id == "test-model"
        assert len(run.tasks) == 1
        assert len(run.executions) == 1
        assert run.tasks[0].task_id == "task_001"
        assert run.executions[0].success is True

    def test_load_benchmark_run_missing_metadata(self, temp_dir):
        """Test loading from directory without metadata.json."""
        run = DataLoader.load_benchmark_run(temp_dir)
        # Should use directory name as run_id
        assert run.run_id == temp_dir.name
        assert run.benchmark_name == "unknown"
        assert run.model_id == "unknown"


class TestCreateSampleData:
    """Tests for the create_sample_data function."""

    def test_create_sample_data_default(self):
        """Test creating sample data with default parameters."""
        run = create_sample_data()
        assert run.run_id == "sample_run_001"
        assert run.benchmark_name == "Sample Benchmark"
        assert run.model_id == "sample-agent-v1"
        assert len(run.tasks) == 10  # Default is 10 tasks
        assert len(run.executions) == 10

    def test_create_sample_data_custom_count(self):
        """Test creating sample data with custom task count."""
        run = create_sample_data(num_tasks=5)
        assert len(run.tasks) == 5
        assert len(run.executions) == 5

    def test_create_sample_data_structure(self):
        """Test that sample data has proper structure."""
        run = create_sample_data(num_tasks=3)

        for task in run.tasks:
            assert task.task_id is not None
            assert task.instruction is not None
            assert task.domain is not None
            assert task.difficulty in ["easy", "medium", "hard"]

        for execution in run.executions:
            assert execution.task_id is not None
            assert len(execution.steps) >= 3
            assert isinstance(execution.success, bool)

    def test_create_sample_data_task_execution_match(self):
        """Test that tasks and executions have matching IDs."""
        run = create_sample_data(num_tasks=5)

        task_ids = {task.task_id for task in run.tasks}
        execution_ids = {execution.task_id for execution in run.executions}

        assert task_ids == execution_ids

    def test_create_sample_data_domains(self):
        """Test that sample data uses expected domains."""
        run = create_sample_data(num_tasks=50)  # Larger sample to ensure coverage

        expected_domains = {"office", "browser", "system", "file_management", "communication"}
        actual_domains = {task.domain for task in run.tasks}

        # At least some domains should be represented
        assert len(actual_domains & expected_domains) > 0

    def test_create_sample_data_steps_have_details(self):
        """Test that execution steps have proper action details."""
        run = create_sample_data(num_tasks=1)

        for execution in run.executions:
            for step in execution.steps:
                assert step.action_type in ["click", "type", "scroll", "wait"]
                assert step.reasoning is not None
                assert step.step_number >= 0


class TestLoadBenchmarkData:
    """Tests for the load_benchmark_data function."""

    def test_load_benchmark_data(self, benchmark_data_dir):
        """Test the wrapper function for loading benchmark data."""
        run = load_benchmark_data(benchmark_data_dir)
        assert run is not None
        assert run.run_id == "test_run"
        assert len(run.tasks) == 1
