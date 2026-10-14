"""Tests for TRL rollout robustness features ported from standalone trainer.

Tests cover:
- Pre-rollout health check (P0)
- Corrupt screenshot retry (P0)
- Stuck detection (P1)
- Truncation warning (P1)
- DiagnosticsCallback (P2)

All tests are "light" -- they mock heavy deps (torch, transformers, PIL) and
run with [dev] deps only.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from openadapt_evals.adapters.base import (
    BenchmarkObservation,
    BenchmarkResult,
    BenchmarkTask,
)
from openadapt_evals.errors import RolloutInfrastructureError
from openadapt_evals.task_config import TaskConfig
from openadapt_evals.training.trl_rollout import (
    _run_episode,
    make_waa_rollout_func,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# A valid PNG header + padding to make a "good" screenshot (>100 bytes).
_GOOD_SCREENSHOT = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200

# A different valid screenshot (different content for stuck detection).
_ALT_SCREENSHOT = b"\x89PNG\r\n\x1a\n" + b"\xff" * 200


def _make_mock_adapter(screenshot=_GOOD_SCREENSHOT):
    """Create a mock adapter that returns a working observation.

    Uses spec=["observe", "reset", "step", ...] to limit which attributes
    exist on the mock. This prevents RLEnvironment from detecting
    pixel_action and screen_size (which would require proper return values).
    """
    adapter = MagicMock(
        spec=["observe", "reset", "step", "load_task", "load_task_from_json",
              "evaluate", "config"],
    )

    adapter.observe.return_value = BenchmarkObservation(
        screenshot=screenshot,
        viewport=(1920, 1200),
        raw_observation={},
    )
    adapter.reset.return_value = BenchmarkObservation(
        screenshot=screenshot,
        viewport=(1920, 1200),
        raw_observation={},
    )
    adapter.step.return_value = (
        BenchmarkObservation(
            screenshot=screenshot,
            viewport=(1920, 1200),
            raw_observation={},
        ),
        False,
        {},
    )
    adapter.load_task.return_value = BenchmarkTask(
        task_id="test-task", instruction="Test", domain="desktop",
    )
    adapter.load_task_from_json.return_value = BenchmarkTask(
        task_id="test-task", instruction="Test", domain="desktop",
    )
    adapter.evaluate.return_value = BenchmarkResult(
        task_id="test-task", success=False, score=0.0,
    )
    adapter.config = MagicMock(server_url="http://mock:5001")
    return adapter


class _HealthAdapter:
    """A non-mock adapter type used to exercise the live health gate."""

    def __init__(self, screenshot=_GOOD_SCREENSHOT, error=None):
        self._screenshot = screenshot
        self._error = error

    def observe(self):
        if self._error is not None:
            raise self._error
        return BenchmarkObservation(screenshot=self._screenshot, raw_observation={})


def _make_mock_trainer(num_generations=2):
    """Create a mock TRL GRPOTrainer."""
    trainer = MagicMock()
    trainer.args = MagicMock()
    trainer.args.num_generations = num_generations
    # Mock model with a parameters() method returning something with .device
    param = MagicMock()
    param.device = "cpu"
    trainer.model.parameters.return_value = iter([param])
    trainer.processing_class = MagicMock()
    return trainer


def _make_task_config() -> TaskConfig:
    return TaskConfig(
        name="Test task",
        id="test-task",
        domain="desktop",
        setup=[],
        checks=[],
        combine="and",
        max_steps=5,
        milestones=[],
    )


# ---------------------------------------------------------------------------
# Feature 1: Pre-rollout health check
# ---------------------------------------------------------------------------


class TestHealthCheck:
    """Tests for the pre-rollout health check in rollout_func."""

    def test_health_check_passes(self):
        """When adapter.observe() returns a good screenshot, rollout proceeds."""
        adapter = _make_mock_adapter()
        func = make_waa_rollout_func(
            adapter, task_configs=[_make_task_config()], max_steps=3
        )
        trainer = _make_mock_trainer(num_generations=1)

        from openadapt_evals.training import trl_rollout

        def mock_run(env, gfn, instr, tid, ms, stuck_threshold=3):
            from openadapt_evals.adapters.rl_env import ResetConfig
            env.reset(config=ResetConfig(task_id=tid))
            return [1], [2, 3], [-0.1, -0.2], 0.5

        with patch.object(trl_rollout, "_run_episode", side_effect=mock_run):
            result = func(["Test task"], trainer)

        # Should have proceeded normally
        assert result["env_reward"] == [0.5]
        assert result["completion_ids"] == [[2, 3]]

    def test_health_check_failure_is_not_zero_reward(self):
        adapter = _HealthAdapter(error=ConnectionError("server down"))
        func = make_waa_rollout_func(adapter, max_steps=3)
        trainer = _make_mock_trainer(num_generations=2)

        with pytest.raises(RolloutInfrastructureError, match="server down"):
            func(["Task A", "Task B"], trainer)

    def test_health_check_empty_screenshot(self):
        """When adapter returns empty screenshot bytes, returns zeros."""
        adapter = _HealthAdapter(screenshot=b"")
        func = make_waa_rollout_func(adapter, max_steps=3)
        trainer = _make_mock_trainer(num_generations=1)

        with pytest.raises(RolloutInfrastructureError, match="0 bytes"):
            func(["Test task"], trainer)

    def test_health_check_small_screenshot(self):
        """Screenshot smaller than 100 bytes triggers health check failure."""
        adapter = _HealthAdapter(screenshot=b"\x89PNG" + b"\x00" * 50)
        func = make_waa_rollout_func(adapter, max_steps=3)
        trainer = _make_mock_trainer(num_generations=1)

        with pytest.raises(RolloutInfrastructureError, match="54 bytes"):
            func(["Test task"], trainer)


# ---------------------------------------------------------------------------
# Feature 2: Corrupt screenshot retry
# ---------------------------------------------------------------------------


class TestCorruptScreenshotRetry:
    """Tests for screenshot retry in generate_fn.

    These tests exercise the retry loop by calling generate_fn() directly
    from within a mock _run_episode, and patching PIL.Image.open to
    simulate corrupt screenshots.
    """

    def test_corrupt_screenshot_retry_succeeds(self, caplog):
        """First PIL.open attempt fails, second succeeds -- warns but proceeds.

        We verify that the retry mechanism fires by checking log output:
        a warning for the failed attempt, and NO "corrupt after N attempts"
        error. We use caplog instead of trying to run the full model pipeline.
        """
        adapter = _make_mock_adapter()
        func = make_waa_rollout_func(
            adapter,
            task_configs=[_make_task_config()],
            max_steps=3,
            screenshot_retries=3,
            screenshot_retry_delay=0,
        )
        trainer = _make_mock_trainer(num_generations=1)

        import io as _io

        from PIL import Image

        from openadapt_evals.training import trl_rollout

        # Create a real valid PNG
        img = Image.new("RGB", (10, 10), color="red")
        buf = _io.BytesIO()
        img.save(buf, format="PNG")
        valid_png = buf.getvalue()

        original_open = Image.open
        open_calls = {"n": 0}

        def flaky_open(fp):
            open_calls["n"] += 1
            if open_calls["n"] == 1:
                raise OSError("corrupt PNG on first attempt")
            return original_open(fp)

        def mock_run(env, gfn, instr, tid, ms, stuck_threshold=3):
            from openadapt_evals.adapters.rl_env import ResetConfig
            env.reset(config=ResetConfig(task_id=tid))

            with patch.object(Image, "open", side_effect=flaky_open):
                try:
                    text, ids, lps = gfn(valid_png, "test instruction")
                except Exception:
                    # If model pipeline fails (expected in test), that's OK.
                    # We only care that the PIL retry succeeded.
                    pass

            return [1], [2], [-0.1], 0.5

        with caplog.at_level(logging.WARNING, logger="openadapt_evals.training.trl_rollout"):
            with patch.object(trl_rollout, "_run_episode", side_effect=mock_run):
                func(["Test task"], trainer)

        # Retry should have been attempted (first call fails, second succeeds)
        assert open_calls["n"] >= 2, f"Expected at least 2 open calls, got {open_calls['n']}"
        # Should see the warning for the failed attempt
        assert any("corrupt screenshot (attempt 1/3)" in r.message.lower() for r in caplog.records), (
            f"Expected corrupt screenshot warning, got: {[r.message for r in caplog.records]}"
        )
        # Should NOT see "corrupt after 3 attempts" error
        assert not any("corrupt after 3 attempts" in r.message.lower() for r in caplog.records), (
            "Should not have exhausted all retries"
        )

    def test_corrupt_screenshot_all_fail(self):
        """Unreadable observation data must stop the rollout."""
        adapter = _make_mock_adapter()
        func = make_waa_rollout_func(
            adapter,
            task_configs=[_make_task_config()],
            max_steps=3,
            screenshot_retries=3,
            screenshot_retry_delay=0,
        )
        trainer = _make_mock_trainer(num_generations=1)

        from PIL import Image

        from openadapt_evals.training import trl_rollout

        def mock_run(env, gfn, instr, tid, ms, stuck_threshold=3):
            from openadapt_evals.adapters.rl_env import ResetConfig
            env.reset(config=ResetConfig(task_id=tid))

            with patch.object(Image, "open", side_effect=OSError("always corrupt")):
                gfn(b"not a png", "test instruction")

        with patch.object(trl_rollout, "_run_episode", side_effect=mock_run):
            with pytest.raises(RolloutInfrastructureError, match="unreadable"):
                func(["Test task"], trainer)


# ---------------------------------------------------------------------------
# Feature 3: Stuck detection
# ---------------------------------------------------------------------------


class TestStuckDetection:
    """Tests for stuck detection in _run_episode."""

    def test_stuck_detection_breaks_early(self):
        """3 identical screenshots in a row triggers early break."""
        from openadapt_evals.adapters.rl_env import RLEnvironment

        adapter = _make_mock_adapter()
        env = RLEnvironment(adapter)

        # generate_fn that returns a non-done action so the loop would
        # continue forever without stuck detection
        call_count = {"n": 0}

        def fake_generate(screenshot_bytes, instruction):
            call_count["n"] += 1
            return '{"type": "click", "x": 0.5, "y": 0.5}', [1, 2], [-0.1, -0.2]

        # All screenshots are identical (adapter.observe returns same bytes)
        p_ids, c_ids, lps, reward = _run_episode(
            env, fake_generate, "Test", "test-task", max_steps=10,
            stuck_threshold=3,
        )

        # With stuck_threshold=3 and identical screenshots, should break
        # before all 10 steps. The first 2 screenshots collect, third triggers
        # stuck detection at step index 2 (before generate is called).
        # So generate should be called fewer than 10 times.
        assert call_count["n"] < 10
        # Specifically: steps 0,1 proceed normally (hashes accumulate),
        # step 2 detects stuck and breaks. So generate called 2 times.
        assert call_count["n"] == 2

    def test_stuck_detection_no_false_positive(self):
        """Different screenshots do not trigger stuck detection."""
        from openadapt_evals.adapters.rl_env import RLEnvironment

        adapter = _make_mock_adapter()

        # Make adapter return different screenshots each time
        screenshots = [
            b"\x89PNG\r\n\x1a\n" + bytes([i]) * 200
            for i in range(10)
        ]
        call_idx = {"n": 0}

        original_observe = adapter.observe

        def varying_observe(*args, **kwargs):
            obs = original_observe(*args, **kwargs)
            idx = min(call_idx["n"], len(screenshots) - 1)
            obs.screenshot = screenshots[idx]
            call_idx["n"] += 1
            return obs

        adapter.observe = varying_observe
        # Also vary reset return
        adapter.reset.return_value = BenchmarkObservation(
            screenshot=screenshots[0],
            viewport=(1920, 1200),
            raw_observation={},
        )

        env = RLEnvironment(adapter)

        step_count = {"n": 0}

        def counting_generate(screenshot_bytes, instruction):
            step_count["n"] += 1
            if step_count["n"] >= 5:
                return '{"type": "done"}', [1], [-0.1]
            return '{"type": "click", "x": 0.5, "y": 0.5}', [1, 2], [-0.1, -0.2]

        # Vary the observation screenshots returned by step
        step_idx = {"n": 0}

        def varying_step(action):
            idx = min(step_idx["n"] + 1, len(screenshots) - 1)
            step_idx["n"] += 1
            return (
                BenchmarkObservation(
                    screenshot=screenshots[idx],
                    viewport=(1920, 1200),
                    raw_observation={},
                ),
                False,
                {},
            )

        adapter.step.side_effect = varying_step

        p_ids, c_ids, lps, reward = _run_episode(
            env, counting_generate, "Test", "test-task", max_steps=10,
            stuck_threshold=3,
        )

        # Should have run 5 steps (until generate returned done), not broken early
        assert step_count["n"] == 5

    def test_stuck_threshold_configurable(self):
        """A custom stuck_threshold changes when detection fires."""
        from openadapt_evals.adapters.rl_env import RLEnvironment

        adapter = _make_mock_adapter()
        env = RLEnvironment(adapter)

        call_count = {"n": 0}

        def fake_generate(screenshot_bytes, instruction):
            call_count["n"] += 1
            return '{"type": "click", "x": 0.5, "y": 0.5}', [1], [-0.1]

        # With stuck_threshold=5 and identical screenshots
        _run_episode(
            env, fake_generate, "Test", "test-task", max_steps=20,
            stuck_threshold=5,
        )

        # Should run 4 steps (steps 0-3 proceed, step 4 detects 5 identical)
        assert call_count["n"] == 4

    def test_stuck_detection_disabled(self):
        """stuck_threshold=0 disables stuck detection entirely."""
        from openadapt_evals.adapters.rl_env import RLEnvironment

        adapter = _make_mock_adapter()
        env = RLEnvironment(adapter)

        call_count = {"n": 0}

        def fake_generate(screenshot_bytes, instruction):
            call_count["n"] += 1
            if call_count["n"] >= 8:
                return '{"type": "done"}', [1], [-0.1]
            return '{"type": "click", "x": 0.5, "y": 0.5}', [1], [-0.1]

        _run_episode(
            env, fake_generate, "Test", "test-task", max_steps=20,
            stuck_threshold=0,
        )

        # Without stuck detection, all 8 steps should run
        assert call_count["n"] == 8

    def test_scroll_up_uses_structured_step_dispatch(self):
        from openadapt_evals.adapters.rl_env import RLEnvironment

        adapter = _make_mock_adapter()
        env = RLEnvironment(adapter)
        outputs = iter(
            [
                (
                    '{"type":"scroll","x":0.5,"y":0.5,"direction":"up"}',
                    [1],
                    [-0.1],
                ),
                ('{"type":"done"}', [2], [-0.1]),
            ]
        )

        _run_episode(
            env,
            lambda screenshot, instruction: next(outputs),
            "Test",
            "test-task",
            max_steps=2,
            stuck_threshold=0,
        )

        dispatched = adapter.step.call_args_list[0].args[0]
        assert dispatched.type == "scroll"
        assert dispatched.scroll_direction == "up"


# ---------------------------------------------------------------------------
# Feature 4: Truncation warning
# ---------------------------------------------------------------------------


class TestTruncationWarning:
    """Tests for truncation warning in generate_fn.

    These tests exercise the ACTUAL truncation check in generate_fn by
    calling it through _run_episode. The mock_run calls the real gfn()
    (the closure created by make_waa_rollout_func) with mocked torch and
    PIL so the truncation check in trl_rollout.py is exercised — not
    reimplemented in the test.
    """

    def test_truncation_warning_logged_hf_path(self, caplog):
        """HF generate path: output hitting max_new_tokens triggers warning.

        We intercept _run_episode, call gfn (the real generate_fn),
        and mock model.generate to return max_new_tokens tokens so the
        truncation check fires.
        """
        import io as _io

        from PIL import Image

        from openadapt_evals.training import trl_rollout

        adapter = _make_mock_adapter()
        max_new_tokens = 10
        func = make_waa_rollout_func(
            adapter,
            task_configs=[_make_task_config()],
            max_steps=1,
            max_new_tokens=max_new_tokens,
            screenshot_retries=1,
            screenshot_retry_delay=0,
        )
        trainer = _make_mock_trainer(num_generations=1)

        # Create a real small PNG so PIL.Image.open succeeds inside generate_fn
        img = Image.new("RGB", (10, 10), color="red")
        buf = _io.BytesIO()
        img.save(buf, format="PNG")
        valid_png = buf.getvalue()

        # Make adapter return a real PNG for the health check
        adapter.observe.return_value = BenchmarkObservation(
            screenshot=valid_png, raw_observation={},
        )

        # Build a mock torch module with the pieces generate_fn needs
        mock_torch = MagicMock()
        mock_torch.no_grad.return_value.__enter__ = MagicMock(return_value=None)
        mock_torch.no_grad.return_value.__exit__ = MagicMock(return_value=False)

        # model.generate returns prompt(5) + completion(10) tokens
        prompt_len = 5
        fake_completion = list(range(max_new_tokens))  # 10 tokens >= 10-1
        fake_seq = MagicMock()
        fake_seq.__getitem__ = lambda self, idx: MagicMock(
            tolist=lambda: fake_completion,
        )
        mock_gen_output = MagicMock()
        mock_gen_output.sequences = [fake_seq]
        mock_gen_output.scores = []
        trainer.model.generate.return_value = mock_gen_output

        # Mock processor to return inputs with known prompt length
        mock_inputs = MagicMock()
        mock_input_ids = MagicMock()
        mock_input_ids.shape = [1, prompt_len]
        mock_inputs.__getitem__ = lambda self, key: (
            mock_input_ids if key == "input_ids" else MagicMock()
        )
        mock_inputs.to.return_value = mock_inputs
        trainer.processing_class.return_value = mock_inputs
        trainer.processing_class.apply_chat_template.return_value = "fake prompt"
        trainer.processing_class.decode.return_value = '{"type": "done"}'

        def mock_run(env, gfn, instr, tid, ms, stuck_threshold=3):
            """Call the REAL generate_fn with mocked torch."""
            from openadapt_evals.adapters.rl_env import ResetConfig
            env.reset(config=ResetConfig(task_id=tid))

            with patch.dict("sys.modules", {"torch": mock_torch}):
                text, ids, lps = gfn(valid_png, "test instruction")

            return [1], ids, lps, 0.0

        with caplog.at_level(logging.WARNING, logger="openadapt_evals.training.trl_rollout"):
            with patch.object(trl_rollout, "_run_episode", side_effect=mock_run):
                func(["Test task"], trainer)

        assert any("generation hit max_new_tokens" in r.message.lower() for r in caplog.records), (
            f"Expected truncation warning from generate_fn, got: "
            f"{[r.message for r in caplog.records]}"
        )

    def test_no_truncation_warning_when_short(self, caplog):
        """HF generate path: short output does NOT trigger warning."""
        import io as _io

        from PIL import Image

        from openadapt_evals.training import trl_rollout

        adapter = _make_mock_adapter()
        max_new_tokens = 256
        func = make_waa_rollout_func(
            adapter,
            task_configs=[_make_task_config()],
            max_steps=1,
            max_new_tokens=max_new_tokens,
            screenshot_retries=1,
            screenshot_retry_delay=0,
        )
        trainer = _make_mock_trainer(num_generations=1)

        img = Image.new("RGB", (10, 10), color="red")
        buf = _io.BytesIO()
        img.save(buf, format="PNG")
        valid_png = buf.getvalue()

        adapter.observe.return_value = BenchmarkObservation(
            screenshot=valid_png, raw_observation={},
        )

        mock_torch = MagicMock()
        mock_torch.no_grad.return_value.__enter__ = MagicMock(return_value=None)
        mock_torch.no_grad.return_value.__exit__ = MagicMock(return_value=False)

        # Short completion — only 3 tokens, well below max_new_tokens
        prompt_len = 5
        fake_completion = list(range(3))
        fake_seq = MagicMock()
        fake_seq.__getitem__ = lambda self, idx: MagicMock(
            tolist=lambda: fake_completion,
        )
        mock_gen_output = MagicMock()
        mock_gen_output.sequences = [fake_seq]
        mock_gen_output.scores = []
        trainer.model.generate.return_value = mock_gen_output

        mock_inputs = MagicMock()
        mock_input_ids = MagicMock()
        mock_input_ids.shape = [1, prompt_len]
        mock_inputs.__getitem__ = lambda self, key: (
            mock_input_ids if key == "input_ids" else MagicMock()
        )
        mock_inputs.to.return_value = mock_inputs
        trainer.processing_class.return_value = mock_inputs
        trainer.processing_class.apply_chat_template.return_value = "fake prompt"
        trainer.processing_class.decode.return_value = '{"type": "done"}'

        def mock_run(env, gfn, instr, tid, ms, stuck_threshold=3):
            from openadapt_evals.adapters.rl_env import ResetConfig
            env.reset(config=ResetConfig(task_id=tid))

            with patch.dict("sys.modules", {"torch": mock_torch}):
                text, ids, lps = gfn(valid_png, "test instruction")

            return [1], ids, lps, 0.0

        with caplog.at_level(logging.WARNING, logger="openadapt_evals.training.trl_rollout"):
            with patch.object(trl_rollout, "_run_episode", side_effect=mock_run):
                func(["Test task"], trainer)

        assert not any(
            "generation hit max_new_tokens" in r.message.lower()
            for r in caplog.records
        ), (
            f"Should NOT see truncation warning for short output, got: "
            f"{[r.message for r in caplog.records]}"
        )


# ---------------------------------------------------------------------------
# Feature 5: DiagnosticsCallback
# ---------------------------------------------------------------------------


class TestDiagnosticsCallback:
    """Tests for the DiagnosticsCallback."""

    def test_diagnostics_callback_logs(self, caplog):
        """Step with loss/grad_norm in log_history produces log output."""
        from openadapt_evals.integrations.trl_callbacks import DiagnosticsCallback

        cb = DiagnosticsCallback()

        state = MagicMock()
        state.global_step = 42
        state.log_history = [
            {"loss": -0.0035, "grad_norm": 1.2345, "reward": 0.75},
        ]
        args = MagicMock()
        control = MagicMock()

        with caplog.at_level(logging.INFO, logger="openadapt_evals.integrations.trl_callbacks"):
            cb.on_step_end(args, state, control)

        assert any("Step 42" in r.message for r in caplog.records), (
            f"Expected 'Step 42' in logs, got: {[r.message for r in caplog.records]}"
        )
        assert any("grad_norm" in r.message for r in caplog.records)
        assert any("reward" in r.message for r in caplog.records)

    def test_diagnostics_callback_no_log_history(self, caplog):
        """No log_history -- callback is a no-op, no crash."""
        from openadapt_evals.integrations.trl_callbacks import DiagnosticsCallback

        cb = DiagnosticsCallback()

        state = MagicMock()
        state.log_history = []
        args = MagicMock()
        control = MagicMock()

        with caplog.at_level(logging.INFO, logger="openadapt_evals.integrations.trl_callbacks"):
            cb.on_step_end(args, state, control)

        # No log lines about steps
        assert not any("Step" in r.message for r in caplog.records)

    def test_diagnostics_callback_missing_metrics(self, caplog):
        """Missing metrics default to 0.0, no crash."""
        from openadapt_evals.integrations.trl_callbacks import DiagnosticsCallback

        cb = DiagnosticsCallback()

        state = MagicMock()
        state.global_step = 1
        state.log_history = [{"learning_rate": 5e-6}]  # no loss/grad_norm/reward
        args = MagicMock()
        control = MagicMock()

        with caplog.at_level(logging.INFO, logger="openadapt_evals.integrations.trl_callbacks"):
            cb.on_step_end(args, state, control)

        # Should still log with zeroed metrics
        assert any("Step 1" in r.message for r in caplog.records)
