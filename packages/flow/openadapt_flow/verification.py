"""Shared, machine-readable strength contract for effect verification."""

from __future__ import annotations

from enum import IntEnum
from typing import Any, Optional


class EffectContractMutationError(RuntimeError):
    """An external verifier changed the effect contract it received."""


def _isolated_effect(effect: Any) -> tuple[Any, Any]:
    """Return a deep callback copy and its exact serializable semantics."""

    copier = getattr(effect, "model_copy", None)
    dumper = getattr(effect, "model_dump", None)
    if not callable(copier) or not callable(dumper):
        return effect, None
    candidate = copier(deep=True)
    return candidate, candidate.model_dump(mode="json")


def verify_effect_without_mutation(
    verifier: object,
    effect: Any,
    before: Any,
    *,
    context: Any = None,
) -> Any:
    """Call a verifier with an isolated effect and reject contract mutation."""

    candidate, original = _isolated_effect(effect)
    callback = getattr(verifier, "verify")
    if context is None:
        verdict = callback(candidate, before)
    else:
        verdict = callback(candidate, before, context=context)
    if original is not None and candidate.model_dump(mode="json") != original:
        raise EffectContractMutationError(
            "effect verifier changed the resolved effect contract"
        )
    return verdict


class VerificationTier(IntEnum):
    """Strength of evidence for a declared business effect.

    Lower values are stronger. Verifiers advertise strength explicitly; callers
    never infer it from class names, substrate labels, or prose.
    """

    #: A read through the system of record's own API, database, or store
    #: (REST, GraphQL, FHIR, SQL, file, email, document). Seal oracle tier 2.
    INDEPENDENT_SYSTEM = 1
    #: The same application read through its own record page in a separate,
    #: separately signed-in, read-only session (``ui_readback``). Independent
    #: of the session that acted; NOT a system-of-record read. Seal oracle
    #: tier 1.
    INDEPENDENT_SESSION = 2
    #: The acting session re-navigates to the record and re-reads it.
    PERSISTED_STATE_REACQUISITION = 3
    #: The acting screen still shows the value.
    IMMEDIATE_SCREEN = 4

    def satisfies(self, minimum: "VerificationTier") -> bool:
        return int(self) <= int(minimum)

    def reads_system_of_record(self) -> bool:
        """True only for a read through the system of record (tier 1)."""

        return self is VerificationTier.INDEPENDENT_SYSTEM

    def is_independent_of_actuation(self) -> bool:
        """True when the read does not go through the session that acted.

        Tier 1 reads the system of record. Tier 2 reads the application's
        record page in a separate signed-in session. Tiers 3 and 4 re-read
        through the acting session (different-path or same-surface). A
        pixel-only Citrix run with neither tier 1 nor tier 2 evidence cannot
        be ``VERIFIED``.
        """

        return int(self) <= int(VerificationTier.INDEPENDENT_SESSION)

    def is_independent_system_of_record(self) -> bool:
        """Deprecated name; use :meth:`reads_system_of_record`.

        Until the separate-session read-back existed this returned True for
        tier 2 as well, which labeled a second application session as a
        system-of-record read. It now returns True for tier 1 only. Nothing in
        Flow's runtime calls it; outcome gating uses
        :data:`VERIFIED_EFFECT_TIER`, which is unchanged.
        """

        return self.reads_system_of_record()


def oracle_tier_from_verification_tier(
    tier: VerificationTier | int,
) -> int:
    """Map the legacy Flow verifier rank to the public Seal oracle ladder.

    ``VerificationTier`` is a persisted Flow v1 field where lower numbers are
    stronger. Public receipts use the Seal ladder, where higher numbers are
    stronger. Keep this conversion at the boundary instead of presenting the
    two incompatible number systems to an operator.
    """

    value = VerificationTier(tier)
    if value is VerificationTier.INDEPENDENT_SYSTEM:
        return 2
    if value is VerificationTier.INDEPENDENT_SESSION:
        return 1
    return 0


#: Production ``VERIFIED`` requires this floor or stronger (lower int).
#: The Standard *gate* still admits persisted-state read-back so a
#: pixel-only run can execute with halt-on-doubt; the outcome classifier
#: refuses ``VERIFIED`` unless a read independent of the acting session
#: (tier 1 system of record, or tier 2 separate-session read-back) confirmed
#: every consequential effect. Policies that need a system-of-record read
#: (the ``regulated`` policy pack) set their own tier-1 floor.
VERIFIED_EFFECT_TIER = VerificationTier.INDEPENDENT_SESSION


def declared_effect_is_independent_read(effect: Any) -> bool:
    """True when a compiled effect oracle is not the acting surface.

    Static counterpart of :meth:`VerificationTier.is_independent_of_actuation`.
    An on-screen read-back (same page session, screenshot, or banner) shares
    the actuation channel. Unbound placeholders are not a read. API, SQL,
    file, and separate-session (``ui_readback``) contracts have no
    ``readback`` spec.
    """

    if getattr(effect, "readback", None) is not None:
        return False
    if getattr(effect, "needs_operator_confirmation", False):
        return False
    return True


def verifier_effect_tier(
    verifier: object,
    effect: Any = None,
) -> Optional[VerificationTier]:
    """Return a verifier's declared evidence tier, or ``None`` if untyped."""

    tier_for = getattr(verifier, "verification_tier_for", None)
    if callable(tier_for):
        candidate, original = _isolated_effect(effect)
        try:
            value = tier_for(candidate)
        except (TypeError, ValueError):
            return None
        if original is not None and candidate.model_dump(mode="json") != original:
            return None
    else:
        value = getattr(verifier, "verification_tier", None)
    if isinstance(value, bool):
        return None
    try:
        return VerificationTier(value)
    except (TypeError, ValueError):
        return None
