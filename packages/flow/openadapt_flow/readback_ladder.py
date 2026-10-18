"""How each save was checked, and what a run needs to be ``VERIFIED``.

Every save a run makes is checked at one :class:`~openadapt_flow.check_level.CheckLevel`
(a projection of :class:`~openadapt_flow.verification.VerificationTier`):

- ``unchecked``: nothing confirmed the save.
- ``same_screen`` (tier 4): the screen the save left was re-read.
- ``reopened`` (tier 3): the acting session reset its state, found the record
  again and re-read it. Same application, same session.
- ``separate_session`` (tier 2): a separately signed-in session opened the
  record and read it (``ui_readback``).
- ``system_of_record`` (tier 1): an API, database, FHIR or file read outside
  the application's screens.

``VERIFIED`` keeps one meaning: every save was read back independently of the
acting session, from a separate session or from the system of record. That
is the default floor for every production profile. ``same_screen`` and
``reopened`` are reported with their own labels, and a run whose saves were
confirmed only that way is ``COMPLETED_UNVERIFIED`` with ``below_minimum``
set. A reopen counts toward ``VERIFIED`` only under a profile whose contract
sets ``reopened_counts_as_verified``; no built-in profile does.

A workflow can only raise the floor: ``minimum_readback`` is
``separate_session`` (the default) or ``system_of_record``.

The persisted tier integers, the receipt ``verification_tier`` labels
(:mod:`openadapt_flow.assurance`) and the Seal ``oracle_tier`` are unchanged.
"""

from __future__ import annotations

from typing import Any, Final, Literal, Mapping, Optional

from openadapt_flow.check_level import CheckLevel, from_verification_tier, weakest
from openadapt_flow.verification import VerificationTier

__all__ = [
    "CHECKED_HOW_LABELS",
    "DEFAULT_MINIMUM_READBACK",
    "MINIMUM_READBACK_VALUES",
    "MinimumReadback",
    "below_minimum",
    "checked_how_from_report",
    "level_for_tier",
    "minimum_from_effect_strength",
    "parse_minimum_readback",
    "summarize_report",
    "tier_floor",
    "workflow_tier_floor",
]

MinimumReadback = Literal["separate_session", "system_of_record"]

MINIMUM_READBACK_VALUES: Final[tuple[MinimumReadback, ...]] = (
    "separate_session",
    "system_of_record",
)
DEFAULT_MINIMUM_READBACK: Final[MinimumReadback] = "separate_session"

#: Plain customer words for each check level (the agent result's
#: ``checked_how`` code is the level's name).
CHECKED_HOW_LABELS: Final[dict[str, str]] = {
    CheckLevel.UNCHECKED.label: "Not checked",
    CheckLevel.SAME_SCREEN.label: "The screen said it saved",
    CheckLevel.REOPENED.label: "Reopened in the same session",
    CheckLevel.SEPARATE_SESSION.label: "Checked in a separate session",
    CheckLevel.SYSTEM_OF_RECORD.label: "Checked in your system's records",
    CheckLevel.COUNTERPARTY_CONFIRMED.label: "Confirmed by the other party",
}

#: The weakest Flow tier that reaches each ``minimum_readback``.
_FLOOR: Final[dict[MinimumReadback, VerificationTier]] = {
    "separate_session": VerificationTier.INDEPENDENT_SESSION,
    "system_of_record": VerificationTier.INDEPENDENT_SYSTEM,
}


def parse_minimum_readback(value: object) -> MinimumReadback:
    """Accept ``separate_session`` or ``system_of_record``; refuse the rest.

    ``reopened`` and ``same_screen`` are refused on purpose: a workflow can
    raise the ``VERIFIED`` floor, never lower it.
    """

    if isinstance(value, str):
        key = value.strip().lower().replace("-", "_").replace(" ", "_")
        if key in MINIMUM_READBACK_VALUES:
            return key  # type: ignore[return-value]
    raise ValueError(
        "minimum_readback must be separate_session (the default) or "
        f"system_of_record, not {value!r}; a same-session reopen or a "
        "same-screen check never makes a run VERIFIED by itself"
    )


