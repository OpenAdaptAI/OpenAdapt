"""Planner trajectory logger for SFT training data collection.

Hooks into PlannerGrounderAgent to save each planner call's inputs and
outputs during evaluation episodes. Successful episodes (reward > 0)
provide free supervised fine-tuning data for planner distillation.

Each step saves:
- Screenshot PNG to ``{output_dir}/{episode_id}/step_{N:03d}.png``
- JSONL line to ``{output_dir}/trajectories.jsonl`` with all planner
  inputs/outputs and episode metadata.

On ``end_episode``, if reward <= 0 the episode directory is deleted
since failed episodes are not useful for SFT.

Usage::

    from openadapt_evals.training.trajectory_logger import PlannerTrajectoryLogger

    logger = PlannerTrajectoryLogger(output_dir="./planner_trajectories")
    agent = PlannerGrounderAgent(
        planner="claude-sonnet-4-20250514",
        grounder="gpt-4.1-mini",
        trajectory_logger=logger,
    )

    # During evaluation, logger.log_step() is called automatically
    # after each planner call. Call end_episode() when done.
    logger.end_episode(episode_id, reward=1.0)
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Maximum characters for a11y tree in JSONL to keep file sizes reasonable.
_MAX_A11Y_CHARS = 4000


class PlannerTrajectoryLogger:
    """Save planner trajectories to JSONL files for SFT training.

    Thread-safe for single-episode use. For parallel episodes, use
    separate logger instances.

    Args:
        output_dir: Directory to save trajectory data. Created if needed.
        max_a11y_chars: Maximum characters for a11y tree truncation.
    """

    def __init__(
        self,
        output_dir: str,
        max_a11y_chars: int = _MAX_A11Y_CHARS,
        keep_failed: bool = False,
    ) -> None:
        self._keep_failed = keep_failed
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._max_a11y_chars = max_a11y_chars
        self._jsonl_path = self._output_dir / "trajectories.jsonl"

        logger.info(
            "PlannerTrajectoryLogger initialized: output_dir=%s", self._output_dir
        )

    @property
    def output_dir(self) -> Path:
        """Return the output directory path."""
        return self._output_dir

    @property
    def jsonl_path(self) -> Path:
        """Return the path to the JSONL file."""
        return self._jsonl_path

    def log_step(
        self,
        episode_id: str,
        step_index: int,
        screenshot_bytes: bytes | None,
        a11y_tree: dict | None,
        task_instruction: str,
        action_history: list[str],
        planner_output: dict[str, Any],
    ) -> None:
        """Log one planner step.

        Saves screenshot as PNG and appends a JSONL record with all
        planner inputs and outputs.

        Args:
            episode_id: Unique identifier for the episode.
            step_index: Zero-based step index within the episode.
            screenshot_bytes: PNG screenshot bytes (may be None).
            a11y_tree: Accessibility tree dict (may be None).
            task_instruction: Natural language task instruction.
            action_history: List of previous action strings.
            planner_output: Dict with decision, instruction, reasoning
                from the planner.
        """
        # Save screenshot
        screenshot_path: str | None = None
        if screenshot_bytes:
            ep_dir = self._output_dir / episode_id
            ep_dir.mkdir(parents=True, exist_ok=True)
            screenshot_filename = f"step_{step_index:03d}.png"
            screenshot_file = ep_dir / screenshot_filename
            screenshot_file.write_bytes(screenshot_bytes)
            screenshot_path = str(screenshot_file.relative_to(self._output_dir))

        # Truncate a11y tree
        a11y_str: str | None = None
        if a11y_tree is not None:
            a11y_str = json.dumps(a11y_tree, ensure_ascii=False)
            if len(a11y_str) > self._max_a11y_chars:
                a11y_str = a11y_str[: self._max_a11y_chars] + "..."

        # Build JSONL record
        record = {
            "episode_id": episode_id,
            "step_index": step_index,
            "screenshot_path": screenshot_path,
            "a11y_tree": a11y_str,
            "task_instruction": task_instruction,
            "action_history": action_history,
            "planner_output": planner_output,
        }

        # Append to JSONL
        with open(self._jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        logger.debug(
            "Logged step %d for episode %s (screenshot=%s)",
            step_index,
            episode_id,
            screenshot_path,
        )

    def end_episode(self, episode_id: str, reward: float) -> None:
        """Mark episode complete with reward.

        If reward <= 0, deletes the episode screenshot directory and
        removes that episode's entries from the JSONL file since failed
        episodes are not useful for SFT.

        Appends an episode-level metadata line with the reward.

        Args:
            episode_id: Unique identifier for the episode.
            reward: Final episode reward (typically 0.0 or 1.0).
        """
        ep_dir = self._output_dir / episode_id

        if reward <= 0 and not self._keep_failed:
            # Delete screenshot directory for failed episodes
            if ep_dir.exists():
                shutil.rmtree(ep_dir)
                logger.info(
                    "Deleted failed episode directory: %s (reward=%.2f)",
                    ep_dir,
                    reward,
                )

            # Remove JSONL entries for this episode
            self._remove_episode_from_jsonl(episode_id)
            logger.info(
                "Removed JSONL entries for failed episode %s", episode_id
            )
            return

        # For successful episodes, update JSONL entries with reward
        self._set_episode_reward(episode_id, reward)
        logger.info(
            "Episode %s completed with reward=%.2f", episode_id, reward
        )

    def _remove_episode_from_jsonl(self, episode_id: str) -> None:
        """Remove all JSONL entries for a given episode.

        Reads the file, filters out matching lines, and rewrites it.
        """
        if not self._jsonl_path.exists():
            return

        lines = self._jsonl_path.read_text(encoding="utf-8").splitlines()
        kept = []
        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                if record.get("episode_id") != episode_id:
                    kept.append(line)
            except json.JSONDecodeError:
                kept.append(line)

        self._jsonl_path.write_text(
            "\n".join(kept) + ("\n" if kept else ""), encoding="utf-8"
        )

    def _set_episode_reward(self, episode_id: str, reward: float) -> None:
        """Set the episode_reward field on all JSONL entries for an episode."""
        if not self._jsonl_path.exists():
            return

        lines = self._jsonl_path.read_text(encoding="utf-8").splitlines()
        updated = []
        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                if record.get("episode_id") == episode_id:
                    record["episode_reward"] = reward
                    updated.append(json.dumps(record, ensure_ascii=False))
                else:
                    updated.append(line)
            except json.JSONDecodeError:
                updated.append(line)

        self._jsonl_path.write_text(
            "\n".join(updated) + ("\n" if updated else ""), encoding="utf-8"
        )
