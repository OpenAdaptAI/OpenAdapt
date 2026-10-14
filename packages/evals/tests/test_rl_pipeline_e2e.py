"""Synthetic end-to-end test of the RL training pipeline.

Validates the full chain: TaskConfig YAML → RLEnvironment → collect_rollout
→ dense rewards → output dict matching TRL rollout_func signature.

Uses WAAMockAdapter — no VM or GPU required. When the enterprise customer
is ready with real tasks, the only change is swapping MockAdapter for
WAALiveAdapter.
"""

from __future__ import annotations

import textwrap
from unittest.mock import MagicMock, patch

import pytest

from openadapt_evals.adapters.base import (
    BenchmarkAction,
    BenchmarkObservation,
    BenchmarkResult,
    BenchmarkTask,
)
from openadapt_evals.adapters.rl_env import ResetConfig, RLEnvironment
from openadapt_evals.task_config import TaskConfig


def _make_mock_adapter(step_count_to_done: int = 3):
    """Create a mock adapter that terminates after N steps."""
    adapter = MagicMock()
    call_count = {"n": 0}

    def mock_step(action):
        call_count["n"] += 1
        done = call_count["n"] >= step_count_to_done
        return (
            BenchmarkObservation(
                screenshot=b"\x89PNG\r\n\x1a\n" + b"\x00" * 100,  # minimal PNG header
                raw_observation={},
            ),
            done,
            {"step": call_count["n"]},
        )

    adapter.step.side_effect = mock_step
    adapter.reset.return_value = BenchmarkObservation(
        screenshot=b"\x89PNG\r\n\x1a\n" + b"\x00" * 100,
        raw_observation={},
    )
    adapter.load_task.return_value = BenchmarkTask(
        task_id="test-task", instruction="Test", domain="desktop"
    )
    adapter.load_task_from_json.return_value = BenchmarkTask(
        task_id="test-task", instruction="Test", domain="desktop"
    )
    adapter.evaluate.return_value = BenchmarkResult(
        task_id="test-task", success=False, score=0.0
    )
    adapter.config = MagicMock(server_url="http://mock:5001")
    return adapter


