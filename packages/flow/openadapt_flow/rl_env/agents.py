"""Reference agents for validating an environment.

``scripted`` replays a task's reference solution: it shows the task can
reach the maximum reward. ``random`` picks uniformly among the visible
elements and the action kinds, then reports a random claim: it measures the
reward floor. Neither calls a model.
"""

from __future__ import annotations

import random
import string
from typing import Any, Iterator, Optional

from openadapt_flow.rl_env.tasks import Task


def scripted(task: Task) -> Iterator[dict[str, Any]]:
    yield from task.solution


class RandomAgent:
    def __init__(self, seed: int, *, steps: int = 12) -> None:
        self.rng = random.Random(seed)
        self.steps = steps

    def act(self, observation: dict[str, Any], step: int) -> dict[str, Any]:
        if step >= self.steps:
            return {"kind": "done", "claim": self.rng.choice(["success", "failure"])}
        elements = [e for e in observation.get("elements", []) if e.get("selector")]
        kind = self.rng.choice(["click", "click", "click", "fill", "press", "scroll"])
        target: Optional[dict[str, Any]] = (
            self.rng.choice(elements) if elements else None
        )
        if kind == "click" and target:
            return {"kind": "click", "selector": target["selector"]}
        if kind == "fill" and target and target["tag"] in ("input", "textarea"):
            text = "".join(self.rng.choices(string.ascii_letters + " ", k=12))
            return {"kind": "fill", "selector": target["selector"], "text": text}
        if kind == "press":
            return {
                "kind": "press",
                "text": self.rng.choice(["Enter", "Tab", "Escape"]),
            }
        if kind == "scroll":
            return {"kind": "scroll", "dy": self.rng.choice([-400, 400])}
        return {
            "kind": "click",
            "x": self.rng.randrange(0, 1280),
            "y": self.rng.randrange(0, 800),
        }
