"""Tests for PlannerTrajectoryLogger."""

from __future__ import annotations

import json

import pytest

from openadapt_evals.training.trajectory_logger import PlannerTrajectoryLogger

# -- Fixtures ----------------------------------------------------------------


@pytest.fixture
def output_dir(tmp_path):
    """Provide a temporary output directory."""
    return str(tmp_path / "trajectories")


@pytest.fixture
def logger_instance(output_dir):
    """Create a PlannerTrajectoryLogger with a temp directory."""
    return PlannerTrajectoryLogger(output_dir=output_dir)


@pytest.fixture
def sample_screenshot():
    """Minimal valid PNG bytes (1x1 white pixel)."""
    # Minimal 1x1 white PNG
    import struct
    import zlib

    def _make_png():
        sig = b"\x89PNG\r\n\x1a\n"

        def _chunk(chunk_type, data):
            c = chunk_type + data
            crc = struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
            return struct.pack(">I", len(data)) + c + crc

        ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
        raw = b"\x00\xFF\xFF\xFF"
        idat = zlib.compress(raw)
        return sig + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")

    return _make_png()


@pytest.fixture
def sample_a11y_tree():
    """Sample accessibility tree."""
    return {
        "role": "window",
        "name": "Desktop",
        "id": "1",
        "children": [
            {"role": "button", "name": "Settings", "id": "2"},
            {"role": "taskbar", "name": "Taskbar", "id": "3"},
        ],
    }


@pytest.fixture
def sample_planner_output():
    """Sample planner output dict."""
    return {
        "decision": "COMMAND",
        "instruction": "Click the Settings button",
        "reasoning": "Need to open settings to change theme",
    }


# -- Tests: Basic save and load ---------------------------------------------


class TestSaveAndLoad:
    def test_log_step_creates_jsonl(
        self, logger_instance, sample_screenshot, sample_a11y_tree, sample_planner_output
    ):
        """log_step creates a JSONL file with one record."""
        logger_instance.log_step(
            episode_id="ep_001",
            step_index=0,
            screenshot_bytes=sample_screenshot,
            a11y_tree=sample_a11y_tree,
            task_instruction="Open Settings and enable dark mode",
            action_history=[],
            planner_output=sample_planner_output,
        )

        assert logger_instance.jsonl_path.exists()
        lines = logger_instance.jsonl_path.read_text().strip().split("\n")
        assert len(lines) == 1

        record = json.loads(lines[0])
        assert record["episode_id"] == "ep_001"
        assert record["step_index"] == 0
        assert record["task_instruction"] == "Open Settings and enable dark mode"
        assert record["planner_output"]["decision"] == "COMMAND"
        assert record["action_history"] == []

    def test_log_step_saves_screenshot_png(
        self, logger_instance, sample_screenshot, sample_planner_output
    ):
        """log_step saves screenshot bytes as a PNG file."""
        logger_instance.log_step(
            episode_id="ep_001",
            step_index=0,
            screenshot_bytes=sample_screenshot,
            a11y_tree=None,
            task_instruction="Test task",
            action_history=[],
            planner_output=sample_planner_output,
        )

        screenshot_file = logger_instance.output_dir / "ep_001" / "step_000.png"
        assert screenshot_file.exists()
        assert screenshot_file.read_bytes() == sample_screenshot

    def test_log_step_no_screenshot(self, logger_instance, sample_planner_output):
        """log_step works when screenshot_bytes is None."""
        logger_instance.log_step(
            episode_id="ep_002",
            step_index=0,
            screenshot_bytes=None,
            a11y_tree=None,
            task_instruction="Test task",
            action_history=[],
            planner_output=sample_planner_output,
        )

        lines = logger_instance.jsonl_path.read_text().strip().split("\n")
        record = json.loads(lines[0])
        assert record["screenshot_path"] is None

        # No episode directory should be created
        ep_dir = logger_instance.output_dir / "ep_002"
        assert not ep_dir.exists()

    def test_multiple_steps_append_to_jsonl(
        self, logger_instance, sample_screenshot, sample_planner_output
    ):
        """Multiple log_step calls append to the same JSONL file."""
        for i in range(3):
            logger_instance.log_step(
                episode_id="ep_003",
                step_index=i,
                screenshot_bytes=sample_screenshot,
                a11y_tree=None,
                task_instruction="Multi-step task",
                action_history=[f"action_{j}" for j in range(i)],
                planner_output=sample_planner_output,
            )

        lines = logger_instance.jsonl_path.read_text().strip().split("\n")
        assert len(lines) == 3

        for i, line in enumerate(lines):
            record = json.loads(line)
            assert record["step_index"] == i
            assert len(record["action_history"]) == i

    def test_screenshot_path_is_relative(
        self, logger_instance, sample_screenshot, sample_planner_output
    ):
        """Screenshot path in JSONL is relative to output_dir."""
        logger_instance.log_step(
            episode_id="ep_004",
            step_index=5,
            screenshot_bytes=sample_screenshot,
            a11y_tree=None,
            task_instruction="Test",
            action_history=[],
            planner_output=sample_planner_output,
        )

        lines = logger_instance.jsonl_path.read_text().strip().split("\n")
        record = json.loads(lines[0])
        assert record["screenshot_path"] == "ep_004/step_005.png"
        # Should not contain absolute path
        assert not record["screenshot_path"].startswith("/")


