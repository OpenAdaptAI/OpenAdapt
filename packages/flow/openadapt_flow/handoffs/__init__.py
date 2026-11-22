"""Handoffs: did the other party record what we sent?

A handoff is one thing a run sends to another party's system: a claim to a
payer, a referral to a receiving clinic. A counterparty confirmation is
evidence, read from that other system, of what it recorded. Each handoff
moves through four states:

- ``sent``: we sent it, and nothing from the other side has said so yet;
- ``acknowledged``: the other side says it has it, without saying what it
  recorded (an X12 999 or a 277CA ``A1``), or says it won't take it;
- ``recorded_as_intended``: the other side recorded it, and every field we
  compared matches what we sent (or, for a status-only report, it accepted
  it: see ``strength``);
- ``recorded_differently``: the other side recorded it, and these fields
  differ from what we sent (field ids only, never the values).

One mechanism serves every producer: a local ledger of handoffs
(:mod:`.ledger`), readers that look at the other system (:mod:`.readers`),
one comparator (:mod:`.intake`), and the mapping of the existing deferred
claim check onto handoffs (:mod:`.payer`). :mod:`.line` turns a handoff into
the receipt's ``counterparty_confirmation`` line. No engine code names an
app, a payer or a clinic: what to read, and how each field compares, is a
profile (data).

No model calls. The ledger holds no record values: keys and field values are
HMAC-SHA256 digests under a salt kept in the ledger directory (0600).
"""

from openadapt_flow.handoffs.ledger import (
    FieldSpec,
    Handoff,
    HandoffLedger,
    HandoffState,
    Observation,
    ReadMethod,
    Reason,
    Strength,
)

__all__ = [
    "FieldSpec",
    "Handoff",
    "HandoffLedger",
    "HandoffState",
    "Observation",
    "ReadMethod",
    "Reason",
    "Strength",
]
