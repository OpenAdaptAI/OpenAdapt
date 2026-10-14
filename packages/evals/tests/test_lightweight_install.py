"""Guardrail tests for Phase 0b: openadapt-ml as optional dependency.

These tests verify that the core openadapt-evals functionality works
WITHOUT openadapt-ml installed. They define the desired post-refactor
behavior and should be run AFTER making openadapt-ml optional.

Run with: pytest tests/test_lightweight_install.py -v
"""

from __future__ import annotations

import importlib
import sys

import pytest


def _hide_openadapt_ml():
    """Context manager that makes openadapt_ml unimportable."""
    # Block the import by inserting a finder that raises ImportError
    class _BlockML:
        def find_module(self, name, path=None):
            if name == "openadapt_ml" or name.startswith("openadapt_ml."):
                return self
            return None

        def load_module(self, name):
            raise ImportError(f"Simulated: {name} not installed (lightweight mode)")

    return _BlockML()


# ---------------------------------------------------------------------------
# Core modules must import without openadapt-ml
# ---------------------------------------------------------------------------


class TestCoreImportsWithoutML:
    """Verify that core modules import cleanly without openadapt-ml."""

    @pytest.fixture(autouse=True)
    def block_ml(self):
        """Block openadapt_ml imports for every test in this class."""
        blocker = _hide_openadapt_ml()
        sys.meta_path.insert(0, blocker)
        # Clear any cached openadapt_ml modules
        to_remove = [k for k in sys.modules if k.startswith("openadapt_ml")]
        for k in to_remove:
            del sys.modules[k]
        yield
        sys.meta_path.remove(blocker)

    def test_standalone_trainer_imports(self):
        """The standalone trainer has zero openadapt-ml imports."""
        mod = importlib.import_module("openadapt_evals.training.standalone.trainer")
        assert hasattr(mod, "GRPOTrainer")

    def test_standalone_config_imports(self):
        mod = importlib.import_module("openadapt_evals.training.standalone.config")
        assert hasattr(mod, "TrainingConfig")

    def test_standalone_prompt_imports(self):
        mod = importlib.import_module("openadapt_evals.training.standalone.prompt")
        assert hasattr(mod, "SYSTEM_PROMPT")

    def test_demo_executor_imports(self):
        mod = importlib.import_module("openadapt_evals.agents.demo_executor")
        assert hasattr(mod, "DemoExecutor")

    def test_demo_library_imports(self):
        mod = importlib.import_module("openadapt_evals.demo_library")
        assert hasattr(mod, "DemoLibrary")

    def test_task_config_imports(self):
        mod = importlib.import_module("openadapt_evals.task_config")
        assert hasattr(mod, "TaskConfig")

    def test_telemetry_imports(self):
        """Telemetry should not depend on openadapt-ml."""
        mod = importlib.import_module("openadapt_evals.telemetry")
        assert hasattr(mod, "capture_event")

    def test_waa_connection_imports(self):
        mod = importlib.import_module(
            "openadapt_evals.infrastructure.waa_connection"
        )
        assert hasattr(mod, "WAAConnection")

    def test_rl_env_imports(self):
        mod = importlib.import_module("openadapt_evals.adapters.rl_env")
        assert hasattr(mod, "RLEnvironment")

    def test_benchmark_action_imports(self):
        mod = importlib.import_module("openadapt_evals.adapters.base")
        assert hasattr(mod, "BenchmarkAction")


# ---------------------------------------------------------------------------
# ML-dependent modules degrade gracefully
# ---------------------------------------------------------------------------


