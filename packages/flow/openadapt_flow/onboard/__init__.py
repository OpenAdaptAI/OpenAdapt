"""Domain-agnostic pack onboarding and planted-fault qualification loop."""

from openadapt_flow.onboard.engine import onboard_pack, qualify_pack, synthesize_pack
from openadapt_flow.onboard.types import (
    CleanWriteCase,
    CleanWriteOutcome,
    KindOnboardingSpec,
    OnboardSpec,
    PlantedFaultOutcome,
    QualificationResult,
)

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
