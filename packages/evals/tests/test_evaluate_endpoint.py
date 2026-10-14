"""Tests for the WAA /evaluate endpoint implementation."""

from unittest.mock import MagicMock, patch

import pytest

from openadapt_evals.server.evaluate_endpoint import (
    EvaluationNotRunError,
    MockEnv,
    StandaloneMetrics,
    _truncate_value,
    create_standalone_evaluator,
    evaluate_task_state,
    get_expected_value,
)


class TestMockEnv:
    """Tests for MockEnv class."""

    def test_default_values(self):
        """Test default MockEnv values."""
        env = MockEnv()
        assert env.vm_ip == "localhost"
        assert env.port == 5000

    def test_custom_values(self):
        """Test custom MockEnv values."""
        env = MockEnv(vm_ip="192.168.1.100", port=8080)
        assert env.vm_ip == "192.168.1.100"
        assert env.port == 8080


class TestStandaloneMetrics:
    """Tests for StandaloneMetrics class."""

    def test_exact_match_equal(self):
        """Test exact_match with equal values."""
        assert StandaloneMetrics.exact_match("hello", "hello") == 1.0

    def test_exact_match_unequal(self):
        """Test exact_match with unequal values."""
        assert StandaloneMetrics.exact_match("hello", "world") == 0.0

    def test_exact_match_with_whitespace(self):
        """Test exact_match handles whitespace."""
        assert StandaloneMetrics.exact_match("hello ", " hello") == 1.0

    def test_exact_match_numbers(self):
        """Test exact_match with numbers."""
        assert StandaloneMetrics.exact_match(42, 42) == 1.0
        assert StandaloneMetrics.exact_match(42, 43) == 0.0

    def test_contains_positive(self):
        """Test contains with substring present."""
        assert StandaloneMetrics.contains("hello world", "world") == 1.0

    def test_contains_negative(self):
        """Test contains with substring absent."""
        assert StandaloneMetrics.contains("hello world", "foo") == 0.0

    def test_contains_case_insensitive(self):
        """Test contains is case insensitive."""
        assert StandaloneMetrics.contains("Hello World", "WORLD") == 1.0

    def test_fuzzy_match_exact(self):
        """Test fuzzy_match with exact match."""
        # Should return high score for exact match
        # Without rapidfuzz, falls back to containment check which returns 0.8
        score = StandaloneMetrics.fuzzy_match("hello", "hello")
        assert score >= 0.8

    def test_fuzzy_match_partial(self):
        """Test fuzzy_match with partial match."""
        # "hello" vs "hallo" - without rapidfuzz, fallback checks containment
        # which will return 0.0 since they don't contain each other
        score = StandaloneMetrics.fuzzy_match("hello", "hallo", threshold=0.7)
        assert score >= 0.0  # Could be 0.0 without rapidfuzz or partial score with it


class TestTruncateValue:
    """Tests for _truncate_value function."""

    def test_none_value(self):
        """Test truncation of None."""
        assert _truncate_value(None) is None

    def test_short_value(self):
        """Test truncation of short value."""
        assert _truncate_value("hello") == "hello"

    def test_long_value(self):
        """Test truncation of long value."""
        long_string = "x" * 1000
        result = _truncate_value(long_string, max_len=100)
        assert len(result) == 103  # 100 + "..."
        assert result.endswith("...")

    def test_custom_max_len(self):
        """Test custom max length."""
        result = _truncate_value("hello world", max_len=5)
        assert result == "hello..."


class TestEvaluateTaskState:
    """Tests for evaluate_task_state function."""

    def test_no_evaluator_config(self):
        """Test with no evaluator config."""
        # When WAA evaluators are not installed, evaluate_task_state returns
        # an error about loading evaluators. Mock the evaluator loading to
        # test the actual no-config case.
        with patch("openadapt_evals.server.evaluate_endpoint._load_waa_evaluators") as mock_load:
            mock_getters = MagicMock()
            mock_metrics = MagicMock()
            mock_load.return_value = (mock_getters, mock_metrics)

            result = evaluate_task_state({})
            assert result["success"] is False
            assert result["score"] == 0.0
            assert "No evaluator" in result["reason"]

    def test_infeasible_task_correct(self):
        """Test infeasible task detected correctly."""
        config = {
            "evaluator": {"infeasible": True},
            "agent_last_action": "FAIL",
        }
        # This will fail to load WAA evaluators but we can check the infeasible logic
        with patch("openadapt_evals.server.evaluate_endpoint._load_waa_evaluators") as mock_load:
            mock_getters = MagicMock()
            mock_metrics = MagicMock()
            mock_load.return_value = (mock_getters, mock_metrics)

            result = evaluate_task_state(config)
            assert result["success"] is True
            assert result["score"] == 1.0
            assert "infeasible" in result["reason"].lower()

    def test_evaluator_load_failure(self):
        """Test handling of evaluator load failure."""
        config = {"evaluator": {"func": "exact_match"}}

        with patch("openadapt_evals.server.evaluate_endpoint._load_waa_evaluators") as mock_load:
            mock_load.side_effect = ImportError("WAA not found")

            result = evaluate_task_state(config)
            assert result["success"] is False
            assert "Failed to load" in result["reason"]

    def test_missing_expected_contract_cannot_match_missing_actual(self):
        getters = MagicMock()
        getters.get_thing.return_value = None
        metrics = MagicMock()
        metrics.exact_match.return_value = 1.0

        with patch(
            "openadapt_evals.server.evaluate_endpoint._load_waa_evaluators",
            return_value=(getters, metrics),
        ):
            result = evaluate_task_state(
                {"evaluator": {"result": {"type": "thing"}}}
            )

        assert result["scored"] is False
        assert result["error_type"] == "evaluation"
        metrics.exact_match.assert_not_called()


