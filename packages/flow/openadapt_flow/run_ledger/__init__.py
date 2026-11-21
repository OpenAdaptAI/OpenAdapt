"""Per-run accounting ledger for any executor.

``openadapt-flow ledger add RUN_DIR`` reads the files a run already left
behind (agent guard, verify-run, a compiled run's report and receipt, wrap's
run store, item approvals, task-ledger questions, compile-agent's agent
ledger, and the contract files of the shadow, flight, gate and exception
pieces) and appends one value-free ``openadapt.run-ledger/v1`` row to
``$OPENADAPT_HOME/ledger/rows.jsonl``. The values a person may need to see
(what was meant to be saved, what was saved, the difference, and the inputs
salt) go to a 0600 sidecar beside the store, never into the row.

* :mod:`.outcome`: the VERIFIED / ESCALATED / CAUGHT_ERROR / FAILED / UNKNOWN
  rule.
* :mod:`.collect`: one reader per source; files only.
* :mod:`.store`: the append-only store, its private sidecars, and
  :func:`build_row`.
* :mod:`.cli`: ``openadapt-flow ledger``.

See ``docs/RUN_LEDGER.md``. Nothing here edits a producer's files, calls a
model, or uses the network except ``ledger push``.
"""

from __future__ import annotations

from openadapt_flow.run_ledger.collect import Collected, collect
from openadapt_flow.run_ledger.outcome import Signal, decide
from openadapt_flow.run_ledger.store import (
    LedgerStore,
    PrivateSidecar,
    build_row,
    ledger_home,
)

__all__ = [
    "Collected",
    "LedgerStore",
    "PrivateSidecar",
    "Signal",
    "build_row",
    "collect",
    "decide",
    "ledger_home",
]
