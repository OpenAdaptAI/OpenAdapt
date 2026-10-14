"""Tests for the evaluation runner."""

import json
from unittest.mock import Mock

import pytest

from openadapt_evals.adapters import (
    BenchmarkAction,
    BenchmarkResult,
    BenchmarkTask,
    WAAMockAdapter,
)
from openadapt_evals.agents import (
    BenchmarkAgent,
    RandomAgent,
    ScriptedAgent,
    SmartMockAgent,
    action_to_string,
    format_accessibility_tree,
    parse_action_response,
)
from openadapt_evals.benchmarks.data_collection import ExecutionTraceCollector
from openadapt_evals.benchmarks.runner import (
    EvaluationConfig,
    _evaluate_parallel,
    _evaluate_sequential,
    _run_single_task,
    compute_domain_metrics,
    compute_metrics,
    evaluate_agent_on_benchmark,
)
from openadapt_evals.errors import ActionParseError, RolloutInfrastructureError
from openadapt_evals.integrations.wandb_logger import WandbLogger


class TestEvaluationConfig:
    """Tests for EvaluationConfig dataclass."""

    def test_default_config(self):
        """Test default configuration values."""
        config = EvaluationConfig()
        assert config.max_steps == 50
        assert config.parallel == 1
        assert config.save_trajectories is True
        assert config.verbose is True
        assert config.save_execution_traces is True
        assert config.model_id == "unknown"
        assert config.output_dir == "benchmark_results"

    def test_custom_config(self):
        """Test custom configuration values."""
        config = EvaluationConfig(
            max_steps=100,
            parallel=4,
            save_trajectories=False,
            verbose=False,
            model_id="test-model",
        )
        assert config.max_steps == 100
        assert config.parallel == 4
        assert config.save_trajectories is False
        assert config.verbose is False
        assert config.model_id == "test-model"