class TestRLPipelineE2E:
    """Full pipeline test: YAML → Environment → Rollout → Dense Rewards."""

    def test_full_pipeline_with_milestones(self, tmp_path):
        """Simulate a 3-step task where agent passes 2/3 milestones."""
        # 1. Write a task YAML
        task_yaml = tmp_path / "test_task.yaml"
        task_yaml.write_text(
            textwrap.dedent("""\
            name: "Test task with milestones"
            id: test-task
            setup:
              - sleep: 1
            evaluate:
              - check: command
                run: "echo done"
                expect: "done"
            milestones:
              - name: "Step 1 done"
                check: command
                run: "echo pass"
                expect: "pass"
                match: exact
              - name: "Step 2 done"
                check: command
                run: "echo pass"
                expect: "pass"
                match: exact
              - name: "Step 3 NOT done"
                check: command
                run: "echo fail"
                expect: "pass"
                match: exact
            max_steps: 5
            """)
        )

        # 2. Load TaskConfig
        tc = TaskConfig.from_yaml(str(task_yaml))
        assert tc.name == "Test task with milestones"
        assert len(tc.milestones) == 3

        # 3. Create environment with mock adapter
        adapter = _make_mock_adapter(step_count_to_done=3)
        env = RLEnvironment(adapter, task_config=tc)

        # 4. Define a simple agent
        def agent_fn(obs):
            return BenchmarkAction(type="click", x=100, y=100)

        # 5. Collect rollout with dense rewards
        with patch.object(TaskConfig, "_run_vm_command") as mock_cmd:
            # First two milestones pass, third fails
            mock_cmd.side_effect = ["pass", "pass", "fail"]
            trajectory = env.collect_rollout(agent_fn, max_steps=5, task_id="test-task")

        # 6. Verify trajectory
        assert len(trajectory) == 3  # 3 steps before done
        assert trajectory[-1].done is True

        # 7. Verify dense reward
        final_reward = trajectory[-1].reward
        assert final_reward == pytest.approx(2 / 3, abs=0.01)  # 2/3 milestones

        # 8. Verify info contains milestone details
        info = trajectory[-1].info
        assert info["milestones_passed"] == 2
        assert info["milestones_total"] == 3
        assert "milestone_score" in info

    def test_pipeline_binary_fallback_no_milestones(self, tmp_path):
        """Without milestones, falls back to binary evaluation."""
        task_yaml = tmp_path / "binary_task.yaml"
        task_yaml.write_text(
            textwrap.dedent("""\
            name: "Binary task"
            id: binary-task
            evaluate:
              - check: command
                run: "echo ok"
                expect: "ok"
            """)
        )

        tc = TaskConfig.from_yaml(str(task_yaml))
        adapter = _make_mock_adapter(step_count_to_done=2)
        adapter.evaluate.return_value = BenchmarkResult(
            task_id="binary-task", success=True, score=1.0
        )
        env = RLEnvironment(adapter, task_config=tc)

        def agent_fn(obs):
            return BenchmarkAction(type="click", x=50, y=50)

        trajectory = env.collect_rollout(agent_fn, max_steps=5, task_id="binary-task")

        # No milestones → binary evaluation
        assert trajectory[-1].reward == 1.0

    def test_rollout_func_output_shape(self, tmp_path):
        """Verify output matches TRL rollout_func return signature."""
        task_yaml = tmp_path / "shape_task.yaml"
        task_yaml.write_text(
            textwrap.dedent("""\
            name: "Shape test"
            id: shape-task
            evaluate:
              - check: screenshot
                description: "Task is done"
            milestones:
              - name: "M1"
                check: command
                run: "echo 1"
                expect: "1"
                match: exact
            max_steps: 3
            """)
        )

        tc = TaskConfig.from_yaml(str(task_yaml))
        adapter = _make_mock_adapter(step_count_to_done=2)
        env = RLEnvironment(adapter, task_config=tc)

        def agent_fn(obs):
            return BenchmarkAction(type="click", x=50, y=50)

        with patch.object(TaskConfig, "_run_vm_command", return_value="1"):
            trajectory = env.collect_rollout(agent_fn, max_steps=3, task_id="shape-task")

        # Simulate what a rollout_func would return to TRL
        rollout_result = {
            "prompt_ids": [[1, 2, 3]],  # would be real token IDs
            "completion_ids": [[4, 5, 6]],
            "logprobs": [[-0.5, -0.3, -0.1]],
            "env_reward": [trajectory[-1].reward],
        }

        # Verify shape matches TRL expectations
        assert "prompt_ids" in rollout_result
        assert "completion_ids" in rollout_result
        assert "logprobs" in rollout_result
        assert "env_reward" in rollout_result
        assert isinstance(rollout_result["env_reward"][0], float)
        assert 0.0 <= rollout_result["env_reward"][0] <= 1.0

    def test_multiple_rollouts_produce_reward_variance(self, tmp_path):
        """GRPO needs reward variance. Dense rewards provide it."""
        task_yaml = tmp_path / "variance_task.yaml"
        task_yaml.write_text(
            textwrap.dedent("""\
            name: "Variance test"
            id: variance-task
            evaluate:
              - check: command
                run: "echo ok"
                expect: "ok"
            milestones:
              - name: "M1"
                check: command
                run: "echo x"
                expect: "pass"
                match: exact
              - name: "M2"
                check: command
                run: "echo x"
                expect: "pass"
                match: exact
              - name: "M3"
                check: command
                run: "echo x"
                expect: "pass"
                match: exact
            max_steps: 3
            """)
        )

        tc = TaskConfig.from_yaml(str(task_yaml))
        rewards = []

        # Simulate N=4 rollouts with varying milestone success
        milestone_results = [
            ["pass", "pass", "pass"],  # 3/3 = 1.0
            ["pass", "pass", "fail"],  # 2/3 = 0.67
            ["pass", "fail", "fail"],  # 1/3 = 0.33
            ["fail", "fail", "fail"],  # 0/3 = 0.0
        ]

        for ms_results in milestone_results:
            adapter = _make_mock_adapter(step_count_to_done=2)
            env = RLEnvironment(adapter, task_config=tc)

            def agent_fn(obs):
                return BenchmarkAction(type="click", x=50, y=50)

            with patch.object(TaskConfig, "_run_vm_command") as mock_cmd:
                mock_cmd.side_effect = ms_results
                trajectory = env.collect_rollout(
                    agent_fn, max_steps=3, task_id="variance-task"
                )
            rewards.append(trajectory[-1].reward)

        # Verify reward variance exists (GRPO can compute advantages)
        assert len(set(rewards)) > 1, f"No variance in rewards: {rewards}"
        assert max(rewards) > min(rewards)
        # Rewards should be approximately [1.0, 0.67, 0.33, 0.0]
        assert rewards[0] > rewards[1] > rewards[2] > rewards[3]

    def test_load_example_tasks_and_create_environments(self):
        """All example YAML files produce valid environments."""
        import os

        example_dir = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "example_tasks"
        )
        if not os.path.isdir(example_dir):
            pytest.skip("example_tasks/ not found")

        tasks = TaskConfig.from_dir(example_dir)
        assert len(tasks) >= 3

        for tc in tasks:
            adapter = _make_mock_adapter()
            env = RLEnvironment(adapter, task_config=tc)

            # Verify environment can be created and reset
            env.reset(config=ResetConfig(task_id=tc.id))
            assert env._current_task is not None
            assert env._current_task.instruction == tc.name
