"""Tests for the standalone GRPO trainer.

Covers constrained decoding, task rotation, and config handling.
No GPU or WAA server required.
"""

from __future__ import annotations

import io
import re
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from openadapt_evals.errors import (
    ActionParseError,
    RolloutEvaluationError,
    RolloutInfrastructureError,
    RolloutLossError,
)
from openadapt_evals.task_config import Milestone, TaskCheck
from openadapt_evals.training.standalone.config import TrainingConfig
from openadapt_evals.training.standalone.prompt import (
    SimpleAction,
    format_action_as_text,
    parse_vlm_output_to_action,
)
from openadapt_evals.training.standalone.reward import (
    compute_group_advantages,
    evaluate_milestones_screenshot,
)
from openadapt_evals.training.standalone.trainer import GRPOTrainer
from openadapt_evals.training.standalone.waa_direct import Rollout, RolloutStep


def _tiny_png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2)).save(buffer, format="PNG")
    return buffer.getvalue()

# ---------------------------------------------------------------------------
# Action regex
# ---------------------------------------------------------------------------


class TestActionRegex:
    """Verify the Thought/Action regex accepts valid output and rejects junk."""

    full_regex = GRPOTrainer._ACTION_REGEX
    action_regex = GRPOTrainer._ACTION_RE

    @pytest.mark.parametrize(
        "output",
        [
            "Thought: I need to click the start menu.\nAction: CLICK(x=0.50, y=0.30)",
            'Thought: Type notepad in the search box.\nAction: TYPE(text="notepad")',
            "Thought: Wait for the UI to load.\nAction: WAIT()",
            "Thought: The task is complete.\nAction: DONE()",
            "Thought: Click Chrome icon.\nAction: CLICK(x=0.05, y=0.20)",
            "Thought: x\nAction: CLICK(x=0.0, y=0.0)",
        ],
    )
    def test_valid_thought_action(self, output: str) -> None:
        assert re.match(self.full_regex, output), f"Should match: {output!r}"

    @pytest.mark.parametrize(
        "text",
        [
            "CLICK(x=0.50, y=0.30)",
            "Action: CLICK(x=0.50, y=0.30)",
            "** Let me think about this...",
            "",
            "Thought: I should click here.",
            "Thought: Click\nAction: click(0.5, 0.3)",
            "Thought: Click\nAction: CLICK",
        ],
    )
    def test_invalid_text_rejected(self, text: str) -> None:
        assert not re.match(self.full_regex, text), f"Should NOT match: {text!r}"

    @pytest.mark.parametrize(
        "action",
        [
            "CLICK(x=0.50, y=0.30)",
            "CLICK(x=0.999, y=0.123)",
            'TYPE(text="hello world")',
            'TYPE(text="")',
            "WAIT()",
            "DONE()",
        ],
    )
    def test_action_only_regex(self, action: str) -> None:
        assert re.match(self.action_regex, action), f"Should match: {action!r}"

    def test_no_large_bounded_quantifiers(self) -> None:
        """Bounded quantifiers > 10 cause DFA state explosion in Outlines."""
        bounds = re.findall(r"\{(\d+),(\d+)\}", self.full_regex)
        for lo, hi in bounds:
            assert int(hi) <= 10, f"Quantifier {{{lo},{hi}}} will explode Outlines DFA. Use +/*."

    def test_unparseable_output_is_not_task_completion(self) -> None:
        with pytest.raises(ActionParseError):
            parse_vlm_output_to_action("I do not know what action to take")

    @pytest.mark.parametrize(
        "output",
        [
            "CLICK(x=-0.1, y=0.5)",
            '{"action_type": "click", "coordinate": [-1, 20]}',
            '{"action_type": "type"}',
        ],
    )
    def test_invalid_action_arguments_are_rejected(self, output: str) -> None:
        with pytest.raises(ActionParseError):
            parse_vlm_output_to_action(output)

    def test_normalized_edge_stays_inside_viewport(self) -> None:
        action = parse_vlm_output_to_action("CLICK(x=1.0, y=1.0)", screen_size=(1920, 1080))
        assert (action.x, action.y) == (1919, 1079)

    @pytest.mark.parametrize(
        "coordinate",
        [
            [0.5, 200],
            [100.5, 200],
            [True, 200],
        ],
    )
    def test_json_click_rejects_mixed_or_fractional_pixel_coordinates(
        self, coordinate
    ) -> None:
        with pytest.raises(ActionParseError):
            parse_vlm_output_to_action(
                '{"action_type":"click","coordinate":'
                f"{coordinate!r}".replace("True", "true")
                + "}",
                screen_size=(1920, 1080),
            )

    def test_json_click_accepts_explicit_integer_pixel_coordinates(self) -> None:
        action = parse_vlm_output_to_action(
            '{"action_type":"click","coordinate":[100,200]}',
            screen_size=(1920, 1080),
        )
        assert (action.x, action.y) == (100, 200)

    @pytest.mark.parametrize(
        "action",
        [
            SimpleAction(type="click", x=None, y=10),
            SimpleAction(type="type", text=None),
            SimpleAction(type="scroll"),
        ],
    )
    def test_formatter_does_not_invent_missing_or_unknown_actions(
        self, action: SimpleAction
    ) -> None:
        with pytest.raises(ActionParseError):
            format_action_as_text(action)


