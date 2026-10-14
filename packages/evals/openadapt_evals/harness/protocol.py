"""The unified ``Environment`` protocol for the lightweight meta-benchmark.

This is a **consolidation over existing code**, not new infrastructure. The
repo already has three parallel abstractions:

- :class:`openadapt_evals.adapters.base.BenchmarkAdapter` -- ``list_tasks`` /
  ``load_task`` / ``reset`` / ``step`` / ``evaluate`` (WAA, mock, local, ...).
- :class:`openadapt_evals.evaluation.verifier_registry.TaskVerifierRegistry` --
  declaratively-registered task verifiers returning
  :class:`~openadapt_evals.evaluation.verifier_registry.VerificationResult`.
- The flow-side ``EffectVerifier`` protocol (openadapt-flow) that rules
  CONFIRMED / REFUTED / INDETERMINATE against a real system of record.

Each benchmark family scores success a DIFFERENT way (WAA's native evaluator,
a registry verifier reading VM state, an effect verifier reading a FHIR/REST
system of record). The meta-benchmark needs ONE way to ask "did this task
actually succeed?" regardless of family. :class:`Environment` provides that:
its :meth:`~Environment.verify` is the single entry point that folds
``BenchmarkAdapter.evaluate`` + ``TaskVerifierRegistry`` + ``EffectVerifier``
into one call that always returns a :class:`VerificationResult`.

Nothing here is heavy: it imports only the dependency-free canonical types from
``openadapt-types`` and the stdlib. ``runtime_checkable`` lets the runner (and
the tests) assert that any adapter/env conforms structurally.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# Reuse the canonical, dependency-free schema (openadapt-types). No new types.
from openadapt_types import (
    BenchmarkAction,
    BenchmarkObservation,
    BenchmarkTask,
)

# Reuse the existing verification result -- do NOT define a parallel one.
from openadapt_evals.evaluation.verifier_registry import VerificationResult

#: An observation is exactly the canonical benchmark observation. Aliased so the
#: protocol reads in the meta-benchmark's own vocabulary.
Observation = BenchmarkObservation

__all__ = [
    "Environment",
    "Observation",
    "MetaTask",
    "VerificationResult",
    "BenchmarkAction",
    "BenchmarkObservation",
    "BenchmarkTask",
]


@dataclass
class MetaTask(BenchmarkTask):
    """A :class:`~openadapt_types.BenchmarkTask` extended for the meta-benchmark.

    Extends (never replaces) the canonical task with the three fields the
    meta-runner needs to drive record->compile->replay->verify across families:

    Attributes:
        demo: The demonstration this task is compiled from -- a compiled
            ``bundle_dir`` (:class:`pathlib.Path` / str), a ``Demo`` object,
            or None. What a policy replays.
        verifier: A :class:`TaskVerifierRegistry` key (or None). When set and
            the env carries a registry, :meth:`Environment.verify` dispatches
            to it; otherwise the env falls back to its native verifier.
        env: Which environment family scores this task --
            ``"waa" | "parallels" | "openemr" | "mockmed"`` (and, in phase 2,
            ``"osworld" | "browsergym"``). Recorded on every metrics row.
    """

    demo: Any | None = None
    verifier: str | None = None
    env: str = ""

    @classmethod
    def from_benchmark_task(
        cls,
        task: BenchmarkTask,
        *,
        env: str = "",
        verifier: str | None = None,
        demo: Any | None = None,
    ) -> "MetaTask":
        """Wrap an existing :class:`BenchmarkTask`, adding the meta fields.

        Copies every canonical field losslessly, so an adapter's own
        ``load_task`` output can be promoted to a ``MetaTask`` without re-parsing
        the benchmark's config.
        """
        return cls(
            task_id=task.task_id,
            instruction=task.instruction,
            domain=task.domain,
            initial_state_ref=task.initial_state_ref,
            time_limit_steps=task.time_limit_steps,
            raw_config=dict(task.raw_config or {}),
            evaluation_spec=task.evaluation_spec,
            demo=demo,
            verifier=verifier,
            env=env,
        )


@runtime_checkable
class Environment(Protocol):
    """One interface every benchmark family conforms to for the meta-benchmark.

    A structural (``runtime_checkable``) protocol: any object exposing these
    five methods *is* an ``Environment`` -- the existing adapters conform via
    the thin shims in :mod:`openadapt_evals.harness.adapters`, no inheritance
    required. This is the unification point of the audit's "extend
    openadapt-evals, ~80% reuse" finding.

    The critical unification is :meth:`verify`: a SINGLE entry point that
    delegates to the environment's *native* verifier (WAA's evaluator, a
    registry verifier, or a flow ``EffectVerifier``) and always returns a
    :class:`VerificationResult`. Ground truth for "did the task succeed?" comes
    from here -- never from a policy's self-report.
    """

    def reset(self, task: BenchmarkTask) -> Observation:
        """Reset to ``task``'s initial state and return the first observation."""
        ...

    def observe(self) -> Observation:
        """Return the current observation without advancing the environment."""
        ...

    def act(
        self, action: BenchmarkAction
    ) -> "tuple[Observation, bool, dict[str, Any]]":
        """Execute ``action``; return ``(observation, done, info)``."""
        ...

    def verify(self, task: BenchmarkTask) -> VerificationResult:
        """Score ``task`` via the env's native verifier (the unified gate)."""
        ...

    def close(self) -> None:
        """Release resources (VMs, browsers, sessions)."""
        ...


@dataclass
class _NullObservation:
    """Placeholder kept for callers that need an empty observation sentinel."""

    raw_observation: dict[str, Any] = field(default_factory=dict)
