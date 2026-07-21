"""The pluggable seam -- bring YOUR OWN system of record + independent oracle.

EffectBench scores a :class:`~effectbench.adapter.SystemUnderTest` against a
*reference system of record* read by an *independent ground-truth oracle*. The
built-in reference is the synthetic :mod:`effectbench.fixtures.mockmed` fixture
scored by an :class:`~effectbench.oracle.RecordSnapshotOracle`. That fixture ships
its oracle as a FREEBIE -- the harness authored a cheap, correct, independent
record-readback oracle for its own synthetic app. On a REAL legacy system of
record, authoring that oracle is the whole problem (the SPEC concedes: "effects
are authored per deployment; the compiler does not infer them"), and EffectBench
does NOT do it for you.

:class:`BenchmarkProvider` is the interface a THIRD PARTY implements to run the
same runner, the same two reference baselines, and the same leaderboard against a
system of record the benchmark authors did NOT build. A provider supplies:

    (a) the environment / fixture + the task suite; and
    (b) the INDEPENDENT ground-truth oracle the harness scores with -- a read
        path the system under test can never reach.

Authoring (b) -- a cheap, correct, independent oracle for a real system of
record -- is the real-world cost this benchmark abstracts away ONLY on its
built-in synthetic fixture. A provider that does not (or cannot) hand the system
under test its own product verifier returns ``None`` from
:meth:`~effectbench.adapter.EnvHandle.product_effect_verifier`; the
:class:`~effectbench.adapter.EffectVerifiedSUT` then FAILS SAFE (halts) rather
than getting a correct verifier for free.

The runner consumes providers through
:func:`~effectbench.runner.evaluate_provider`; the built-in
:class:`~effectbench.runner.MockMedProvider` is one implementation of this
interface (marked REFERENCE-ONLY), so ``evaluate(sut)`` is exactly
``evaluate_provider(sut, MockMedProvider())``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Protocol, Sequence, runtime_checkable

from effectbench.adapter import EnvHandle
from effectbench.effect import Effect, EffectVerifier
from effectbench.schema import TaskSpec


@dataclass
class EpisodeSetup:
    """One fully provisioned ``(task x trial)`` episode a provider hands the runner.

    It bundles the three isolated pieces the runner needs, already wired for one
    trial:

    - ``env`` -- the arm-facing action + perception channel
      (:class:`~effectbench.adapter.EnvHandle`), including the OPTIONAL product
      verifier the system under test may consult;
    - ``oracle`` -- the INDEPENDENT ground-truth oracle the harness scores with
      (an :class:`~effectbench.effect.EffectVerifier`). It MUST be a distinct
      object / read path from any verifier reachable through ``env`` -- the
      system under test can never reach it;
    - the metadata one :class:`~effectbench.schema.EpisodeRecord` needs.

    ``expected_effect`` defaults to ``task.expected_effect``; the runner resolves
    it against ``params`` so the record checked is the record this trial wrote.
    """

    #: The arm-facing task metadata handed identically to every arm.
    task: TaskSpec
    #: The arm-facing action + perception channel for this episode.
    env: EnvHandle
    #: The harness's INDEPENDENT ground-truth oracle (the SUT cannot reach it).
    oracle: EffectVerifier
    #: This trial's trial-unique payload (the note / key the arm must write).
    params: Mapping[str, str]
    #: Whether the correct action was in fact available (over-halt vs safe-halt).
    correct_action_available: bool
    #: The trial index (also the default seed).
    trial: int

    #: The primary effect the oracle checks; defaults to ``task.expected_effect``.
    expected_effect: Optional[Effect] = None
    #: Override the derived ``arm::task::trial`` episode id if desired.
    episode_id: Optional[str] = None
    #: Override the seed (defaults to ``trial``).
    seed: Optional[int] = None
    #: Free-form provenance recorded on the episode row (env name, substrate, ...).
    env_fingerprint: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class BenchmarkProvider(Protocol):
    """A pluggable reference system of record + its INDEPENDENT ground-truth oracle.

    Implement this to run EffectBench against a system of record the benchmark
    authors did NOT build. See the module docstring for the contract and the
    honesty caveat: authoring the independent oracle (b) is the real-world cost
    the benchmark abstracts away only on its built-in synthetic fixture.
    """

    #: Stable provider name (recorded for provenance).
    name: str

    def tasks(self) -> Sequence[Any]:
        """The task handles to run.

        Handles are OPAQUE to the runner -- it only iterates them and passes each
        back to :meth:`provision`. They may be any type the provider chooses
        (e.g. a :class:`~effectbench.schema.TaskSpec` or a richer wrapper).
        """
        ...

    def provision(self, task: Any, trial: int) -> EpisodeSetup:
        """Provision a FRESH env + independent oracle for one ``(task, trial)``.

        Each call MUST build a fresh system-of-record state (no cross-trial
        contamination) and return an :class:`EpisodeSetup` whose ``oracle`` is a
        distinct read path from anything reachable through ``env``.
        """
        ...
