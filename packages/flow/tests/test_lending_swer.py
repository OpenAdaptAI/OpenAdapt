"""Lending (MockLoan) effect-verified SWER + fault-taxonomy tests (no browser).

Two layers, both CI-fast and model-free:

- Pure-Python tests of the lending outcome taxonomy
  (``benchmark.lending_fault_model.faults``) and the fault registry.
- A LIVE three-arm Silent Wrong-Effect run through the shared EffectBench
contract (``benchmark.lending_fault_model.swer``) against the MockLoan fault
server's SQLite persistence boundary - asserting the
  headline: screen-only leaves silent wrong effects, a single-surface verifier
  leaves the collateral residual, and the complete read path drives SWER to
  zero. No Playwright browser is needed
  (the writes go straight to the boundary), so this runs in the fast `test`
  gate, not the `e2e-browser` gate.
"""

from __future__ import annotations

import inspect
import sqlite3

from benchmark.lending_fault_model import faults as F
from benchmark.lending_fault_model import ground_truth as G
from benchmark.lending_fault_model import swer as S
from openadapt_flow.benchmark.effectbench import summarize
from openadapt_flow.mockloan.fault_server import LedgerDB


def _row(memo: str = F.MEMO_TEXT, source: str = "replay", **kw) -> dict:
    base = {
        "id": 1,
        "loan_id": F.TARGET_LOAN,
        "product": F.TARGET_PRODUCT,
        "amount": F.TARGET_AMOUNT,
        "memo": memo,
        "source": source,
        "key": None,
    }
    base.update(kw)
    return base


class TestClassify:
    def test_success(self) -> None:
        outcome, _ = F.classify(
            report_success=True, records=[_row()], seeded_concurrent=False
        )
        assert outcome == F.SUCCESS

    def test_phantom_success_is_undetected(self) -> None:
        outcome, _ = F.classify(
            report_success=True, records=[], seeded_concurrent=False
        )
        assert outcome == F.UNDETECTED_FAILURE

    def test_partial_save_is_undetected(self) -> None:
        outcome, _ = F.classify(
            report_success=True, records=[_row(memo="")], seeded_concurrent=False
        )
        assert outcome == F.UNDETECTED_FAILURE

    def test_duplicate_is_wrong_action(self) -> None:
        outcome, _ = F.classify(
            report_success=True,
            records=[_row(id=1), _row(id=2)],
            seeded_concurrent=False,
        )
        assert outcome == F.WRONG_ACTION

    def test_lost_update_is_wrong_action(self) -> None:
        # The concurrent 'other' row was overwritten (gone), only ours remains.
        outcome, _ = F.classify(
            report_success=True, records=[_row(id=2)], seeded_concurrent=True
        )
        assert outcome == F.WRONG_ACTION

    def test_lost_update_not_flagged_when_concurrent_row_survives(self) -> None:
        outcome, _ = F.classify(
            report_success=True,
            records=[_row(id=1, source="other"), _row(id=2)],
            seeded_concurrent=True,
        )
        assert outcome == F.SUCCESS

    def test_timeout_after_write_is_false_abort(self) -> None:
        outcome, _ = F.classify(
            report_success=False, records=[_row()], seeded_concurrent=False
        )
        assert outcome == F.FALSE_ABORT

    def test_clean_halt_is_safe(self) -> None:
        outcome, _ = F.classify(
            report_success=False, records=[], seeded_concurrent=False
        )
        assert outcome == F.SAFE_HALT

    def test_silently_mishandled_flag(self) -> None:
        assert F.is_silently_mishandled(F.WRONG_ACTION, report_success=True)
        assert F.is_silently_mishandled(F.UNDETECTED_FAILURE, report_success=True)
        assert not F.is_silently_mishandled(F.FALSE_ABORT, report_success=False)
        assert not F.is_silently_mishandled(F.SUCCESS, report_success=True)


class TestRegistry:
    def test_seven_transactional_classes_present(self) -> None:
        transactional = [f for f in F.FAULTS if f.fault_class[0].isdigit()]
        numbers = sorted(f.fault_class.split(".")[0] for f in transactional)
        assert numbers == ["1", "2", "3", "4", "5", "6", "7"]

    def test_controls_present(self) -> None:
        assert "ok" in F.FAULTS_BY_MODE
        assert "idempotent" in F.FAULTS_BY_MODE

    def test_only_stale_seeds_a_concurrent_row(self) -> None:
        seeding = {f.mode for f in F.FAULTS if f.seed_concurrent}
        assert seeding == {"stale"}