# -- Tests: Failed episode cleanup ------------------------------------------


class TestFailedEpisodeCleanup:
    def test_end_episode_deletes_failed_directory(
        self, logger_instance, sample_screenshot, sample_planner_output
    ):
        """end_episode with reward <= 0 deletes the episode directory."""
        for i in range(3):
            logger_instance.log_step(
                episode_id="ep_fail",
                step_index=i,
                screenshot_bytes=sample_screenshot,
                a11y_tree=None,
                task_instruction="Failing task",
                action_history=[],
                planner_output=sample_planner_output,
            )

        ep_dir = logger_instance.output_dir / "ep_fail"
        assert ep_dir.exists()

        logger_instance.end_episode("ep_fail", reward=0.0)

        assert not ep_dir.exists()

    def test_end_episode_removes_jsonl_entries_for_failed(
        self, logger_instance, sample_screenshot, sample_planner_output
    ):
        """end_episode with reward <= 0 removes JSONL entries."""
        # Log steps for two episodes
        for i in range(2):
            logger_instance.log_step(
                episode_id="ep_good",
                step_index=i,
                screenshot_bytes=sample_screenshot,
                a11y_tree=None,
                task_instruction="Good task",
                action_history=[],
                planner_output=sample_planner_output,
            )
        for i in range(3):
            logger_instance.log_step(
                episode_id="ep_bad",
                step_index=i,
                screenshot_bytes=sample_screenshot,
                a11y_tree=None,
                task_instruction="Bad task",
                action_history=[],
                planner_output=sample_planner_output,
            )

        # 5 total lines
        lines = logger_instance.jsonl_path.read_text().strip().split("\n")
        assert len(lines) == 5

        # End the bad episode
        logger_instance.end_episode("ep_bad", reward=0.0)

        # Only 2 lines remain (the good episode)
        lines = logger_instance.jsonl_path.read_text().strip().split("\n")
        assert len(lines) == 2
        for line in lines:
            record = json.loads(line)
            assert record["episode_id"] == "ep_good"

    def test_end_episode_negative_reward_also_cleans_up(
        self, logger_instance, sample_screenshot, sample_planner_output
    ):
        """Negative reward also triggers cleanup."""
        logger_instance.log_step(
            episode_id="ep_neg",
            step_index=0,
            screenshot_bytes=sample_screenshot,
            a11y_tree=None,
            task_instruction="Negative reward task",
            action_history=[],
            planner_output=sample_planner_output,
        )

        logger_instance.end_episode("ep_neg", reward=-1.0)

        ep_dir = logger_instance.output_dir / "ep_neg"
        assert not ep_dir.exists()

        # JSONL should be empty
        content = logger_instance.jsonl_path.read_text().strip()
        assert content == ""

    def test_end_episode_preserves_successful(
        self, logger_instance, sample_screenshot, sample_planner_output
    ):
        """end_episode with reward > 0 preserves data and sets reward."""
        for i in range(2):
            logger_instance.log_step(
                episode_id="ep_success",
                step_index=i,
                screenshot_bytes=sample_screenshot,
                a11y_tree=None,
                task_instruction="Successful task",
                action_history=[],
                planner_output=sample_planner_output,
            )

        logger_instance.end_episode("ep_success", reward=1.0)

        # Directory should still exist
        ep_dir = logger_instance.output_dir / "ep_success"
        assert ep_dir.exists()

        # JSONL entries should have episode_reward
        lines = logger_instance.jsonl_path.read_text().strip().split("\n")
        assert len(lines) == 2
        for line in lines:
            record = json.loads(line)
            assert record["episode_reward"] == 1.0

    def test_end_episode_no_dir_exists(self, logger_instance):
        """end_episode handles missing directory gracefully."""
        # Should not raise even if no steps were logged
        logger_instance.end_episode("ep_nonexistent", reward=0.0)


