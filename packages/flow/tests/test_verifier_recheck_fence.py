"""Pre-read durability and recovery through real independent SQLite reads."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from openadapt_flow import verifier
from openadapt_flow.verifier import Contract, RecheckNotDue, RecheckState
from tests.test_verifier_recheck import (
    BOT,
    _doc,
    _first,
    _params,
    _recheck,
    _save_sql,
    _sql,
)
from tests.test_verifier_recheck_sticky import _attempt, _ledger


@pytest.fixture(autouse=True)
def issued_authority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "issued-authority"
    monkeypatch.setattr(verifier, "USED_DIR", root)
    monkeypatch.setenv("OPENADAPT_VERIFIER_STATE_DIR", str(root))


@pytest.fixture
def due_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verifier, "seconds_until", lambda due: 0.0)


def _files(tmp_path: Path) -> tuple[Contract, Path, Any, Path, Path, Path]:
    contract, db, result = _first(tmp_path, recheck_after_s=0.01)
    assert result.recheck is not None
    state = result.recheck.save(tmp_path / "copied.json")
    config = tmp_path / "contract.json"
    config.write_text(json.dumps(_doc(db, recheck_after_s=0.01)))
    authority = verifier._issued_dir("rechecks")
    return contract, db, result, state, config, authority


def _native_spy(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    original = sqlite3.connect
    calls: list[bool] = []

    def connect(*args: Any, **kwargs: Any) -> Any:
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    return calls


def _child(config: Path, state: Path, mode: str, phase: str = "") -> Any:
    code = """
import json, os, sqlite3, sys
from openadapt_flow import verifier
from openadapt_flow.verifier import Actor, Contract, RecheckState
from openadapt_flow.write_ledger import Ledger, MemoryLedger
params = json.loads(sys.argv[3])
contract, state = Contract.load(sys.argv[1]), RecheckState.load(sys.argv[2], params=params)
options = {} if sys.argv[4] == 'default' else {'ledger': None}
if sys.argv[4] == 'alternate':
    options = {'ledger': Ledger(MemoryLedger())}
calls = []
real_connect, real_read = sqlite3.connect, verifier.recheck_run
def connect(*args, **kwargs):
    calls.append(True)
    return real_connect(*args, **kwargs)
sqlite3.connect = connect
if sys.argv[5]:
    def interrupted(*args, **kwargs):
        if sys.argv[5] == 'after_fence':
            print(json.dumps({'phase': 'after_fence', 'native_connects': len(calls)}), flush=True)
            os._exit(94)
        verdict, read_at = real_read(*args, **kwargs)
        assert verdict.status == 'refuted'
        print(json.dumps({'phase': 'after_refuting_read', 'native_connects': len(calls)}), flush=True)
        os._exit(95)
    verifier.recheck_run = interrupted