class TestFailureVisibility:
    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("num_training_steps", 0),
            ("num_rollouts_per_step", 0),
            ("max_steps_per_episode", 0),
            ("save_every_steps", 0),
            ("learning_rate", float("nan")),
            ("learning_rate", 0.0),
        ],
    )
    def test_invalid_training_config_stops_before_checkpoint(
        self, tmp_path, field, value
    ) -> None:
        config = TrainingConfig(output_dir=str(tmp_path / "checkpoints"))
        setattr(config, field, value)
        trainer = GRPOTrainer(config)

        with pytest.raises(ValueError, match=field):
            trainer.train()

        assert not (tmp_path / "checkpoints").exists()

    def test_all_skipped_steps_do_not_publish_a_trained_checkpoint(
        self, tmp_path
    ) -> None:
        config = TrainingConfig(
            task_ids=["task-1"],
            num_training_steps=2,
            output_dir=str(tmp_path / "checkpoints"),
        )
        trainer = GRPOTrainer(config)
        model = MagicMock()
        model.parameters.return_value = []
        environment = MagicMock()
        environment.health_check.return_value = True
        fake_torch = SimpleNamespace(
            optim=SimpleNamespace(AdamW=MagicMock(return_value=MagicMock()))
        )

        with (
            patch.object(trainer, "_load_task_configs"),
            patch.object(trainer, "_collect_group", return_value=[MagicMock()]),
            patch.object(
                trainer,
                "_training_step",
                return_value={"reward_mean": 0.5, "loss": 0.0, "skipped": True},
            ),
            patch.object(trainer, "_save_checkpoint") as save_checkpoint,
            patch(
                "openadapt_evals.training.standalone.trainer.load_model_and_processor",
                return_value=(model, MagicMock()),
            ),
            patch(
                "openadapt_evals.training.standalone.trainer.WAADirect",
                return_value=environment,
            ),
            patch.dict("sys.modules", {"torch": fake_torch}),
        ):
            with pytest.raises(RolloutLossError, match="no optimizer updates"):
                trainer.train()

        save_checkpoint.assert_not_called()

    def test_probe_failure_does_not_return_an_empty_group(self) -> None:
        trainer = GRPOTrainer(TrainingConfig())
        trainer._env = MagicMock()
        trainer._env.probe.return_value = {"screenshot_ok": False}

        with pytest.raises(RolloutInfrastructureError):
            trainer._collect_group("task-1")

    @pytest.mark.parametrize(
        "steps",
        [
            [],
            [RolloutStep(screenshot=b"not an image", action=MagicMock())],
        ],
    )
    def test_missing_loss_evidence_does_not_return_zero(self, steps) -> None:
        trainer = GRPOTrainer(TrainingConfig())
        trainer._model = MagicMock()
        rollout = Rollout(task_id="task-1", steps=steps)

        with pytest.raises(RolloutLossError):
            trainer._compute_rollout_loss(rollout, advantage=1.0, scale=1.0)

    def test_empty_training_group_does_not_return_zero_metrics(self) -> None:
        trainer = GRPOTrainer(TrainingConfig())
        with pytest.raises(RolloutLossError):
            trainer._training_step([])

    @pytest.mark.parametrize(
        "reward",
        [-0.1, 1.1, float("nan"), float("inf"), True, "1.0"],
    )
    def test_invalid_reward_cannot_become_an_advantage(self, reward) -> None:
        with pytest.raises(RolloutLossError, match="Reward 0"):
            compute_group_advantages([reward])

    def test_milestone_evaluator_failure_is_not_a_zero_reward(self) -> None:
        task = SimpleNamespace(
            milestones=[
                Milestone(
                    name="Saved",
                    check=TaskCheck(check="screenshot", description="Saved result"),
                )
            ]
        )
        with patch(
            "openadapt_evals.vlm_evaluator.vlm_judge",
            side_effect=RuntimeError("judge unavailable"),
        ):
            with pytest.raises(RolloutEvaluationError):
                evaluate_milestones_screenshot(task, _tiny_png())

    def test_measured_failed_milestone_remains_zero(self) -> None:
        task = SimpleNamespace(
            milestones=[
                Milestone(
                    name="Saved",
                    check=TaskCheck(check="screenshot", description="Saved result"),
                )
            ]
        )
        with patch(
            "openadapt_evals.vlm_evaluator.vlm_judge",
            return_value=(False, 0.1),
        ):
            assert evaluate_milestones_screenshot(task, _tiny_png()) == 0.0

    def test_unreadable_screenshot_is_not_a_measured_reward(self) -> None:
        task = SimpleNamespace(
            milestones=[
                Milestone(
                    name="Saved",
                    check=TaskCheck(check="screenshot", description="Saved result"),
                )
            ]
        )
        with patch("openadapt_evals.vlm_evaluator.vlm_judge") as judge:
            with pytest.raises(RolloutEvaluationError, match="decodable"):
                evaluate_milestones_screenshot(task, b"not an image")
        judge.assert_not_called()

    def test_malformed_judge_result_is_not_a_measured_reward(self) -> None:
        task = SimpleNamespace(
            milestones=[
                Milestone(
                    name="Saved",
                    check=TaskCheck(check="screenshot", description="Saved result"),
                )
            ]
        )
        with patch(
            "openadapt_evals.vlm_evaluator.vlm_judge",
            return_value=(True, float("nan")),
        ):
            with pytest.raises(RolloutEvaluationError, match="confidence"):
                evaluate_milestones_screenshot(task, _tiny_png())

    def test_nonfinite_loss_stops_before_optimizer_step(self) -> None:
        trainer = GRPOTrainer(TrainingConfig())
        trainer._optimizer = MagicMock()
        trainer._compute_rollout_loss = MagicMock(return_value=float("nan"))
        rollouts = [
            Rollout(task_id="task", reward=0.0),
            Rollout(task_id="task", reward=1.0),
        ]

        with pytest.raises(RolloutLossError, match="loss must be finite"):
            trainer._training_step(rollouts)
        trainer._optimizer.step.assert_not_called()

    def test_overflowed_loss_metrics_stop_before_optimizer_step(self) -> None:
        trainer = GRPOTrainer(TrainingConfig())
        trainer._optimizer = MagicMock()
        trainer._compute_rollout_loss = MagicMock(return_value=1e308)
        rollouts = [
            Rollout(task_id="task", reward=0.0),
            Rollout(task_id="task", reward=1.0),
        ]

        with pytest.raises(RolloutLossError, match="metrics must be finite"):
            trainer._training_step(rollouts)
        trainer._optimizer.step.assert_not_called()

    def test_nonfinite_gradient_stops_before_optimizer_step(self) -> None:
        torch = pytest.importorskip("torch")
        trainer = GRPOTrainer(TrainingConfig())
        trainer._optimizer = MagicMock()
        parameter = torch.nn.Parameter(torch.tensor(1.0))
        trainer._model = MagicMock()
        trainer._model.parameters.return_value = [parameter]

        def write_bad_gradient(*_args, **_kwargs):
            parameter.grad = torch.tensor(float("nan"))
            return 1.0

        trainer._compute_rollout_loss = MagicMock(side_effect=write_bad_gradient)
        rollouts = [
            Rollout(task_id="task", reward=0.0),
            Rollout(task_id="task", reward=1.0),
        ]

        with pytest.raises(RolloutLossError, match="non-finite gradient"):
            trainer._training_step(rollouts)
        trainer._optimizer.step.assert_not_called()


