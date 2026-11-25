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

Receipt words and the basis
---------------------------

A receipt names its outcome with one word, chosen by :func:`outcome_word`
from the level and the basis (who produced the evidence, not its type):

- ``VERIFIED``: levels 3 and 4 only, always with a basis. Level 3 has basis
  ``separate_session`` or ``fresh_reload`` (a new navigation in the acting
  session that is proven to have fetched from the server: the request was
  sent with the cache bypassed and the response shows no cache, with the
  request kept as evidence). A reopen without that proof is level 2,
  ``SHOWN``. Level 4 has ``system_api`` (a cache-bypassed,
  version-checked API read), ``activity_log`` (the system's own log),
  ``db_frame`` (a database read of the record and its frame) or
  ``owned_connection`` (a connection the verifier owns, such as its own SQL
  or file read). It means "as of the read-back time, the read-back showed
  this value", never "persisted".
- ``SHOWN``: levels 1 and 2, the acting screen re-read.
- ``CONFIRMED``: level 5, the counterparty acknowledged the item.
- ``ATTESTED``: an operator confirmed it.
- ``RECORDED``: the receipt records the run and claims no check. A check that
  did not confirm, or a level 3 or 4 read below the run's required level, is
  ``RECORDED``.

``openadapt_types.evidence_scope`` builds the receipt block that states the
level, basis, word and what the evidence did not cover.

This module imports only the standard library, so the contracts and the Flow
runtime can both use it. ``openadapt_flow.check_level`` re-exports it.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Iterable, Literal, Optional

__all__ = [
    "BASES",
    "Basis",
    "CheckLevel",
    "CONFIRMED_WORD",
    "OUTCOME_WORDS",
    "OutcomeWord",
    "ReadbackLabel",
    "VERIFIED_LEVELS",
    "VERIFIED_WORD",
    "from_effect_strength",
    "from_oracle_tier",
    "from_verification_tier",
    "level_for_basis",
    "outcome_word",
    "to_effect_strength",
    "to_oracle_tier",
    "to_readback_label",
    "to_verification_tier",
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


# -- the inverse maps ----------------------------------------------------------

#: Check level to ``VerificationTier`` (lower is stronger). Levels 0 and 5
#: have no Flow tier.
_TO_VERIFICATION_TIER: dict[CheckLevel, int] = {
    level: tier for tier, level in _FROM_VERIFICATION_TIER.items()
}

#: Check level to Seal ``OracleTier``. ``reopened`` reads through the acting
#: session's screens, so it is ``VISUAL`` like ``same_screen``.
_TO_ORACLE_TIER: dict[CheckLevel, int] = {
    CheckLevel.UNCHECKED: 0,
    CheckLevel.SAME_SCREEN: 0,
    CheckLevel.REOPENED: 0,
    CheckLevel.SEPARATE_SESSION: 1,
    CheckLevel.SYSTEM_OF_RECORD: 2,
    CheckLevel.COUNTERPARTY_CONFIRMED: 3,
}

#: Execute ``EffectStrengthV1`` names. Each name's rank is its check level.
_EFFECT_STRENGTH: dict[str, CheckLevel] = {
    "immediate_screen_confirmation": CheckLevel.SAME_SCREEN,
    "persisted_state_reacquisition": CheckLevel.REOPENED,
    "independent_session": CheckLevel.SEPARATE_SESSION,
    "independent_system_of_record": CheckLevel.SYSTEM_OF_RECORD,
}


def to_verification_tier(level: CheckLevel) -> Optional[int]:
    """The ``VerificationTier`` for ``level``; None for levels 0 and 5."""

    return _TO_VERIFICATION_TIER.get(CheckLevel(level))


def to_oracle_tier(level: CheckLevel) -> int:
    """The Seal ``OracleTier`` for ``level`` (0 below ``separate_session``)."""

    return _TO_ORACLE_TIER[CheckLevel(level)]


def from_effect_strength(name: object) -> CheckLevel:
    """The check level of an Execute effect strength; ``unchecked`` if unknown."""

    value = getattr(name, "value", name)
    return _EFFECT_STRENGTH.get(value, CheckLevel.UNCHECKED)  # type: ignore[arg-type]


def to_effect_strength(level: CheckLevel) -> Optional[str]:
    """The Execute effect strength name for ``level``; None for levels 0 and 5."""

    for name, mapped in _EFFECT_STRENGTH.items():
        if mapped == level:
            return name
    return None


# -- the basis and the receipt word ---------------------------------------------

#: Who produced the evidence for a ``VERIFIED`` read-back.
Basis = Literal[
    "separate_session",
    "fresh_reload",
    "system_api",
    "activity_log",
    "db_frame",
    "owned_connection",
]
BASES: tuple[Basis, ...] = (
    "separate_session",
    "fresh_reload",
    "system_api",
    "activity_log",
    "db_frame",
    "owned_connection",
)
_BASIS_LEVEL: dict[str, CheckLevel] = {
    "separate_session": CheckLevel.SEPARATE_SESSION,
    "fresh_reload": CheckLevel.SEPARATE_SESSION,
    "system_api": CheckLevel.SYSTEM_OF_RECORD,
    "activity_log": CheckLevel.SYSTEM_OF_RECORD,
    "db_frame": CheckLevel.SYSTEM_OF_RECORD,
    "owned_connection": CheckLevel.SYSTEM_OF_RECORD,
}

#: The receipt outcome words.
OutcomeWord = Literal["VERIFIED", "SHOWN", "CONFIRMED", "ATTESTED", "RECORDED"]
VERIFIED_WORD: OutcomeWord = "VERIFIED"
CONFIRMED_WORD: OutcomeWord = "CONFIRMED"
OUTCOME_WORDS: tuple[OutcomeWord, ...] = (
    "VERIFIED",
    "SHOWN",
    "CONFIRMED",
    "ATTESTED",
    "RECORDED",
)
#: The only levels a ``VERIFIED`` word may name.
VERIFIED_LEVELS: tuple[CheckLevel, ...] = (
    CheckLevel.SEPARATE_SESSION,
    CheckLevel.SYSTEM_OF_RECORD,
)


def level_for_basis(basis: str) -> CheckLevel:
    """The check level ``basis`` reaches; raises ``ValueError`` for others."""

    try:
        return _BASIS_LEVEL[basis]
    except (KeyError, TypeError):
        raise ValueError(f"unknown basis {basis!r}") from None


def outcome_word(
    level: CheckLevel,
    basis: Optional[str] = None,
    *,
    confirmed: bool = True,
    attested: bool = False,
    minimum: CheckLevel = CheckLevel.SEPARATE_SESSION,
) -> OutcomeWord:
    """The receipt word for one check, the same on every path.

    ``level`` is the level the check reached, ``basis`` who produced the
    evidence, ``confirmed`` whether the read-back matched, ``attested``
    whether an operator confirmed it, and ``minimum`` the weakest level the
    run accepts for ``VERIFIED`` (never below ``separate_session``).

    A level 3 or 4 check is ``VERIFIED`` only with a basis that reaches that
    exact level. A basis that names a different level is a producer bug and
    raises ``ValueError``; a missing basis fails closed to ``RECORDED``.
    """

    level = CheckLevel(level)
    if basis is not None and level_for_basis(basis) != level:
        raise ValueError(f"basis {basis!r} does not reach level {level.label}")
    if not confirmed:
        return "ATTESTED" if attested else "RECORDED"
    if level is CheckLevel.COUNTERPARTY_CONFIRMED:
        return "CONFIRMED"
    if level in VERIFIED_LEVELS:
        floor = max(CheckLevel(minimum), CheckLevel.SEPARATE_SESSION)
        if basis is not None and level >= floor:
            return "VERIFIED"
        return "ATTESTED" if attested else "RECORDED"
    if level >= CheckLevel.SAME_SCREEN:
        return "SHOWN"
    return "ATTESTED" if attested else "RECORDED"