class TestMLDependentModulesDegrade:
    """Modules that use openadapt-ml should degrade, not crash."""

    @pytest.fixture(autouse=True)
    def block_ml(self):
        blocker = _hide_openadapt_ml()
        sys.meta_path.insert(0, blocker)
        to_remove = [k for k in sys.modules if k.startswith("openadapt_ml")]
        for k in to_remove:
            del sys.modules[k]
        yield
        sys.meta_path.remove(blocker)

    def test_baseline_agent_importable(self):
        """BaselineAgent uses try/except — should import without crash."""
        mod = importlib.import_module("openadapt_evals.agents.baseline_agent")
        assert hasattr(mod, "BaselineAgent")

    def test_trace_export_importable(self):
        """trace_export has top-level openadapt_ml import — must be guarded."""
        # This test will FAIL until we add try/except to trace_export.py
        try:
            importlib.import_module("openadapt_evals.benchmarks.trace_export")
        except ImportError as e:
            if "openadapt_ml" in str(e):
                pytest.fail(
                    f"trace_export.py crashes without openadapt-ml: {e}. "
                    f"Needs try/except guard around openadapt_ml.schema import."
                )
            raise

    def test_policy_agent_importable(self):
        """PolicyAgent uses lazy import in method body."""
        mod = importlib.import_module("openadapt_evals.agents.policy_agent")
        assert hasattr(mod, "PolicyAgent")


# ---------------------------------------------------------------------------
# The client's exact import pattern must work
# ---------------------------------------------------------------------------


class TestClientImportPattern:
    """Verify the exact imports the client uses in grpo_clean_run.py."""

    def test_grpo_trainer_import(self):
        from openadapt_evals.training.standalone.trainer import GRPOTrainer
        assert callable(GRPOTrainer)

    def test_training_config_import(self):
        from openadapt_evals.training.standalone.config import TrainingConfig
        assert callable(TrainingConfig)

    def test_waa_connection_import(self):
        from openadapt_evals.infrastructure import WAAConnection
        assert callable(WAAConnection)

    def test_wandb_callbacks_import(self):
        from openadapt_evals.integrations.wandb_callbacks import (
            wandb_model_loaded,
            wandb_rollout_logger,
            wandb_step_logger,
        )
        assert callable(wandb_model_loaded)
        assert callable(wandb_rollout_logger)
        assert callable(wandb_step_logger)

    def test_task_config_import(self):
        from openadapt_evals.task_config import TaskConfig
        assert callable(TaskConfig.from_yaml)

    def test_demo_library_import(self):
        from openadapt_evals.demo_library import DemoLibrary
        assert callable(DemoLibrary)

    def test_benchmark_action_import(self):
        from openadapt_evals.adapters.base import BenchmarkAction
        assert callable(BenchmarkAction)


# ---------------------------------------------------------------------------
# The oa-vm entry point must not import the ML training stack
# ---------------------------------------------------------------------------


class TestVmCliStaysLight:
    """`oa-vm` (VM lifecycle) must import without the ML/vision stack.

    Regression guard: the ``oa-vm`` console script imports
    ``openadapt_evals.benchmarks.vm_cli``, which triggers the package
    ``__init__`` modules. Those used to eagerly import ``agents`` -> transformers
    / peft, which (a) is dead weight for VM management and (b) crashed ``oa-vm``
    outright under a NumPy 2 / stale-transformers environment. The package
    ``__init__``s now resolve those names lazily (PEP 562); this test pins that
    importing the CLI path pulls in none of the heavy modules.

    Run in a subprocess with a fresh interpreter so that heavy modules imported
    by other tests in this process cannot mask a regression.
    """

    def test_importing_vm_cli_does_not_import_ml_stack(self):
        import subprocess

        heavy = ["transformers", "peft", "torch", "openadapt_evals.agents"]
        code = (
            "import sys\n"
            "import openadapt_evals\n"
            "import openadapt_evals.benchmarks\n"
            "import openadapt_evals.benchmarks.vm_cli\n"
            f"heavy = {heavy!r}\n"
            "leaked = [m for m in heavy if m in sys.modules]\n"
            "print('LEAKED:' + ','.join(leaked))\n"
            "sys.exit(1 if leaked else 0)\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            "oa-vm import path pulled in heavy modules: "
            f"{result.stdout.strip()} {result.stderr.strip()}"
        )

    def test_lazy_public_api_still_resolves(self):
        # The laziness must not break the re-exported public API.
        import openadapt_evals as oe
        from openadapt_evals.adapters import WAAAdapter as direct_adapter

        assert oe.WAAAdapter is direct_adapter  # lazy value is the real object
        assert callable(oe.compute_metrics)
        from openadapt_evals.benchmarks import EvaluationConfig, WAAMockAdapter

        assert WAAMockAdapter is not None
        assert EvaluationConfig is not None
