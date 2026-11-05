"""EffectBench multi-baseline runner adapter (downstream effort #2).

Drives every agent *arm* through one common interface against the IDENTICAL task
and INDEPENDENT oracle, records one
:class:`~benchmark.harness.effectbench.schema.EpisodeRecord` per
(task × arm × trial), and summarizes per arm with
:func:`~benchmark.harness.effectbench.metrics.summarize`. Built on the
stable contract in :mod:`benchmark.harness.effectbench` (schema / oracle /
metrics) — this package adds only the runner, never re-implements the taxonomy.

Layers:

- :mod:`.arms` — the ``AgentArm`` interface + the concrete in-repo arms:
  :class:`~benchmark.harness.effectbench.runner.arms.CompilerArm`
  (record→compile→replay, effect-gated),
  :class:`~benchmark.harness.effectbench.runner.arms.ScreenOnlyArm`
  (the silent-wrong-effect ablation), and a substrate-free
  :class:`~benchmark.harness.effectbench.runner.arms.MockArm` for CI.
- :mod:`.substrate` — the arm-facing action+perception channel (the reference
  MockMed session), isolated from the oracle read path.
- :mod:`.compound` — the compound consequential-save verifier (record + note).
- :mod:`.baselines` — SCAFFOLDING (no live calls, no spend) for the external
  paid baselines: Claude computer-use, OpenAI operator/CUA, UI-TARS, Skyvern.
- :mod:`.reference_tasks` — the CI-fast MockMed reference task pack + env factory.
- :mod:`.harness` — ``run_episode`` / ``run_matrix``: wire each arm through
  ``score_episode`` with the task's oracle, fairly and identically.

Run the dry-run: ``python -m benchmark.harness.effectbench.runner``.
"""

from __future__ import annotations

from benchmark.harness.effectbench.runner.arms import (
    AgentArm,
    ArmResult,
    CompilerArm,
    MockArm,
    ScreenOnlyArm,
)
from benchmark.harness.effectbench.runner.baselines import (
    SCAFFOLDED_ARMS,
    BaselineRequirements,
    ClaudeComputerUseArm,
    OpenAIOperatorArm,
    ScaffoldNotWired,
    SkyvernArm,
    UITarsArm,
)
from benchmark.harness.effectbench.runner.compound import (
    CompoundEffectVerifier,
)
from benchmark.harness.effectbench.runner.harness import (
    EnvFactory,
    EpisodeEnv,
    run_episode,
    run_matrix,
)
from benchmark.harness.effectbench.runner.reference_tasks import (
    MockMedEnvProvider,
    mockmed_env_factory,
    reference_tasks,
)
from benchmark.harness.effectbench.runner.substrate import (
    MockMedFault,
    MockMedSession,
    ScreenObservation,
    SubstrateSession,
    mockmed_session,
)

#: The concrete, in-repo, LIVE arms the dry-run compares.
LIVE_ARMS: tuple[AgentArm, ...] = (CompilerArm(), ScreenOnlyArm(), MockArm())

__all__ = [
    # interface + concrete arms
    "AgentArm",
    "ArmResult",
    "CompilerArm",
    "ScreenOnlyArm",
    "MockArm",
    "LIVE_ARMS",
    # substrate
    "SubstrateSession",
    "ScreenObservation",
    "MockMedSession",
    "MockMedFault",
    "mockmed_session",
    # compound verifier
    "CompoundEffectVerifier",
    # harness
    "EpisodeEnv",
    "EnvFactory",
    "run_episode",
    "run_matrix",
    # reference task pack
    "reference_tasks",
    "mockmed_env_factory",
    "MockMedEnvProvider",
    # external baseline scaffolds
    "SCAFFOLDED_ARMS",
    "BaselineRequirements",
    "ScaffoldNotWired",
    "ClaudeComputerUseArm",
    "OpenAIOperatorArm",
    "UITarsArm",
    "SkyvernArm",
]