class TestComputeMetrics:
    """Tests for compute_metrics function."""

    def test_empty_results(self):
        """Test compute_metrics with empty results."""
        metrics = compute_metrics([])
        assert metrics["num_tasks"] == 0
        assert metrics["success_rate"] == 0.0
        assert metrics["avg_score"] == 0.0
        assert metrics["avg_steps"] == 0.0
        assert metrics["avg_time_seconds"] == 0.0

    def test_all_success(self):
        """Test compute_metrics with all successful results."""
        results = [
            BenchmarkResult(task_id="1", success=True, score=1.0, num_steps=5, total_time_seconds=1.0),
            BenchmarkResult(task_id="2", success=True, score=1.0, num_steps=10, total_time_seconds=2.0),
        ]
        metrics = compute_metrics(results)
        assert metrics["num_tasks"] == 2
        assert metrics["success_rate"] == 1.0
        assert metrics["avg_score"] == 1.0
        assert metrics["avg_steps"] == 7.5
        assert metrics["avg_time_seconds"] == 1.5
        assert metrics["success_count"] == 2
        assert metrics["fail_count"] == 0

    def test_mixed_results(self):
        """Test compute_metrics with mixed success/failure."""
        results = [
            BenchmarkResult(task_id="1", success=True, score=1.0, num_steps=5, total_time_seconds=1.0),
            BenchmarkResult(task_id="2", success=False, score=0.0, num_steps=10, total_time_seconds=2.0),
            BenchmarkResult(task_id="3", success=False, score=0.5, num_steps=8, total_time_seconds=1.5),
        ]
        metrics = compute_metrics(results)
        assert metrics["num_tasks"] == 3
        assert metrics["success_rate"] == pytest.approx(1/3)
        assert metrics["avg_score"] == pytest.approx(0.5)
        assert metrics["avg_steps"] == pytest.approx(7.67, rel=0.01)
        assert metrics["success_count"] == 1
        assert metrics["fail_count"] == 2

    def test_all_failures(self):
        """Test compute_metrics with all failed results."""
        results = [
            BenchmarkResult(task_id="1", success=False, score=0.0, num_steps=15, total_time_seconds=5.0),
            BenchmarkResult(task_id="2", success=False, score=0.2, num_steps=15, total_time_seconds=5.0),
        ]
        metrics = compute_metrics(results)
        assert metrics["success_rate"] == 0.0
        assert metrics["avg_score"] == 0.1

    def test_unavailable_attempts_do_not_enter_outcome_rates(self):
        results = [
            BenchmarkResult(task_id="pass", success=True, score=1.0),
            BenchmarkResult(task_id="fail", success=False, score=0.0),
            BenchmarkResult(
                task_id="infra",
                success=True,
                score=1.0,
                error_type="infrastructure",
            ),
            BenchmarkResult(
                task_id="evaluation",
                success=False,
                score=0.0,
                error_type="evaluation",
            ),
            BenchmarkResult(
                task_id="agent",
                success=False,
                score=0.0,
                error_type="agent",
            ),
        ]

        metrics = compute_metrics(results)

        assert metrics["num_tasks"] == 5
        assert metrics["num_outcome_tasks"] == 3
        assert metrics["error_count"] == 3
        assert metrics["success_rate"] == pytest.approx(1 / 3)
        assert metrics["avg_score"] == pytest.approx(1 / 3)
        assert metrics["success_count"] == 1
        assert metrics["fail_count"] == 2
        assert metrics["num_infrastructure_failures"] == 1
        assert metrics["num_evaluation_failures"] == 1
        assert metrics["num_agent_failures"] == 1

    def test_agent_failures_reduce_the_headline_success_rate(self):
        results = [BenchmarkResult(task_id="pass", success=True, score=1.0)]
        results.extend(
            BenchmarkResult(
                task_id=f"agent-{index}",
                success=False,
                score=0.0,
                error_type="agent",
            )
            for index in range(99)
        )

        metrics = compute_metrics(results)

        assert metrics["num_outcome_tasks"] == 100
        assert metrics["success_rate"] == 0.01

    def test_trace_summary_uses_the_same_outcome_denominator(self, tmp_path):
        collector = ExecutionTraceCollector(
            benchmark_name="test",
            run_name="run",
            output_dir=tmp_path,
            capture_logs=False,
        )
        collector.save_summary(
            [
                BenchmarkResult(task_id="measured", success=True, score=1.0),
                BenchmarkResult(
                    task_id="unavailable",
                    success=False,
                    score=0.0,
                    error_type="evaluation",
                ),
            ]
        )

        summary = json.loads((collector.run_dir / "summary.json").read_text())
        assert summary["num_tasks"] == 2
        assert summary["num_outcome_tasks"] == 1
        assert summary["error_count"] == 1
        assert summary["num_success"] == 1
        assert summary["success_rate"] == 1.0

    def test_wandb_metrics_use_the_same_outcome_denominator(self):
        results = [
            BenchmarkResult(task_id="measured", success=True, score=1.0),
            BenchmarkResult(
                task_id="unavailable",
                success=False,
                score=0.0,
                error_type="infrastructure",
            ),
        ]

        metrics = WandbLogger._compute_metrics(object(), results)

        assert metrics["num_tasks"] == 2
        assert metrics["num_outcome_tasks"] == 1
        assert metrics["error_count"] == 1
        assert metrics["num_success"] == 1
        assert metrics["success_rate"] == 1.0


class TestComputeDomainMetrics:
    """Tests for compute_domain_metrics function."""

    def test_single_domain(self):
        """Test compute_domain_metrics with single domain."""
        tasks = [
            BenchmarkTask(task_id="1", instruction="test", domain="browser"),
            BenchmarkTask(task_id="2", instruction="test", domain="browser"),
        ]
        results = [
            BenchmarkResult(task_id="1", success=True, score=1.0, num_steps=5, total_time_seconds=1.0),
            BenchmarkResult(task_id="2", success=False, score=0.0, num_steps=10, total_time_seconds=2.0),
        ]
        metrics = compute_domain_metrics(results, tasks)
        assert "browser" in metrics
        assert metrics["browser"]["num_tasks"] == 2
        assert metrics["browser"]["success_rate"] == 0.5

    def test_multiple_domains(self):
        """Test compute_domain_metrics with multiple domains."""
        tasks = [
            BenchmarkTask(task_id="1", instruction="test", domain="browser"),
            BenchmarkTask(task_id="2", instruction="test", domain="office"),
            BenchmarkTask(task_id="3", instruction="test", domain="office"),
        ]
        results = [
            BenchmarkResult(task_id="1", success=True, score=1.0, num_steps=5, total_time_seconds=1.0),
            BenchmarkResult(task_id="2", success=True, score=1.0, num_steps=10, total_time_seconds=2.0),
            BenchmarkResult(task_id="3", success=False, score=0.0, num_steps=8, total_time_seconds=1.5),
        ]
        metrics = compute_domain_metrics(results, tasks)
        assert len(metrics) == 2
        assert metrics["browser"]["success_rate"] == 1.0
        assert metrics["office"]["success_rate"] == 0.5


