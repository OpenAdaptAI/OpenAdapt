"""The runner harness — wire each arm through ``score_episode`` identically.

This is the multi-baseline runner's core: given a set of
:class:`~openadapt_flow.benchmark.effectbench.schema.TaskSpec` and a set of arms,
it produces one
:class:`~openadapt_flow.benchmark.effectbench.schema.EpisodeRecord` per
(task × arm × trial), each scored by the task's INDEPENDENT oracle through
:func:`~openadapt_flow.benchmark.effectbench.oracle.score_episode`. Every arm
runs the SAME task and is judged by the SAME oracle — the harness guarantees the
comparison is fair by construction:

* the oracle is built by the ``env_factory`` from the task and is passed only to
  ``score_episode``, NEVER to the arm — an arm cannot reach the reading that
  judges it (README non-gameability contract);
* the arm receives only ``task`` (goal/intent, no step list) + the shared
  ``session`` + the run ``params`` (the trial-unique payload);
* an UNREADABLE oracle (INDETERMINATE system of record) is a harness condition,
  not an agent outcome — ``score_episode`` raises and the episode is DROPPED
  (never scored as a success or a SWER), consistent with the schema contract.

The ``env_factory`` abstracts substrate provisioning: it maps ``(task, seed)``
to a live :class:`EpisodeEnv` (an arm-facing session, the independent oracle,
the resolved params, ``correct_action_available``, and a teardown). The MockMed
reference factory lives in :mod:`.reference_tasks`; a Dockerized environment
(OpenEMR / Frappe) supplies its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, List, Mapping, Optional, Sequence

from openadapt_flow.benchmark.effectbench.oracle import score_episode
from openadapt_flow.benchmark.effectbench.runner.arms import AgentArm
from openadapt_flow.benchmark.effectbench.runner.substrate import SubstrateSession
from openadapt_flow.benchmark.effectbench.schema import (
    AgentReport,
    EpisodeRecord,
    ModelCall,
    TaskSpec,
)
from openadapt_flow.runtime.effects.effect import EffectVerifier


@dataclass
class EpisodeEnv:
    """One provisioned episode: the arm-facing session, the independent oracle,
    the resolved run params, and teardown — everything ``run_episode`` needs.

    ``oracle`` reads the TRUE effect through a path the ``session`` (and thus
    the arm) cannot reach. ``correct_action_available`` is set by the
    environment (was a correct effect attainable this episode?) — the axis that
    separates OVER_HALT from SAFE_HALT — never inferred from the agent.
    """

    session: SubstrateSession
    oracle: EffectVerifier
    params: Mapping[str, str]
    correct_action_available: bool
    close: Callable[[], None]
    env_fingerprint: dict[str, Any] = field(default_factory=dict)


#: Maps ``(task, seed)`` to a freshly provisioned :class:`EpisodeEnv`.
EnvFactory = Callable[[TaskSpec, int], EpisodeEnv]


def run_episode(
    task: TaskSpec,
    arm: AgentArm,
    trial: int,
    *,
    env_factory: EnvFactory,
    seed: Optional[int] = None,
) -> Optional[EpisodeRecord]:
    """Run and score ONE (task, arm, trial), returning the record (or ``None``).

    Provisions a fresh environment, adapts ``arm.run`` into the
    ``run_action: Callable[[], AgentReport]`` ``score_episode`` calls (capturing
    the arm's recorded :class:`ModelCall`s so cost is auditable), and always
    tears the environment down. Returns ``None`` when the oracle read the system
    of record as UNREADABLE (the episode is not scoreable and is dropped).
    """
    resolved_seed = trial if seed is None else seed
    env = env_factory(task, resolved_seed)
    try:
        collected: List[ModelCall] = []

        def run_action() -> AgentReport:
            # The arm drives the SHARED substrate with goal + params only; it is
            # never handed the oracle (env.oracle) — isolation by construction.
            result = arm.run(task, env.session, params=env.params)
            collected.extend(result.model_calls)
            return result.report

        fingerprint: dict[str, Any] = dict(env.env_fingerprint)
        fingerprint.setdefault("arm", arm.name)
        try:
            return score_episode(
                episode_id=f"{arm.name}::{task.task_id}::t{trial}",
                task_id=task.task_id,
                arm=arm.name,
                trial=trial,
                substrate=task.substrate,
                category=task.category,
                oracle=env.oracle,
                expected_effect=task.expected_effect,
                run_action=run_action,
                correct_action_available=env.correct_action_available,
                params=dict(env.params),
                seed=resolved_seed,
                model_calls=collected,
                env_fingerprint=fingerprint,
            )
        except ValueError:
            # UNREADABLE oracle -> not scoreable. Drop rather than guess.
            return None
    finally:
        env.close()


def run_matrix(
    tasks: Sequence[TaskSpec],
    arms: Sequence[AgentArm],
    *,
    env_factory: EnvFactory,
    trials: int = 1,
    include_scaffolded: bool = False,
) -> list[EpisodeRecord]:
    """Run the full (tasks × arms × trials) matrix into a flat record list.

    Args:
        tasks: The authored tasks (each carries only a goal, never steps).
        arms: The arms to compare. By default only ``live`` arms run;
            scaffolded external baselines (:mod:`.baselines`) are skipped so the
            dry-run never attempts a paid call.
        env_factory: Provisions each episode's substrate + independent oracle.
        trials: Trials per (task, arm) — trial ``i`` seeds with ``i`` so the
            trial-unique payload is reproducible.
        include_scaffolded: If ``True``, do NOT skip non-live arms (they will
            raise) — only for an explicitly funded run.

    Returns:
        Every scoreable :class:`EpisodeRecord` (UNREADABLE episodes dropped).
    """
    records: list[EpisodeRecord] = []
    for task in tasks:
        for arm in arms:
            if not include_scaffolded and not getattr(arm, "live", False):
                continue
            for trial in range(trials):
                record = run_episode(task, arm, trial, env_factory=env_factory)
                if record is not None:
                    records.append(record)
    return records


__all__ = ["EpisodeEnv", "EnvFactory", "run_episode", "run_matrix"]
