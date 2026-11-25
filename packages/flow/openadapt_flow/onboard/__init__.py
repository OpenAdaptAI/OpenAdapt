"""Domain-agnostic pack onboarding and planted-fault qualification loop."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from openadapt_flow.onboard.types import (
    CleanWriteCase,
    CleanWriteOutcome,
    KindOnboardingSpec,
    OnboardSpec,
    PlantedFaultOutcome,
    QualificationResult,
)

if TYPE_CHECKING:
    from openadapt_flow.onboard.engine import (
        onboard_pack,
        qualify_pack,
        synthesize_pack,
    )

_ENGINE_EXPORTS = frozenset({"onboard_pack", "qualify_pack", "synthesize_pack"})


def __getattr__(name: str) -> Any:
    if name in _ENGINE_EXPORTS:
        from openadapt_flow.onboard import engine

        return getattr(engine, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "CleanWriteCase",
    "CleanWriteOutcome",
    "KindOnboardingSpec",
    "OnboardSpec",
    "PlantedFaultOutcome",
    "QualificationResult",
    "onboard_pack",
    "qualify_pack",
    "synthesize_pack",
]