class TestLedgerDB:
    def test_ground_truth_import_graph_excludes_product_verifiers_and_judge(
        self,
    ) -> None:
        source = inspect.getsource(G)
        assert "judge_records" not in source
        assert "RestRecordVerifier" not in source
        assert "SqlRecordVerifier" not in source

    def test_ground_truth_discovers_business_tables_only(self) -> None:
        db = LedgerDB()
        assert G.audited_tables(db.database_path) == ("records",)

    def test_ground_truth_catches_new_business_table_without_record_row(self) -> None:
        db = LedgerDB()
        before = G.capture(db.database_path)
        with sqlite3.connect(db.database_path) as conn:
            conn.execute("CREATE TABLE outbound_payments (id INTEGER PRIMARY KEY)")
            conn.execute("INSERT INTO outbound_payments(id) VALUES (1)")
        after = G.capture(db.database_path)
        truth = G.judge(
            before,
            after,
            intended_loan=S.TARGET_LOAN,
            intended_product=S.TARGET_PRODUCT,
            intended_amount=S.TARGET_AMOUNT,
            intended_memo="trial memo",
        )

        assert truth.correct is False
        assert truth.fault_class == "collateral_write"
        assert truth.persisted_count > 0
        assert truth.table_deltas["outbound_payments"] == 1

    def test_ground_truth_catches_same_count_business_table_update(self) -> None:
        db = LedgerDB()
        with sqlite3.connect(db.database_path) as conn:
            conn.execute(
                "CREATE TABLE account_state "
                "(id INTEGER PRIMARY KEY, balance TEXT NOT NULL)"
            )
            conn.execute("INSERT INTO account_state VALUES (1, '100')")
        before = G.capture(db.database_path)
        db.add(
            S.TARGET_LOAN,
            S.TARGET_PRODUCT,
            S.TARGET_AMOUNT,
            "trial memo",
        )
        with sqlite3.connect(db.database_path) as conn:
            conn.execute("UPDATE account_state SET balance = '0' WHERE id = 1")
        after = G.capture(db.database_path)
        truth = G.judge(
            before,
            after,
            intended_loan=S.TARGET_LOAN,
            intended_product=S.TARGET_PRODUCT,
            intended_amount=S.TARGET_AMOUNT,
            intended_memo="trial memo",
        )

        assert truth.correct is False
        assert truth.fault_class == "collateral_write"
        assert truth.table_deltas["account_state"] == 0
        assert "account_state" in truth.table_changes

    def test_ground_truth_catches_same_count_delete_insert(self) -> None:
        db = LedgerDB()
        with sqlite3.connect(db.database_path) as conn:
            conn.execute(
                "CREATE TABLE payout_queue "
                "(id INTEGER PRIMARY KEY, destination TEXT NOT NULL)"
            )
            conn.execute("INSERT INTO payout_queue VALUES (1, 'approved')")
        before = G.capture(db.database_path)
        db.add(
            S.TARGET_LOAN,
            S.TARGET_PRODUCT,
            S.TARGET_AMOUNT,
            "trial memo",
        )
        with sqlite3.connect(db.database_path) as conn:
            conn.execute("DELETE FROM payout_queue WHERE id = 1")
            conn.execute("INSERT INTO payout_queue VALUES (2, 'redirected')")
        after = G.capture(db.database_path)
        truth = G.judge(
            before,
            after,
            intended_loan=S.TARGET_LOAN,
            intended_product=S.TARGET_PRODUCT,
            intended_amount=S.TARGET_AMOUNT,
            intended_memo="trial memo",
        )

        assert truth.correct is False
        assert truth.fault_class == "collateral_write"
        assert truth.table_deltas["payout_queue"] == 0
        assert "payout_queue" in truth.table_changes

    def test_ground_truth_catches_same_count_schema_change(self) -> None:
        db = LedgerDB()
        with sqlite3.connect(db.database_path) as conn:
            conn.execute("CREATE TABLE approvals (id INTEGER PRIMARY KEY)")
            conn.execute("INSERT INTO approvals VALUES (1)")
        before = G.capture(db.database_path)
        db.add(
            S.TARGET_LOAN,
            S.TARGET_PRODUCT,
            S.TARGET_AMOUNT,
            "trial memo",
        )
        with sqlite3.connect(db.database_path) as conn:
            conn.execute("ALTER TABLE approvals ADD COLUMN status TEXT")
        after = G.capture(db.database_path)
        truth = G.judge(
            before,
            after,
            intended_loan=S.TARGET_LOAN,
            intended_product=S.TARGET_PRODUCT,
            intended_amount=S.TARGET_AMOUNT,
            intended_memo="trial memo",
        )

        assert truth.correct is False
        assert truth.fault_class == "collateral_write"
        assert truth.table_deltas["approvals"] == 0
        assert "approvals" in truth.table_changes

    def test_ground_truth_catches_records_schema_change(self) -> None:
        db = LedgerDB()
        before = G.capture(db.database_path)
        db.add(
            S.TARGET_LOAN,
            S.TARGET_PRODUCT,
            S.TARGET_AMOUNT,
            "trial memo",
        )
        with sqlite3.connect(db.database_path) as conn:
            conn.execute("ALTER TABLE records ADD COLUMN approval_state TEXT")
        after = G.capture(db.database_path)
        truth = G.judge(
            before,
            after,
            intended_loan=S.TARGET_LOAN,
            intended_product=S.TARGET_PRODUCT,
            intended_amount=S.TARGET_AMOUNT,
            intended_memo="trial memo",
        )

        assert truth.correct is False
        assert truth.fault_class == "collateral_mutation"
        assert truth.table_deltas["records"] == 1
        assert "records" in truth.table_changes

    def test_ground_truth_catches_unselected_preexisting_record_mutation(
        self,
    ) -> None:
        db = LedgerDB()
        with sqlite3.connect(db.database_path) as conn:
            conn.execute(
                "ALTER TABLE records ADD COLUMN audit_state TEXT DEFAULT 'clean'"
            )
        existing = db.add("L0999", "Legacy", "10", "pre-existing")
        before = G.capture(db.database_path)
        db.add(
            S.TARGET_LOAN,
            S.TARGET_PRODUCT,
            S.TARGET_AMOUNT,
            "trial memo",
        )
        with sqlite3.connect(db.database_path) as conn:
            conn.execute(
                "UPDATE records SET audit_state = 'corrupt' WHERE id = ?",
                (existing["id"],),
            )
        after = G.capture(db.database_path)
        truth = G.judge(
            before,
            after,
            intended_loan=S.TARGET_LOAN,
            intended_product=S.TARGET_PRODUCT,
            intended_amount=S.TARGET_AMOUNT,
            intended_memo="trial memo",
        )

        assert truth.correct is False
        assert truth.fault_class == "collateral_loss"
        assert truth.table_deltas["records"] == 1
        assert "records" in truth.table_changes

    def test_ground_truth_catches_unkeyed_shadowed_rowid_table_update(self) -> None:
        db = LedgerDB()
        with sqlite3.connect(db.database_path) as conn:
            conn.execute(
                "CREATE TABLE unkeyed_state "
                "(rowid TEXT, _rowid_ TEXT, oid TEXT, value TEXT)"
            )
            conn.execute(
                "INSERT INTO unkeyed_state VALUES "
                "('shadow-1', 'shadow-2', 'shadow-3', 'approved')"
            )
        before = G.capture(db.database_path)
        assert before.tables["unkeyed_state"].identity_kind == (
            "canonical_row_multiset"
        )
        db.add(
            S.TARGET_LOAN,
            S.TARGET_PRODUCT,
            S.TARGET_AMOUNT,
            "trial memo",
        )
        with sqlite3.connect(db.database_path) as conn:
            conn.execute("UPDATE unkeyed_state SET value = 'redirected'")
        after = G.capture(db.database_path)
        truth = G.judge(
            before,
            after,
            intended_loan=S.TARGET_LOAN,
            intended_product=S.TARGET_PRODUCT,
            intended_amount=S.TARGET_AMOUNT,
            intended_memo="trial memo",
        )

        assert truth.correct is False
        assert truth.fault_class == "collateral_write"
        assert truth.table_deltas["unkeyed_state"] == 0
        assert "unkeyed_state" in truth.table_changes

    def test_idempotency_key_dedups(self) -> None:
        db = LedgerDB()
        db.reset()
        db.add("L1001", "Personal", "18500", "m", key="k1")
        db.add("L1001", "Personal", "18500", "m", key="k1")
        assert len(db.snapshot()["records"]) == 1

    def test_no_key_appends(self) -> None:
        db = LedgerDB()
        db.reset()
        db.add("L1001", "Personal", "18500", "m")
        db.add("L1001", "Personal", "18500", "m")
        assert len(db.snapshot()["records"]) == 2

    def test_overwrite_loan_drops_concurrent_row(self) -> None:
        db = LedgerDB()
        db.reset(seed_concurrent=True)
        assert any(r["source"] == "other" for r in db.snapshot()["records"])
        db.add("L1001", "Personal", "18500", "m", overwrite_loan=True)
        recs = db.snapshot()["records"]
        assert all(r["source"] != "other" for r in recs)

    def test_partial_drops_memo(self) -> None:
        db = LedgerDB()
        db.reset()
        rec = db.add("L1001", "Personal", "18500", "")
        assert rec["memo"] == ""

    def test_fees_surface_hidden_from_single_surface_read(self) -> None:
        db = LedgerDB()
        db.reset()
        db.add("L1001", "Personal", "18500", "m")  # disbursement
        db.add("L1001", "Personal", "18500", "m", surface="fees")  # collateral
        full = db.snapshot()["records"]
        single = db.snapshot(surface="disbursements")["records"]
        # The complete read path sees both surfaces; the single-surface read
        # path is blind to the fees-surface (collateral) row.
        assert len(full) == 2
        assert len(single) == 1
        assert single[0]["surface"] == "disbursements"

    def test_overwrite_loan_is_surface_scoped(self) -> None:
        db = LedgerDB()
        db.reset()
        db.add("L1001", "Personal", "18500", "m", surface="fees")
        # A last-write-wins disbursement must not wipe the fees-surface row.
        db.add("L1001", "Personal", "18500", "m2", overwrite_loan=True)
        surfaces = sorted(r["surface"] for r in db.snapshot()["records"])
        assert surfaces == ["disbursements", "fees"]


