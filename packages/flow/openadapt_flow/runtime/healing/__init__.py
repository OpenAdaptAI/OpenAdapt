"""Governed healing: a repair is a reviewable, gated PATCH -- never a silent swap.

A heal may change HOW a step is performed (its locator / rung) but must NEVER
silently weaken WHAT it means (its identity band) or how its effects are
verified. This package turns a raw :class:`~openadapt_flow.ir.HealEvent` into a
reviewable :class:`HealPatch`, runs it through a deterministic regression GATE
(identity + effect + risk) and promotion pipeline, and reproduces synthetic
UI-drift to validate a patch before it is promoted. Everything here is ``$0``
and model-free on the runtime hot path.

Entry point for the replayer: :func:`govern_heal`. Reusable validation:
:mod:`openadapt_flow.runtime.healing.perturbation`.
"""

from openadapt_flow.runtime.healing.governance import (  # noqa: F401
    GateResult,
    PreservationVerdict,
    RegressionGate,
    effect_regression,
    identity_preserved,
    risk_regression,
)
from openadapt_flow.runtime.healing.patch import (  # noqa: F401
    IDENTITY_FIELDS,
    LOCATOR_FIELDS,
    AnchorChange,
    HealPatch,
    IdentitySnapshot,
)
from openadapt_flow.runtime.healing.perturbation import (  # noqa: F401
    DriftCase,
    DriftKind,
    HarnessReport,
    band_sampler,
    perturb,
    perturbation_set,
    replay_patch,
)
from openadapt_flow.runtime.healing.pipeline import (  # noqa: F401
    HealOutcome,
    PromotionOutcome,
    govern_heal,
    persist_patch,
    run_promotion,
)

__all__ = [
    # patch
    "HealPatch",
    "AnchorChange",
    "IdentitySnapshot",
    "IDENTITY_FIELDS",
    "LOCATOR_FIELDS",
    # governance
    "RegressionGate",
    "GateResult",
    "PreservationVerdict",
    "identity_preserved",
    "effect_regression",
    "risk_regression",
    # pipeline
    "govern_heal",
    "run_promotion",
    "persist_patch",
    "HealOutcome",
    "PromotionOutcome",
    # perturbation harness
    "perturb",
    "perturbation_set",
    "replay_patch",
    "band_sampler",
    "DriftCase",
    "DriftKind",
    "HarnessReport",
]
