"""Deterministic SQLite GnuCash ledger schema and double-entry transaction seed."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LedgerEntryFixture:
    """Synthetic double-entry GnuCash transaction fixture."""

    tx_guid: str
    description: str
    post_date: str
    amount_cents: int
    debit_account_guid: str = "acct-expense-office"
    credit_account_guid: str = "acct-asset-checking"


def default_ledger_entry() -> LedgerEntryFixture:
    return LedgerEntryFixture(
        tx_guid="tx-syn-gnucash-3001",
        description="Synthetic Office Supplies Invoice #3001",
        post_date="2026-11-22 12:00:00",
        amount_cents=18500,
    )


def seed_gnucash_sqlite(db_path: Path) -> Path:
    """Initialize a GnuCash-compatible SQLite ledger with accounts and baseline tables."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS gnclock (
                Hostname TEXT,
                PID INTEGER
            );
            CREATE TABLE IF NOT EXISTS accounts (
                guid TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                account_type TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS transactions (
                guid TEXT PRIMARY KEY,
                currency_guid TEXT NOT NULL,
                num TEXT NOT NULL,
                post_date TEXT NOT NULL,
                enter_date TEXT NOT NULL,
                description TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS splits (
                guid TEXT PRIMARY KEY,
                tx_guid TEXT NOT NULL,
                account_guid TEXT NOT NULL,
                memo TEXT NOT NULL,
                value_num INTEGER NOT NULL,
                value_denom INTEGER NOT NULL,
                FOREIGN KEY (tx_guid) REFERENCES transactions(guid),
                FOREIGN KEY (account_guid) REFERENCES accounts(guid)
            );
            """
        )
        conn.executemany(
            "INSERT OR REPLACE INTO accounts (guid, name, account_type) VALUES (?, ?, ?)",
            [
                ("acct-asset-checking", "Checking Account", "BANK"),
                ("acct-expense-office", "Office Supplies", "EXPENSE"),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def record_balanced_transaction(db_path: Path, entry: LedgerEntryFixture) -> None:
    """Persist a balanced double-entry transaction + two splits into the SQLite ledger."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO transactions (guid, currency_guid, num, post_date, enter_date, description)
            VALUES (?, 'USD', '3001', ?, ?, ?)
            """,
            (entry.tx_guid, entry.post_date, entry.post_date, entry.description),
        )
        conn.executemany(
            """
            INSERT INTO splits (guid, tx_guid, account_guid, memo, value_num, value_denom)
            VALUES (?, ?, ?, ?, ?, 100)
            """,
            [
                (
                    f"{entry.tx_guid}-debit",
                    entry.tx_guid,
                    entry.debit_account_guid,
                    entry.description,
                    entry.amount_cents,
                ),
                (
                    f"{entry.tx_guid}-credit",
                    entry.tx_guid,
                    entry.credit_account_guid,
                    entry.description,
                    -entry.amount_cents,
                ),
            ],
        )
        conn.commit()
    finally:
        conn.close()
