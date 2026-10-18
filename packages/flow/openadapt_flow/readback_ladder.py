"""The read-back ladder: how far a save was checked, in four rungs.

Every save a run makes is checked somewhere on this ladder:

- ``R0`` "Not checked": nothing read the save back.
- ``R1`` "The screen said it saved": the screen the save left was re-read
  (Flow tier 4), or a confirmation or reference number appeared on it.
- ``R2`` "Reopened and checked on screen": the record was found again after
  a state reset and re-read, in the acting session (tier 3) or in a fresh
  session (tier 2, ``ui_readback``).
- ``R3`` "Checked in your system's records": a read of the system of record
  outside the application's screens (tier 1: REST, GraphQL, FHIR, SQL, file).

A workflow names the rung each save must reach, ``minimum_readback``. The
default is ``R2``. ``R1`` counts only when a workflow sets it explicitly. A run
is ``VERIFIED`` (the agent's ``done``) only when every save reached the
minimum. A save that was confirmed below the minimum is reported as
``checked_below_minimum``, never as done.

The rungs are a projection of :class:`~openadapt_flow.verification.VerificationTier`
and do not replace it: the persisted tier integers, the receipt
``verification_tier`` labels (:mod:`openadapt_flow.assurance`), and the Seal
``oracle_tier`` are unchanged.
"""

from __future__ import annotations

from typing import Any, Final, Iterable, Literal, Mapping, Optional

from openadapt_flow.contracts.approval import CheckedHowLabel
from openadapt_flow.verification import VerificationTier

__all__ = [
    "CHECKED_HOW",
    "CHECKED_HOW_LABELS",
    "DEFAULT_MINIMUM_READBACK",
    "MINIMUM_READBACK_VALUES",
    "MinimumReadback",
    "Rung",
    "checked_how_for",
    "minimum_from_effect_strength",
    "parse_minimum_readback",
    "rung_for_tier",
    "summarize_report",
    "tier_floor",
    "below_minimum",
    "checked_how_from_report",
    "workflow_minimum",
]

Rung = Literal["R0", "R1", "R2", "R3"]
MinimumReadback = Literal["R1", "R2", "R3"]

MINIMUM_READBACK_VALUES: Final[tuple[MinimumReadback, ...]] = ("R1", "R2", "R3")
DEFAULT_MINIMUM_READBACK: Final[MinimumReadback] = "R2"

#: Closed code for each rung, as the agent result's ``checked_how``.
CHECKED_HOW: Final[dict[Rung, CheckedHowLabel]] = {
    "R0": "not_checked",
    "R1": "screen_said_saved",
    "R2": "reopened",
    "R3": "records",
}

#: Plain customer words for each ``checked_how`` code.
CHECKED_HOW_LABELS: Final[dict[str, str]] = {
    "not_checked": "Not checked",
    "screen_said_saved": "The screen said it saved",
    "reopened": "Reopened and checked on screen",
    "records": "Checked in your system's records",
}

_ORDER: Final[tuple[Rung, ...]] = ("R0", "R1", "R2", "R3")

#: The weakest Flow tier that still reaches each minimum rung.
_FLOOR: Final[dict[MinimumReadback, VerificationTier]] = {
    "R1": VerificationTier.IMMEDIATE_SCREEN,
    "R2": VerificationTier.PERSISTED_STATE_REACQUISITION,
    "R3": VerificationTier.INDEPENDENT_SYSTEM,
}

_ALIASES: Final[dict[str, MinimumReadback]] = {
    "r1": "R1",
    "confirmation": "R1",
    "screen": "R1",
    "r2": "R2",
    "reopened": "R2",
    "r3": "R3",
    "records": "R3",
}


def parse_minimum_readback(value: object) -> MinimumReadback:
    """Accept ``R1``/``R2``/``R3`` or the words ``confirmation``,
    ``reopened`` and ``records``; refuse anything else."""

    if isinstance(value, str):
        key = value.strip().lower()
        if key in _ALIASES:
            return _ALIASES[key]
    raise ValueError(
        f"minimum_readback must be one of R1 (confirmation), R2 (reopened) or "
        f"R3 (records), not {value!r}"
    )


def minimum_from_effect_strength(value: str) -> VerificationTier:
    """The tier floor of the older ``minimum_effect_strength`` names.

    ``independent_session`` keeps its exact meaning (tier 2, a fresh
    session), which is stricter than ``R2``; it is not rounded to a rung.
    """

    if value == "independent_system_of_record":
        return VerificationTier.INDEPENDENT_SYSTEM
    if value == "independent_session":
        return VerificationTier.INDEPENDENT_SESSION
    raise ValueError(f"unknown minimum_effect_strength {value!r}")