late = verifier.recheck(contract, state, params=params, actor=Actor('agent', 'bot'), wait=True, **options)
print(json.dumps({'outcome': late.outcome, 'verified': late.verified, 'native_connects': len(calls)}))
"""
    return subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(config),
            str(state),
            json.dumps(_params(2)),
            mode,
            phase,
        ],
        capture_output=True,
        text=True,
        env=dict(os.environ),
        timeout=30,
    )


def _refusal_after_cleanup(config: Path, state: Path) -> None:
    for mode in ("none", "default", "alternate"):
        completed = _child(config, state, mode)
        assert completed.returncode == 0, completed.stderr
        reply = json.loads(completed.stdout)
        assert reply["outcome"] in ("UNCERTAIN", "REFUTED"), (mode, reply)
        assert not reply["verified"] and reply["native_connects"] == 0


@pytest.mark.parametrize("mode", ["default", "none"])
def test_failed_fence_permission_refuses_before_native_read(
    tmp_path: Path, due_now: None, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    assert os.geteuid() != 0, "permission control requires the normal unprivileged user"
    contract, db, result, _, _, authority = _files(tmp_path)
    _sql(db, "INSERT INTO calendar(pid, title) VALUES (2, 'unexplained')")
    calls = _native_spy(monkeypatch)
    authority.chmod(0o500)
    try:
        late = _recheck(contract, result, **_ledger(mode))
    finally:
        authority.chmod(0o700)
    assert late.outcome == "UNCERTAIN" and not late.verified
    assert calls == []
    # The same spy sees genuine native I/O once the pre-read fence is writable.
    assert _recheck(contract, result, **_ledger(mode)).outcome == "REFUTED"
    assert calls


@pytest.mark.parametrize("mode", ["default", "none"])
def test_failed_terminal_permission_remains_fenced_after_cleanup_and_restart(
    tmp_path: Path, due_now: None, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    assert os.geteuid() != 0, "permission control requires the normal unprivileged user"
    contract, db, result, state, config, authority = _files(tmp_path)
    _sql(db, "INSERT INTO calendar(pid, title) VALUES (2, 'unexplained')")
    real = verifier.recheck_run

    def read(*args: Any, **kwargs: Any) -> Any:
        verdict, at = real(*args, **kwargs)
        assert verdict.status == "refuted"
        authority.chmod(0o500)  # only after real evidence, before terminal persistence
        return verdict, at

    monkeypatch.setattr(verifier, "recheck_run", read)
    try:
        late = _recheck(contract, result, **_ledger(mode))
    finally:
        authority.chmod(0o700)
    assert late.outcome == "REFUTED" and not late.verified
    _sql(db, "DELETE FROM calendar WHERE title='unexplained'")
    # Run the semantic tripwire BEFORE checking implementation-side markers.
    # On v2 this fresh no-ledger process incorrectly reports CONFIRMED.
    _refusal_after_cleanup(config, state)
    assert list(authority.glob("*.inflight"))


@pytest.mark.parametrize(
    "stage", ["inflight_file", "inflight_dir", "refuted_file", "refuted_dir"]
)
def test_fsync_failure_preserves_read_boundary_and_restart_refusal(
    tmp_path: Path, due_now: None, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    contract, db, result, state, config, authority = _files(tmp_path)
    _sql(db, "INSERT INTO calendar(pid, title) VALUES (2, 'unexplained')")
    assert result.recheck is not None
    digest = verifier._state_digest(result.recheck.payload)
    suffix, kind = stage.split("_")
    target = authority / (digest + "." + suffix)
    calls = _native_spy(monkeypatch)
    real = os.fsync
    injected: list[bool] = []

    def sync(fd: int) -> None:
        info = os.fstat(fd)
        path = target if kind == "file" else authority
        if not injected and target.exists():
            expected = path.stat()
            if (info.st_dev, info.st_ino) == (expected.st_dev, expected.st_ino):
                injected.append(True)
                raise OSError("synthetic fsync failure on owned marker")
        real(fd)

    monkeypatch.setattr(os, "fsync", sync)
    late = _recheck(contract, result, ledger=None)
    assert injected == [True]
    if suffix == "inflight":
        assert late.outcome == "UNCERTAIN" and calls == []
    else:
        assert late.outcome == "REFUTED" and calls
    monkeypatch.setattr(os, "fsync", real)
    _sql(db, "DELETE FROM calendar WHERE title='unexplained'")
    _refusal_after_cleanup(config, state)
    assert list(authority.glob("*.inflight"))


@pytest.mark.parametrize("phase,code", [("after_fence", 94), ("after_read", 95)])
def test_process_crash_leaves_old_state_fenced(
    tmp_path: Path, phase: str, code: int, request: pytest.FixtureRequest
) -> None:
    _, db, _, state, config, authority = _files(tmp_path)
    _sql(db, "INSERT INTO calendar(pid, title) VALUES (2, 'unexplained')")
    interrupted = _child(config, state, "none", phase)
    assert interrupted.returncode == code, interrupted.stderr
    progress = json.loads(interrupted.stdout)
    assert (progress["native_connects"] == 0) is (phase == "after_fence")
    request.node.user_properties.append(("actual_child_exit", str(code)))
    request.node.user_properties.append(
        ("native_connects", str(progress["native_connects"]))
    )
    _sql(db, "DELETE FROM calendar WHERE title='unexplained'")
    _refusal_after_cleanup(config, state)
    assert list(authority.glob("*.inflight"))


def test_failed_ledger_completion_does_not_clear_the_fence(
    tmp_path: Path, due_now: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openadapt_flow.write_ledger import LedgerError

    contract, _, result, state, config, authority = _files(tmp_path)
    assert result.ledger is not None
    real = result.ledger.backend.append

    def append(key: str, event: Any) -> None:
        if event.get("event") == "rechecked":
            raise LedgerError("synthetic completion append failure")
        real(key, event)

    monkeypatch.setattr(result.ledger.backend, "append", append)
    late = _recheck(contract, result)
    assert late.outcome == "UNCERTAIN" and not late.verified
    _refusal_after_cleanup(config, state)
    assert list(authority.glob("*.inflight"))


def test_fully_classified_no_ledger_uncertain_can_complete_and_recover(
    tmp_path: Path, due_now: None
) -> None:
    contract, db, result, state, config, authority = _files(tmp_path)
    preserved = db.with_suffix(".preserved")
    db.rename(preserved)
    assert _recheck(contract, result, ledger=None).outcome == "UNCERTAIN"
    assert not list(authority.glob("*.inflight"))
    preserved.rename(db)
    completed = _child(config, state, "none")
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["outcome"] == "CONFIRMED"
    assert not list(authority.glob("*.inflight"))


@pytest.mark.parametrize(
    "fault", ["actor", "params", "contract", "tampered", "unissued", "not_due"]
)
def test_invalid_preflight_creates_no_fence_and_reads_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    contract, _, result, _, _, authority = _files(tmp_path)
    assert result.recheck is not None
    state = RecheckState.load(result.recheck.path, params=_params(2))
    actor, params = BOT, _params(2)
    if fault == "actor":
        actor = verifier.Actor("agent", "other")
    elif fault == "params":
        params = _params(3)
    elif fault == "contract":
        doc = _doc(tmp_path / "different.sqlite", recheck_after_s=0.01)
        contract = Contract.from_dict(doc)
    elif fault == "tampered":
        state.payload["post_state"]["read_at"] = "2001-01-01T00:00:00Z"
    elif fault == "unissued":
        (authority / verifier._state_digest(state.payload)).unlink()
    if fault != "not_due":
        monkeypatch.setattr(verifier, "seconds_until", lambda due: 0.0)
    else:
        monkeypatch.setattr(verifier, "seconds_until", lambda due: 60.0)
    calls = _native_spy(monkeypatch)
    if fault == "not_due":
        with pytest.raises(RecheckNotDue):
            verifier.recheck(contract, state, params=params, actor=actor)
    else:
        late = verifier.recheck(contract, state, params=params, actor=actor)
        assert late.outcome == "UNCERTAIN"
    assert calls == [] and not list(authority.glob("*.inflight"))


def test_person_review_releases_only_a_new_digest_after_incomplete_recheck(
    tmp_path: Path, due_now: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract, db, result, _, _, authority = _files(tmp_path)
    assert result.ledger is not None

    def interrupted(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("synthetic interrupted native operation")

    with monkeypatch.context() as patch:
        patch.setattr(verifier, "recheck_run", interrupted)
        assert _recheck(contract, result).outcome == "UNCERTAIN"
    assert list(authority.glob("*.inflight"))
    _sql(
        db,
        "DELETE FROM history_data WHERE pid=2",
        "DELETE FROM patient_data WHERE pid=2",
    )
    result.ledger.release(
        result.write_key,
        by="synthetic-reviewer",
        reason="owned target confirmed absent",
        found="absent",
    )
    assert _recheck(contract, result).outcome == "UNCERTAIN"
    assert _attempt(result).released
    pending = verifier.begin_write(contract, _params(2), actor=BOT)
    _sql(db, *_save_sql(2))
    new = verifier.verify_write(pending)
    assert new.verified and _recheck(contract, new).outcome == "CONFIRMED"
    assert new.recheck is not None and result.recheck is not None
    assert verifier._state_digest(new.recheck.payload) != verifier._state_digest(
        result.recheck.payload
    )
    assert _recheck(contract, result).outcome == "UNCERTAIN"
