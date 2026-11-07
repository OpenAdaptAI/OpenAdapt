"""A recheck's returned time must cover its issued deadline before confirmation."""

from __future__ import annotations

import copy
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from openadapt_flow import verifier
from openadapt_flow.runtime.effects.late_write import format_time, parse_time
from openadapt_flow.verifier import Contract, RecheckState, begin_write, verify_write
from openadapt_flow.write_ledger import default_ledger
from tests.test_verifier_recheck import (
    BOT,
    _db,
    _doc,
    _params,
    _recheck,
    _save_sql,
    _sql,
)
from tests.test_verifier_recheck import (
    _used_dir as _used_dir,
)


def _issued_write(tmp_path: Path) -> tuple[Contract, Path, verifier.WriteResult]:
    db = _db(tmp_path)
    contract = Contract.from_dict(_doc(db, recheck_after_s=3600))
    pending = begin_write(contract, _params(2), actor=BOT, write_key="deadline")
    _sql(db, *_save_sql(2))
    result = verify_write(pending)
    assert result.verified and result.provisional and result.recheck is not None
    return contract, db, result


@pytest.mark.parametrize("saved", [False, True])
def test_actual_early_sqlite_read_keeps_the_issued_recheck_owed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, saved: bool
) -> None:
    """Inject premature scheduler admission, never the native result or clocks."""

    contract, _, result = _issued_write(tmp_path)
    assert result.recheck is not None and result.recheck.path is not None
    write = (
        RecheckState.load(result.recheck.path, params=_params(2)) if saved else result
    )
    issued_payload = copy.deepcopy(result.recheck.payload)
    ledger = default_ledger()
    before = ledger.latest(result.write_key)
    assert before is not None and before.recheck_owed == result.recheck_due_at
    monkeypatch.setattr(verifier, "seconds_until", lambda due: 0.0)

    late = _recheck(contract, write)

    assert parse_time(late.read_at) < parse_time(late.due_at)
    assert late.frame == "held" and late.evidence["status"] == "held"
    assert late.outcome == "UNCERTAIN" and not late.verified and late.recheck_owed
    assert late.read_at and late.due_at == result.recheck_due_at
    assert result.recheck.payload == issued_payload
    after = ledger.latest(result.write_key)
    assert after is not None and after.events == before.events
    assert after.rechecked is None and after.recheck_owed == result.recheck_due_at
    assert dict(ledger.rechecks_owed())[result.write_key].attempt_id == result.write_id
    with pytest.raises(verifier.AlreadyWritten) as raised:
        begin_write(contract, _params(2), actor=BOT, write_key="deadline")
    assert raised.value.result["provisional"] is True


@pytest.mark.parametrize("seconds_after_due", [0, 1])
def test_valid_returned_read_time_can_settle_at_or_after_the_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seconds_after_due: int
) -> None:
    """Metadata injection tests the boundary, not actual elapsed-window coverage."""

    contract, _, result = _issued_write(tmp_path)
    returned_time = format_time(
        parse_time(result.recheck_due_at) + timedelta(seconds=seconds_after_due)
    )
    real_read = verifier.recheck_run
    actual_reads: list[str] = []

    def read(*args: Any, **kwargs: Any) -> Any:
        verdict, actual_time = real_read(*args, **kwargs)
        assert verdict.status == "held"
        actual_reads.append(actual_time)
        return verdict, returned_time

    monkeypatch.setattr(verifier, "seconds_until", lambda due: 0.0)
    monkeypatch.setattr(verifier, "recheck_run", read)
    late = _recheck(contract, result)

    assert len(actual_reads) == 1
    assert late.read_at == returned_time and late.outcome == "CONFIRMED"
    assert not getattr(late, "recheck_owed", False)
    ledger = default_ledger()
    attempt = ledger.latest(result.write_key)
    assert attempt is not None and attempt.rechecked is not None
    assert attempt.rechecked["read_at"] == returned_time
    assert ledger.rechecks_owed() == []


@pytest.mark.parametrize(
    "returned_time",
    [
        None,
        "",
        "not-a-time",
        "2026-11-06T10:00:00",
        "2026-11-06T10:00:00+00:00",
        "2026-02-30T10:00:00Z",
        "2099-11-06T10:00:00Z\n",
    ],
)
def test_invalid_read_metadata_cannot_settle_a_real_held_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, returned_time: Any
) -> None:
    contract, _, result = _issued_write(tmp_path)
    ledger = default_ledger()
    before = ledger.latest(result.write_key)
    assert before is not None
    real_read = verifier.recheck_run
    actual_reads: list[str] = []

    def read(*args: Any, **kwargs: Any) -> Any:
        verdict, actual_time = real_read(*args, **kwargs)
        assert verdict.status == "held"
        actual_reads.append(actual_time)
        return verdict, returned_time

    monkeypatch.setattr(verifier, "seconds_until", lambda due: 0.0)
    monkeypatch.setattr(verifier, "recheck_run", read)
    late = _recheck(contract, result)

    assert len(actual_reads) == 1
    assert late.outcome == "UNCERTAIN" and not late.verified and late.recheck_owed
    assert late.read_at == returned_time and late.frame == "held"
    after = ledger.latest(result.write_key)
    assert after is not None and after.events == before.events
    assert after.rechecked is None and after.recheck_owed == result.recheck_due_at


def test_genuine_early_native_collateral_still_refutes_and_holds_the_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract, db, result = _issued_write(tmp_path)
    _sql(db, "INSERT INTO calendar (pid, title) VALUES (2, 'Delayed side record')")
    monkeypatch.setattr(verifier, "seconds_until", lambda due: 0.0)

    late = _recheck(contract, result)

    assert parse_time(late.read_at) < parse_time(late.due_at)
    assert late.outcome == "REFUTED" and late.frame == "refuted"
    assert not getattr(late, "recheck_owed", False)
    assert late.evidence["collateral_count"] > 0 and not late.verified
    ledger = default_ledger()
    attempt = ledger.latest(result.write_key)
    assert attempt is not None and attempt.resolved is not None
    assert attempt.resolved["outcome"] == "REFUTED" and attempt.rechecked is None
    with pytest.raises(verifier.WriteHeld):
        begin_write(contract, _params(2), actor=BOT, write_key="deadline")