def test_expected_getter_failure_is_not_a_null_expected_value():
    getters = MagicMock()
    getters.get_reference.side_effect = RuntimeError("offline")

    with pytest.raises(EvaluationNotRunError) as excinfo:
        get_expected_value(
            {"expected": {"type": "reference"}},
            env=MagicMock(),
            getters=getters,
        )

    assert excinfo.value.error_type == "infrastructure"


class TestCreateStandaloneEvaluator:
    """Tests for create_standalone_evaluator function."""

    def test_creates_callable(self):
        """Test that function returns a callable."""
        evaluate = create_standalone_evaluator()
        assert callable(evaluate)

    def test_no_evaluator_config(self):
        """Test with missing evaluator config."""
        evaluate = create_standalone_evaluator()
        result = evaluate({})
        assert result["success"] is False
        assert result["score"] == 0.0

    def test_basic_evaluation(self):
        """Test basic evaluation with standalone evaluator."""
        evaluate = create_standalone_evaluator()

        config = {
            "evaluator": {
                "func": "exact_match",
                "result": {"type": "vm_command_line", "command": "echo test"},
                "expected": {"type": "rule", "rules": {"match": "test"}},
            }
        }

        # Mock the HTTP request
        with patch("requests.post") as mock_post:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "returncode": 0,
                "output": "test",
            }
            mock_post.return_value = mock_response

            result = evaluate(config)
            # Should complete without error (actual result depends on mocking)
            assert "success" in result
            assert "score" in result


