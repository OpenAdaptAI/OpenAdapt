"""An issued recheck's refutation survives cleanup, overrides and restarts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from openadapt_flow import verifier
from openadapt_flow.verifier import Contract, RecheckState, begin_write, verify_write
from openadapt_flow.write_ledger import Ledger, MemoryLedger
from tests.test_verifier_recheck import (
    BOT,
    _doc,
    _first,
    _params,
    _recheck,
    _save_sql,
    _sql,
)


@pytest.fixture(autouse=True)
def issued_authority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "issued-authority"
    monkeypatch.setattr(verifier, "USED_DIR", root)
    monkeypatch.setenv("OPENADAPT_VERIFIER_STATE_DIR", str(root))


@pytest.fixture
def due_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verifier, "seconds_until", lambda due: 0.0)


def _ledger(mode: str) -> dict[str, Any]:
    if mode == "none":
        return {"ledger": None}
    if mode == "alternate":
        return {"ledger": Ledger(MemoryLedger())}
    return {}


def _attempt(result: verifier.WriteResult) -> Any:
    assert result.ledger is not None
    found = result.ledger.find_attempt(result.write_id)
    assert found is not None
    return found[1]


@pytest.mark.parametrize("state_kind", ["result", "saved"])
@pytest.mark.parametrize("ledger_mode", ["default", "none", "alternate"])
def test_delete_after_refutation_cannot_confirm_with_any_ledger_override(
    tmp_path: Path, due_now: None, state_kind: str, ledger_mode: str
) -> None:
    contract, db, result = _first(tmp_path)
    assert result.recheck is not None
    write = (
        result
        if state_kind == "result"
        else RecheckState.load(
            result.recheck.save(tmp_path / "copied.json"), params=_params(2)
        )
    )
    _sql(db, "INSERT INTO calendar (pid, title) VALUES (2, 'unexplained')")
    assert _recheck(contract, write, **_ledger(ledger_mode)).outcome == "REFUTED"
    _sql(db, "DELETE FROM calendar WHERE title = 'unexplained'")
    # Neither changing authority options nor retry-until-zero clears it.
    for override in ("default", "none", "alternate"):
        late = _recheck(contract, write, **_ledger(override))
        assert late.outcome == "REFUTED", (override, late.reasons)
        assert not late.verified
    assert _attempt(result).resolved["outcome"] == "REFUTED"
    assert _attempt(result).state == "held"


@pytest.mark.parametrize("ledger_mode", ["default", "none", "alternate"])
def test_saved_state_refutation_survives_a_fresh_process(
    tmp_path: Path, due_now: None, ledger_mode: str
) -> None:
    contract, db, result = _first(tmp_path, recheck_after_s=0.01)
    assert result.recheck is not None
    state = result.recheck.save(tmp_path / "copied.json")
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(json.dumps(_doc(db, recheck_after_s=0.01)))
    _sql(db, "INSERT INTO calendar (pid, title) VALUES (2, 'unexplained')")
    assert (
        _recheck(contract, result.recheck, **_ledger(ledger_mode)).outcome == "REFUTED"
    )
    _sql(db, "DELETE FROM calendar WHERE title = 'unexplained'")
    code = """
import json, sys
from openadapt_flow.verifier import Actor, Contract, RecheckState, recheck
from openadapt_flow.write_ledger import Ledger, MemoryLedger
params = json.loads(sys.argv[3])
options = {} if sys.argv[4] == 'default' else {'ledger': None}
if sys.argv[4] == 'alternate':
    options = {'ledger': Ledger(MemoryLedger())}
late = recheck(Contract.load(sys.argv[1]), RecheckState.load(sys.argv[2], params=params),
               params=params, actor=Actor('agent', 'bot'), wait=True, **options)
