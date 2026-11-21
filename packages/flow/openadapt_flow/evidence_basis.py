"""The basis of a read-back: who produced the evidence, on every Flow path.

The check level (:mod:`openadapt_types.check_level`) says how strong a check
was; the basis says who produced its evidence. Flow has three paths that
grade a save (the runtime's effect checks, ``verify-run`` and the ``tx``
commit engine). Each maps its evidence here, so the same evidence gets the
same level, basis and receipt word whichever path produced it:

- a separate signed-in session (Flow tier 2) is level 3, basis
  ``separate_session``;
- a verifier's own read of the system of record (tier 1) is level 4, basis
  ``db_frame`` for a SQL read and ``owned_connection`` otherwise;
- the ``tx`` engine's cache-bypassed, version-checked API read is level 4,
  basis ``system_api`` (:mod:`openadapt_flow.tx.api_readback`);
- an atomic SQLite row read by the ``tx`` engine is level 4, basis
  ``owned_connection``;
- tiers 3 and 4 (the acting session) have no basis and are never
  ``VERIFIED``.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional

from openadapt_types.check_level import (
    Basis,
    CheckLevel,
    from_verification_tier,
    to_verification_tier,
)

__all__ = [
    "SQL_KINDS",
    "basis_for_tier",
    "evidence_scope_for_run",
    "level_and_basis",
]

#: Verifier kinds that read the database directly.
SQL_KINDS = frozenset({"sql"})


def basis_for_tier(
    tier: Optional[int], kinds: Iterable[str] = ()
) -> Optional[Basis]:
    """The basis of a confirmed check at Flow ``tier`` by verifiers ``kinds``.

    None for tiers 3 and 4, for no tier, and for any unknown tier.
    """

    try:
        level = from_verification_tier(tier)
    except ValueError:
        return None
    if level is CheckLevel.SEPARATE_SESSION:
        return "separate_session"
    if level is CheckLevel.SYSTEM_OF_RECORD:
        names = {str(kind).strip().lower() for kind in kinds}
        if names and names <= SQL_KINDS:
            return "db_frame"
        return "owned_connection"
    return None


def level_and_basis(
    tier: Optional[int], *, confirmed: bool, kinds: Iterable[str] = ()
) -> tuple[CheckLevel, Optional[Basis]]:
    """The check level and basis of one check (``unchecked`` and None when
    it did not confirm)."""

    try:
        level = from_verification_tier(tier, confirmed=confirmed)
    except ValueError:
        return CheckLevel.UNCHECKED, None
    if level is CheckLevel.UNCHECKED:
        return level, None
    return level, basis_for_tier(tier, kinds)


def evidence_scope_for_run(
    readback: Optional[Mapping[str, Any]],
    outcome: str,
    *,
    read_back_at: Optional[str] = None,
) -> dict[str, Any]:
    """The evidence-scope block for a Flow run, from its ``report.json``.

    ``readback`` is the report's ``readback`` block
    (:func:`openadapt_flow.readback_ladder.summarize_report`), or None for a
    run that made no save; ``outcome`` is the run's ``ExecutionOutcome``
    value. The receipt for the run (``record/run/v1``) carries this block as
    ``evidence_scope``. Only a ``VERIFIED`` run names a basis, so only a
    ``VERIFIED`` run gets the word ``VERIFIED``; a confirmed level 1 or 2
    re-read is ``SHOWN`` whatever the run's outcome.
    """

    from openadapt_types.evidence_scope import build_evidence_scope

    level = CheckLevel.UNCHECKED
    minimum = CheckLevel.SEPARATE_SESSION
    if isinstance(readback, Mapping):
        try:
            level = CheckLevel.from_label(str(readback.get("checked_how")))
            minimum = CheckLevel.from_label(
                str(readback.get("minimum") or minimum.label)
            )
        except ValueError:
            level = CheckLevel.UNCHECKED
    verified = str(outcome).upper() == "VERIFIED"
    basis = basis_for_tier(to_verification_tier(level)) if verified else None
    return build_evidence_scope(
        level,
        basis=basis,
        # ``checked_how`` is the weakest save's level and is ``unchecked``
        # when any save did not confirm, so any higher level confirmed.
        confirmed=level > CheckLevel.UNCHECKED,
        minimum=minimum,
        read_back_at=read_back_at if level > CheckLevel.UNCHECKED else None,
        not_covered=(
            "changes_after_read_back",
            # A run report records no per-read time yet.
            *(("read_back_time",) if read_back_at is None else ()),
        ),
    )