# -- Tests: JSONL format validation ------------------------------------------


class TestJSONLFormat:
    def test_each_line_is_valid_json(
        self, logger_instance, sample_screenshot, sample_a11y_tree, sample_planner_output
    ):
        """Every line in the JSONL file is valid JSON."""
        for i in range(5):
            logger_instance.log_step(
                episode_id="ep_json",
                step_index=i,
                screenshot_bytes=sample_screenshot,
                a11y_tree=sample_a11y_tree,
                task_instruction="Validate JSON format",
                action_history=[f"step_{j}" for j in range(i)],
                planner_output=sample_planner_output,
            )

        lines = logger_instance.jsonl_path.read_text().strip().split("\n")
        assert len(lines) == 5

        for line in lines:
            record = json.loads(line)  # Should not raise
            assert isinstance(record, dict)

    def test_record_has_all_required_fields(
        self, logger_instance, sample_screenshot, sample_a11y_tree, sample_planner_output
    ):
        """Each JSONL record contains all expected fields."""
        logger_instance.log_step(
            episode_id="ep_fields",
            step_index=0,
            screenshot_bytes=sample_screenshot,
            a11y_tree=sample_a11y_tree,
            task_instruction="Check fields",
            action_history=["CLICK(0.5, 0.3)"],
            planner_output=sample_planner_output,
        )

        lines = logger_instance.jsonl_path.read_text().strip().split("\n")
        record = json.loads(lines[0])

        required_fields = {
            "episode_id",
            "step_index",
            "screenshot_path",
            "a11y_tree",
            "task_instruction",
            "action_history",
            "planner_output",
        }
        assert required_fields.issubset(set(record.keys()))

    def test_a11y_tree_truncated(self, logger_instance, sample_planner_output):
        """Large accessibility trees are truncated."""
        huge_tree = {
            "role": "window",
            "name": "X" * 10000,
            "children": [
                {"role": "item", "name": f"child_{i}"} for i in range(100)
            ],
        }

        traj_logger = PlannerTrajectoryLogger(
            output_dir=str(logger_instance.output_dir),
            max_a11y_chars=500,
        )

        traj_logger.log_step(
            episode_id="ep_truncate",
            step_index=0,
            screenshot_bytes=None,
            a11y_tree=huge_tree,
            task_instruction="Test truncation",
            action_history=[],
            planner_output=sample_planner_output,
        )

        lines = traj_logger.jsonl_path.read_text().strip().split("\n")
        record = json.loads(lines[0])
        assert len(record["a11y_tree"]) <= 503  # 500 + "..."
        assert record["a11y_tree"].endswith("...")


# -- Tests: Screenshot saving -----------------------------------------------


class TestScreenshotSaving:
    def test_screenshots_numbered_correctly(
        self, logger_instance, sample_screenshot, sample_planner_output
    ):
        """Screenshots are saved with zero-padded step numbers."""
        for i in range(3):
            logger_instance.log_step(
                episode_id="ep_screenshots",
                step_index=i,
                screenshot_bytes=sample_screenshot,
                a11y_tree=None,
                task_instruction="Screenshot test",
                action_history=[],
                planner_output=sample_planner_output,
            )

        ep_dir = logger_instance.output_dir / "ep_screenshots"
        assert (ep_dir / "step_000.png").exists()
        assert (ep_dir / "step_001.png").exists()
        assert (ep_dir / "step_002.png").exists()

    def test_multiple_episodes_separate_dirs(
        self, logger_instance, sample_screenshot, sample_planner_output
    ):
        """Different episodes get separate directories."""
        for ep_id in ["ep_a", "ep_b"]:
            logger_instance.log_step(
                episode_id=ep_id,
                step_index=0,
                screenshot_bytes=sample_screenshot,
                a11y_tree=None,
                task_instruction="Multi-episode test",
                action_history=[],
                planner_output=sample_planner_output,
            )

        assert (logger_instance.output_dir / "ep_a" / "step_000.png").exists()
        assert (logger_instance.output_dir / "ep_b" / "step_000.png").exists()

    def test_screenshot_bytes_preserved(
        self, logger_instance, sample_screenshot, sample_planner_output
    ):
        """Saved PNG file contains exact original bytes."""
        logger_instance.log_step(
            episode_id="ep_bytes",
            step_index=0,
            screenshot_bytes=sample_screenshot,
            a11y_tree=None,
            task_instruction="Byte test",
            action_history=[],
            planner_output=sample_planner_output,
        )

        saved = (logger_instance.output_dir / "ep_bytes" / "step_000.png").read_bytes()
        assert saved == sample_screenshot