def minimum_from_effect_strength(value: str) -> VerificationTier:
    """The tier floor of the older ``minimum_effect_strength`` names."""

    if value == "independent_system_of_record":
        return VerificationTier.INDEPENDENT_SYSTEM
    if value == "independent_session":
        return VerificationTier.INDEPENDENT_SESSION
    raise ValueError(f"unknown minimum_effect_strength {value!r}")


def tier_floor(minimum: MinimumReadback) -> VerificationTier:
    """The weakest :class:`VerificationTier` that reaches ``minimum``."""

    return _FLOOR[minimum]


def workflow_tier_floor(workflow: Any) -> Optional[VerificationTier]:
    """The floor the workflow's ``minimum_readback`` sets, or None when it
    sets none (the profile's floor applies)."""

    value = getattr(workflow, "minimum_readback", None)
    return None if value is None else tier_floor(parse_minimum_readback(value))


def level_for_tier(tier: Optional[int], *, confirmed: bool) -> CheckLevel:
    """The check level one effect check reached."""

    try:
        return from_verification_tier(tier, confirmed=confirmed)
    except ValueError:
        return CheckLevel.UNCHECKED


def _level_for_floor(floor: VerificationTier) -> CheckLevel:
    return from_verification_tier(int(floor))


def summarize_report(report: Any, floor: VerificationTier) -> Optional[dict[str, Any]]:
    """The ``readback`` block of ``report.json``, or None for a run that made
    no save.

    ``floor`` is the weakest tier that counts toward ``VERIFIED`` for this run
    (the profile's floor, raised by the workflow's ``minimum_readback``).

    One entry per step result that declared effects (a save). A save's level
    is the weakest level among its effect checks; an effect with no evidence
    is ``unchecked``. ``below_minimum`` is True only when every save was
    confirmed and at least one of them is weaker than ``floor``: that is the
    one case the agent reports as ``checked_below_minimum``.
    """

    saves: list[dict[str, Any]] = []
    all_confirmed = True
    any_weaker = False
    for result in getattr(report, "results", None) or []:
        evidence = list(getattr(result, "effect_evidence", None) or [])
        declared = list(getattr(result, "effect_contract_hashes", None) or [])
        if not evidence and not declared:
            continue
        levels: list[CheckLevel] = []
        tiers: list[int] = []
        for item in evidence:
            confirmed = (
                item.final_verdict == "confirmed"
                and item.observed_effect == "present"
                and item.verification_tier is not None
            )
            levels.append(level_for_tier(item.verification_tier, confirmed=confirmed))
            if confirmed:
                tiers.append(int(item.verification_tier))
                if int(item.verification_tier) > int(floor):
                    any_weaker = True
            else:
                all_confirmed = False
        if len(evidence) < len(declared) or not evidence:
            levels.append(CheckLevel.UNCHECKED)
            all_confirmed = False
        saves.append(
            {
                "step_id": str(result.step_id),
                "checked_how": weakest(levels).label,
                "flow_tier": max(tiers)
                if tiers and len(tiers) == len(evidence)
                else None,
            }
        )
    if not saves:
        return None
    reached = weakest(CheckLevel.from_label(save["checked_how"]) for save in saves)
    return {
        "minimum": _level_for_floor(floor).label,
        "checked_how": reached.label,
        "label": CHECKED_HOW_LABELS[reached.label],
        "below_minimum": bool(all_confirmed and any_weaker),
        "saves": saves,
    }


def below_minimum(report: Mapping[str, Any]) -> bool:
    """Whether a persisted ``report.json`` says every save was confirmed but
    one was checked below the run's ``VERIFIED`` floor."""

    block = report.get("readback") if isinstance(report, Mapping) else None
    return isinstance(block, Mapping) and block.get("below_minimum") is True


def checked_how_from_report(report: object) -> Optional[str]:
    """The ``checked_how`` code a persisted ``report.json`` records, if any."""

    block = report.get("readback") if isinstance(report, Mapping) else None
    if not isinstance(block, Mapping):
        return None
    code = block.get("checked_how")
    return code if code in CHECKED_HOW_LABELS else None
