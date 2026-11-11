"""A fresh native read cannot erase a refutation or substitute another receipt."""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from openadapt_flow import verifier
from openadapt_flow.verifier import RecheckState
from openadapt_flow.write_ledger import Attempt
from tests import test_verifier_recheck as controls
from tests.test_verifier_recheck import (
    _assert_terminal_hold,
    _first,
    _params,
    _recheck,
    _save_sql,
    _sql,
)

# Register the exact original fixture objects without shadowed fixture imports.
_used_dir = controls._used_dir
due_now = controls.due_now


def _attempt(first):
    assert first.ledger is not None
    found = first.ledger.find_attempt(first.write_id)
    assert found is not None
    return found[1]


def _state(first, kind):
    assert first.recheck is not None
    return (
        first
        if kind == "result"
        else RecheckState.load(first.recheck.path, params=_params(2))
    )


@pytest.mark.parametrize("kind", ["result", "saved"])
def test_refute_restore_never_reports_confirmation(tmp_path, due_now, kind):
    contract, db, first = _first(tmp_path)
    state = _state(first, kind)
    _sql(db, "UPDATE patient_data SET fname='unexplained' WHERE id=1")
    assert _recheck(contract, state).outcome == "REFUTED"
    original = _attempt(first).resolved
    _sql(db, "UPDATE patient_data SET fname='Wendell' WHERE id=1")
    for _ in range(2):
        late = _recheck(contract, state)
        _assert_terminal_hold(late, "CONFIRMED")
        assert _attempt(first).state == "held"
        assert _attempt(first).resolved == original
        assert _attempt(first).rechecked is None


@pytest.mark.parametrize("kind", ["result", "saved"])
def test_transient_unreadable_recheck_recovers_only_exact_confirmation(
    tmp_path, due_now, kind
):
    contract, db, first = _first(tmp_path)
    state = _state(first, kind)
    preserved = db.with_suffix(".preserved")
    db.rename(preserved)
    assert _recheck(contract, state).outcome == "UNCERTAIN"
    assert _attempt(first).state == "uncertain"
    preserved.rename(db)
    late = _recheck(contract, state)
    assert late.outcome == "CONFIRMED" and late.verified
    attempt = _attempt(first)
    assert attempt.resolved["outcome"] == "CONFIRMED"
    assert attempt.resolved["receipt_sha256"] == first.receipt_sha256
    assert attempt.rechecked["receipt_sha256"] == first.receipt_sha256
    assert not attempt.recheck_owed and not attempt.review


def test_recovery_read_cannot_overwrite_concurrent_real_refutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, due_now: None
):
    contract, db, first = _first(tmp_path)
    preserved = db.with_suffix(".preserved")
    db.rename(preserved)
    assert _recheck(contract, first).outcome == "UNCERTAIN"
    preserved.rename(db)
    read_finished, resume = threading.Event(), threading.Event()
    original = verifier.recheck_run

    def pause(*args: Any, **kwargs: Any):
        observed = original(*args, **kwargs)
        if threading.current_thread().name.startswith("recover-read"):
            read_finished.set()
            assert resume.wait(15)
        return observed

    monkeypatch.setattr(verifier, "recheck_run", pause)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="recover-read") as pool:
        recovering = pool.submit(_recheck, contract, first)
        try:
            assert read_finished.wait(15)
            _sql(db, "INSERT INTO calendar(pid,title) VALUES (2,'concurrent')")
            assert _recheck(contract, first).outcome == "REFUTED"
        finally:
            resume.set()
        _assert_terminal_hold(recovering.result(timeout=15), "CONFIRMED")
    assert _attempt(first).state == "held"
    assert _attempt(first).resolved["outcome"] == "REFUTED"
    assert _attempt(first).rechecked is None


def test_another_receipt_in_the_same_attempt_cannot_authorize_recovery(
    tmp_path, due_now
):
    contract, db, first = _first(tmp_path)
    preserved = db.with_suffix(".preserved")
    db.rename(preserved)
    assert _recheck(contract, first).outcome == "UNCERTAIN"
    preserved.rename(db)
    ledger = first.ledger
    assert ledger is not None
    ledger.resolved(
        first.write_key,
        first.write_id,
        outcome="UNCERTAIN",
        via="recheck",
        receipt_sha256="0" * 64,
        review=True,
        retry_safe=False,
    )
    late = _recheck(contract, first)
    assert not late.verified and late.outcome == "UNCERTAIN"
    assert _attempt(first).resolved["outcome"] == "UNCERTAIN"
    assert _attempt(first).rechecked is None


