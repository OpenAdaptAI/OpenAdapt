"""Tests for WAAMockAdapter."""

import pytest

from openadapt_evals.adapters import (
    BenchmarkAction,
    BenchmarkObservation,
    BenchmarkResult,
    WAAMockAdapter,
)
from openadapt_evals.adapters.waa import WAA_DOMAINS


class TestWAAMockAdapterInit:
    """Tests for WAAMockAdapter initialization."""

    def test_default_init(self):
        """Test default initialization creates expected number of tasks."""
        adapter = WAAMockAdapter()
        tasks = adapter.list_tasks()
        assert len(tasks) == 20  # Default num_tasks

    def test_custom_num_tasks(self):
        """Test initialization with custom number of tasks."""
        adapter = WAAMockAdapter(num_tasks=5)
        tasks = adapter.list_tasks()
        assert len(tasks) == 5

    def test_custom_domains(self):
        """Test initialization with custom domains."""
        adapter = WAAMockAdapter(num_tasks=10, domains=["browser", "office"])
        tasks = adapter.list_tasks()
        domains = {t.domain for t in tasks}
        assert domains == {"browser", "office"}

    def test_name_property(self, mock_adapter):
        """Test name property returns expected value."""
        assert mock_adapter.name == "waa-mock"

    def test_benchmark_type_property(self, mock_adapter):
        """Test benchmark_type property returns 'interactive'."""
        assert mock_adapter.benchmark_type == "interactive"


class TestWAAMockAdapterTaskGeneration:
    """Tests for task generation in WAAMockAdapter."""

    def test_task_ids_are_unique(self, mock_adapter):
        """Test that all generated task IDs are unique."""
        tasks = mock_adapter.list_tasks()
        task_ids = [t.task_id for t in tasks]
        assert len(task_ids) == len(set(task_ids))

    def test_task_has_required_fields(self, mock_adapter):
        """Test that generated tasks have required fields."""
        tasks = mock_adapter.list_tasks()
        for task in tasks:
            assert task.task_id is not None
            assert task.instruction is not None
            assert task.domain is not None
            assert task.time_limit_steps is not None

    def test_list_tasks_by_domain(self, mock_adapter):
        """Test filtering tasks by domain."""
        browser_tasks = mock_adapter.list_tasks(domain="browser")
        for task in browser_tasks:
            assert task.domain == "browser"

    def test_load_task_by_id(self, mock_adapter):
        """Test loading a specific task by ID."""
        tasks = mock_adapter.list_tasks()
        task_id = tasks[0].task_id
        loaded_task = mock_adapter.load_task(task_id)
        assert loaded_task.task_id == task_id

    def test_load_nonexistent_task_raises(self, mock_adapter):
        """Test that loading nonexistent task raises KeyError."""
        with pytest.raises(KeyError):
            mock_adapter.load_task("nonexistent_task_id")

    def test_task_instruction_format(self, mock_adapter):
        """Test that task instructions follow expected format."""
        tasks = mock_adapter.list_tasks()
        for task in tasks:
            assert "Mock task" in task.instruction
            assert task.domain in task.instruction

    def test_raw_config_has_mock_flag(self, mock_adapter):
        """Test that raw_config contains mock flag."""
        tasks = mock_adapter.list_tasks()
        for task in tasks:
            assert task.raw_config.get("mock") is True


class TestWAAMockAdapterReset:
    """Tests for WAAMockAdapter reset functionality."""

    def test_reset_returns_observation(self, mock_adapter):
        """Test that reset returns a BenchmarkObservation."""
        task = mock_adapter.list_tasks()[0]
        obs = mock_adapter.reset(task)
        assert isinstance(obs, BenchmarkObservation)

    def test_reset_sets_current_task(self, mock_adapter):
        """Test that reset sets the current task."""
        task = mock_adapter.list_tasks()[0]
        mock_adapter.reset(task)
        assert mock_adapter._current_task == task

    def test_reset_resets_step_count(self, mock_adapter):
        """Test that reset resets the step count."""
        task = mock_adapter.list_tasks()[0]
        mock_adapter.reset(task)
        # Take a step
        action = BenchmarkAction(type="click", x=0.5, y=0.5)
        mock_adapter.step(action)
        assert mock_adapter._step_count == 1
        # Reset should clear step count
        mock_adapter.reset(task)
        assert mock_adapter._step_count == 0

    def test_reset_clears_actions(self, mock_adapter):
        """Test that reset clears the action history."""
        task = mock_adapter.list_tasks()[0]
        mock_adapter.reset(task)
        # Take a step
        action = BenchmarkAction(type="click", x=0.5, y=0.5)
        mock_adapter.step(action)
        assert len(mock_adapter._actions) == 1
        # Reset should clear actions
        mock_adapter.reset(task)
        assert len(mock_adapter._actions) == 0