class TestSwerCoverage:
    def test_all_public_categories_plus_lending_collateral_variant(self) -> None:
        cats = {t.category.value for t in S.TASKS}
        for c in (
            "C1_partial_save",
            "C2_duplicate_submission",
            "C3_optimistic_then_reject",
            "C4_stale_overwrite",
            "C5_double_delivered_input",
            "C6_wrong_record_homonym",
            "C7_silent_noop_wrong_target",
            "control",
        ):
            assert c in cats, c
        collateral = next(t for t in S.TASKS if t.fault == "collateral")
        assert collateral.category.value == "C2_duplicate_submission"
        assert collateral.task_id == "lending_collateral_unaudited"

    def test_three_arm_ladder(self) -> None:
        assert S.ARMS == (
            "screen_only",
            "effect_verify_single",
            "effect_verify_full",
        )


class TestLiveThreeArmSwer:
    """The headline ladder, live through the independent complete-read oracle."""

    def test_ladder_screen_then_single_residual_then_full_zero(self) -> None:
        episodes = S.run_pack(trials=3)
        screen = summarize(episodes, arm="screen_only")
        single = summarize(episodes, arm="effect_verify_single")
        full = summarize(episodes, arm="effect_verify_full")

        # The screen-only arm silently mishandles the injected faults.
        assert screen.swer.rate > 0.4, screen.swer.rate
        # The single-surface oracle leaves a NON-ZERO residual (the collateral
        # write on the fees surface it cannot see) - the lending analog of the
        # clinical 9/90 single-surface residual.
        assert single.swer.numerator > 0, single.outcome_counts
        assert single.swer.numerator < screen.swer.numerator
        # The COMPLETE read path over every mutable surface drives it to zero.
        assert full.swer.numerator == 0, full.outcome_counts
        assert full.swer.rate == 0.0
        # The controls still succeed under effect verification (not always-halt).
        assert full.task_success.numerator > 0
        # The success-effect gap collapses down the ladder.
        assert full.success_effect_gap <= single.success_effect_gap
        assert single.success_effect_gap < screen.success_effect_gap

    def test_committed_shape_is_a_bounded_public_aggregate(self) -> None:
        result = S.measure(trials=1)

        assert set(result) == {"meta", *S.ARMS}
        assert result["meta"]["evidence_scope"] == "bounded_aggregate"
        observed_keys: set[str] = set()

        def collect_keys(value: object) -> None:
            if isinstance(value, dict):
                observed_keys.update(str(key) for key in value)
                for child in value.values():
                    collect_keys(child)
            elif isinstance(value, list):
                for child in value:
                    collect_keys(child)

        collect_keys(result)
        for raw_key in (
            "episode_id",
            "task_id",
            "trial",
            "params",
            "env_fingerprint",
            "expected_effect_hash",
        ):
            assert raw_key not in observed_keys

        # EffectBench's category-level decomposition remains public; the raw
        # task/episode matrix does not.
        assert result["screen_only"]["cells"]
        assert "episodes" not in result
        assert "coverage_matrix" not in result

    def test_single_surface_residual_is_exactly_the_collateral_class(self) -> None:
        episodes = S.run_pack(trials=3)
        silent = {
            e.task_id
            for e in episodes
            if e.arm == "effect_verify_single"
            and e.outcome.value == "silent_wrong_effect"
        }
        assert silent == {"lending_collateral_unaudited"}, silent
        # ...and the full read path catches that same class (not silent).
        collateral_full = [
            e
            for e in episodes
            if e.arm == "effect_verify_full"
            and e.task_id == "lending_collateral_unaudited"
        ]
        assert collateral_full and all(
            e.outcome.value != "silent_wrong_effect" for e in collateral_full
        )

    def test_oracle_is_isolated_from_the_arm(self) -> None:
        # The benchmark oracle reads SQLite directly; the product arm reads REST.
        episodes = S.run_pack(
            tasks=(S.TASKS[0],), arms=("effect_verify_full",), trials=1
        )
        assert episodes[0].oracle.channel == "sqlite_ground_truth"

    def test_independent_judge_classifies_wrong_loan_and_collateral_deltas(
        self,
    ) -> None:
        wrong_loan = next(
            task for task in S.TASKS if task.task_id == "lending_c6_wrong_loan"
        )
        collateral = next(task for task in S.TASKS if task.fault == "collateral")
        with S.serve_mockloan() as (base_url, db):
            wrong_episode = S.run_episode(
                wrong_loan,
                arm="effect_verify_full",
                trial=0,
                base_url=base_url,
                db=db,
            )
            collateral_episode = S.run_episode(
                collateral,
                arm="effect_verify_full",
                trial=0,
                base_url=base_url,
                db=db,
            )

        assert wrong_episode.outcome.value == "wrong_action"
        assert "wrong_record" in wrong_episode.oracle.reason
        assert "table_deltas={'records': 1}" in wrong_episode.oracle.reason
        assert collateral_episode.outcome.value == "wrong_action"
        assert "collateral_write" in collateral_episode.oracle.reason
        assert "table_deltas={'records': 2}" in collateral_episode.oracle.reason

    def test_lying_full_rest_readback_cannot_fool_sqlite_judge(self) -> None:
        collateral = next(task for task in S.TASKS if task.fault == "collateral")
        with S.serve_mockloan() as (base_url, db):
            real_snapshot = db.snapshot

            def hide_fee_surface(*, surface=None):
                # Simulate a corrupt / incomplete REST handler: even /api/db
                # returns only the disbursement surface.
                return real_snapshot(surface="disbursements")

            db.snapshot = hide_fee_surface  # type: ignore[method-assign]
            episode = S.run_episode(
                collateral,
                arm="effect_verify_full",
                trial=0,
                base_url=base_url,
                db=db,
            )

        assert episode.reported_success is True  # the lied-to REST arm certifies
        assert episode.oracle.channel == "sqlite_ground_truth"
        assert episode.outcome.value == "silent_wrong_effect"