class TestScriptedAgent:
    """Tests for ScriptedAgent."""

    def test_follows_script(self, sample_observation, sample_task):
        """Test that agent follows predefined script."""
        actions = [
            BenchmarkAction(type="click", x=0.5, y=0.5),
            BenchmarkAction(type="type", text="hello"),
            BenchmarkAction(type="done"),
        ]
        agent = ScriptedAgent(actions)

        # Should return actions in order
        action1 = agent.act(sample_observation, sample_task)
        assert action1.type == "click"

        action2 = agent.act(sample_observation, sample_task)
        assert action2.type == "type"

        action3 = agent.act(sample_observation, sample_task)
        assert action3.type == "done"

    def test_returns_done_after_script(self, sample_observation, sample_task):
        """Test that agent returns done after script exhausted."""
        actions = [BenchmarkAction(type="click", x=0.5, y=0.5)]
        agent = ScriptedAgent(actions)

        agent.act(sample_observation, sample_task)  # Consume the action
        action = agent.act(sample_observation, sample_task)
        assert action.type == "done"

    def test_reset_restarts_script(self, sample_observation, sample_task):
        """Test that reset restarts the script."""
        actions = [
            BenchmarkAction(type="click", x=0.5, y=0.5),
            BenchmarkAction(type="done"),
        ]
        agent = ScriptedAgent(actions)

        agent.act(sample_observation, sample_task)
        agent.reset()

        action = agent.act(sample_observation, sample_task)
        assert action.type == "click"


class TestSmartMockAgent:
    """Tests for SmartMockAgent."""

    def test_clicks_submit_button(self, sample_observation, sample_task):
        """Test that SmartMockAgent clicks Submit button."""
        agent = SmartMockAgent()
        action = agent.act(sample_observation, sample_task)

        assert action.type == "click"
        assert action.target_node_id == "4"  # Submit button ID

    def test_calls_done_after_click(self, sample_observation, sample_task):
        """Test that SmartMockAgent calls done after clicking."""
        agent = SmartMockAgent()
        agent.act(sample_observation, sample_task)  # First action (click)
        action = agent.act(sample_observation, sample_task)

        assert action.type == "done"

    def test_reset_restarts_sequence(self, sample_observation, sample_task):
        """Test that reset restarts the action sequence."""
        agent = SmartMockAgent()
        agent.act(sample_observation, sample_task)  # First action
        agent.reset()
        action = agent.act(sample_observation, sample_task)

        assert action.type == "click"


class TestRandomAgent:
    """Tests for RandomAgent."""

    def test_returns_valid_actions(self, sample_observation, sample_task):
        """Test that RandomAgent returns valid actions."""
        agent = RandomAgent(seed=42)
        action = agent.act(sample_observation, sample_task)

        assert action.type in ["click", "type", "scroll", "done"]

    def test_deterministic_with_seed(self, sample_observation, sample_task):
        """Test that same seed produces same actions."""
        agent1 = RandomAgent(seed=42)
        agent2 = RandomAgent(seed=42)

        action1 = agent1.act(sample_observation, sample_task)
        action2 = agent2.act(sample_observation, sample_task)

        assert action1.type == action2.type

    def test_returns_done_after_many_steps(self, sample_observation, sample_task):
        """Test that RandomAgent returns done after many steps."""
        agent = RandomAgent(seed=42)
        # Simulate long history
        long_history = [(sample_observation, BenchmarkAction(type="click"))] * 25
        action = agent.act(sample_observation, sample_task, history=long_history)

        assert action.type == "done"


