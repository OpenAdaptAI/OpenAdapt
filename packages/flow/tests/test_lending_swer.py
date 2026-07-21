"""Lending (MockLoan) effect-verified SWER + fault-taxonomy tests (no browser).

Two layers, both CI-fast and model-free:

- Pure-Python tests of the lending outcome taxonomy
  (``benchmark.lending_fault_model.faults``) and the fault registry.
- A LIVE two-arm Silent Wrong-Effect run through the shared EffectBench
  contract (``benchmark.lending_fault_model.swer``) against the MockLoan fault
  server's real HTTP persistence boundary (``GET /api/db``) - asserting the
  headline: the screen-only arm silently mishandles the injected faults while
  the effect-verified arm drives SWER to zero. No Playwright browser is needed
  (the writes go straight to the boundary), so this runs in the fast `test`
  gate, not the `e2e-browser` gate.
"""

from __future__ import annotations

from benchmark.lending_fault_model import faults as F
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


class TestSwerCoverage:
    def test_all_seven_categories_plus_controls(self) -> None:
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


class TestLiveTwoArmSwer:
    """The headline, live through the independent /api/db oracle."""

    def test_screen_only_is_silently_wrong_but_effect_verify_is_zero(self) -> None:
        episodes = S.run_pack(trials=3)
        screen = summarize(episodes, arm="screen_only")
        effect = summarize(episodes, arm="effect_verify")

        # The screen-only arm silently mishandles the injected faults.
        assert screen.swer.rate > 0.4, screen.swer.rate
        # Effect verification drives the SILENT wrong-effect rate to zero.
        assert effect.swer.numerator == 0, effect.outcome_counts
        assert effect.swer.rate == 0.0
        # The controls still succeed under effect verification (not always-halt).
        assert effect.task_success.numerator > 0
        # The success-effect gap collapses under effect verification.
        assert effect.success_effect_gap < screen.success_effect_gap

    def test_oracle_is_isolated_from_the_arm(self) -> None:
        # The benchmark oracle reads /api/db, a path the SPA never calls, and is
        # a distinct instance from the effect_verify arm's own verifier.
        episodes = S.run_pack(tasks=(S.TASKS[0],), arms=("effect_verify",), trials=1)
        assert episodes[0].oracle.channel == "rest"
