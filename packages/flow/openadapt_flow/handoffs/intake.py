"""Read a handoff back from the other party's system and record what it shows.

One comparator for every producer. For the item's key, the reader answers
``found`` (with fields), ``not_found``, ``ambiguous`` or ``unreachable``:

| Read | Observation |
|---|---|
| found, every field matches | ``recorded_as_intended`` |
| found, some fields differ | ``recorded_differently`` with those field ids |
| not_found | unchanged (``sent`` stays ``sent``), reason ``not_found`` |
| ambiguous | unchanged, reason ``ambiguous_key`` |
| unreachable | unchanged, reason ``receiver_unreachable`` |

A field matches when the value the other side holds normalizes, for the
field's kind, to the same digest as the value we sent (``text`` ignores case
and runs of whitespace, ``code`` ignores separators, ``date`` compares the
calendar date; see :mod:`openadapt_flow.runtime.effects.readback_values`).
A value the other side doesn't hold, or that doesn't parse, differs.

A record that was confirmed and then can't be found (or now matches two
records) goes back to ``sent``: nothing confirms it any more. An unreachable
receiver changes nothing it had confirmed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from openadapt_flow.handoffs.ledger import (
    Handoff,
    HandoffLedger,
    HandoffState,
    LedgerError,
    Observation,
    ReadMethod,
    Reason,
    Strength,
    iso_seconds,
    now_utc,
)
from openadapt_flow.handoffs.readers import Reader, ReadResult

_REASONS: dict[str, Reason] = {
    "not_found": "not_found",
    "ambiguous": "ambiguous_key",
    "unreachable": "receiver_unreachable",
}


def judge(
    ledger: HandoffLedger, handoff: Handoff, result: ReadResult, method: ReadMethod
) -> Observation:
    """The observation one read gives, without writing it."""

    previous: HandoffState = handoff.state
    observed_at = iso_seconds(now_utc())
    if result.status != "found":
        reason: Reason = _REASONS[result.status]
        state = previous
        if reason != "receiver_unreachable" and previous.startswith("recorded_"):
            state = "sent"
        last = handoff.last
        strength: Strength = (
            "none" if state == "sent" or last is None else last.strength
        )
        return Observation(
            state=state,
            observed_at=observed_at,
            read_method=method,
            strength=strength,
            reason=reason,
            evidence_sha256=result.evidence_sha256,
        )
    differing: list[str] = []
    digests: dict[str, str] = {}
    for field_id, spec in sorted(handoff.fields.items()):
        value = result.fields.get(field_id)
        seen = (
            None
            if value is None
            else ledger.field_digest(value, spec.kind, spec.date_order)
        )
        digests[field_id] = seen or "missing"
        if seen != spec.digest:
            differing.append(field_id)
    return Observation(
        state="recorded_differently" if differing else "recorded_as_intended",
        observed_at=observed_at,
        read_method=method,
        strength="field_readback",
        differing_fields=differing,
        observed_digests=digests,
        evidence_sha256=result.evidence_sha256,
    )


def check(
    ledger: HandoffLedger,
    handoff_id: str,
    key: str,
    reader: Reader,
) -> Handoff:
    """Read the item back through ``reader`` and append the observation."""

    handoff = ledger.get(handoff_id)
    if handoff is None:
        raise LedgerError("no such handoff")
    if ledger.digest(key) != handoff.key_digest:
        raise LedgerError("that key isn't this handoff's key")
    if not handoff.fields:
        raise LedgerError("this handoff sent no fields to compare")
    result = reader.read(key)
    return ledger.observe(
        handoff_id, judge(ledger, handoff, result, reader.read_method)
    )


def needs_follow_up(
    ledger: HandoffLedger, now: Optional[datetime] = None
) -> list[Handoff]:
    return [h for h in ledger.entries() if h.status(now) == "needs_follow_up"]


__all__ = ["check", "judge", "needs_follow_up"]
