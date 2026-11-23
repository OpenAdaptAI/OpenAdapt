"""GnuCash native desktop adapter for the Real-App Verification Matrix."""

from __future__ import annotations

import time
from pathlib import Path

from benchmarks.real_apps.apps.gnucash.fault_injectors import (
    inject_readonly_lock,
    simulate_unsaved_buffer_write,
)
from benchmarks.real_apps.apps.gnucash.launcher import resolve_gnucash_binary
from benchmarks.real_apps.apps.gnucash.readback import readback_gnucash_transaction
from benchmarks.real_apps.apps.gnucash.seed import (
    LedgerEntryFixture,
    default_ledger_entry,
    record_balanced_transaction,
    seed_gnucash_sqlite,
)
from benchmarks.real_apps.harness import (
    AppRunResult,
    ReadbackResult,
    RealAppSpec,
    WorkflowSpec,
)

GNUCASH_SPEC = RealAppSpec(
    app_id="gnucash",
    display_name="GnuCash Desktop",
    vertical="desktop-accounting",
    surface="desktop",
    ui_architecture="gtk3-native-desktop",
    persistence_oracle="sqlite-ledger",
    compose_file=None,
    tier="pr",
    workflows=(
        WorkflowSpec(
            workflow_id="post_journal_entry",
            description="Post a balanced double-entry transaction and verify SQLite transactions + splits",
            expected_writes=1,
            fault_mode="readonly_lock_unsaved_buffer",
        ),
    ),
)


class GnuCashAdapter:
    """RealAppAdapter for native desktop GnuCash."""

    spec = GNUCASH_SPEC

    def run_contract_verification(self, work_dir: Path) -> AppRunResult:
        t0 = time.monotonic()
        db_path = seed_gnucash_sqlite(work_dir / "ledger.gnucash")
        happy_entry = default_ledger_entry()
        record_balanced_transaction(db_path, happy_entry)
        happy_rb = readback_gnucash_transaction(
            db_path,
            expected_tx_guid=happy_entry.tx_guid,
            expected_description=happy_entry.description,
            expected_amount_cents=happy_entry.amount_cents,
        )

        # Inject gnclock read-only lock + unsaved buffer write
        inject_readonly_lock(db_path)
        fault_entry = LedgerEntryFixture(
            tx_guid="tx-syn-gnucash-fault-3002",
            description="Synthetic Unsaved Entry #3002",
            post_date="2026-11-22 12:05:00",
            amount_cents=42000,
        )
        ui_state = simulate_unsaved_buffer_write(db_path, fault_entry)
        fault_rb = readback_gnucash_transaction(
            db_path,
            expected_tx_guid=fault_entry.tx_guid,
            expected_description=fault_entry.description,
            expected_amount_cents=fault_entry.amount_cents,
        )

        return AppRunResult(
            app_id=self.spec.app_id,
            display_name=self.spec.display_name,
            vertical=self.spec.vertical,
            surface=self.spec.surface,
            tier=self.spec.tier,
            mode="contract",
            happy_path_verified=happy_rb.confirmed,
            silent_fault_caught=(
                bool(ui_state["ui_row_visible"]) and not fault_rb.confirmed
            ),
            fault_mode_tested="readonly_lock_unsaved_buffer",
            elapsed_s=time.monotonic() - t0,
            happy_readback=happy_rb,
            fault_readback=fault_rb,
        )

    def run_live_verification(self, work_dir: Path) -> AppRunResult:
        t0 = time.monotonic()
        binary = resolve_gnucash_binary()
        if binary is None:
            rb = ReadbackResult(
                confirmed=False,
                oracle="gnucash-sqlite",
                reason="GnuCash binary is not installed on this host",
            )
            return AppRunResult(
                app_id=self.spec.app_id,
                display_name=self.spec.display_name,
                vertical=self.spec.vertical,
                surface=self.spec.surface,
                tier=self.spec.tier,
                mode="live",
                happy_path_verified=False,
                silent_fault_caught=False,
                fault_mode_tested="readonly_lock_unsaved_buffer",
                elapsed_s=time.monotonic() - t0,
                happy_readback=rb,
                fault_readback=rb,
            )
        return self.run_contract_verification(work_dir)