class TestWAAMockAdapterObservation:
    """Tests for observation generation in WAAMockAdapter."""

    def test_observation_has_viewport(self, mock_adapter):
        """Test that observations have viewport information."""
        task = mock_adapter.list_tasks()[0]
        obs = mock_adapter.reset(task)
        assert obs.viewport == (1920, 1200)

    def test_observation_has_screenshot(self, mock_adapter):
        """Test that observations include screenshot data."""
        task = mock_adapter.list_tasks()[0]
        obs = mock_adapter.reset(task)
        assert obs.screenshot is not None
        assert isinstance(obs.screenshot, bytes)

    def test_observation_has_screenshot_path(self, mock_adapter):
        """Test that observations include screenshot path."""
        task = mock_adapter.list_tasks()[0]
        obs = mock_adapter.reset(task)
        assert obs.screenshot_path is not None

    def test_observation_has_accessibility_tree(self, mock_adapter):
        """Test that observations include accessibility tree."""
        task = mock_adapter.list_tasks()[0]
        obs = mock_adapter.reset(task)
        assert obs.accessibility_tree is not None
        assert "role" in obs.accessibility_tree
        assert "children" in obs.accessibility_tree

    def test_observation_has_window_title(self, mock_adapter):
        """Test that observations include window title."""
        task = mock_adapter.list_tasks()[0]
        obs = mock_adapter.reset(task)
        assert obs.window_title is not None

    def test_accessibility_tree_has_expected_elements(self, mock_adapter):
        """Test that accessibility tree contains expected UI elements."""
        task = mock_adapter.list_tasks()[0]
        obs = mock_adapter.reset(task)
        tree = obs.accessibility_tree
        children = tree.get("children", [])

        # Should have OK, Input, Cancel, Submit elements
        element_names = {child.get("name") for child in children}
        assert "OK" in element_names
        assert "Submit" in element_names


class TestWAAMockAdapterStep:
    """Tests for step execution in WAAMockAdapter."""

    def test_step_increments_counter(self, mock_adapter):
        """Test that step increments the step counter."""
        task = mock_adapter.list_tasks()[0]
        mock_adapter.reset(task)
        action = BenchmarkAction(type="click", x=0.5, y=0.5)
        mock_adapter.step(action)
        assert mock_adapter._step_count == 1

    def test_step_returns_observation(self, mock_adapter):
        """Test that step returns a new observation."""
        task = mock_adapter.list_tasks()[0]
        mock_adapter.reset(task)
        action = BenchmarkAction(type="click", x=0.5, y=0.5)
        obs, done, info = mock_adapter.step(action)
        assert isinstance(obs, BenchmarkObservation)

    def test_step_returns_done_on_done_action(self, mock_adapter):
        """Test that step returns done=True on done action."""
        task = mock_adapter.list_tasks()[0]
        mock_adapter.reset(task)
        action = BenchmarkAction(type="done")
        obs, done, info = mock_adapter.step(action)
        assert done is True

    def test_step_returns_done_after_max_steps(self, mock_adapter):
        """Test that step returns done=True after max steps."""
        task = mock_adapter.list_tasks()[0]
        mock_adapter.reset(task)
        action = BenchmarkAction(type="click", x=0.5, y=0.5)

        # Take 14 steps
        for _ in range(14):
            obs, done, info = mock_adapter.step(action)

        # 15th step should trigger done
        obs, done, info = mock_adapter.step(action)
        assert done is True

    def test_step_tracks_actions(self, mock_adapter):
        """Test that step tracks actions for evaluation."""
        task = mock_adapter.list_tasks()[0]
        mock_adapter.reset(task)

        click_action = BenchmarkAction(type="click", target_node_id="4")
        type_action = BenchmarkAction(type="type", text="hello")

        mock_adapter.step(click_action)
        mock_adapter.step(type_action)

        assert len(mock_adapter._actions) == 2
        assert mock_adapter._actions[0].type == "click"
        assert mock_adapter._actions[1].type == "type"

    def test_step_returns_info_with_step_count(self, mock_adapter):
        """Test that step returns info dict with step count."""
        task = mock_adapter.list_tasks()[0]
        mock_adapter.reset(task)
        action = BenchmarkAction(type="click", x=0.5, y=0.5)
        obs, done, info = mock_adapter.step(action)
        assert "step" in info
        assert info["step"] == 1