print(json.dumps({'outcome': late.outcome, 'verified': late.verified}))
"""
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(contract_path),
            str(state),
            json.dumps(_params(2)),
            ledger_mode,
        ],
        capture_output=True,
        text=True,
        env=dict(os.environ),
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"outcome": "REFUTED", "verified": False}


def test_unreadable_then_restored_store_can_confirm_the_same_bound_attempt(
    tmp_path: Path, due_now: None
) -> None:
    contract, db, result = _first(tmp_path)
    preserved = db.with_suffix(".preserved")
    db.rename(preserved)
    assert _recheck(contract, result).outcome == "UNCERTAIN"
    assert _attempt(result).state == "uncertain"
    preserved.rename(db)
    late = _recheck(contract, result)
    assert late.outcome == "CONFIRMED", late.reasons
    attempt = _attempt(result)
    assert attempt.resolved["outcome"] == "CONFIRMED"
    assert attempt.resolved["receipt_sha256"] == result.receipt_sha256
    assert attempt.rechecked["receipt_sha256"] == result.receipt_sha256
    assert not attempt.recheck_owed and not attempt.review


def test_untrusted_call_does_not_make_a_state_terminal(
    tmp_path: Path, due_now: None
) -> None:
    contract, _, result = _first(tmp_path)
    assert result.recheck is not None
    invalid = verifier.recheck(
        contract,
        result.recheck,
        params=_params(2),
        actor=verifier.Actor("agent", "other"),
    )
    assert invalid.outcome == "UNCERTAIN"
    assert _recheck(contract, result.recheck).outcome == "CONFIRMED"


@pytest.mark.parametrize("error", [PermissionError, OSError])
def test_unreadable_terminal_authority_cannot_confirm(
    tmp_path: Path, due_now: None, monkeypatch: pytest.MonkeyPatch, error: type[OSError]
) -> None:
    contract, _, result = _first(tmp_path)

    def unreadable(payload: Any) -> bool:
        raise error("synthetic authority failure")

    monkeypatch.setattr(verifier, "_recheck_refuted", unreadable)
    late = _recheck(contract, result)
    assert late.outcome == "UNCERTAIN" and not late.verified
    assert _attempt(result).resolved["outcome"] == "UNCERTAIN"


def test_busy_issued_state_cannot_confirm(tmp_path: Path, due_now: None) -> None:
    import fcntl

    contract, _, result = _first(tmp_path)
    assert result.recheck is not None
    issued = verifier._issued_dir("rechecks") / verifier._state_digest(
        result.recheck.payload
    )
    with issued.open("rb") as locked:
        fcntl.flock(locked.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        late = _recheck(contract, result)
        assert late.outcome == "UNCERTAIN" and not late.verified


@pytest.mark.parametrize("state_kind", ["result", "saved"])
def test_existing_refuted_ledger_history_cannot_be_promoted(
    tmp_path: Path, due_now: None, state_kind: str
) -> None:
    contract, _, result = _first(tmp_path)
    assert result.ledger is not None and result.recheck is not None
    # A pre-fix ledger has no terminal sidecar, but its exact attempt still holds.
    result.ledger.resolved(
        result.write_key,
        result.write_id,
        outcome="REFUTED",
        review=True,
        retry_safe=False,
        delivered=True,
        via="recheck",
        receipt_sha256=result.receipt_sha256,
    )
    write = result if state_kind == "result" else result.recheck
    assert _recheck(contract, write).outcome == "REFUTED"
    assert _attempt(result).state == "held"


def test_recorded_person_review_allows_only_a_new_attempt(
    tmp_path: Path, due_now: None
) -> None:
    db = tmp_path / "person"
    db.mkdir()
    from test_verifier_recheck import _keyed

    contract, path, result = _keyed(db, "reviewed-order")
    assert result.ledger is not None
    _sql(path, "INSERT INTO calendar (pid, title) VALUES (2, 'unexplained')")
    assert _recheck(contract, result).outcome == "REFUTED"
    # The synthetic reviewer checks absence after explicit fixture cleanup.
    _sql(
        path,
        "DELETE FROM calendar",
        "DELETE FROM history_data WHERE pid=2",
        "DELETE FROM patient_data WHERE pid=2",
    )
    result.ledger.release(
        result.write_key,
        by="synthetic-reviewer",
        reason="checked owned fixture: target absent",
        found="absent",
    )
    assert _attempt(result).released
    assert _recheck(contract, result).outcome == "REFUTED"
    assert _attempt(result).released  # rechecking does not undo recorded review
    pending = begin_write(contract, _params(2), actor=BOT, write_key="reviewed-order")
    assert pending.write_id != result.write_id
    _sql(path, *_save_sql(2))
    new = verify_write(pending)
    assert new.verified and _recheck(contract, new).outcome == "CONFIRMED"
    assert _recheck(contract, result).outcome == "REFUTED"
    assert _attempt(new).resolved["outcome"] == "CONFIRMED"


def test_confirming_no_ledger_capture_remains_supported(
    tmp_path: Path, due_now: None
) -> None:
    from test_verifier_recheck import _db

    path = _db(tmp_path)
    contract = Contract.from_dict(_doc(path))
    with pytest.warns(DeprecationWarning):
        before = verifier.capture(contract, _params(2))
    _sql(path, *_save_sql(2))
    result = verify_write(contract, before, params=_params(2), actor=BOT)
    assert result.ledger is None
    assert _recheck(contract, result, ledger=None).outcome == "CONFIRMED"
