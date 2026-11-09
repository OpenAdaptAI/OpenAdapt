"""How a write was checked, in three grades: the read-back assurance.

``VerificationTier`` (``openadapt_flow.verification``) and the Seal ladder
(``openadapt_types.oracle.OracleTier``) both map onto the check-level
ladder in :mod:`openadapt_flow.check_level`. These three grades are the coarse
rollup of that ladder that RFC-0002 publishes. A reviewer usually needs this
plainer answer: was the save read back independently of the session that
typed it, read back on the same screen, or not checked at all?
:func:`assurance_for` gives that answer:

- ``independent_readback``: a read of the system of record that does not go
  through the UI: an API, database, FHIR, GraphQL, file or email read
  (tier 1).
- ``screen_readback``: a read through the application's own screens. That is
  the record page read in a separate signed-in session (``ui_readback``,
  tier 2), the acting session re-opening the record (tier 3), or the screen
  the save left (tier 4).
- ``unchecked``: no effect verifier confirmed the write.

Policy rule. A separate-session UI read goes through the same interface the
executor used, so a UI bug or a stale view can fool both. It is therefore
``screen_readback`` here, even though
:meth:`VerificationTier.is_independent_of_actuation` is True for tier 2. A
screen-only target such as Citrix gets an honest "screen-checked" grade,
never ``independent_readback``. ``independent_readback`` still does not by
itself mean a production Seal.

The three names are the shared verification-grade vocabulary published as
:class:`openadapt_types.approval.ReadBackTier` (RFC-0002
``verification_tier``). :data:`Assurance` is that contract's wire type and the
constants below are its labels (a test keeps them equal); :func:`assurance_for` stays the only mapping
from Flow tiers.
"""

from __future__ import annotations

from typing import Final, Literal, Optional

from openadapt_flow.check_level import from_verification_tier, to_readback_label
from openadapt_flow.verification import VerificationTier

__all__ = [
    "ASSURANCE_GRADES",
    "INDEPENDENT_READBACK",
    "SCREEN_READBACK",
    "UNCHECKED",
    "Assurance",
    "assurance_for",
    "weakest_assurance",
]

#: The RFC-0002 ``verification_tier`` wire type. It spells
#: ``openadapt_types.approval.VerificationTierLabel`` out rather than
#: importing it, so importing Flow's runtime does not load the contracts
#: package; tests/test_demonstrated_readback_verifier.py pins the two equal.
Assurance = Literal["independent_readback", "screen_readback", "unchecked"]
INDEPENDENT_READBACK: Final[Literal["independent_readback"]] = "independent_readback"
SCREEN_READBACK: Final[Literal["screen_readback"]] = "screen_readback"
UNCHECKED: Final[Literal["unchecked"]] = "unchecked"
#: Strongest first, in the contract's order.
ASSURANCE_GRADES: tuple[Assurance, ...] = (
    INDEPENDENT_READBACK,
    SCREEN_READBACK,
    UNCHECKED,
)


def assurance_for(
    tier: Optional[int | VerificationTier], *, confirmed: bool = True
) -> Assurance:
    """The assurance grade of one effect check.

    ``tier`` is the verifier's ``VerificationTier`` (1 to 4) and ``confirmed``
    whether its final verdict was CONFIRMED. An unconfirmed check, or one with
    no tier, is ``unchecked``: a halted or unverifiable write was not shown to
    have persisted, whatever verifier ran.
    """

    try:
        level = from_verification_tier(tier, confirmed=confirmed)
    except ValueError:
        return UNCHECKED
    return to_readback_label(level)


def weakest_assurance(grades: list[Assurance]) -> Assurance:
    """The weakest grade among ``grades``; ``unchecked`` when empty."""

    if not grades:
        return UNCHECKED
    return max(grades, key=ASSURANCE_GRADES.index)