class TestWAAMockAdapterEvaluation:
    """Tests for evaluation in WAAMockAdapter."""

    def test_evaluate_returns_result(self, mock_adapter):
        """Test that evaluate returns a BenchmarkResult."""
        task = mock_adapter.list_tasks()[0]
        mock_adapter.reset(task)
        result = mock_adapter.evaluate(task)
        assert isinstance(result, BenchmarkResult)

    def test_evaluate_success_on_submit_click(self, mock_adapter):
        """Test that clicking Submit (ID 4) results in success."""
        task = mock_adapter.list_tasks()[0]
        mock_adapter.reset(task)

        # Click Submit button (ID 4)
        action = BenchmarkAction(type="click", target_node_id="4")
        mock_adapter.step(action)

        result = mock_adapter.evaluate(task)
        assert result.success is True
        assert result.score == 1.0

    def test_evaluate_success_on_type_and_ok(self, mock_adapter):
        """Test that typing + clicking OK (ID 1) results in success."""
        task = mock_adapter.list_tasks()[0]
        mock_adapter.reset(task)

        # Type some text
        type_action = BenchmarkAction(type="type", text="test input")
        mock_adapter.step(type_action)

        # Click OK button (ID 1)
        click_action = BenchmarkAction(type="click", target_node_id="1")
        mock_adapter.step(click_action)

        result = mock_adapter.evaluate(task)
        assert result.success is True
        assert result.score == 1.0

    def test_evaluate_no_false_positive_on_done_with_actions(self, mock_adapter):
        """Test that calling DONE after non-target actions does NOT count as success."""
        task = mock_adapter.list_tasks()[0]
        mock_adapter.reset(task)

        # Click random area (not Submit/OK) and call done
        mock_adapter.step(BenchmarkAction(type="click", x=0.5, y=0.5))
        mock_adapter.step(BenchmarkAction(type="done"))

        result = mock_adapter.evaluate(task)
        assert result.success is False
        assert result.score < 1.0

    def test_evaluate_partial_score_on_some_actions(self, mock_adapter):
        """Test that partial actions get partial credit."""
        task = mock_adapter.list_tasks()[0]
        mock_adapter.reset(task)

        # Just type something without submitting
        type_action = BenchmarkAction(type="type", text="partial")
        mock_adapter.step(type_action)

        result = mock_adapter.evaluate(task)
        # Should get partial credit but not full success
        assert result.score > 0.0
        assert result.score < 1.0

    def test_evaluate_includes_task_id(self, mock_adapter):
        """Test that result includes correct task_id."""
        task = mock_adapter.list_tasks()[0]
        mock_adapter.reset(task)
        result = mock_adapter.evaluate(task)
        assert result.task_id == task.task_id

    def test_evaluate_includes_step_count(self, mock_adapter):
        """Test that result includes step count."""
        task = mock_adapter.list_tasks()[0]
        mock_adapter.reset(task)

        mock_adapter.step(BenchmarkAction(type="click", x=0.5, y=0.5))
        mock_adapter.step(BenchmarkAction(type="click", x=0.6, y=0.6))

        result = mock_adapter.evaluate(task)
        assert result.num_steps == 2


class TestWAADomains:
    """Tests for WAA domain constants."""

    def test_waa_domains_list(self):
        """Test that WAA_DOMAINS contains expected domains."""
        expected_domains = [
            "chrome",
            "clock",
            "file_explorer",
            "libreoffice_calc",
            "libreoffice_writer",
            "microsoft_paint",
            "msedge",
            "notepad",
            "settings",
            "vlc",
            "vs_code",
            "windows_calc",
        ]
        assert WAA_DOMAINS == expected_domains

    def test_waa_domains_count(self):
        """Test that WAA_DOMAINS has 12 domains."""
        assert len(WAA_DOMAINS) == 12


class TestWAAMockAdapterContextManager:
    """Tests for context manager functionality."""

    def test_context_manager_enter(self):
        """Test that context manager returns adapter on enter."""
        with WAAMockAdapter() as adapter:
            assert isinstance(adapter, WAAMockAdapter)

    def test_context_manager_exit(self):
        """Test that context manager exits cleanly."""
        adapter = WAAMockAdapter()
        with adapter:
            tasks = adapter.list_tasks()
            assert len(tasks) > 0
        # Should not raise on exit
