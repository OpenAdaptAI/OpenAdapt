"""Direct read-only SQLite independent read-back oracle for GnuCash."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from benchmarks.real_apps.harness import ReadbackResult


def readback_gnucash_transaction(
    db_path: Path,
    *,
    expected_tx_guid: str,
    expected_description: str,
    expected_amount_cents: int,
) -> ReadbackResult:
    """Open the GnuCash SQLite file strictly read-only and verify ``transactions`` + ``splits``."""
    if not db_path.is_file():
        return ReadbackResult(
            confirmed=False,
            oracle="gnucash-sqlite",
            record_id=expected_tx_guid,
            reason=f"SQLite ledger not found: {db_path}",
        )

    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.execute("PRAGMA query_only = ON")
        tx_row = conn.execute(
            "SELECT guid, description, post_date FROM transactions WHERE guid = ?",
            (expected_tx_guid,),
        ).fetchone()
        if tx_row is None:
            return ReadbackResult(
                confirmed=False,
                oracle="gnucash-sqlite",
                record_id=expected_tx_guid,
                reason=f"Transaction {expected_tx_guid!r} not found in SQLite ledger",
            )

        splits = conn.execute(
            "SELECT account_guid, value_num, value_denom FROM splits WHERE tx_guid = ?",
            (expected_tx_guid,),
        ).fetchall()
    finally:
        conn.close()

    split_sum = sum(int(s[1]) for s in splits)
    max_abs_cents = max((abs(int(s[1])) for s in splits), default=0)
    balanced = len(splits) >= 2 and split_sum == 0
    matched = (
        str(tx_row[1]) == expected_description
        and balanced
        and max_abs_cents == expected_amount_cents
    )
    return ReadbackResult(
        confirmed=matched,
        oracle="gnucash-sqlite",
        record_id=expected_tx_guid,
        observed_fields={
            "description": str(tx_row[1]),
            "split_count": len(splits),
            "split_sum": split_sum,
            "amount_cents": max_abs_cents,
        },
        reason="" if matched else "Transaction or balanced splits mismatch in GnuCash SQLite",
    )
