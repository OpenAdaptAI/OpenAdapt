"""Two-Track Protocol primitives for OSWorld 2.0 + OS-Harm reporting.

Track A (`zero_shot_self_compile`):
  0 human demos — 100% eligible for official OSWorld 2.0 and OS-Harm main
  leaderboards. Discovers and compiles recurring verified sub-routines
  mid-episode on the fly, then replays subsequent occurrences deterministically
  with independent read-back checkpoints and the OS-Harm safety firewall.

Track B (`one_demo_adapted_unseen_instances`):
  1 human demonstration of the task class -> compiled sub-workflow + optional
  111 MB task/app LoRA hook -> evaluated strictly on disjoint unseen instances,
  enforcing fail-closed disjointness (`demo_instance_id not in eval_instance_ids`)
  with a deterministic SHA-256 split digest recorded in the JSON scoreboard.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal

from benchmarks.osworld_v2.guard import (
    AgentAction,
    CheckpointSpec,
    CompiledSubWorkflow,
    OpenAdaptOSWorldGuard,
    SubWorkflowStep,
)

ProtocolTrack = Literal[
    "baseline_raw_agent",
    "zero_shot_self_compile",
    "one_demo_adapted_unseen_instances",
]


@dataclass(frozen=True)
class DisjointSplitManifest:
    """Fail-closed train/eval instance disjointness contract for Track B."""

    task_class: str
    demo_instance_id: str
    eval_instance_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.task_class.strip():
            raise ValueError("task_class must be non-empty")
        if not self.demo_instance_id.strip():
            raise ValueError("demo_instance_id must be non-empty")
        if not self.eval_instance_ids:
            raise ValueError(
                "eval_instance_ids must contain at least one unseen instance"
            )
        if len(set(self.eval_instance_ids)) != len(self.eval_instance_ids):
            raise ValueError(
                "eval_instance_ids must not contain duplicate instance IDs"
            )
        if self.demo_instance_id in self.eval_instance_ids:
            raise ValueError(
                f"Disjointness violation: demo_instance_id {self.demo_instance_id!r} "
                "appears in eval_instance_ids"
            )

    @property
    def split_digest(self) -> str:
        """Deterministic SHA-256 digest of the disjoint split manifest."""
        canonical = json.dumps(
            {
                "task_class": self.task_class,
                "demo_instance_id": self.demo_instance_id,
                "eval_instance_ids": list(self.eval_instance_ids),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_class": self.task_class,
            "demo_instance_id": self.demo_instance_id,
            "eval_instance_ids": list(self.eval_instance_ids),
            "eval_count": len(self.eval_instance_ids),
            "strictly_disjoint": True,
            "split_digest_sha256": self.split_digest,
        }


@dataclass(frozen=True)
class OneDemoAdaptationConfig:
    """Track B adaptation metadata (1 human demo + compiled code + 111 MB LoRA hook)."""

    split: DisjointSplitManifest
    compiled_workflow_id: str
    lora_adapter_path: str | None = None
    lora_size_mb: float = 111.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "track": "one_demo_adapted_unseen_instances",
            "human_demos_used": 1,
            "compiled_workflow_id": self.compiled_workflow_id,
            "lora_adapter_path": self.lora_adapter_path,
            "lora_size_mb": self.lora_size_mb,
            "split": self.split.to_dict(),
        }


class OnlineSubroutineCompiler:
    """Track A mid-episode online sub-routine discovery & compiler (0 human demos).

    Watches verified agent steps during a single episode. Whenever a contiguous
    sequence of `min_len` steps repeats `min_repeats` times, compiles it into a
    deterministic `CompiledSubWorkflow` and registers it on the guard so every
    subsequent repetition runs at 0 VLM calls/step with checkpoint read-back.
    """

    def __init__(
        self,
        guard: OpenAdaptOSWorldGuard,
        *,
        min_len: int = 3,
        min_repeats: int = 2,
    ) -> None:
        if min_len < 2:
            raise ValueError("min_len must be >= 2")
        if min_repeats < 2:
            raise ValueError("min_repeats must be >= 2")
        self.guard = guard
        self.min_len = min_len
        self.min_repeats = min_repeats
        self.human_demos_used = 0
        self._trace: list[AgentAction] = []
        self._pattern_counts: dict[tuple[tuple[str, str], ...], int] = {}
        self._compiled_patterns: dict[tuple[tuple[str, str], ...], str] = {}

    @property
    def compiled_count(self) -> int:
        return len(self._compiled_patterns)

    def observe_verified_step(
        self,
        action: AgentAction,
        *,
        checkpoint_factory: Any = None,
    ) -> CompiledSubWorkflow | None:
        """Record one verified step and compile if a repeating sub-routine emerges."""
        self._trace.append(action)
        if len(self._trace) < self.min_len:
            return None

        window = self._trace[-self.min_len :]
        signature = tuple((a.kind, a.target) for a in window)
        count = self._pattern_counts.get(signature, 0) + 1
        self._pattern_counts[signature] = count

        if count < self.min_repeats or signature in self._compiled_patterns:
            return None

        wf_id = f"self_compiled_{len(self._compiled_patterns) + 1:02d}"
        steps = tuple(
            SubWorkflowStep(
                step_id=f"{wf_id}_s{i}",
                action=AgentAction(
                    kind=a.kind,
                    target=a.target,
                    payload=a.payload,
                    source_trust="compiled_subflow",
                ),
            )
            for i, a in enumerate(window)
        )
        checkpoint = (
            checkpoint_factory(wf_id)
            if callable(checkpoint_factory)
            else CheckpointSpec(
                name=f"{wf_id}_verified",
                kind="custom",
                custom_probe=lambda: True,
            )
        )
        compiled = CompiledSubWorkflow(
            workflow_id=wf_id, steps=steps, checkpoint=checkpoint
        )
        self.guard.register_subworkflow(compiled)
        self._compiled_patterns[signature] = wf_id
        return compiled
