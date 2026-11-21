"""Turn a handoff into the receipt's ``counterparty_confirmation`` line.

This is the one place that knows the signed shape
(``openadapt_types.receipt.Confirmation`` in ``record/run/v1``). The ledger
keeps richer, local-only detail; this module maps it onto the closed line
and the values its commitments open to:

| Ledger | Line |
|---|---|
| ``x12_999``, ``x12_277ca``, ``report_csv``, ``report_text`` | ``read_method: acknowledgment_message`` |
| ``api_readback`` | ``api`` |
| ``ui_readback`` | ``portal_read_back`` |
| ``field_readback`` | ``strength: read_back`` |
| ``status_only`` | ``acknowledgment`` |
| ``sent`` (any) | ``none`` |
| differing field ids | ``differing_count``; each compared field opens as ``<id>:<digest>`` with ``:differs`` when it differs |

The counterparty opens as ``<system_kind>:cp-<16 hex>`` (a digest of its
label, never the label), ``observed_at`` as whole seconds in UTC, and the
reason only when the schema allows it for the state. A reason the schema
doesn't allow there stays in the ledger and is left off the line, so a line
is never inconsistent.

When ``openadapt_types.receipt`` is installed (it carries the schema from
``record/run/v1`` with confirmations), :func:`build_confirmations` also
commits the values and validates each line against the real model. Without
it, the lines and the values to commit come back unchecked, for a writer
that commits them itself.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from openadapt_flow.handoffs.ledger import Handoff, HandoffLedger

KIND = "counterparty_confirmation"
#: A line commits to at most 34 values: counterparty, observed_at, reason
#: and up to 31 fields.
_MAX_FIELDS = 31

_READ_METHOD = {
    "x12_999": "acknowledgment_message",
    "x12_277ca": "acknowledgment_message",
    "report_csv": "acknowledgment_message",
    "report_text": "acknowledgment_message",
    "api_readback": "api",
    "ui_readback": "portal_read_back",
}
_STRENGTH = {"field_readback": "read_back", "status_only": "acknowledgment"}
#: The schema's rule: which reasons each state may carry. Mirrors
#: ``openadapt_types.receipt.REASONS_FOR_STATE``; a test keeps them equal.
REASONS_FOR_STATE: Mapping[str, frozenset[str]] = {
    "sent": frozenset(
        {
            "rejected_by_counterparty",
            "ambiguous_key",
            "receiver_unreachable",
            "not_found",
            "no_answer_by_deadline",
        }
    ),
    "acknowledged": frozenset({"ambiguous_key", "not_found", "no_answer_by_deadline"}),
    "recorded_as_intended": frozenset({"conflicting_report"}),
    "recorded_differently": frozenset({"conflicting_report"}),
}
#: The read a handoff has before anything came back: nothing, so the line's
#: method is the one the producer will use.
_DEFAULT_METHOD = {"payer_portal": "acknowledgment_message"}


@dataclass(frozen=True)
class ConfirmationLine:
    """One line and the values its commitments open to (private)."""

    line: dict[str, Any]
    #: ``counterparty``, ``observed_at``, ``reason``, ``field.N`` -> value.
    values: dict[str, str]
    #: Field ids in ``field.N`` order, for the local view only.
    field_ids: tuple[str, ...] = field(default=())


def counterparty_ref(handoff: Handoff) -> str:
    label = hashlib.sha256(handoff.counterparty.encode("utf-8")).hexdigest()[:16]
    return f"{handoff.system_kind}:cp-{label}"


def to_line(handoff: Handoff) -> ConfirmationLine:
    """The line for ``handoff`` as it stands now (its latest observation)."""

    last = handoff.evidence
    state = last.state if last is not None else "sent"
    if last is not None:
        method = _READ_METHOD[last.read_method]
        strength = "none" if state == "sent" else _STRENGTH.get(last.strength, "none")
        observed_at = last.observed_at
        reason = last.reason if last.reason in REASONS_FOR_STATE[state] else None
    else:
        method = _DEFAULT_METHOD.get(handoff.system_kind, "api")
        strength = "none"
        observed_at = handoff.sent_at
        reason = None
    values: dict[str, str] = {
        "counterparty": counterparty_ref(handoff),
        "observed_at": observed_at,
    }
    if reason is not None:
        values["reason"] = reason
    field_ids: list[str] = []
    differing = 0
    if last is not None and last.strength == "field_readback" and state != "sent":
        for index, field_id in enumerate(sorted(handoff.fields)[:_MAX_FIELDS]):
            seen = last.observed_digests.get(field_id) or handoff.fields[field_id].digest
            if seen == "missing":
                seen = hashlib.sha256(b"missing").hexdigest()
            differs = field_id in last.differing_fields
            differing += differs
            values[f"field.{index}"] = (
                f"{field_id}:{seen}" + (":differs" if differs else "")
            )
            field_ids.append(field_id)
    line = {
        "kind": KIND,
        "for_step": handoff.step,
        "system_kind": handoff.system_kind,
        "read_method": method,
        "state": state,
        "strength": strength,
        "differing_count": differing,
    }
    return ConfirmationLine(line=line, values=values, field_ids=tuple(field_ids))


def build_confirmations(
    handoffs: Sequence[Handoff], *, salt: Optional[Any] = None
) -> tuple[list[dict[str, Any]], dict[str, dict[str, str]]]:
    """Signed-shape lines for ``predicate.confirmations`` and their openings.

    Needs ``openadapt_types.receipt`` with confirmations; raises ImportError
    without it. Each line is validated against the schema's model.
    """

    from openadapt_types import receipt as r

    lines: list[dict[str, Any]] = []
    openings: dict[str, dict[str, str]] = {}
    for index, handoff in enumerate(handoffs):
        item = to_line(handoff)
        values = {r.confirmation_path(index, k): v for k, v in item.values.items()}
        commitments, opened = r.make_openings(values, salt=salt)
        line = {
            **item.line,
            "commitments": {k.split(".", 2)[2]: v for k, v in commitments.items()},
        }
        r.Confirmation.model_validate(line)
        lines.append(line)
        openings.update(opened)
    return lines, openings


def handoffs_for_run(
    report: Mapping[str, Any], ledgers: Sequence[HandoffLedger]
) -> list[Handoff]:
    """The handoffs a run made, each with ``step`` set to its write's index.

    A deferred claim check is found by the ``deferred_check`` id on the
    step's effect evidence; any other handoff by its ``run_id`` and ``step``.
    """

    by_source: dict[str, Handoff] = {}
    by_run: list[Handoff] = []
    run_id = report.get("run_id")
    for ledger in ledgers:
        for handoff in ledger.entries():
            if handoff.source:
                by_source[handoff.source] = handoff
            if run_id and handoff.run_id == run_id:
                by_run.append(handoff)
    found: dict[str, Handoff] = {h.id: h for h in by_run}
    results = report.get("results")
    for index, result in enumerate(results if isinstance(results, list) else []):
        if not isinstance(result, Mapping):
            continue
        for evidence in result.get("effect_evidence") or []:
            entry_id = evidence.get("deferred_check") if isinstance(evidence, Mapping) else None
            handoff = by_source.get(entry_id) if entry_id else None
            if handoff is not None:
                found[handoff.id] = handoff.model_copy(update={"step": index})
    return sorted(found.values(), key=lambda h: (h.step or 0, h.sent_at, h.id))


def attach_confirmations(
    statement: dict[str, Any],
    openings: dict[str, dict[str, str]],
    handoffs: Sequence[Handoff],
    *,
    salt: Optional[Any] = None,
) -> tuple[dict[str, Any], dict[str, dict[str, str]]]:
    """Put the handoffs' lines into a ``record/run/v1`` statement before it
    is signed: ``predicate.confirmations`` and their openings. A handoff
    whose step has no line on the statement is attached without a step."""

    steps = {line["step"] for line in statement["predicate"].get("lines", [])}
    placed = [
        h if h.step in steps else h.model_copy(update={"step": None})
        for h in handoffs
    ]
    lines, opened = build_confirmations(placed, salt=salt)
    out = {**statement, "predicate": {**statement["predicate"], "confirmations": lines}}
    if not lines:
        out["predicate"].pop("confirmations")
    return out, {**openings, **opened}


__all__ = [
    "KIND",
    "attach_confirmations",
    "handoffs_for_run",
    "REASONS_FOR_STATE",
    "ConfirmationLine",
    "build_confirmations",
    "counterparty_ref",
    "to_line",
]
