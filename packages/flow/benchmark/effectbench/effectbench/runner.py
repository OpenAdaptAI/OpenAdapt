"""The runner -- evaluate a :class:`~effectbench.adapter.SystemUnderTest`.

Given a system under test, the runner drives it against a
:class:`~effectbench.provider.BenchmarkProvider` -- a pluggable reference system
of record + its INDEPENDENT ground-truth oracle -- and scores each ``(task x
trial)`` into one :class:`~effectbench.schema.EpisodeRecord`. Every headline
(:func:`~effectbench.metrics.summarize`) is a pure function of those rows.

The DEFAULT provider is :class:`MockMedProvider`, the synthetic MockMed reference
fixture (marked REFERENCE-ONLY; see its docstring), so ``evaluate(sut)`` is
exactly ``evaluate_provider(sut, MockMedProvider())``. A third party swaps in
their own provider to score EffectBench on a system of record the benchmark
authors did NOT build -- see :mod:`effectbench.provider`.

Fairness / isolation by construction:

- the harness oracle and any product verifier the SUT may reach are DISTINCT
  instances built over the same store (different objects / read paths);
- the SUT receives only the task goal (never a step list) + the trial-unique
  params; it cannot reach the harness oracle;
- an UNREADABLE oracle is a harness condition, not an agent outcome -- the
  episode is dropped, never scored as success or SWER.
"""

from __future__ import annotations

from typing import Optional, Sequence

from effectbench.adapter import SystemUnderTest
from effectbench.fixtures.mockmed import MockMedEnv, MockMedSoR
from effectbench.oracle import CompoundSnapshotOracle, score_episode
from effectbench.provider import BenchmarkProvider, EpisodeSetup
from effectbench.schema import EpisodeRecord
from effectbench.tasks.mockmed import MOCKMED_TASKS, MockMedTask, trial_params


def _provision(task: MockMedTask, params: dict[str, str]) -> MockMedEnv:
    """Build a fresh MockMed store + arm-facing env for one episode."""
    sor = MockMedSoR()
    sor.reset(seed_concurrent=task.seed_concurrent)
    resolved_extra = [e.resolve(params) for e in task.extra_effects]

    def verifier_factory() -> CompoundSnapshotOracle:
        # The SUT's OWN verifier -- a distinct instance from the harness oracle.
        return CompoundSnapshotOracle(
            sor.read_records, extra=resolved_extra, substrate="mockmed"
        )

    return MockMedEnv(
        goal=task.spec.goal,
        sor=sor,
        fault=task.fault,
        n_posts=task.n_posts,
        params=params,
        _verifier_factory=verifier_factory,
    )


class MockMedProvider:
    """The built-in REFERENCE provider: the synthetic MockMed system of record.

    REFERENCE-ONLY. This provider ships a correct, independent record-readback
    oracle for its OWN synthetic fixture as a freebie -- the very cost a real
    deployment must pay itself (see :mod:`effectbench.provider`). It exists to
    reproduce the published reference result and to give a third party a runnable
    template; it is NOT a general result about any real system of record.

    It also hands the system under test a working product verifier through
    :meth:`~effectbench.fixtures.mockmed.MockMedEnv.product_effect_verifier`,
    again a convenience of the synthetic fixture. An external provider that does
    not author its own product verifier returns ``None`` there, and
    :class:`~effectbench.adapter.EffectVerifiedSUT` fails safe.
    """

    name = "mockmed"

    def __init__(self, tasks: Sequence[MockMedTask] = MOCKMED_TASKS) -> None:
        self._tasks = tuple(tasks)

    def tasks(self) -> Sequence[MockMedTask]:
        return self._tasks

    def provision(self, task: MockMedTask, trial: int) -> EpisodeSetup:
        params = trial_params(task.spec.task_id, trial)
        env = _provision(task, params)
        resolved_extra = [e.resolve(params) for e in task.extra_effects]
        # The harness oracle -- a SEPARATE instance over the same store.
        oracle = CompoundSnapshotOracle(
            env.sor.read_records, extra=resolved_extra, substrate="mockmed"
        )
        return EpisodeSetup(
            task=task.spec,
            env=env,
            oracle=oracle,
            params=params,
            correct_action_available=task.correct_action_available,
            trial=trial,
            seed=trial,
            env_fingerprint={
                "env": "mockmed",
                "substrate": task.spec.substrate.value,
                "fault": task.fault,
                "synthetic": True,
            },
        )