# -- Tests: Integration with PlannerGrounderAgent ----------------------------


class TestAgentIntegration:
    def test_agent_logs_trajectory_on_act(
        self, output_dir, sample_screenshot, sample_planner_output
    ):
        """PlannerGrounderAgent calls trajectory_logger.log_step in act()."""

        from openadapt_evals.adapters.base import (
            BenchmarkAction,
            BenchmarkObservation,
            BenchmarkTask,
        )
        from openadapt_evals.agents.planner_grounder_agent import PlannerGrounderAgent

        # Mock planner and grounder
        class _Planner:
            def act(self, obs, task, history=None):
                return BenchmarkAction(
                    type="click", x=0.5, y=0.5,
                    raw_action={"instruction": "Click Settings"},
                )

        class _Grounder:
            def act(self, obs, task, history=None):
                return BenchmarkAction(type="click", x=0.8, y=0.3)

        traj_logger = PlannerTrajectoryLogger(output_dir=output_dir)

        agent = PlannerGrounderAgent(
            planner=_Planner(),
            grounder=_Grounder(),
            trajectory_logger=traj_logger,
        )

        observation = BenchmarkObservation(
            screenshot=sample_screenshot,
            viewport=(1920, 1200),
            accessibility_tree={"role": "window", "name": "Desktop", "id": "1"},
        )
        task = BenchmarkTask(
            task_id="test_task",
            instruction="Open Settings",
            domain="desktop",
        )

        agent.act(observation, task)

        assert traj_logger.jsonl_path.exists()
        lines = traj_logger.jsonl_path.read_text().strip().split("\n")
        assert len(lines) == 1

        record = json.loads(lines[0])
        assert record["episode_id"] == "test_task"
        assert record["step_index"] == 0
        assert record["planner_output"]["decision"] == "COMMAND"

    def test_agent_without_logger_works(self):
        """PlannerGrounderAgent works fine without trajectory_logger."""
        from openadapt_evals.adapters.base import (
            BenchmarkAction,
            BenchmarkObservation,
            BenchmarkTask,
        )
        from openadapt_evals.agents.planner_grounder_agent import PlannerGrounderAgent

        class _Planner:
            def act(self, obs, task, history=None):
                return BenchmarkAction(type="done", raw_action={"reasoning": "done"})

        class _Grounder:
            def act(self, obs, task, history=None):
                return BenchmarkAction(type="click", x=0.5, y=0.5)

        agent = PlannerGrounderAgent(
            planner=_Planner(),
            grounder=_Grounder(),
        )

        observation = BenchmarkObservation(
            screenshot=b"\x89PNG\r\n\x1a\nfake",
            viewport=(1920, 1200),
        )
        task = BenchmarkTask(
            task_id="test_no_logger",
            instruction="Do nothing",
            domain="desktop",
        )

        action = agent.act(observation, task)
        assert action.type == "done"

    def test_agent_reset_clears_step_index(self, output_dir, sample_screenshot):
        """Agent.reset() resets the trajectory step counter."""
        from openadapt_evals.adapters.base import (
            BenchmarkAction,
            BenchmarkObservation,
            BenchmarkTask,
        )
        from openadapt_evals.agents.planner_grounder_agent import PlannerGrounderAgent

        class _Planner:
            def act(self, obs, task, history=None):
                return BenchmarkAction(
                    type="click", x=0.5, y=0.5,
                    raw_action={"instruction": "Click X"},
                )

        class _Grounder:
            def act(self, obs, task, history=None):
                return BenchmarkAction(type="click", x=0.8, y=0.3)

        traj_logger = PlannerTrajectoryLogger(output_dir=output_dir)

        agent = PlannerGrounderAgent(
            planner=_Planner(),
            grounder=_Grounder(),
            trajectory_logger=traj_logger,
        )

        observation = BenchmarkObservation(
            screenshot=sample_screenshot,
            viewport=(1920, 1200),
        )
        task = BenchmarkTask(
            task_id="test_reset",
            instruction="Test reset",
            domain="desktop",
        )

        # Act twice
        agent.act(observation, task)
        agent.act(observation, task)
        assert agent._step_index == 2

        # Reset
        agent.reset()
        assert agent._step_index == 0

        # Act again - should start from 0
        agent.act(observation, task)
        assert agent._step_index == 1
