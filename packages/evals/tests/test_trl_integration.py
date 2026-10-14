"""Tests for TRL GRPOTrainer integration.

Validates the rollout_func, mock mode, config separation, and wrapper
without requiring a GPU, real model, or WAA server.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Mock rollout_func tests
# ---------------------------------------------------------------------------


class TestMockRolloutFunc:
    """Test the mock rollout function from train_trl_grpo.py."""

    def _make_task_configs(self, n=3):
        """Create simple task configs."""
        from openadapt_evals.task_config import TaskConfig

        configs = []
        for i in range(n):
            tc = MagicMock(spec=TaskConfig)
            tc.name = f"Task {i}"
            tc.id = f"task-{i}"
            tc.milestones = [MagicMock() for _ in range(2)]
            tc.max_steps = 10
            configs.append(tc)
        return configs

    def test_mock_returns_correct_keys(self):
        """Mock rollout returns prompt_ids, completion_ids, logprobs, env_reward."""
        # Import the mock creator from the training script
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "train_trl_grpo", "scripts/train_trl_grpo.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        configs = self._make_task_configs()
        rollout_func = mod.create_mock_rollout_func(configs)

        mock_trainer = MagicMock()
        mock_trainer.args.num_generations = 4

        result = rollout_func(["Task 0", "Task 1"], mock_trainer)

        assert "prompt_ids" in result
        assert "completion_ids" in result
        assert "logprobs" in result
        assert "env_reward" in result

    def test_mock_returns_correct_count(self):
        """Mock returns num_prompts * num_generations entries."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "train_trl_grpo", "scripts/train_trl_grpo.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        configs = self._make_task_configs()
        rollout_func = mod.create_mock_rollout_func(configs)

        mock_trainer = MagicMock()
        mock_trainer.args.num_generations = 4

        result = rollout_func(["Task 0", "Task 1"], mock_trainer)

        expected = 2 * 4  # 2 prompts * 4 generations
        assert len(result["env_reward"]) == expected
        assert len(result["prompt_ids"]) == expected

    def test_mock_has_reward_variance(self):
        """Mock produces different reward values (needed for GRPO)."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "train_trl_grpo", "scripts/train_trl_grpo.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        configs = self._make_task_configs()
        rollout_func = mod.create_mock_rollout_func(configs)

        mock_trainer = MagicMock()
        mock_trainer.args.num_generations = 8

        # Run multiple times to get reward variance (randomized)
        all_rewards = []
        for _ in range(5):
            result = rollout_func(["Task 0"], mock_trainer)
            all_rewards.extend(result["env_reward"])

        unique_rewards = set(all_rewards)
        assert len(unique_rewards) > 1, (
            f"Mock should produce reward variance, got {unique_rewards}"
        )


# ---------------------------------------------------------------------------
# Config separation tests
# ---------------------------------------------------------------------------


class TestConfigSeparation:
    """Verify TrainingConfig and TRL GRPOConfig have clean separation."""

    def test_training_config_has_no_trl_fields(self):
        """TrainingConfig should NOT have loss_type, gradient_accumulation, etc."""
        from openadapt_evals.training.standalone.config import TrainingConfig

        tc = TrainingConfig()
        # These belong to TRL's GRPOConfig, not ours
        assert not hasattr(tc, "loss_type"), "loss_type belongs in GRPOConfig"
        assert not hasattr(tc, "gradient_accumulation_steps"), "belongs in GRPOConfig"
        assert not hasattr(tc, "per_device_train_batch_size"), "belongs in GRPOConfig"
        assert not hasattr(tc, "bf16"), "belongs in GRPOConfig"
        assert not hasattr(tc, "report_to"), "belongs in GRPOConfig"
        assert not hasattr(tc, "use_vllm"), "belongs in GRPOConfig"

    def test_training_config_has_our_fields(self):
        """TrainingConfig should have OpenAdapt-specific fields."""
        from openadapt_evals.training.standalone.config import TrainingConfig

        tc = TrainingConfig()
        assert hasattr(tc, "server_url")
        assert hasattr(tc, "task_dir")
        assert hasattr(tc, "constrained_decoding")
        assert hasattr(tc, "max_new_tokens")
        assert hasattr(tc, "vision_loss_mode")
        assert hasattr(tc, "model_name")
        assert hasattr(tc, "use_unsloth")
        assert hasattr(tc, "weave_project")

    def test_wrapper_accepts_trl_config(self):
        """The TRL wrapper accepts a trl_config kwarg."""
        from openadapt_evals.training.standalone.config import TrainingConfig
        from openadapt_evals.training.trl_wrapper import GRPOTrainer

        tc = TrainingConfig(task_dir="tasks/")

        # Should not crash — trl_config is stored, not used until train()
        trainer = GRPOTrainer(tc, trl_config="mock_grpo_config")
        assert trainer._trl_config == "mock_grpo_config"

    def test_wrapper_defaults_without_trl_config(self):
        """Without trl_config, wrapper builds defaults from TrainingConfig."""
        from openadapt_evals.training.standalone.config import TrainingConfig
        from openadapt_evals.training.trl_wrapper import GRPOTrainer

        tc = TrainingConfig(task_dir="tasks/")
        trainer = GRPOTrainer(tc)
        assert trainer._trl_config is None  # will build defaults in train()


# ---------------------------------------------------------------------------
# TRL wrapper construction tests
# ---------------------------------------------------------------------------


class TestTRLWrapperConstruction:
    """Test the wrapper can be constructed with all callback combinations."""

    def test_no_callbacks(self):
        from openadapt_evals.training.standalone.config import TrainingConfig
        from openadapt_evals.training.trl_wrapper import GRPOTrainer

        trainer = GRPOTrainer(TrainingConfig())
        assert trainer._on_model_loaded is None
        assert trainer._on_step_complete is None

    def test_all_callbacks(self):
        from openadapt_evals.training.standalone.config import TrainingConfig
        from openadapt_evals.training.trl_wrapper import GRPOTrainer

        def fn(*a, **kw):
            return None

        trainer = GRPOTrainer(
            TrainingConfig(),
            on_model_loaded=fn,
            on_before_collect=fn,
            on_rollout_complete=fn,
            on_step_complete=fn,
        )
        assert trainer._on_model_loaded is fn
        assert trainer._on_before_collect is fn
        assert trainer._on_rollout_complete is fn
        assert trainer._on_step_complete is fn

    def test_trl_config_passthrough(self):
        """TRL config is stored as-is, not translated."""
        from openadapt_evals.training.standalone.config import TrainingConfig
        from openadapt_evals.training.trl_wrapper import GRPOTrainer

        mock_trl = MagicMock()
        mock_trl.loss_type = "dapo"
        mock_trl.output_dir = "/tmp/test"

        trainer = GRPOTrainer(TrainingConfig(), trl_config=mock_trl)
        assert trainer._trl_config.loss_type == "dapo"
        assert trainer._trl_config.output_dir == "/tmp/test"


# ---------------------------------------------------------------------------
# TelemetryCallback tests
# ---------------------------------------------------------------------------


class TestTelemetryCallback:
    """Test the TRL TelemetryCallback."""

    def test_callback_importable(self):
        try:
            from openadapt_evals.integrations.trl_callbacks import TelemetryCallback
            cb = TelemetryCallback()
            assert cb is not None
        except ImportError:
            pytest.skip("trl_callbacks not available")

    def test_callback_fires_events(self):
        try:
            from openadapt_evals.integrations.trl_callbacks import TelemetryCallback
        except ImportError:
            pytest.skip("trl_callbacks not available")

        cb = TelemetryCallback()
        # These should not crash even without a real trainer
        args = MagicMock()
        state = MagicMock()
        state.global_step = 5
        state.log_history = [{"loss": 0.5, "reward_mean": 0.7}]
        control = MagicMock()

        with patch("openadapt_evals.telemetry.capture_event"):
            cb.on_train_begin(args, state, control)
            cb.on_step_end(args, state, control)


# ---------------------------------------------------------------------------
# Wrapper callback passthrough tests
# ---------------------------------------------------------------------------


class TestWrapperPassesCallbacks:
    """Verify GRPOTrainer passes on_before_collect and on_rollout_complete
    through to make_waa_rollout_func, not into HookBridge."""

    def test_wrapper_passes_callbacks_to_rollout_func(self):
        """Verify on_before_collect and on_rollout_complete are forwarded
        to make_waa_rollout_func as keyword arguments.

        Since train() has local imports of heavy dependencies (datasets, trl,
        torch), we verify by inspecting the source code of train() to confirm
        the kwargs are passed. This avoids needing GPU/torch in CI.
        """
        import inspect

        from openadapt_evals.training.standalone.config import TrainingConfig
        from openadapt_evals.training.trl_wrapper import GRPOTrainer

        def before_fn(task_id, env):
            return None

        def complete_fn(rollout, index):
            return None

        trainer = GRPOTrainer(
            TrainingConfig(task_dir="tasks/"),
            on_before_collect=before_fn,
            on_rollout_complete=complete_fn,
        )

        # 1. Verify the stored callbacks match what was passed.
        assert trainer._on_before_collect is before_fn
        assert trainer._on_rollout_complete is complete_fn

        # 2. Verify train() source passes callbacks to make_waa_rollout_func.
        source = inspect.getsource(GRPOTrainer.train)
        assert "on_before_collect=self._on_before_collect" in source, (
            "train() must pass on_before_collect to make_waa_rollout_func"
        )
        assert "on_rollout_complete=self._on_rollout_complete" in source, (
            "train() must pass on_rollout_complete to make_waa_rollout_func"
        )

        # 3. Verify HookBridge no longer stores these callbacks.
        hookbridge_section = source.split("class HookBridge")[1].split(
            "callbacks.append"
        )[0] if "class HookBridge" in source else ""
        assert "on_before_collect" not in hookbridge_section, (
            "HookBridge should not store on_before_collect"
        )
        assert "on_rollout_complete" not in hookbridge_section, (
            "HookBridge should not store on_rollout_complete"
        )


# ---------------------------------------------------------------------------
# VLMModelWrapper integration
# ---------------------------------------------------------------------------


class TestVLMPatchIntegration:
    """Verify VLM model patching is wired into the TRL training pipeline."""

    def test_patch_used_in_train_source(self):
        """trl_wrapper.train() patches the model for VLM compatibility."""
        import inspect

        from openadapt_evals.training import trl_wrapper

        source = inspect.getsource(trl_wrapper.GRPOTrainer.train)
        assert "patch_model_for_trl" in source, (
            "GRPOTrainer.train() must call patch_model_for_trl(model) "
            "to patch forward() for pixel_values injection. Without this, "
            "TRL's forward pass won't have pixel_values and the VLM will be blind."
        )

    def test_generate_fn_calls_cache_vision_inputs(self):
        """generate_fn caches vision inputs on the wrapper before generating."""
        import inspect

        from openadapt_evals.training import trl_rollout

        source = inspect.getsource(trl_rollout.make_waa_rollout_func)
        assert "cache_vision_inputs" in source, (
            "generate_fn must call model.cache_vision_inputs(inputs) before "
            "model.generate() so the VLMModelWrapper can inject pixel_values "
            "during TRL's training forward pass."
        )