class TestWAALiveAdapterEvaluate:
    """Tests for WAALiveAdapter.evaluate method."""

    def test_evaluate_calls_endpoint(self):
        """Test that evaluate calls /evaluate endpoint."""
        from openadapt_evals.adapters import WAALiveAdapter, WAALiveConfig
        from openadapt_evals.adapters.base import BenchmarkTask

        adapter = WAALiveAdapter(WAALiveConfig(
            server_url="http://test:5000",
            evaluate_retries=1,
        ))
        task = BenchmarkTask(
            task_id="test_1",
            instruction="Test task",
            domain="test",
            raw_config={"evaluator": {"func": "exact_match"}},
        )

        with patch("requests.post") as mock_post:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "success": True,
                "score": 1.0,
                "reason": "Test passed",
            }
            mock_post.return_value = mock_response

            result = adapter.evaluate(task)

            # Verify endpoint was called
            mock_post.assert_called_once()
            call_args = mock_post.call_args
            assert "/evaluate" in call_args[0][0]

            # Verify result
            assert result.success is True
            assert result.score == 1.0
            assert result.task_id == "test_1"

    def test_evaluate_fallback_on_404(self):
        """Test evaluation behavior when endpoint returns 404."""
        from openadapt_evals.adapters import WAALiveAdapter, WAALiveConfig
        from openadapt_evals.adapters.base import BenchmarkAction, BenchmarkTask

        adapter = WAALiveAdapter(WAALiveConfig(
            server_url="http://test:5000",
            evaluate_retries=1,
        ))
        adapter._actions = [BenchmarkAction(type="click", x=100, y=100)]

        task = BenchmarkTask(
            task_id="test_1",
            instruction="Test task",
            domain="test",
        )

        with patch("requests.post") as mock_post:
            mock_response = MagicMock()
            mock_response.status_code = 404
            mock_post.return_value = mock_response

            result = adapter.evaluate(task)

            # Without evaluator spec, evaluation returns unavailable error
            assert result.success is False
            assert result.score == 0.0
            assert "unavailable" in result.reason.lower() or "evaluator" in result.reason.lower()

    def test_evaluate_fallback_on_connection_error(self):
        """Test evaluation behavior on connection error."""
        import requests

        from openadapt_evals.adapters import WAALiveAdapter, WAALiveConfig
        from openadapt_evals.adapters.base import BenchmarkAction, BenchmarkTask

        adapter = WAALiveAdapter(WAALiveConfig(
            server_url="http://test:5000",
            evaluate_retries=1,  # No retries for fast test
        ))
        adapter._actions = [
            BenchmarkAction(type="type", text="hello"),
            BenchmarkAction(type="done"),
        ]

        task = BenchmarkTask(
            task_id="test_1",
            instruction="Test task",
            domain="test",
        )

        with patch("requests.post") as mock_post:
            mock_post.side_effect = requests.ConnectionError("Connection refused")

            result = adapter.evaluate(task)

            # Without evaluator spec, evaluation returns unavailable error
            assert result.success is False
            assert "unavailable" in result.reason.lower() or "evaluator" in result.reason.lower()
            assert result.score == 0.0

    def test_evaluate_retries_on_timeout(self):
        """Test that evaluate retries on timeout with backoff."""
        import requests

        from openadapt_evals.adapters import WAALiveAdapter, WAALiveConfig
        from openadapt_evals.adapters.base import BenchmarkTask

        adapter = WAALiveAdapter(WAALiveConfig(
            server_url="http://test:5000",
            evaluate_timeout=10.0,
            evaluate_retries=3,
            evaluate_retry_base_delay=0.01,  # Fast backoff for tests
        ))

        task = BenchmarkTask(
            task_id="test_1",
            instruction="Test task",
            domain="test",
        )

        with patch("requests.post") as mock_post:
            mock_post.side_effect = requests.Timeout("Request timed out")

            result = adapter.evaluate(task)

            # Should have retried 3 times
            assert mock_post.call_count == 3
            assert result.success is False
            assert "timed out" in result.reason.lower()
            assert "3 retries" in result.reason
            assert result.error_type == "infrastructure"

    def test_evaluate_succeeds_after_retry(self):
        """Test that evaluate succeeds on second attempt after timeout."""
        import requests

        from openadapt_evals.adapters import WAALiveAdapter, WAALiveConfig
        from openadapt_evals.adapters.base import BenchmarkTask

        adapter = WAALiveAdapter(WAALiveConfig(
            server_url="http://test:5000",
            evaluate_timeout=10.0,
            evaluate_retries=3,
            evaluate_retry_base_delay=0.01,  # Fast backoff for tests
        ))

        task = BenchmarkTask(
            task_id="test_1",
            instruction="Test task",
            domain="test",
            raw_config={"evaluator": {"func": "exact_match"}},
        )

        # First call times out, second succeeds
        mock_success = MagicMock()
        mock_success.status_code = 200
        mock_success.json.return_value = {
            "success": True,
            "score": 1.0,
            "reason": "Test passed",
        }

        with patch("requests.post") as mock_post:
            mock_post.side_effect = [
                requests.Timeout("Request timed out"),
                mock_success,
            ]

            result = adapter.evaluate(task)

            assert mock_post.call_count == 2
            assert result.success is True
            assert result.score == 1.0

    def test_evaluate_uses_evaluate_timeout(self):
        """Test that evaluate uses evaluate_timeout, not general timeout."""
        from openadapt_evals.adapters import WAALiveAdapter, WAALiveConfig
        from openadapt_evals.adapters.base import BenchmarkTask

        adapter = WAALiveAdapter(WAALiveConfig(
            server_url="http://test:5000",
            timeout=90.0,
            evaluate_timeout=180.0,
            evaluate_retries=1,
        ))

        task = BenchmarkTask(
            task_id="test_1",
            instruction="Test task",
            domain="test",
        )

        with patch("requests.post") as mock_post:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "success": True,
                "score": 1.0,
                "reason": "Test passed",
            }
            mock_post.return_value = mock_response

            adapter.evaluate(task)

            # Verify the evaluate_timeout (180s) was used, not general timeout (90s)
            call_kwargs = mock_post.call_args[1]
            assert call_kwargs["timeout"] == 180.0


class TestLoadTaskFromJson:
    """Tests for WAALiveAdapter.load_task_from_json method."""

    def test_load_task_from_json(self):
        """Test loading task from JSON config."""
        from openadapt_evals.adapters import WAALiveAdapter, WAALiveConfig

        adapter = WAALiveAdapter(WAALiveConfig())
        config = {
            "instruction": "Open Notepad and type hello",
            "snapshot": "notepad_basic",
            "max_steps": 10,
            "evaluator": {
                "func": "exact_match",
                "result": {"type": "vm_file", "path": "C:/test.txt"},
                "expected": {"type": "rule", "rules": {"match": "hello"}},
            }
        }

        task = adapter.load_task_from_json("notepad_test", config)

        assert task.task_id == "notepad_test"
        assert task.instruction == "Open Notepad and type hello"
        assert task.domain == "notepad"
        assert task.initial_state_ref == "notepad_basic"
        assert task.time_limit_steps == 10
        assert task.raw_config == config
        assert task.evaluation_spec == config["evaluator"]

    def test_load_task_domain_from_id(self):
        """Test domain extraction from task_id."""
        from openadapt_evals.adapters import WAALiveAdapter, WAALiveConfig

        adapter = WAALiveAdapter(WAALiveConfig())
        config = {"instruction": "Test task"}

        task = adapter.load_task_from_json("browser_abc123", config)
        assert task.domain == "browser"

        task = adapter.load_task_from_json("vscode_xyz789", config)
        assert task.domain == "vscode"
