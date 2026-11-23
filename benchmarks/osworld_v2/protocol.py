"""Two-Track Protocol primitives for desktop adapter reporting.

Track A (`zero_shot_self_compile`):
  0 human demos. Discovers and compiles recurring safe sub-routines mid-episode
  on the fly. Every observed step is checked by `guard.check_action` before
  recording, preserves its original `source_trust` (never launders untrusted
  provenance), and requires a real read-back checkpoint (fails closed if none is
  provided).

Track B (`one_demo_adapted_unseen_instances`):
  1 human demonstration of a task class evaluated strictly on disjoint unseen
  instances, enforcing fail-closed disjointness (`demo_instance_id not in
  eval_instance_ids`) with a deterministic SHA-256 split digest.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
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
    """Track B adaptation metadata (1 human demo + compiled workflow + optional adapter hook)."""

    split: DisjointSplitManifest
    compiled_workflow_id: str
    adapter_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "track": "one_demo_adapted_unseen_instances",
            "human_demos_used": 1,
            "compiled_workflow_id": self.compiled_workflow_id,
            "adapter_path": self.adapter_path,
            "split": self.split.to_dict(),
        }


class OnlineSubroutineCompiler:
    """Track A mid-episode online sub-routine discovery & compiler (0 human demos).

    Security invariants:
    - Every observed step is verified via `guard.check_action(action)` BEFORE
      entering the trace; halted/untrusted actions are never recorded or compiled.
    - Compiled steps preserve their original `source_trust` (no provenance
      laundering).
    - If no `checkpoint_factory` is supplied, the compiled sub-workflow carries a
      fail-closed checkpoint (`custom_probe=None`) that returns `False` until a
      real independent read-back probe is bound.
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
        self._pattern_counts: dict[tuple[tuple[str, str, str], ...], int] = {}
        self._compiled_patterns: dict[tuple[tuple[str, str, str], ...], str] = {}

    @property
    def compiled_count(self) -> int:
        return len(self._compiled_patterns)

    def observe_verified_step(
        self,
        action: AgentAction,
        *,
        checkpoint_factory: Callable[[str], CheckpointSpec] | None = None,
    ) -> CompiledSubWorkflow | None:
        """Validate one step via `guard.check_action` and compile if a safe sub-routine repeats."""
        verdict = self.guard.check_action(action)
        if verdict.decision != "ALLOW":
            # Break the contiguous trace so a compiled sub-routine never bridges across a halted step.
            self._trace.clear()
            return None

        self._trace.append(action)
        if len(self._trace) < self.min_len:
            return None

        window = self._trace[-self.min_len :]
        signature = tuple((a.kind, a.target, a.source_trust) for a in window)
        count = self._pattern_counts.get(signature, 0) + 1
        self._pattern_counts[signature] = count

        if count < self.min_repeats or signature in self._compiled_patterns:
            return None

        wf_id = f"self_compiled_{len(self._compiled_patterns) + 1:02d}"
        # Preserve original source_trust — never launder provenance!
        steps = tuple(
            SubWorkflowStep(
                step_id=f"{wf_id}_s{i}",
                action=AgentAction(
                    kind=a.kind,
                    target=a.target,
                    payload=a.payload,
                    source_trust=a.source_trust,
                    observed_context=a.observed_context,
                    referenced_paths=a.referenced_paths,
                ),
            )
            for i, a in enumerate(window)
        )
        checkpoint = (
            checkpoint_factory(wf_id)
            if checkpoint_factory is not None
            else CheckpointSpec(
                name=f"{wf_id}_unbound_fail_closed",
                kind="custom",
                custom_probe=None,
            )
        )
        compiled = CompiledSubWorkflow(
            workflow_id=wf_id, steps=steps, checkpoint=checkpoint
        )
        self.guard.register_subworkflow(compiled)
        self._compiled_patterns[signature] = wf_id
        return compiled
