"""Actual SQLite observation timing, independent of scheduler admission."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from openadapt_flow import verifier, verify_run
from openadapt_flow.runtime.effects.late_write import parse_time
from openadapt_flow.verifier import Contract, begin_write, verify_write
from openadapt_flow.write_ledger import default_ledger
from tests.test_verifier_recheck import BOT, _db, _doc, _params, _save_sql, _sql
from tests.test_verifier_recheck import _used_dir as _used_dir  # noqa: F401

LATE = "INSERT INTO calendar (pid, title) VALUES (2, 'Delayed side record')"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _issue(tmp_path: Path) -> tuple[Contract, Path, verifier.WriteResult]:
    db = _db(tmp_path)
    contract = Contract.from_dict(_doc(db, recheck_after_s=3))
    pending = begin_write(contract, _params(2), actor=BOT, write_key="read-start")
    _sql(db, *_save_sql(2))
    result = verify_write(pending)
    assert result.verified and result.provisional and result.recheck is not None
    return contract, db, result


@pytest.mark.parametrize("late_write", [False, True])
def test_early_snapshot_returning_after_due_keeps_recheck_owed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, late_write: bool
) -> None:
    contract, db, result = _issue(tmp_path)
    due = parse_time(result.recheck_due_at)
    ledger = default_ledger()
    before = ledger.latest(result.write_key)
    assert before is not None
    monkeypatch.setattr(verifier, "seconds_until", lambda _due: 0.0)
    real_judge = verify_run.judge_run_frame
    observed: dict[str, datetime] = {}

    def delayed_return(*args: Any, **kwargs: Any) -> Any:
        observed["started"] = _now()
        frame = real_judge(*args, **kwargs)
        observed["read_done"] = _now()
        assert observed["read_done"] < due
        assert frame.status == "held"
        if late_write:
            _sql(db, LATE)
            observed["late_commit"] = _now()
            assert observed["late_commit"] < due
        # Only return latency is delayed. The native snapshot and its verdict
        # are unchanged, and no clock or returned read timestamp is injected.
        limit = time.monotonic() + 6
        while _now() < due:
            assert time.monotonic() < limit, "clock did not reach issued deadline"
            time.sleep(min(0.01, max(0.0, (due - _now()).total_seconds())))
        observed["returned"] = _now()
        return frame

    monkeypatch.setattr(verify_run, "judge_run_frame", delayed_return)
    late = verifier.recheck(contract, result, params=_params(2), actor=BOT)

    assert observed["started"] <= observed["read_done"] < due <= observed["returned"]
    assert parse_time(late.read_at) < due
    assert late.outcome == "UNCERTAIN" and late.recheck_owed and not late.verified
    assert late.frame == "held" and late.evidence["status"] == "held"
    after = ledger.latest(result.write_key)
    assert after is not None and after.events == before.events
    assert after.rechecked is None and after.recheck_owed == result.recheck_due_at
    assert dict(ledger.rechecks_owed())[result.write_key].attempt_id == result.write_id


@pytest.mark.parametrize("late_write", [False, True])
def test_actual_due_read_can_confirm_or_refute_without_timestamp_injection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, late_write: bool
) -> None:
    contract, db, result = _issue(tmp_path)
    due = parse_time(result.recheck_due_at)
    if late_write:
        _sql(db, LATE)
        assert _now() < due
    real_judge = verify_run.judge_run_frame
    observations: list[datetime] = []

    def observe(*args: Any, **kwargs: Any) -> Any:
        observations.append(_now())
        return real_judge(*args, **kwargs)

    monkeypatch.setattr(verify_run, "judge_run_frame", observe)
    late = verifier.recheck(contract, result, params=_params(2), actor=BOT, wait=True)

    assert len(observations) == 1 and observations[0] >= due
    assert parse_time(late.read_at) >= due
    assert late.outcome == ("REFUTED" if late_write else "CONFIRMED")
    assert not late.recheck_owed
    attempt = default_ledger().latest(result.write_key)
    assert attempt is not None
    if late_write:
        assert attempt.resolved is not None and attempt.resolved["outcome"] == "REFUTED"
        assert attempt.rechecked is None
    else:
        assert late.verified and attempt.rechecked is not None
        assert attempt.rechecked["read_at"] == late.read_at
