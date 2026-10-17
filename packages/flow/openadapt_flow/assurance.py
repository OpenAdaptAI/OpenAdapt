"""How a write was checked, in three grades: the read-back assurance.

``VerificationTier`` (``openadapt_flow.verification``) ranks evidence in four
steps, and the Seal ladder (``openadapt_types.oracle.OracleTier``) ranks it in
four other steps. A reviewer usually needs one plainer answer: was the save
read back independently of the session that typed it, read back on the same
screen, or not checked at all? :func:`assurance_for` gives that answer:

- ``independent_readback``: a read that does not go through the acting
  session. An API, database, FHIR, file or email read (tier 1), or the
  application's record page read in a separate signed-in session
  (``ui_readback``, tier 2).
- ``screen_readback``: the acting session read the value back off the screen,
  after re-opening the record (tier 3) or on the screen the save left
  (tier 4).
- ``unchecked``: no effect verifier confirmed the write.

Policy rule in the current code. A separate-session UI read counts as
independent here because :meth:`VerificationTier.is_independent_of_actuation`
is True for tier 2, and :data:`openadapt_flow.verification.VERIFIED_EFFECT_TIER`
lets tier 2 reach ``VERIFIED``. It is still not a system-of-record read: the
``regulated`` policy pack requires tier 1, and the Seal ladder puts a second
session at oracle tier 1, below the production Seal minimum of 2. So
``independent_readback`` does not by itself mean a production Seal.

ADAPTER POINT. The three names are the shared verification-grade vocabulary
being added to ``openadapt-types``. Until that enum is published, this module
defines them as plain strings. When it lands, replace :data:`Assurance` and
the constants below with the ``openadapt_types`` enum and keep
:func:`assurance_for` as the only mapping from Flow tiers.
"""

from __future__ import annotations

from typing import Final, Literal, Optional

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

# -- ADAPTER POINT: replace with the openadapt-types enum when it is published.
Assurance = Literal["independent_readback", "screen_readback", "unchecked"]
INDEPENDENT_READBACK: Final = "independent_readback"
SCREEN_READBACK: Final = "screen_readback"
UNCHECKED: Final = "unchecked"
#: Strongest first.
ASSURANCE_GRADES: tuple[Assurance, ...] = (
    INDEPENDENT_READBACK,
    SCREEN_READBACK,
    UNCHECKED,
)
# -- end ADAPTER POINT


def assurance_for(
    tier: Optional[int | VerificationTier], *, confirmed: bool = True
) -> Assurance:
    """The assurance grade of one effect check.

    ``tier`` is the verifier's ``VerificationTier`` (1 to 4) and ``confirmed``
    whether its final verdict was CONFIRMED. An unconfirmed check, or one with
    no tier, is ``unchecked``: a halted or unverifiable write was not shown to
    have persisted, whatever verifier ran.
    """

    if not confirmed or tier is None:
        return UNCHECKED
    try:
        value = VerificationTier(int(tier))
    except ValueError:
        return UNCHECKED
    if value.is_independent_of_actuation():
        return INDEPENDENT_READBACK
    return SCREEN_READBACK


def weakest_assurance(grades: list[Assurance]) -> Assurance:
    """The weakest grade among ``grades``; ``unchecked`` when empty."""

    if not grades:
        return UNCHECKED
    return max(grades, key=ASSURANCE_GRADES.index)
