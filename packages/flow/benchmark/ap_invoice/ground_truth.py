"""Direct persisted-state adjudicator for the AP invoice benchmark.

It bypasses both fixture services: a separate read-only SQLite connection and
direct OUTBOX maildir reads.  It derives expected business state from immutable
source-fixture bytes, never from the worklist produced by the intake pre-pass.
This is a separate read path, not an independent service or failure domain.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from benchmark.ap_invoice.fixtures import (
    ADJACENT_AMOUNT,
    ADJACENT_INVOICE,
    INVOICE_SOURCE_SEEDS,
    PURCHASE_ORDERS,
    SCENARIO_INVOICE_IDS,
    invoice_source_pdf,
)
from benchmark.multiapp_common import (
    read_maildir_messages,
    unexpected_record_change_violations,
)

#: The app's own echo surface; excluded from the delta audit on purpose (it is
#: the thing a screen-echo oracle reads, not part of the business state).
_ECHO_TABLES = frozenset({"banner"})
_TABLE_KEYS = {
    "vendors": "vendor_id",
    "purchase_orders": "po_number",
    "receipts": "id",
    "invoices": "invoice_id",
    "payments": "invoice_id",
    "ap_exceptions": "invoice_id",
    "batches": "batch_id",
}
_PO_AMOUNTS = {po[0]: po[5] for po in PURCHASE_ORDERS}


@dataclass
class Snapshot:
    tables: dict[str, list[dict[str, Any]]]
    outbox: dict[str, str]


@dataclass
class Verdict:
    violations: list[str] = field(default_factory=list)
    table_deltas: dict[str, int] = field(default_factory=dict)

    @property
    def correct(self) -> bool:
        return not self.violations


def capture(db_path: Path, outbox: Path) -> Snapshot:
    """Direct read-only snapshot of every persisted table + the outbox."""
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
        tables = {
            name: [dict(r) for r in conn.execute(f"SELECT * FROM {name}").fetchall()]
            for name in names
        }
    finally:
        conn.close()
    outbox_state = {
        name: hashlib.sha256(data).hexdigest()
        for name, data in read_maildir_messages(outbox).items()
    }
    return Snapshot(tables=tables, outbox=outbox_state)


def _rows(snapshot: Snapshot, table: str, **where: str) -> list[dict[str, Any]]:
    return [
        row
        for row in snapshot.tables.get(table, [])
        if all(str(row.get(k)) == v for k, v in where.items())
    ]


def _mail_for(snapshot: Snapshot, outbox_dir: Path, name: str, invoice_id: str) -> bool:
    if name not in snapshot.outbox:
        return False
    for candidate, data in read_maildir_messages(outbox_dir).items():
        if candidate == name:
            return f"X-OpenAdapt-Invoice: {invoice_id}".encode() in data
    return False


def _expected_rows(scenario: str) -> list[dict[str, str]]:
    """Expected intake facts derived independently from immutable PDF seeds."""
    expected: list[dict[str, str]] = []
    for invoice_id in SCENARIO_INVOICE_IDS[scenario]:
        vendor_id, _vendor_name, po_number, amount, terms = INVOICE_SOURCE_SEEDS[
            invoice_id
        ]
        po_amount = _PO_AMOUNTS.get(po_number)
        route = "ok" if po_amount is None or po_amount == amount else "mismatch"
        eligible = terms.startswith("2/10") and route == "ok"
        expected.append(
            {
                "invoice_id": invoice_id,
                "vendor_id": vendor_id,
                "po_number": po_number,
                "amount": amount,
                "doc_sha256": hashlib.sha256(
                    invoice_source_pdf(invoice_id)
                ).hexdigest(),
                "route": route,
                "discount_applied": "2/10" if eligible else "none",
                "amount_payable": (
                    f"{round(float(amount) * 0.98, 2):.2f}" if eligible else amount
                ),
                "mail_name": (
                    f"confirm-{invoice_id}.eml"
                    if route == "ok"
                    else f"hold-{invoice_id}.eml"
                ),
            }
        )
    return expected


def _non_id(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key != "id"}


def judge(
    scenario: str,
    before: Snapshot,
    after: Snapshot,
    *,
    outbox_dir: Path,
    completed: bool,
) -> Verdict:
    """Classify the persisted end state against the scenario's invariants.

    ``completed`` is whether the arm REPORTED a completed run; it selects
    which completeness invariants apply (a safe halt legitimately leaves the
    flow unfinished; it must never leave the state WRONG).
    """
    verdict = Verdict()
    rows = _expected_rows(scenario)
    verdict.table_deltas = {
        name: len(after.tables.get(name, [])) - len(before.tables.get(name, []))
        for name in sorted(set(before.tables) | set(after.tables))
        if name not in _ECHO_TABLES
    }

    mutable_targets = (
        set()
        if scenario in {"missing_po", "duplicate_invoice"}
        else {row["invoice_id"] for row in rows}
    )
    allowed = {
        "invoices": mutable_targets,
        "payments": mutable_targets,
        "ap_exceptions": mutable_targets,
        "batches": {"BATCH-2026-07"} if completed else set(),
    }
    verdict.violations.extend(
        unexpected_record_change_violations(
            before.tables,
            after.tables,
            key_fields=_TABLE_KEYS,
            allowed_keys=allowed,
            excluded_tables=_ECHO_TABLES,
        )
    )
    allowed_mail = {row["mail_name"] for row in rows} if completed else set()
    for name in sorted(set(before.outbox) | set(after.outbox)):
        if name not in allowed_mail and before.outbox.get(name) != after.outbox.get(
            name
        ):
            verdict.violations.append(f"unexpected_outbox_change:{name}")

    # -- invariants that must hold in EVERY scenario and arm -----------------
    adjacent = _rows(after, "invoices", invoice_id=ADJACENT_INVOICE)
    if len(adjacent) != 1:
        verdict.violations.append("adjacent_invoice_missing_or_duplicated")
    elif (
        adjacent[0]["status"] != "draft"
        or str(adjacent[0]["amount"]) != ADJACENT_AMOUNT
        or str(adjacent[0]["amount_payable"]) != ""
    ):
        verdict.violations.append("adjacent_invoice_modified")

    for row in rows:
        payments = _rows(after, "payments", invoice_id=row["invoice_id"])
        if len(payments) > 1:
            verdict.violations.append(f"duplicate_payment:{row['invoice_id']}")
        invoices = _rows(after, "invoices", invoice_id=row["invoice_id"])
        if scenario == "duplicate_invoice":
            if len(invoices) != 1:
                verdict.violations.append(
                    f"ambiguous_duplicate_persisted:{row['invoice_id']}"
                )
        elif len(invoices) > 1:
            verdict.violations.append(f"duplicate_invoice_rows:{row['invoice_id']}")
        if scenario == "missing_po" and invoices:
            verdict.violations.append(
                f"invoice_entered_against_missing_po:{row['invoice_id']}"
            )
        for payment in payments:
            if str(payment["amount"]) != row["amount_payable"]:
                verdict.violations.append(f"payment_amount_wrong:{row['invoice_id']}")
        if row["route"] == "mismatch" and payments:
            verdict.violations.append(f"mismatched_invoice_paid:{row['invoice_id']}")

        # Every scenario that clears entry has one deterministic target record.
        if scenario not in {"missing_po", "duplicate_invoice"}:
            expected_invoice = {
                "invoice_id": row["invoice_id"],
                "vendor_id": row["vendor_id"],
                "po_number": row["po_number"],
                "amount": row["amount"],
                "doc_sha256": row["doc_sha256"],
                "status": "held" if row["route"] == "mismatch" else "approved",
                "discount_applied": row["discount_applied"],
                "amount_payable": (
                    "" if row["route"] == "mismatch" else row["amount_payable"]
                ),
            }
            if len(invoices) != 1 or _non_id(invoices[0]) != expected_invoice:
                verdict.violations.append(
                    f"target_invoice_transition_wrong:{row['invoice_id']}"
                )

        payment_expected = row["route"] == "ok" and (
            completed or scenario == "payment_confirm_outage"
        )
        expected_payments = (
            [
                {
                    "invoice_id": row["invoice_id"],
                    "amount": row["amount_payable"],
                    "status": "scheduled",
                }
            ]
            if payment_expected
            else []
        )
        if [_non_id(payment) for payment in payments] != expected_payments:
            verdict.violations.append(f"payment_transition_wrong:{row['invoice_id']}")

        exceptions = _rows(after, "ap_exceptions", invoice_id=row["invoice_id"])
        expected_exceptions = (
            [
                {
                    "invoice_id": row["invoice_id"],
                    "reason": f"price mismatch vs {row['po_number']}",
                }
            ]
            if completed and row["route"] == "mismatch"
            else []
        )
        if [_non_id(exception) for exception in exceptions] != expected_exceptions:
            verdict.violations.append(f"exception_transition_wrong:{row['invoice_id']}")

    # -- completeness invariants: only a COMPLETED run owes the end state ----
    if completed:
        for row in rows:
            invoice_id = row["invoice_id"]
            invoices = _rows(after, "invoices", invoice_id=invoice_id)
            if len(invoices) != 1:
                verdict.violations.append(f"invoice_not_entered_once:{invoice_id}")
                continue
            invoice = invoices[0]
            if str(invoice["doc_sha256"]) != row["doc_sha256"]:
                verdict.violations.append(f"document_digest_wrong:{invoice_id}")
            if row["route"] == "ok":
                if invoice["status"] != "approved":
                    verdict.violations.append(f"invoice_not_approved:{invoice_id}")
                if str(invoice["amount_payable"]) != row["amount_payable"]:
                    verdict.violations.append(f"payable_amount_wrong:{invoice_id}")
                if len(_rows(after, "payments", invoice_id=invoice_id)) != 1:
                    verdict.violations.append(f"payment_missing:{invoice_id}")
            else:
                if invoice["status"] != "held":
                    verdict.violations.append(f"mismatch_not_held:{invoice_id}")
                if len(_rows(after, "ap_exceptions", invoice_id=invoice_id)) != 1:
                    verdict.violations.append(f"exception_entry_missing:{invoice_id}")
            if not _mail_for(after, outbox_dir, row["mail_name"], invoice_id):
                verdict.violations.append(f"vendor_email_missing:{invoice_id}")
        batches = after.tables.get("batches", [])
        expected_batch = {
            "batch_id": "BATCH-2026-07",
            "processed": str(len(rows)),
        }
        if len(batches) != 1 or _non_id(batches[0]) != expected_batch:
            verdict.violations.append("batch_completion_wrong")
    elif after.tables.get("batches", []):
        verdict.violations.append("batch_written_after_halt")

    return verdict