# ---------------------------------------------------------------------------
# Outlines integration
# ---------------------------------------------------------------------------


class TestOutlinesIntegration:
    """Verify the Outlines API the trainer depends on."""

    def test_imports(self) -> None:
        """The three outlines functions the trainer calls must exist."""
        try:
            import outlines
        except ImportError:
            pytest.skip("outlines not installed")
        assert callable(outlines.from_transformers)
        assert callable(outlines.regex)
        assert callable(outlines.Generator)

    def test_regex_compiles(self) -> None:
        """The action regex must compile without DFA explosion."""
        try:
            import outlines
        except ImportError:
            pytest.skip("outlines not installed")
        assert outlines.regex(GRPOTrainer._ACTION_REGEX) is not None

    def test_multimodal_accepts_list_not_dict(self) -> None:
        """TransformersMultiModal dispatches on list, not dict."""
        try:
            from outlines.models.transformers import TransformersMultiModalTypeAdapter
        except ImportError:
            pytest.skip("outlines not installed")
        fmt = TransformersMultiModalTypeAdapter.__dict__["format_input"]
        registered = set(fmt.dispatcher.registry.keys())
        assert list in registered, f"list not registered: {registered}"
        assert dict not in registered, "dict accepted — trainer uses list"

    def test_image_wrapper(self) -> None:
        """outlines.Image wraps PIL images (requires .format set)."""
        try:
            import outlines
        except ImportError:
            pytest.skip("outlines not installed")
        import io

        from PIL import Image as PILImage

        img = PILImage.new("RGB", (10, 10))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        assert outlines.Image(PILImage.open(buf)) is not None

    def test_generator_callable_with_kwargs(self) -> None:
        """Generator.__call__ must accept **inference_kwargs (max_new_tokens)."""
        try:
            import inspect

            from outlines.generator import SteerableGenerator
        except ImportError:
            pytest.skip("outlines not installed")
        sig = inspect.signature(SteerableGenerator.__call__)
        has_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
        assert has_kwargs, f"SteerableGenerator.__call__ needs **kwargs: {sig}"


