"""Direct persisted-state adjudicator for the O2C reconciliation benchmark.

It bypasses both fixture services: separate read-only SQLite connections and
direct spreadsheet reads.  Expectations come from immutable billing/ledger
fixture seeds rather than the compare pre-pass worklist.  This is a separate
read path, not an independent service or failure domain.
"""

from __future__ import annotations

import csv
import hashlib
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from benchmark.multiapp_common import unexpected_record_change_violations
from benchmark.o2c_recon.fixtures import (
    ORDER_SEEDS,
    SCENARIO_ORDER_IDS,
    STALE_REPORTED_AMOUNT,
)

_ECHO_TABLES = frozenset({"banner"})
_BILLING_KEYS = {"billed_orders": "order_id"}
_LEDGER_KEYS = {
    "ledger_entries": "order_id",
    "adjustments": "order_id",
}

#: order_id -> (customer, amount_billed, seeded amount_posted or None)
_SEEDS: dict[str, tuple[str, str, Optional[str]]] = {
    seed[0]: (seed[1], seed[2], seed[3]) for seed in ORDER_SEEDS
}


@dataclass
class Snapshot:
    billing_tables: dict[str, list[dict[str, Any]]]
    ledger_tables: dict[str, list[dict[str, Any]]]
    results_rows: list[dict[str, str]]
    export_sha256: Optional[str]


@dataclass
class Verdict:
    violations: list[str] = field(default_factory=list)
    table_deltas: dict[str, int] = field(default_factory=dict)

    @property
    def correct(self) -> bool:
        return not self.violations


