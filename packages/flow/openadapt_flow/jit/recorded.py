"""Replay a recorded CUA trajectory as the agent, with no model calls.

A trajectory file holds the native actions an agent sent in one run, in its
own format, with the task id and inputs that run had::

    {"dialect": "anthropic", "task_id": "mockmed.add_note",
     "inputs": {"note": "..."}, "actions": [{"action": "screenshot"}, ...]}

:class:`RecordedTrajectoryAgent` sends those actions through the capture
computer in order. It refuses a request for a different task or different
inputs, because the actions already contain the inputs they were run with.
Tests and the example use it in place of a live model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openadapt_flow.jit.adapters import DIALECTS, CaptureComputer
from openadapt_flow.jit.compile_agent import TaskRequest

__all__ = ["RecordedTrajectoryAgent"]


@dataclass
class RecordedTrajectoryAgent:
    dialect: str
    task_id: str
    inputs: dict[str, Any]
    actions: list[dict[str, Any]]
    #: How many times :meth:`run` was called.
    calls: int = field(default=0)

    def __post_init__(self) -> None:
        if self.dialect not in DIALECTS:
            raise ValueError(f"unknown dialect {self.dialect!r}")

    @classmethod
    def from_file(cls, path: Path | str) -> "RecordedTrajectoryAgent":
        raw = json.loads(Path(path).read_text())
        return cls(
            dialect=str(raw["dialect"]),
            task_id=str(raw["task_id"]),
            inputs=dict(raw.get("inputs") or {}),
            actions=[dict(action) for action in raw["actions"]],
        )

    def run(self, request: TaskRequest, computer: CaptureComputer) -> None:
        self.calls += 1
        if request.task_id != self.task_id or request.inputs != self.inputs:
            raise ValueError(
                "this trajectory was recorded for task "
                f"{self.task_id!r} with other inputs; it cannot do this request"
            )
        for action in self.actions:
            computer.execute(action)
