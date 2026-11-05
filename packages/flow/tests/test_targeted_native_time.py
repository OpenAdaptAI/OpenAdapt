"""Actual fractional native time and future-time refusal; source-only/unrun."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from openadapt_flow import verifier as api
from tests.test_targeted_recheck import ACTOR, PARAMS, contract, server


def test_fractional_native_timestamp_is_compared_to_full_read_precision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: Any
) -> None:
    from openadapt_flow import verify_run

    monkeypatch.setenv("OPENADAPT_VERIFIER_STATE_DIR", str(tmp_path / "authority"))
    store, base = server
    cfg = contract(base)
    pending = api.begin_write(
        cfg, PARAMS, actor=ACTOR, ledger=None, workdir=tmp_path / "runs"
    )
    native_time = datetime.now(timezone.utc)
    assert native_time.microsecond != 0
    store.save(timestamp=native_time.isoformat())
    # Only the existing whole-second RECEIPT clock is fixed, to make the old
    # comparison fail deterministically. Native timestamp is actual/unmodified.
    monkeypatch.setattr(
        verify_run, "_now", lambda: native_time.strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    result = api.verify_write(pending, write_attempted=True)
    assert result.outcome is api.Outcome.CONFIRMED
    assert result.evidence["frame"]["targeted"]["history_status"] == "complete"


def test_genuinely_future_native_timestamp_stays_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: Any
) -> None:
    monkeypatch.setenv("OPENADAPT_VERIFIER_STATE_DIR", str(tmp_path / "authority"))
    store, base = server
    cfg = contract(base)
    pending = api.begin_write(
        cfg, PARAMS, actor=ACTOR, ledger=None, workdir=tmp_path / "runs"
    )
    store.save(
        timestamp=(datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
    )
    result = api.verify_write(pending, write_attempted=True)
    assert result.outcome is api.Outcome.UNCERTAIN and not result.final