# ---------------------------------------------------------------------------
# Generator cache
# ---------------------------------------------------------------------------


class TestGeneratorCache:
    """Verify the cache sentinel logic for _get_outlines_generator."""

    def test_starts_none(self) -> None:
        assert GRPOTrainer(TrainingConfig())._outlines_generator is None

    def test_false_means_failed(self) -> None:
        t = GRPOTrainer(TrainingConfig(constrained_decoding=True))
        t._outlines_generator = False
        assert t._get_outlines_generator() is None

    def test_cached_generator_returned(self) -> None:
        t = GRPOTrainer(TrainingConfig(constrained_decoding=True))
        t._outlines_generator = "mock"
        assert t._get_outlines_generator() == "mock"


# ---------------------------------------------------------------------------
# Task rotation
# ---------------------------------------------------------------------------


class TestTaskRotation:
    """Verify all tasks from task_dir load and rotate."""

    def test_all_tasks_loaded(self, tmp_path) -> None:
        import yaml

        for i in range(5):
            (tmp_path / f"t{i}.yaml").write_text(
                yaml.dump(
                    {
                        "name": f"Task {i}",
                        "id": f"task-{i}",
                        "setup": [],
                        "evaluate": [{"check": "screenshot", "description": "done"}],
                    }
                )
            )
        config = TrainingConfig(task_dir=str(tmp_path))
        GRPOTrainer(config)._load_task_configs()
        assert len(config.task_ids) == 5

    def test_explicit_ids_preserved(self, tmp_path) -> None:
        import yaml

        for i in range(3):
            (tmp_path / f"t{i}.yaml").write_text(
                yaml.dump(
                    {
                        "name": f"Task {i}",
                        "id": f"task-{i}",
                        "setup": [],
                        "evaluate": [],
                    }
                )
            )
        config = TrainingConfig(task_dir=str(tmp_path), task_ids=["task-1"])
        GRPOTrainer(config)._load_task_configs()
        assert config.task_ids == ["task-1"]

    def test_rotation_covers_all(self, tmp_path) -> None:
        import yaml

        for i in range(3):
            (tmp_path / f"t{i}.yaml").write_text(
                yaml.dump(
                    {
                        "name": f"Task {i}",
                        "id": f"task-{i}",
                        "setup": [],
                        "evaluate": [{"check": "screenshot", "description": "done"}],
                    }
                )
            )
        config = TrainingConfig(task_dir=str(tmp_path))
        GRPOTrainer(config)._load_task_configs()
        selected = {config.task_ids[s % len(config.task_ids)] for s in range(9)}
        assert selected == {"task-0", "task-1", "task-2"}
