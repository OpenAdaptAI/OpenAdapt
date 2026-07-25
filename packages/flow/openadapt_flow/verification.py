"""Shared, machine-readable strength contract for effect verification."""

from __future__ import annotations

from enum import IntEnum
from typing import Any, Optional


class VerificationTier(IntEnum):
    """Strength of evidence for a declared business effect.

    Lower values are stronger. Verifiers advertise strength explicitly; callers
    never infer it from class names, substrate labels, or prose.
    """

    INDEPENDENT_SYSTEM = 1
    INDEPENDENT_SESSION = 2
    PERSISTED_STATE_REACQUISITION = 3
    IMMEDIATE_SCREEN = 4

    def satisfies(self, minimum: "VerificationTier") -> bool:
        return int(self) <= int(minimum)


def verifier_effect_tier(
    verifier: object,
    effect: Any = None,
) -> Optional[VerificationTier]:
    """Return a verifier's declared evidence tier, or ``None`` if untyped."""

    tier_for = getattr(verifier, "verification_tier_for", None)
    if callable(tier_for):
        try:
            value = tier_for(effect)
        except (TypeError, ValueError):
            return None
    else:
        value = getattr(verifier, "verification_tier", None)
    if isinstance(value, bool):
        return None
    try:
        return VerificationTier(value)
    except (TypeError, ValueError):
        return None
