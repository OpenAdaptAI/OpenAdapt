"""How well a save was checked: one ladder, the check level.

Flow has older scales for the same question, each with its own numbers and
direction. :class:`CheckLevel` is the one ladder they all map onto. Higher is
stronger, and the names are the contract; the numbers are only for sorting.

====  ==========================  ==========================================
Level Name                        Meaning
====  ==========================  ==========================================
5     ``counterparty_confirmed``  The other party (payer, registry,
                                  recipient) confirmed it received the value.
4     ``system_of_record``        Read back without the UI: API, database,
                                  FHIR, GraphQL, file, or an export of
                                  committed data made by a separate process.
3     ``separate_session``        Read back through the app's own screens in
                                  a separately signed-in, read-only session.
2     ``reopened``                The acting session closed the record,
                                  reopened it by its identity fields and read
                                  it again.
1     ``same_screen``             Only the acting screen confirms it.
0     ``unchecked``               Nothing confirmed it.
====  ==========================  ==========================================

Published values keep their wire numbers and map through this module:

- ``VerificationTier`` (lower is stronger): 1 is ``system_of_record``, 2
  ``separate_session``, 3 ``reopened``, 4 ``same_screen``.
- Seal ``OracleTier`` (higher is stronger): 0 ``VISUAL`` is ``same_screen``
  (a Seal does not say whether the screen was reopened), 1 ``separate_session``,
  2 ``system_of_record``, 3 ``counterparty_confirmed``.
- RFC-0002 ``verification_tier`` labels are a coarse rollup:
  ``independent_readback`` is levels 4 and 5, ``screen_readback`` levels 1 to
  3, ``unchecked`` level 0.

When a save or record has several checks, the weakest level wins.

This module imports only the standard library, so the contracts and the Flow
runtime can both use it. ``openadapt_flow.check_level`` re-exports it.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Iterable, Literal, Optional

__all__ = [
    "CheckLevel",
    "ReadbackLabel",
    "from_oracle_tier",
    "from_verification_tier",
    "to_readback_label",
    "weakest",
]

#: The RFC-0002 ``verification_tier`` labels (``assurance.Assurance``).
ReadbackLabel = Literal["independent_readback", "screen_readback", "unchecked"]


class CheckLevel(IntEnum):
    """How well a save was checked. Higher is stronger."""

    UNCHECKED = 0
    SAME_SCREEN = 1
    REOPENED = 2
    SEPARATE_SESSION = 3
    SYSTEM_OF_RECORD = 4
    COUNTERPARTY_CONFIRMED = 5

    @property
    def label(self) -> str:
        """The level's name, for example ``system_of_record``."""

        return self.name.lower()

    @classmethod
    def from_label(cls, label: str) -> "CheckLevel":
        """The level named ``label``; raises ``ValueError`` for any other."""

        try:
            return cls[label.upper()]
        except KeyError:
            raise ValueError(f"unknown check level {label!r}") from None


#: ``VerificationTier`` value (lower is stronger) to check level.
_FROM_VERIFICATION_TIER: dict[int, CheckLevel] = {
    1: CheckLevel.SYSTEM_OF_RECORD,
    2: CheckLevel.SEPARATE_SESSION,
    3: CheckLevel.REOPENED,
    4: CheckLevel.SAME_SCREEN,
}

#: Seal ``OracleTier`` value (higher is stronger) to check level.
_FROM_ORACLE_TIER: dict[int, CheckLevel] = {
    0: CheckLevel.SAME_SCREEN,
    1: CheckLevel.SEPARATE_SESSION,
    2: CheckLevel.SYSTEM_OF_RECORD,
    3: CheckLevel.COUNTERPARTY_CONFIRMED,
}


def from_verification_tier(
    tier: Optional[int], *, confirmed: bool = True
) -> CheckLevel:
    """The check level of one effect check by ``VerificationTier``.

    An unconfirmed check, or one with no tier, is ``unchecked``: a halted or
    unverifiable write was not shown to have persisted, whatever verifier ran.
    Raises ``ValueError`` for a number that is not a ``VerificationTier``.
    """

    if not confirmed or tier is None:
        return CheckLevel.UNCHECKED
    try:
        return _FROM_VERIFICATION_TIER[int(tier)]
    except KeyError:
        raise ValueError(f"unknown verification tier {tier!r}") from None


def from_oracle_tier(tier: int) -> CheckLevel:
    """The check level of a Seal ``OracleTier``.

    Raises ``ValueError`` for a number that is not an ``OracleTier``.
    """

    try:
        return _FROM_ORACLE_TIER[int(tier)]
    except KeyError:
        raise ValueError(f"unknown oracle tier {tier!r}") from None


def to_readback_label(level: CheckLevel) -> ReadbackLabel:
    """The RFC-0002 ``verification_tier`` label for ``level``."""

    if level >= CheckLevel.SYSTEM_OF_RECORD:
        return "independent_readback"
    if level >= CheckLevel.SAME_SCREEN:
        return "screen_readback"
    return "unchecked"


def weakest(levels: Iterable[CheckLevel]) -> CheckLevel:
    """The weakest of ``levels``; ``unchecked`` when there are none."""

    return min(levels, default=CheckLevel.UNCHECKED)