class TestFormatAccessibilityTree:
    """Tests for format_accessibility_tree function."""

    def test_simple_tree(self):
        """Test formatting a simple accessibility tree."""
        tree = {"role": "button", "name": "Submit", "id": "1"}
        result = format_accessibility_tree(tree)
        assert "[1] button: Submit" in result

    def test_nested_tree(self):
        """Test formatting a nested accessibility tree."""
        tree = {
            "role": "window",
            "name": "App",
            "id": "root",
            "children": [
                {"role": "button", "name": "OK", "id": "1"},
                {"role": "textfield", "name": "Input", "id": "2"},
            ],
        }
        result = format_accessibility_tree(tree)
        assert "[root] window: App" in result
        assert "[1] button: OK" in result
        assert "[2] textfield: Input" in result


class TestActionToString:
    """Tests for action_to_string function."""

    def test_click_with_node_id(self):
        """Test string representation of click with node ID."""
        action = BenchmarkAction(type="click", target_node_id="4")
        result = action_to_string(action)
        assert result == "CLICK([4])"

    def test_click_with_coords(self):
        """Test string representation of click with coordinates."""
        action = BenchmarkAction(type="click", x=0.5, y=0.3)
        result = action_to_string(action)
        assert result == "CLICK(0.500, 0.300)"

    def test_type_action(self):
        """Test string representation of type action."""
        action = BenchmarkAction(type="type", text="hello")
        result = action_to_string(action)
        assert result == "TYPE('hello')"

    def test_key_action(self):
        """Test string representation of key action."""
        action = BenchmarkAction(type="key", key="Enter")
        result = action_to_string(action)
        assert result == "KEY(Enter)"

    def test_key_with_modifiers(self):
        """Test string representation of key with modifiers."""
        action = BenchmarkAction(type="key", key="c", modifiers=["ctrl"])
        result = action_to_string(action)
        assert result == "KEY(ctrl+c)"

    def test_scroll_action(self):
        """Test string representation of scroll action."""
        action = BenchmarkAction(type="scroll", scroll_direction="down")
        result = action_to_string(action)
        assert result == "SCROLL(down)"

    def test_done_action(self):
        """Test string representation of done action."""
        action = BenchmarkAction(type="done")
        result = action_to_string(action)
        assert result == "DONE()"


class TestParseActionResponse:
    """Tests for parse_action_response function."""

    def test_parse_click_with_id(self):
        """Test parsing CLICK([id]) response."""
        response = "ACTION: CLICK([4])"
        action = parse_action_response(response)
        assert action.type == "click"
        assert action.target_node_id == "4"

    def test_parse_click_with_coords(self):
        """Test parsing CLICK(x, y) response."""
        response = "ACTION: CLICK(0.5, 0.3)"
        action = parse_action_response(response)
        assert action.type == "click"
        assert action.x == 0.5
        assert action.y == 0.3

    def test_parse_type(self):
        """Test parsing TYPE response."""
        response = "ACTION: TYPE('hello world')"
        action = parse_action_response(response)
        assert action.type == "type"
        assert action.text == "hello world"

    def test_parse_key(self):
        """Test parsing KEY response."""
        response = "ACTION: KEY(Enter)"
        action = parse_action_response(response)
        assert action.type == "key"
        assert action.key == "Enter"

    def test_parse_key_with_modifiers(self):
        """Test parsing KEY with modifiers."""
        response = "ACTION: KEY(ctrl+c)"
        action = parse_action_response(response)
        assert action.type == "key"
        assert action.key == "c"
        assert action.modifiers == ["ctrl"]

    def test_parse_scroll(self):
        """Test parsing SCROLL response."""
        response = "ACTION: SCROLL(down)"
        action = parse_action_response(response)
        assert action.type == "scroll"
        assert action.scroll_direction == "down"

    def test_parse_done(self):
        """Test parsing DONE response."""
        response = "ACTION: DONE()"
        action = parse_action_response(response)
        assert action.type == "done"

    def test_parse_without_action_prefix(self):
        """Test parsing response without ACTION: prefix."""
        response = "I'll click the button now. CLICK([1])"
        action = parse_action_response(response)
        assert action.type == "click"
        assert action.target_node_id == "1"

    def test_parse_invalid_returns_error(self):
        """An invalid response is not a completion decision."""
        response = "I don't know what to do"
        action = parse_action_response(response)
        assert action.type == "error"

    def test_coordinate_normalization(self, sample_observation):
        """Test that pixel coordinates are normalized."""
        response = "ACTION: CLICK(960, 600)"
        action = parse_action_response(response, sample_observation)
        assert action.type == "click"
        # Coordinates should be normalized to [0,1]
        assert action.x == 0.5  # 960 / 1920
        assert action.y == 0.5  # 600 / 1200


