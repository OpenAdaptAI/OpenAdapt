"""Actual CLI processes join native readback to the exact ledger settlement."""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from test_cli_verdicts import _cli, _files, _run


def _provisional(store, tmp_path):
    contract, params = _files(store, tmp_path, 0.01)
    started = _run(store, tmp_path, contract, params)
    assert started.returncode == 5, started.stderr
    first = json.loads(started.stdout)
    assert first["provisional"] is True
    due = datetime.fromisoformat(first["recheck_due_at"].replace("Z", "+00:00"))
    time.sleep(max(0.0, (due - datetime.now(timezone.utc)).total_seconds()) + 0.02)
    return contract, params, Path(first["recheck_state_path"]), first


def _check(contract, params, state):
    return _cli(
        "recheck", "--contract", contract, "--params", params, "--recheck-state", state
    )


def _attempt(first):
    from openadapt_flow.write_ledger import default_ledger

    found = default_ledger().find_attempt(first["write_id"])
    assert found is not None
    return found[1]


@pytest.mark.parametrize("unavailable_after_refute", [False, True])
def test_cli_refute_restore_retry_never_exits_zero(
    store, tmp_path, unavailable_after_refute
):
    contract, params, state, first = _provisional(store, tmp_path)
    with sqlite3.connect(store.path) as db:
        db.execute("INSERT INTO item(code,quantity) VALUES ('unexplained','9')")
    refuted = _check(contract, params, state)
    assert refuted.returncode == 4, refuted.stderr
    assert _attempt(first).state == "held"
    with sqlite3.connect(store.path) as db:
        db.execute("DELETE FROM item WHERE code='unexplained'")
    if unavailable_after_refute:
        preserved = store.path.with_suffix(".preserved")
        store.path.rename(preserved)
        unreadable = _check(contract, params, state)
        assert unreadable.returncode == 4, unreadable.stderr
        assert json.loads(unreadable.stdout)["verified"] is False
        preserved.rename(store.path)
    for _ in range(2):
        retry = _check(contract, params, state)
        reply = json.loads(retry.stdout)
        assert retry.returncode == 4, retry.stderr
        assert reply["verified"] is False
        assert reply["ledger_state"] == "held"
        assert reply["ledger_outcome"] == "REFUTED"
        assert reply["receipt_sha256"] == first["receipt_sha256"]
        assert _attempt(first).resolved["outcome"] == "REFUTED"
    assert store.posts == 1 and len(store.records()) == 1


def test_cli_unreadable_then_restored_confirms_the_exact_attempt(store, tmp_path):
    contract, params, state, first = _provisional(store, tmp_path)
    preserved = store.path.with_suffix(".preserved")
    store.path.rename(preserved)
    uncertain = _check(contract, params, state)
    assert uncertain.returncode == 3, uncertain.stderr
    assert json.loads(uncertain.stdout)["verified"] is False
    preserved.rename(store.path)
    confirmed = _check(contract, params, state)
    reply = json.loads(confirmed.stdout)
    assert confirmed.returncode == 0, confirmed.stderr
    assert reply["verified"] is True and reply["ledger_outcome"] == "CONFIRMED"
    assert reply["receipt_sha256"] == first["receipt_sha256"]
    attempt = _attempt(first)
    assert attempt.rechecked["receipt_sha256"] == first["receipt_sha256"]
    assert not attempt.review and not attempt.recheck_owed
    assert store.posts == 1 and len(store.records()) == 1
