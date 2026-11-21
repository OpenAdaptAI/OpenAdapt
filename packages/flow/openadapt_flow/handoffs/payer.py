"""Payer acknowledgments as handoffs: the deferred claim check, seen from
the other side.

A claim saved with a deferred check (``effects.kind: deferred_report``) is a
handoff to the payer. Its handoff lives beside the deferred ledger, in
``<ledger>/handoffs``, with the same id digits (``deferred:<hex>`` ->
``handoff:<hex>``). ``deferred settle`` records what each report says about
it, including a row that settles nothing yet:

| The report says | Handoff |
|---|---|
| nothing yet | ``sent``, follow-up due at the check's deadline |
| received (999 ``A`` or ``E``; 277CA ``A1``; any other pending status) | ``acknowledged`` |
| accepted (277CA ``A2``) | ``acknowledged``, accepted: no follow-up |
| rejected (999 ``R``/``M``/``W``/``X``; 277CA ``A3``/``A4``/``A6``/``A7``/``A8``) | ``sent``, reason ``rejected_by_counterparty`` |
| two rows that disagree | ``acknowledged``, reason ``conflicting_report`` |
| two waiting claims on one key | unchanged, reason ``ambiguous_key`` |
| nothing by the deadline | unchanged, reason ``no_answer_by_deadline`` |

An acknowledgment is never ``recorded_as_intended``: a 277CA ``A2`` says the
payer accepted the claim into adjudication, not what it recorded for each
field. Only a read-back that compares the fields says that.

Only the X12 formats are a counterparty's own answer. A CSV export or a
saved status page may be the sending system's own report, so those entries
are not handoffs unless the caller says they are (``exports=True``).

The deferred check's own settlement is unchanged: this only listens.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from openadapt_flow.handoffs.ledger import (
    Handoff,
    HandoffLedger,
    Observation,
    ReadMethod,
    iso_seconds,
    now_utc,
    parse_iso,
)
from openadapt_flow.reports.parse import ReportRow
from openadapt_flow.runtime.effects.deferred_report import PendingCheck, PendingLedger

COUNTERPARTY_FORMATS = ("x12-999", "x12-277ca")
EXPORT_FORMATS = ("csv", "text")
_METHOD: dict[str, ReadMethod] = {
    "x12-999": "x12_999",
    "x12-277ca": "x12_277ca",
    "csv": "report_csv",
    "text": "report_text",
}


def handoff_ledger(pending: PendingLedger) -> HandoffLedger:
    return HandoffLedger(Path(pending.directory) / "handoffs")


def handoff_id(entry: PendingCheck) -> str:
    return "handoff:" + entry.id.split(":", 1)[1]


def _is_handoff(entry: PendingCheck, exports: bool) -> bool:
    formats = COUNTERPARTY_FORMATS + (EXPORT_FORMATS if exports else ())
    return entry.report_format in formats


def as_handoff(entry: PendingCheck, *, follow_up: str = "billing") -> Handoff:
    return Handoff(
        id=handoff_id(entry),
        sent_at=iso_seconds(parse_iso(entry.created_at)),
        follow_up_due=iso_seconds(parse_iso(entry.deadline)),
        counterparty=entry.describe[:80],
        system_kind="payer_portal",
        follow_up=follow_up,
        key_digest=entry.key_digest,
        source=entry.id,
    )


def observation_for(
    current: Handoff,
    row: Optional[ReportRow],
    waiting_on_key: int,
    method: ReadMethod,
    *,
    evidence_sha256: Optional[str],
    now: Optional[datetime] = None,
) -> Observation:
    """What one report row (or the deadline) says about a payer handoff."""

    at = iso_seconds(now or now_utc())
    previous = current.state
    last = current.evidence
    kept_strength = "none" if previous == "sent" or last is None else last.strength
    if row is None:
        return Observation(
            state=previous,
            observed_at=at,
            read_method=last.read_method if last is not None else method,
            strength=kept_strength,
            reason="no_answer_by_deadline",
        )
    if waiting_on_key > 1:
        return Observation(
            state=previous,
            observed_at=at,
            read_method=method,
            strength=kept_strength,
            reason="ambiguous_key",
            evidence_sha256=evidence_sha256,
        )
    if row.status == "rejected":
        return Observation(
            state="sent",
            observed_at=at,
            read_method=method,
            strength="none",
            reason="rejected_by_counterparty",
            evidence_sha256=evidence_sha256,
        )
    return Observation(
        state="acknowledged",
        observed_at=at,
        read_method=method,
        strength="status_only",
        reason="conflicting_report" if row.status == "conflicting" else None,
        counterparty_accepted=row.status == "accepted",
        evidence_sha256=evidence_sha256,
    )


def _same(a: Observation, b: Observation) -> bool:
    keep = ("state", "reason", "counterparty_accepted", "evidence_sha256")
    return all(getattr(a, k) == getattr(b, k) for k in keep)


class SettleRecorder:
    """The observer ``PendingLedger.settle`` calls; records into handoffs.

    The same report read twice adds nothing the second time.
    """

    def __init__(
        self,
        pending: PendingLedger,
        report_format: str,
        report_sha256: Optional[str],
        *,
        exports: bool = False,
        follow_up: str = "billing",
    ) -> None:
        self.handoffs = handoff_ledger(pending)
        self.method = _METHOD.get(report_format, "report_text")
        self.report_sha256 = report_sha256
        self.exports = exports
        self.follow_up = follow_up
        self.recorded = 0

    def __call__(
        self, entry: PendingCheck, row: Optional[ReportRow], waiting_on_key: int
    ) -> None:
        if not _is_handoff(entry, self.exports):
            return
        current = self.handoffs.adopt(as_handoff(entry, follow_up=self.follow_up))
        observation = observation_for(
            current,
            row,
            waiting_on_key,
            self.method,
            evidence_sha256=self.report_sha256,
        )
        if current.last is not None and _same(current.last, observation):
            return
        self.handoffs.observe(current.id, observation)
        self.recorded += 1


def sync(
    pending: PendingLedger, *, exports: bool = False, follow_up: str = "billing"
) -> HandoffLedger:
    """Make sure every deferred claim check has its handoff.

    A claim that settled before its handoff existed gets one observation from
    the settlement (accepted, rejected, expired); the settlement's reason
    text names which.
    """

    handoffs = handoff_ledger(pending)
    for entry in pending.entries():
        if not _is_handoff(entry, exports):
            continue
        current = handoffs.adopt(as_handoff(entry, follow_up=follow_up))
        if current.observations or entry.state == "waiting":
            continue
        method = _METHOD.get(entry.report_format, "report_text")
        at = parse_iso(entry.settled_at) if entry.settled_at else None
        reason_text = (entry.reason or "").casefold()
        if entry.state == "verified":
            row: Optional[ReportRow] = ReportRow("", "accepted", entry.code or "")
            count = 1
        elif entry.code == "deadline":
            row, count = None, 0
        elif "same report key" in reason_text:
            row, count = ReportRow("", "pending", entry.code or ""), 2
        elif "rejected" in reason_text:
            row, count = ReportRow("", "rejected", entry.code or ""), 1
        else:
            row, count = ReportRow("", "conflicting", entry.code or ""), 1
        handoffs.observe(
            current.id,
            observation_for(
                current, row, count, method, evidence_sha256=entry.settled_by, now=at
            ),
        )
    return handoffs


__all__ = [
    "COUNTERPARTY_FORMATS",
    "SettleRecorder",
    "as_handoff",
    "handoff_id",
    "handoff_ledger",
    "observation_for",
    "sync",
]