class TestEvaluateAgentOnBenchmark:
    """Integration tests for evaluate_agent_on_benchmark."""

    def test_runs_all_tasks(self):
        """Test that evaluation runs all tasks."""
        adapter = WAAMockAdapter(num_tasks=3, domains=["browser"])
        agent = SmartMockAgent()

        config = EvaluationConfig(
            verbose=False,
            save_execution_traces=False,
            enable_live_tracking=False,
        )
        results = evaluate_agent_on_benchmark(agent, adapter, config=config)

        assert len(results) == 3

    def test_runs_specific_tasks(self):
        """Test that evaluation runs only specified tasks."""
        adapter = WAAMockAdapter(num_tasks=5, domains=["browser"])
        agent = SmartMockAgent()
        tasks = adapter.list_tasks()
        task_ids = [tasks[0].task_id, tasks[1].task_id]

        config = EvaluationConfig(
            verbose=False,
            save_execution_traces=False,
            enable_live_tracking=False,
        )
        results = evaluate_agent_on_benchmark(
            agent, adapter, task_ids=task_ids, config=config
        )

        assert len(results) == 2

    def test_smart_agent_succeeds(self):
        """Test that SmartMockAgent achieves success."""
        adapter = WAAMockAdapter(num_tasks=2, domains=["browser"])
        agent = SmartMockAgent()

        config = EvaluationConfig(
            verbose=False,
            save_execution_traces=False,
            enable_live_tracking=False,
        )
        results = evaluate_agent_on_benchmark(agent, adapter, config=config)

        # SmartMockAgent should succeed on all mock tasks
        success_count = sum(1 for r in results if r.success)
        assert success_count == len(results)

    def test_respects_max_steps(self):
        """Test that evaluation respects max_steps limit.

        Note: task.time_limit_steps takes precedence over config.max_steps.
        The mock adapter sets time_limit_steps=15, so the limit is 15 steps.
        """
        adapter = WAAMockAdapter(num_tasks=1, domains=["browser"])
        # Agent that never calls done
        agent = ScriptedAgent([BenchmarkAction(type="click", x=0.5, y=0.5)] * 100)

        config = EvaluationConfig(
            max_steps=5,
            verbose=False,
            save_execution_traces=False,
            enable_live_tracking=False,
        )
        results = evaluate_agent_on_benchmark(agent, adapter, config=config)

        # Task's time_limit_steps (15) takes precedence over config.max_steps (5)
        # So we should stop at the task's limit
        task = adapter.list_tasks()[0]
        expected_max = task.time_limit_steps or config.max_steps
        assert results[0].num_steps <= expected_max

    def test_saves_trajectories_when_enabled(self):
        """Test that trajectories are saved when enabled."""
        adapter = WAAMockAdapter(num_tasks=1, domains=["browser"])
        agent = SmartMockAgent()

        config = EvaluationConfig(
            save_trajectories=True,
            verbose=False,
            save_execution_traces=False,
            enable_live_tracking=False,
        )
        results = evaluate_agent_on_benchmark(agent, adapter, config=config)

        # Should have trajectory steps
        assert len(results[0].steps) > 0

    def test_callbacks_are_called(self):
        """Test that callbacks are invoked."""
        adapter = WAAMockAdapter(num_tasks=1, domains=["browser"])
        agent = SmartMockAgent()

        step_callback = Mock()
        task_callback = Mock()

        config = EvaluationConfig(
            on_step=step_callback,
            on_task_complete=task_callback,
            verbose=False,
            save_execution_traces=False,
            enable_live_tracking=False,
        )
        evaluate_agent_on_benchmark(agent, adapter, config=config)

        # Callbacks should have been called
        assert step_callback.call_count > 0
        assert task_callback.call_count == 1