def run_setup(
    sut: SystemUnderTest,
    setup: EpisodeSetup,
    *,
    provider_name: Optional[str] = None,
) -> Optional[EpisodeRecord]:
    """Run and score ONE provider-provisioned episode. ``None`` if it is dropped.

    Provider-agnostic: it drives ``sut`` through ``setup.env`` (goal + params
    only) and scores it with ``setup.oracle`` -- the independent ground-truth
    oracle the SUT can never reach.
    """
    task = setup.task
    env = setup.env
    params = setup.params
    expected_effect = setup.expected_effect or task.expected_effect
    seed = setup.seed if setup.seed is not None else setup.trial
    episode_id = setup.episode_id or f"{sut.name}::{task.task_id}::t{setup.trial}"
    env_fingerprint = dict(setup.env_fingerprint)
    if provider_name is not None:
        # Provider identity is harness provenance, not provider-supplied data.
        # Overwrite any conflicting fingerprint value so a result cannot claim
        # to have come from a provider other than the one the runner invoked.
        env_fingerprint["provider"] = provider_name

    def run_action():
        return sut.run_task(task, env, params=params)

    try:
        return score_episode(
            episode_id=episode_id,
            task_id=task.task_id,
            arm=sut.name,
            trial=setup.trial,
            substrate=task.substrate,
            category=task.category,
            oracle=setup.oracle,
            expected_effect=expected_effect,
            run_action=run_action,
            correct_action_available=setup.correct_action_available,
            params=params,
            seed=seed,
            env_fingerprint=env_fingerprint,
        )
    except ValueError:
        # UNREADABLE oracle -> not scoreable. Drop rather than guess.
        return None


def run_episode(
    sut: SystemUnderTest, task: MockMedTask, trial: int
) -> Optional[EpisodeRecord]:
    """Run and score ONE (sut, MockMed task, trial). ``None`` if dropped.

    A thin convenience over the default :class:`MockMedProvider`. New code that
    scores a different system of record should implement a
    :class:`~effectbench.provider.BenchmarkProvider` and use
    :func:`evaluate_provider` instead.
    """
    provider = MockMedProvider()
    return run_setup(
        sut,
        provider.provision(task, trial),
        provider_name=provider.name,
    )


def evaluate_provider(
    sut: SystemUnderTest,
    provider: BenchmarkProvider,
    *,
    trials: int = 10,
) -> list[EpisodeRecord]:
    """Run ``sut`` over every task ``provider`` exposes, ``trials`` per task.

    This is the provider-agnostic entry point: pass the built-in
    :class:`MockMedProvider` for the reference fixture, or YOUR OWN
    :class:`~effectbench.provider.BenchmarkProvider` to score EffectBench on a
    system of record the benchmark authors did NOT build. Returns every scoreable
    :class:`~effectbench.schema.EpisodeRecord`.
    """
    records: list[EpisodeRecord] = []
    for task in provider.tasks():
        for trial in range(trials):
            setup = provider.provision(task, trial)
            record = run_setup(sut, setup, provider_name=provider.name)
            if record is not None:
                records.append(record)
    return records


def evaluate(
    sut: SystemUnderTest,
    *,
    tasks: Sequence[MockMedTask] = MOCKMED_TASKS,
    trials: int = 10,
) -> list[EpisodeRecord]:
    """Run the whole synthetic MockMed anchor for ``sut``, ``trials`` per task.

    A convenience over :func:`evaluate_provider` with the built-in REFERENCE
    :class:`MockMedProvider`. Returns every scoreable
    :class:`~effectbench.schema.EpisodeRecord`. Summarize with
    :func:`effectbench.metrics.summarize` to get SWER + co-metrics.
    """
    return evaluate_provider(sut, MockMedProvider(tasks), trials=trials)