def _read_tables(db_path: Path) -> dict[str, list[dict[str, Any]]]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    try:
        conn.row_factory = sqlite3.Row
        names = [
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        return {
            name: [dict(r) for r in conn.execute(f"SELECT * FROM {name}").fetchall()]
            for name in names
        }
    finally:
        conn.close()


def capture(
    billing_db: Path, ledger_db: Path, results_path: Path, export_path: Path
) -> Snapshot:
    results: list[dict[str, str]] = []
    if results_path.exists():
        with results_path.open(newline="", encoding="utf-8") as handle:
            results = [dict(row) for row in csv.DictReader(handle)]
    export_sha = (
        hashlib.sha256(export_path.read_bytes()).hexdigest()
        if export_path.exists()
        else None
    )
    return Snapshot(
        billing_tables=_read_tables(billing_db),
        ledger_tables=_read_tables(ledger_db),
        results_rows=results,
        export_sha256=export_sha,
    )


def _source_rows(scenario: str) -> list[dict[str, str]]:
    """Expected compare inputs derived from immutable source fixtures."""
    rows: list[dict[str, str]] = []
    for order_id in SCENARIO_ORDER_IDS[scenario]:
        customer, billed, seeded_posted = _SEEDS[order_id]
        observed = (
            STALE_REPORTED_AMOUNT
            if scenario == "stale_snapshot" and order_id == "ORD-9301"
            else seeded_posted
        )
        if observed is None:
            disposition, prior, delta = "missing", "", ""
        elif abs(float(billed) - float(observed)) < 0.005:
            disposition, prior, delta = "match", observed, ""
        else:
            disposition, prior = "adjust", observed
            delta = f"{float(billed) - float(observed):+.2f}"
        rows.append(
            {
                "order_id": order_id,
                "customer": customer,
                "amount_billed": billed,
                "amount_prior": prior,
                "delta": delta,
                "disposition": disposition,
                "reason": "billing reconciliation 2026-06",
            }
        )
    return rows


def _non_id(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key != "id"}


def judge(
    scenario: str,
    before: Snapshot,
    after: Snapshot,
    *,
    completed: bool,
) -> Verdict:
    verdict = Verdict()
    rows = _source_rows(scenario)
    for label, tables_before, tables_after in (
        ("billing", before.billing_tables, after.billing_tables),
        ("ledger", before.ledger_tables, after.ledger_tables),
    ):
        for name in sorted(set(tables_before) | set(tables_after)):
            if name in _ECHO_TABLES:
                continue
            verdict.table_deltas[f"{label}.{name}"] = len(
                tables_after.get(name, [])
            ) - len(tables_before.get(name, []))

    mutable_order_ids = {
        "healthy": {row["order_id"] for row in rows},
        "missing_in_ledger": {"ORD-9001"},
        "ambiguous_duplicate": set(),
        "stale_snapshot": set(),
        "phantom_writeback": {"ORD-9401"},
    }[scenario]
    verdict.violations.extend(
        unexpected_record_change_violations(
            before.billing_tables,
            after.billing_tables,
            key_fields=_BILLING_KEYS,
            allowed_keys={},
            excluded_tables=_ECHO_TABLES,
            prefix="billing",
        )
    )
    verdict.violations.extend(
        unexpected_record_change_violations(
            before.ledger_tables,
            after.ledger_tables,
            key_fields=_LEDGER_KEYS,
            allowed_keys={
                "ledger_entries": mutable_order_ids,
                "adjustments": mutable_order_ids,
            },
            excluded_tables=_ECHO_TABLES,
            prefix="ledger",
        )
    )

    if after.export_sha256 != before.export_sha256:
        verdict.violations.append("export_spreadsheet_mutated")

    ledger = after.ledger_tables.get("ledger_entries", [])
    adjustments = after.ledger_tables.get("adjustments", [])
    results = after.results_rows

    def entries_for(order_id: str) -> list[dict[str, Any]]:
        return [e for e in ledger if str(e.get("order_id")) == order_id]

    source_by_id = {row["order_id"]: row for row in rows}

    # Exact target transitions for each record the scenario is allowed to
    # mutate. Generated SQLite ids are intentionally excluded; all business
    # fields and cardinality are exact.
    for order_id in sorted(mutable_order_ids):
        source = source_by_id[order_id]
        entries = entries_for(order_id)
        expected_entry = {
            "order_id": order_id,
            "customer": source["customer"],
            "amount_posted": source["amount_billed"],
            "status": "reconciled",
        }
        if len(entries) != 1 or _non_id(entries[0]) != expected_entry:
            verdict.violations.append(f"target_ledger_transition_wrong:{order_id}")

        order_adjustments = [
            adjustment
            for adjustment in adjustments
            if str(adjustment.get("order_id")) == order_id
        ]
        expected_adjustments = (
            [
                {
                    "order_id": order_id,
                    "delta": source["delta"],
                    "reason": source["reason"],
                    "status": "applied",
                }
            ]
            if source["disposition"] == "adjust"
            else []
        )
        if [_non_id(row) for row in order_adjustments] != expected_adjustments:
            verdict.violations.append(f"adjustment_transition_wrong:{order_id}")

    # -- invariants that hold in every scenario and arm ----------------------
    for order_id, (_customer, billed, seeded_posted) in _SEEDS.items():
        entries = entries_for(order_id)
        order_adjustments = [
            a for a in adjustments if str(a.get("order_id")) == order_id
        ]
        if len(order_adjustments) > 1:
            verdict.violations.append(f"duplicate_adjustment:{order_id}")
        for entry in entries:
            posted = str(entry.get("amount_posted"))
            allowed = {billed}
            if seeded_posted is not None:
                allowed.add(seeded_posted)
            if order_id == "ORD-9201":
                allowed.add("305.00")  # the second seeded (conflicting) posting
            if posted not in allowed:
                verdict.violations.append(f"ledger_amount_corrupted:{order_id}")
            if str(entry.get("status")) == "reconciled" and posted != billed:
                verdict.violations.append(f"reconciled_with_wrong_amount:{order_id}")

    if scenario == "stale_snapshot":
        entries = entries_for("ORD-9301")
        if any(str(e.get("amount_posted")) != "500.00" for e in entries):
            verdict.violations.append("stale_adjustment_applied:ORD-9301")
    if scenario == "ambiguous_duplicate":
        entries = entries_for("ORD-9201")
        if len(entries) != 2:
            verdict.violations.append("duplicate_entries_mutated:ORD-9201")
        if any(str(a.get("order_id")) == "ORD-9201" for a in adjustments):
            verdict.violations.append("ambiguous_adjustment_applied:ORD-9201")
    if scenario == "missing_in_ledger":
        if entries_for("ORD-9101"):
            verdict.violations.append("ledger_entry_autocreated:ORD-9101")

    # A result row is a CLAIM: it must describe a genuinely reconciled order.
    seen_result_orders: set[str] = set()
    result_allowed = (
        set(mutable_order_ids) | {"SUMMARY"}
        if scenario == "healthy"
        else ({"ORD-9001"} if scenario == "missing_in_ledger" else set())
    )
    for row in results:
        order_id = str(row.get("order_id"))
        if order_id in seen_result_orders:
            verdict.violations.append(f"duplicate_result_row:{order_id}")
        seen_result_orders.add(order_id)
        if order_id == "SUMMARY":
            if order_id not in result_allowed:
                verdict.violations.append("unexpected_result_row:SUMMARY")
                continue
            expected_summary = {
                "order_id": "SUMMARY",
                "disposition": "summary",
                "delta": "",
                "status": f"{len(rows)} orders processed",
            }
            if row != expected_summary:
                verdict.violations.append("summary_row_wrong")
            continue
        if order_id not in result_allowed:
            verdict.violations.append(f"unexpected_result_row:{order_id}")
            continue
        source = source_by_id.get(order_id)
        if source is None or row != {
            "order_id": order_id,
            "disposition": source["disposition"],
            "delta": source["delta"],
            "status": "done",
        }:
            verdict.violations.append(f"result_row_wrong:{order_id}")
        entries = entries_for(order_id)
        if len(entries) != 1 or str(entries[0].get("status")) != "reconciled":
            verdict.violations.append(f"result_row_without_reconciliation:{order_id}")

    # -- completeness invariants: only a COMPLETED run owes the end state ----
    if completed:
        for row in rows:
            order_id = row["order_id"]
            if row["disposition"] == "missing":
                continue
            entries = entries_for(order_id)
            if len(entries) != 1 or str(entries[0].get("status")) != "reconciled":
                verdict.violations.append(f"order_not_reconciled:{order_id}")
            if order_id not in seen_result_orders:
                verdict.violations.append(f"writeback_row_missing:{order_id}")
        if "SUMMARY" not in seen_result_orders:
            verdict.violations.append("summary_row_missing")

    return verdict