def tier_floor(minimum: MinimumReadback) -> VerificationTier:
    """The weakest :class:`VerificationTier` that reaches ``minimum``."""

    return _FLOOR[minimum]


def workflow_minimum(workflow: Any) -> MinimumReadback:
    """The workflow's ``minimum_readback``, or the default ``R2``."""

    value = getattr(workflow, "minimum_readback", None)
    return DEFAULT_MINIMUM_READBACK if value is None else parse_minimum_readback(value)


def rung_for_tier(tier: Optional[int], *, confirmed: bool) -> Rung:
    """The rung one effect check reached."""

    if not confirmed or tier is None:
        return "R0"
    try:
        value = VerificationTier(int(tier))
    except ValueError:
        return "R0"
    if value is VerificationTier.INDEPENDENT_SYSTEM:
        return "R3"
    if value in (
        VerificationTier.INDEPENDENT_SESSION,
        VerificationTier.PERSISTED_STATE_REACQUISITION,
    ):
        return "R2"
    return "R1"


def _weakest(rungs: Iterable[Rung]) -> Rung:
    values = list(rungs)
    if not values:
        return "R0"
    return min(values, key=_ORDER.index)


def checked_how_for(rung: Rung) -> tuple[str, str]:
    """``(code, plain label)`` for a rung."""

    code = CHECKED_HOW[rung]
    return code, CHECKED_HOW_LABELS[code]


def summarize_report(report: Any, workflow: Any) -> Optional[dict[str, Any]]:
    """The ``readback`` block of ``report.json``, or None for a run that made
    no save.

    One entry per step result that declared effects (a save). A save's rung is
    the weakest rung among its effect checks; an effect with no evidence is
    ``R0``. ``below_minimum`` is True only when every save was confirmed and
    at least one of them is weaker than the workflow's minimum: that is the
    one case the agent reports as ``checked_below_minimum``.
    """

    minimum = workflow_minimum(workflow)
    floor = tier_floor(minimum)
    saves: list[dict[str, Any]] = []
    all_confirmed = True
    any_weaker = False
    for result in getattr(report, "results", None) or []:
        evidence = list(getattr(result, "effect_evidence", None) or [])
        declared = list(getattr(result, "effect_contract_hashes", None) or [])
        if not evidence and not declared:
            continue
        rungs: list[Rung] = []
        tiers: list[int] = []
        confirmed_hashes = 0
        for item in evidence:
            confirmed = (
                item.final_verdict == "confirmed"
                and item.observed_effect == "present"
                and item.verification_tier is not None
            )
            rungs.append(rung_for_tier(item.verification_tier, confirmed=confirmed))
            if confirmed:
                confirmed_hashes += 1
                tiers.append(int(item.verification_tier))
                if int(item.verification_tier) > int(floor):
                    any_weaker = True
            else:
                all_confirmed = False
        if len(evidence) < len(declared):
            rungs.append("R0")
            all_confirmed = False
        if not evidence:
            all_confirmed = False
        rung = _weakest(rungs)
        saves.append(
            {
                "step_id": str(result.step_id),
                "rung": rung,
                "flow_tier": max(tiers)
                if tiers and confirmed_hashes == len(evidence)
                else None,
            }
        )
    if not saves:
        return None
    reached = _weakest(save["rung"] for save in saves)
    code, label = checked_how_for(reached)
    return {
        "minimum": minimum,
        "reached": reached,
        "checked_how": code,
        "label": label,
        "below_minimum": bool(all_confirmed and any_weaker),
        "saves": saves,
    }


def below_minimum(report: Mapping[str, Any]) -> bool:
    """Whether a persisted ``report.json`` says every save was confirmed but
    one was checked below the workflow's minimum."""

    block = report.get("readback") if isinstance(report, Mapping) else None
    return isinstance(block, Mapping) and block.get("below_minimum") is True


def checked_how_from_report(report: object) -> Optional[str]:
    """The ``checked_how`` code a persisted ``report.json`` records, if any."""

    block = report.get("readback") if isinstance(report, Mapping) else None
    if not isinstance(block, Mapping):
        return None
    code = block.get("checked_how")
    return code if code in CHECKED_HOW_LABELS else None