def test_generated_transient_and_terminal_recheck_histories(tmp_path, monkeypatch):
    from hypothesis import given, settings
    from hypothesis import strategies as st

    case = 0

    @settings(deadline=None)
    @given(
        reads=st.integers(min_value=1, max_value=3),
        terminal=st.booleans(),
        kind=st.sampled_from(["result", "saved"]),
    )
    def history(reads, terminal, kind):
        nonlocal case
        case += 1
        root = tmp_path / str(case)
        root.mkdir()
        with monkeypatch.context() as scoped:
            scoped.setenv("OPENADAPT_LEDGER_DIR", str(root / "ledger"))
            scoped.setattr(verifier, "USED_DIR", root / "issued")
            contract, db, first = _first(root, recheck_after_s=0.01)
            state = _state(first, kind)
            preserved = db.with_suffix(".preserved")
            for _ in range(reads):
                db.rename(preserved)
                assert _recheck(contract, state, wait=True).outcome == "UNCERTAIN"
                preserved.rename(db)
            if terminal:
                _sql(db, "UPDATE patient_data SET fname='generated' WHERE id=1")
                assert _recheck(contract, state, wait=True).outcome == "REFUTED"
                _sql(db, "UPDATE patient_data SET fname='Wendell' WHERE id=1")
            late = _recheck(contract, state, wait=True)
            assert late.verified is (not terminal)
            assert _attempt(first).resolved["outcome"] == (
                "REFUTED" if terminal else "CONFIRMED"
            )
            if terminal:
                _assert_terminal_hold(late, "CONFIRMED")
                assert _attempt(first).rechecked is None
            else:
                assert (
                    _attempt(first).rechecked["receipt_sha256"] == first.receipt_sha256
                )

    history()


@pytest.mark.parametrize(
    ("found", "hold_reason"),
    [("landed", "person_decision"), ("absent", "released_attempt")],
)
def test_person_decision_cannot_be_replaced_by_favorable_read(
    tmp_path: Path, due_now: None, found: str, hold_reason: str
) -> None:
    contract, db, first = _first(tmp_path)
    saved = _state(first, "saved")
    assert first.ledger is not None
    _sql(db, "UPDATE patient_data SET fname='unexplained' WHERE id=1")
    assert _recheck(contract, saved).outcome == "REFUTED"
    _sql(db, "UPDATE patient_data SET fname='Wendell' WHERE id=1")
    if found == "absent":
        _sql(db, "DELETE FROM patient_data WHERE id=2")
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as reader:
        observed = reader.execute(
            "SELECT COUNT(*) FROM patient_data WHERE id=2"
        ).fetchone()[0]
    assert observed == int(found == "landed")
    decision = first.ledger.release(
        first.write_key, by="synthetic-reviewer", reason="checked store", found=found
    )
    assert decision.attempt_id == first.write_id
    if found == "absent":
        assert decision.released
        _sql(db, _save_sql(2)[0])
    else:
        assert decision.resolved["via"] == "person"
    late = _recheck(contract, saved)
    _assert_terminal_hold(late, "CONFIRMED", reason=hold_reason)
    assert late.receipt_sha256 == first.receipt_sha256
    retained = _attempt(first)
    assert retained.events == decision.events
    assert retained.resolved == decision.resolved
    assert retained.released is decision.released
    assert retained.rechecked is None


def test_person_decision_wins_a_refuting_recheck_already_about_to_append(
    tmp_path: Path, due_now: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract, db, first = _first(tmp_path)
    assert first.ledger is not None
    ledger = first.ledger
    preserved = db.with_suffix(".preserved")
    db.rename(preserved)
    assert _recheck(contract, first).outcome == "UNCERTAIN"
    before = _attempt(first)
    assert before.state == "uncertain"
    preserved.rename(db)
    _sql(db, "UPDATE patient_data SET fname='unexplained' WHERE id=1")
    original = ledger.resolved
    decisions: list[Attempt] = []

    def decide_before_append(key: str, attempt_id: str, **fields: Any) -> Attempt:
        assert key == first.write_key and attempt_id == first.write_id
        assert fields["outcome"] == "REFUTED"
        assert fields["expected_revision"] == before.revision
        decisions.append(
            ledger.release(
                key, by="synthetic-reviewer", reason="checked store", found="landed"
            )
        )
        return original(key, attempt_id, **fields)

    monkeypatch.setattr(ledger, "resolved", decide_before_append)
    late = _recheck(contract, first, ledger=ledger)
    _assert_terminal_hold(late, "REFUTED", reason="person_decision")
    (decision,) = decisions
    retained = _attempt(first)
    assert retained.attempt_id == first.write_id
    assert retained.resolved == decision.resolved
    assert retained.resolved["via"] == "person"
    assert retained.rechecked is None
    assert retained.events[:-1] == decision.events
    assert retained.events[-1]["applied"] is False


@pytest.mark.parametrize("receipt_sha256", ["b" * 64, ""], ids=["different", "missing"])
def test_confirmed_settlement_rejects_a_different_or_missing_receipt(receipt_sha256):
    ledger, key = controls._settled_ledger("CONFIRMED")
    before = ledger.latest(key)
    assert before is not None
    state = RecheckState(payload={"receipt": {"correlation_id": "first"}})
    late = verifier.RecheckResult(
        outcome="CONFIRMED", frame="held", receipt_sha256=receipt_sha256
    )
    reply = verifier._mark_rechecked(
        ledger,
        state,
        late,
        controls._NoFrameContract(),
        expected_revision=before.revision,
    )
    assert reply.outcome == "UNCERTAIN" and not reply.verified
    after = ledger.latest(key)
    assert after is not None
    assert after.resolved == before.resolved
    assert after.revision == before.revision and after.rechecked is None