class TestRunSingleTask:
    """Tests for _run_single_task function."""

    def test_handles_agent_exception(self):
        """Test that task handles agent exceptions gracefully."""
        adapter = WAAMockAdapter(num_tasks=1, domains=["browser"])
        task = adapter.list_tasks()[0]

        # Create an agent that raises an exception
        agent = Mock(spec=BenchmarkAgent)
        agent.act.side_effect = ValueError("Test error")
        agent.reset = Mock()

        config = EvaluationConfig(
            verbose=False,
            save_execution_traces=False,
            enable_live_tracking=False,
        )

        result = _run_single_task(agent, adapter, task, config)

        assert result.success is False
        assert "Test error" in result.error

    def test_stops_on_done_action(self):
        """Test that task stops when agent returns done action."""
        adapter = WAAMockAdapter(num_tasks=1, domains=["browser"])
        task = adapter.list_tasks()[0]

        # Agent that immediately returns done
        agent = ScriptedAgent([BenchmarkAction(type="done")])

        config = EvaluationConfig(
            verbose=False,
            save_execution_traces=False,
            enable_live_tracking=False,
        )

        result = _run_single_task(agent, adapter, task, config)

        # Should have only taken 0 steps (done before first step execution)
        assert result.num_steps == 0

    @pytest.mark.parametrize("claimed_error_type", ["infrastructure", "evaluation"])
    def test_agent_error_claim_cannot_leave_outcome_denominator(
        self, claimed_error_type
    ):
        """An agent cannot classify its own terminal failure as unavailable."""
        adapter = WAAMockAdapter(num_tasks=1, domains=["browser"])
        task = adapter.list_tasks()[0]
        adapter.evaluate = Mock(
            return_value=BenchmarkResult(
                task_id=task.task_id,
                success=True,
                score=1.0,
            )
        )
        agent = ScriptedAgent(
            [
                BenchmarkAction(
                    type="error",
                    raw_action={
                        "error": "provider offline",
                        "error_type": claimed_error_type,
                    },
                )
            ]
        )
        config = EvaluationConfig(
            verbose=False,
            save_execution_traces=False,
            enable_live_tracking=False,
        )

        result = _run_single_task(agent, adapter, task, config)

        assert result.success is False
        assert result.score == 0.0
        assert result.error == "provider offline"
        assert result.reason == "provider offline"
        assert result.error_type == "agent"
        adapter.evaluate.assert_not_called()

        metrics = compute_metrics([result])
        assert metrics["num_tasks"] == 1
        assert metrics["num_outcome_tasks"] == 1
        assert metrics["success_count"] == 0
        assert metrics["fail_count"] == 1
        assert metrics["num_agent_failures"] == 1
        assert metrics["success_rate"] == 0.0

    def test_agent_error_without_diagnostics_cannot_report_success(self):
        """The terminal action type alone is sufficient to refuse success."""
        adapter = WAAMockAdapter(num_tasks=1, domains=["browser"])
        task = adapter.list_tasks()[0]
        adapter.evaluate = Mock(
            return_value=BenchmarkResult(
                task_id=task.task_id,
                success=True,
                score=1.0,
            )
        )
        agent = ScriptedAgent([BenchmarkAction(type="error")])
        config = EvaluationConfig(
            verbose=False,
            save_execution_traces=False,
            enable_live_tracking=False,
        )

        result = _run_single_task(agent, adapter, task, config)

        assert (result.success, result.score, result.error_type) == (
            False,
            0.0,
            "agent",
        )
        adapter.evaluate.assert_not_called()

    def test_agent_parse_exception_is_reported_as_agent_error(self):
        adapter = WAAMockAdapter(num_tasks=1, domains=["browser"])
        task = adapter.list_tasks()[0]
        agent = Mock(spec=BenchmarkAgent)
        agent.reset = Mock()
        agent.act.side_effect = ActionParseError("invalid model output")

        result = _run_single_task(
            agent,
            adapter,
            task,
            EvaluationConfig(
                verbose=False,
                save_execution_traces=False,
                enable_live_tracking=False,
            ),
        )

        assert result.success is False
        assert result.error_type == "agent"

    def test_done_gate_evaluation_exception_is_not_accepted_as_done(self):
        adapter = WAAMockAdapter(num_tasks=1, domains=["browser"])
        task = adapter.list_tasks()[0]
        adapter.evaluate = Mock(side_effect=RuntimeError("evaluator offline"))
        agent = ScriptedAgent([BenchmarkAction(type="done")])

        result = _run_single_task(
            agent,
            adapter,
            task,
            EvaluationConfig(
                done_gate=True,
                verbose=False,
                save_execution_traces=False,
                enable_live_tracking=False,
            ),
        )

        assert result.success is False
        assert result.error_type == "evaluation"
        assert "evaluator offline" in (result.error or "")
        assert adapter.evaluate.call_count == 1

    @pytest.mark.parametrize("error_type", ["infrastructure", "evaluation"])
    def test_done_gate_unscored_result_is_not_accepted_as_done(self, error_type):
        adapter = WAAMockAdapter(num_tasks=1, domains=["browser"])
        task = adapter.list_tasks()[0]
        adapter.evaluate = Mock(
            return_value=BenchmarkResult(
                task_id=task.task_id,
                success=False,
                score=0.0,
                reason="result unavailable",
                error_type=error_type,
            )
        )
        agent = ScriptedAgent([BenchmarkAction(type="done")])

        result = _run_single_task(
            agent,
            adapter,
            task,
            EvaluationConfig(
                done_gate=True,
                verbose=False,
                save_execution_traces=False,
                enable_live_tracking=False,
            ),
        )

        assert result.success is False
        assert result.error_type == error_type
        assert result.error == "result unavailable"

    def test_done_gate_refresh_never_dispatches_synthetic_input(self):
        adapter = WAAMockAdapter(num_tasks=1, domains=["browser"])
        task = adapter.list_tasks()[0]
        adapter.evaluate = Mock(
            return_value=BenchmarkResult(
                task_id=task.task_id,
                success=False,
                score=0.0,
            )
        )
        adapter.step = Mock(side_effect=AssertionError("must not dispatch input"))
        adapter.observe = Mock(return_value=adapter.reset(task))
        agent = ScriptedAgent(
            [BenchmarkAction(type="done"), BenchmarkAction(type="done")]
        )

        result = _run_single_task(
            agent,
            adapter,
            task,
            EvaluationConfig(
                done_gate=True,
                done_gate_max_overrides=1,
                verbose=False,
                save_execution_traces=False,
                enable_live_tracking=False,
            ),
        )

        assert result.error_type is None
        assert result.success is False
        adapter.observe.assert_called_once_with()
        adapter.step.assert_not_called()

    def test_done_gate_stops_unscored_when_observation_refresh_is_missing(self):
        adapter = WAAMockAdapter(num_tasks=1, domains=["browser"])
        task = adapter.list_tasks()[0]
        adapter.evaluate = Mock(
            return_value=BenchmarkResult(
                task_id=task.task_id,
                success=False,
                score=0.0,
            )
        )
        agent = Mock(spec=BenchmarkAgent)
        agent.reset = Mock()
        agent.act = Mock(
            side_effect=[BenchmarkAction(type="done"), BenchmarkAction(type="done")]
        )

        result = _run_single_task(
            agent,
            adapter,
            task,
            EvaluationConfig(
                done_gate=True,
                verbose=False,
                save_execution_traces=False,
                enable_live_tracking=False,
            ),
        )

        assert result.success is False
        assert result.error_type == "infrastructure"
        assert "cannot provide a fresh observation" in (result.error or "")
        assert agent.act.call_count == 1
        assert adapter.evaluate.call_count == 1

    def test_done_gate_stops_unscored_when_observation_refresh_fails(self):
        adapter = WAAMockAdapter(num_tasks=1, domains=["browser"])
        task = adapter.list_tasks()[0]
        adapter.evaluate = Mock(
            return_value=BenchmarkResult(
                task_id=task.task_id,
                success=False,
                score=0.0,
            )
        )
        adapter.observe = Mock(side_effect=RuntimeError("frame source offline"))
        agent = Mock(spec=BenchmarkAgent)
        agent.reset = Mock()
        agent.act = Mock(
            side_effect=[BenchmarkAction(type="done"), BenchmarkAction(type="done")]
        )

        result = _run_single_task(
            agent,
            adapter,
            task,
            EvaluationConfig(
                done_gate=True,
                verbose=False,
                save_execution_traces=False,
                enable_live_tracking=False,
            ),
        )

        assert result.success is False
        assert result.error_type == "infrastructure"
        assert "fresh observation failed: frame source offline" in (result.error or "")
        assert agent.act.call_count == 1
        assert adapter.evaluate.call_count == 1

    def test_final_evaluation_exception_is_classified_as_evaluation_error(self):
        adapter = WAAMockAdapter(num_tasks=1, domains=["browser"])
        task = adapter.list_tasks()[0]
        adapter.evaluate = Mock(side_effect=RuntimeError("no evaluator response"))
        agent = ScriptedAgent([BenchmarkAction(type="done")])

        result = _run_single_task(
            agent,
            adapter,
            task,
            EvaluationConfig(
                verbose=False,
                save_execution_traces=False,
                enable_live_tracking=False,
            ),
        )

        assert result.error_type == "evaluation"
        assert "no evaluator response" in (result.error or "")

    def test_unavailable_evaluator_stays_unmeasured_through_public_runner(
        self, monkeypatch
    ):
        adapter = WAAMockAdapter(num_tasks=1, domains=["browser"])
        task = adapter.list_tasks()[0]
        adapter.evaluate = Mock(
            return_value=BenchmarkResult(
                task_id=task.task_id,
                success=False,
                score=0.0,
                error_type="evaluation",
                reason="evaluator unavailable",
            )
        )
        completed_event = Mock()
        monkeypatch.setattr(
            "openadapt_evals.benchmarks.runner.track_agent_run_completed",
            completed_event,
        )

        results = evaluate_agent_on_benchmark(
            ScriptedAgent([BenchmarkAction(type="done")]),
            adapter,
            config=EvaluationConfig(
                verbose=False,
                save_execution_traces=False,
                enable_live_tracking=False,
            ),
        )

        metrics = compute_metrics(results)
        assert results[0].error_type == "evaluation"
        assert metrics["num_tasks"] == 1
        assert metrics["num_outcome_tasks"] == 0
        assert metrics["error_count"] == 1
        assert metrics["success_count"] == 0
        completed_event.assert_called_once()
        event = completed_event.call_args.kwargs
        assert event["num_tasks"] == 1
        assert event["attempt_count"] == 1
        assert event["outcome_count"] == 0
        assert event["error_count"] == 1

    def test_parallel_fallback_preserves_typed_error(self, monkeypatch):
        adapter = WAAMockAdapter(num_tasks=1, domains=["browser"])
        task = adapter.list_tasks()[0]

        def _raise(*_args, **_kwargs):
            raise RolloutInfrastructureError("worker lost")

        monkeypatch.setattr(
            "openadapt_evals.benchmarks.runner._run_single_task", _raise
        )
        results = _evaluate_parallel(
            Mock(spec=BenchmarkAgent),
            adapter,
            [task],
            EvaluationConfig(verbose=False),
        )

        assert len(results) == 1
        assert results[0].error_type == "infrastructure"
        assert results[0].reason == "worker lost"

    def test_sequential_fallback_preserves_typed_error(self, monkeypatch):
        adapter = WAAMockAdapter(num_tasks=1, domains=["browser"])
        task = adapter.list_tasks()[0]

        def _raise(*_args, **_kwargs):
            raise ActionParseError("worker parse failed")

        monkeypatch.setattr(
            "openadapt_evals.benchmarks.runner._run_single_task", _raise
        )
        results = _evaluate_sequential(
            Mock(spec=BenchmarkAgent),
            adapter,
            [task],
            EvaluationConfig(verbose=False),
        )

        assert len(results) == 1
        assert results[0].error_type == "agent"
        assert results[0].reason == "worker parse failed"
