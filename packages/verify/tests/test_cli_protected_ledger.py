"""The installed Verify CLI uses an explicitly enrolled protected journal."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from importlib.metadata import distribution
from pathlib import Path

from openadapt_flow.write_ledger import FileLedger, Ledger, SQLiteJournalAuthority
from test_cli_verdicts import _files
from test_verification import VALUES

_AUTHORITY_ENV = "OPENADAPT_LEDGER_AUTHORITY_CONFIG"


def _configure_authority(tmp_path, monkeypatch, *, enroll):
    journal = tmp_path / "protected-journal"
    operator = tmp_path / "operator"
    operator.mkdir(mode=0o700)
    path = operator / "journal-authority.sqlite"
    identity = {"ledger_id": "synthetic-protected-ledger", "generation": "1"}
    authority = SQLiteJournalAuthority(path, root=journal, **identity)
    if enroll:
        FileLedger(journal).enroll(authority, authorized=True)
    descriptor = operator / "authority.json"
    config = {"journal_root": str(journal), "authority_path": str(path), **identity}
    descriptor.write_text(json.dumps(config), encoding="utf-8")
    descriptor.chmod(0o600)
    monkeypatch.setenv("OPENADAPT_LEDGER_DIR", str(journal))
    monkeypatch.setenv(_AUTHORITY_ENV, str(descriptor))
    return authority


def _installed_verify():
    entrypoints = [
        entry.value
        for entry in distribution("openadapt-verify").entry_points
        if entry.group == "console_scripts" and entry.name == "openadapt-verify"
    ]
    assert entrypoints == ["openadapt_verify.cli:main"]
    path = shutil.which("openadapt-verify", path=str(Path(sys.executable).parent))
    assert path is not None, "the tested environment must install openadapt-verify"
    return Path(path).resolve()


def _child_env(tmp_path):
    # Forward only test-owned verifier paths and harmless process settings.
    settings = (
        "OPENADAPT_LEDGER_DIR",
        _AUTHORITY_ENV,
        "OPENADAPT_SEAL_HOME",
        "OPENADAPT_RECEIPT_HOME",
        "OPENADAPT_VERIFIER_STATE_DIR",
        "OPENADAPT_FRAME_SECRET",
        "OPENADAPT_FLOW_AT_REST_HOME",
        "OPENADAPT_DURABLE_AUTHORITY_DB",
        "OPENADAPT_LOCAL_SEAL",
        "OPENAI_AGENTS_DISABLE_TRACING",
    )
    inherited = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR")
    env = {name: os.environ[name] for name in settings}
    env.update({name: os.environ[name] for name in inherited if name in os.environ})
    home = tmp_path / "verify-child-home"
    home.mkdir(mode=0o700, exist_ok=True)
    return dict(
        env,
        HOME=str(home),
        USERPROFILE=str(home),
        PYTHONNOUSERSITE="1",
        PYTHONDONTWRITEBYTECODE="1",
        PYTHON_DOTENV_DISABLED="1",
    )


def _cli(env, *args):
    return subprocess.run(
        [str(_installed_verify()), *map(str, args)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def _protected_attempt(authority, write_id):
    backend = FileLedger(authority.root, authority=authority)
    found = Ledger(backend).find_attempt(write_id)
    assert found is not None
    key, attempt = found
    snapshot = authority.snapshot()
    assert set(snapshot.heads) == {key} and not snapshot.legacy_keys
    assert snapshot.heads[key].revision == attempt.revision
    assert attempt.attempt_id == write_id
    return attempt


def _assert_no_new_evidence(authority, state):
    assert not authority.path.exists()
    assert not list((authority.root / "entries").glob("*.jsonl"))
    assert not list((authority.root / "attempts").glob("*"))
    assert not list(state.glob("**/*"))


def test_installed_cli_rechecks_the_same_protected_attempt_after_restart(
    store, tmp_path, monkeypatch
):
    authority = _configure_authority(tmp_path, monkeypatch, enroll=True)
    env = _child_env(tmp_path)
    contract, params = _files(store, tmp_path, 0.01)
    writer = (
        "import sqlite3,sys\n"
        "with sqlite3.connect(sys.argv[1]) as db:\n"
        "    db.execute('INSERT INTO item(code, quantity) VALUES (?, ?)', sys.argv[2:])"
    )
    started = _cli(
        env,
        "run",
        "--contract",
        contract,
        "--params",
        params,
        "--state",
        tmp_path / "writes",
        "--",
        sys.executable,
        "-c",
        writer,
        store.path,
        VALUES["code"],
        VALUES["quantity"],
    )
    assert started.returncode == 5, started.stderr
    first = json.loads(started.stdout)
    assert first["outcome"] == "CONFIRMED" and first["provisional"] is True
    assert first["command_returncode"] == 0 and first["write_receipt"] == "issued"
    assert Path(first["write_receipt_path"]).is_file()
    pending = _protected_attempt(authority, first["write_id"])
    assert pending.revision == 2 and pending.recheck_owed == first["recheck_due_at"]
    assert pending.resolved is not None
    assert pending.resolved["receipt_sha256"] == first["receipt_sha256"]

    due = datetime.fromisoformat(first["recheck_due_at"].replace("Z", "+00:00"))
    delay = (due - datetime.now(timezone.utc)).total_seconds()
    assert delay < 2
    if delay > 0:
        time.sleep(delay + 0.01)
    restarted = _cli(
        env,
        "recheck",
        "--contract",
        contract,
        "--params",
        params,
        "--recheck-state",
        first["recheck_state_path"],
    )
    assert restarted.returncode == 0, restarted.stderr
    final = json.loads(restarted.stdout)
    assert final["verified"] is True and final["ledger_outcome"] == "CONFIRMED"
    assert final["receipt_sha256"] == first["receipt_sha256"]
    attempt = _protected_attempt(authority, first["write_id"])
    assert attempt.revision == pending.revision + 1
    assert attempt.rechecked is not None and not attempt.recheck_owed
    assert attempt.rechecked["receipt_sha256"] == first["receipt_sha256"]
    assert store.records() == [{"id": 1, **VALUES}]


def test_installed_cli_missing_authority_holds_before_writer(
    store, tmp_path, monkeypatch
):
    authority = _configure_authority(tmp_path, monkeypatch, enroll=False)
    env = _child_env(tmp_path)
    contract, params = _files(store, tmp_path)
    state = tmp_path / "writes"
    marker = tmp_path / "writer-started"
    refused = _cli(
        env,
        "run",
        "--contract",
        contract,
        "--params",
        params,
        "--state",
        state,
        "--",
        sys.executable,
        "-c",
        "from pathlib import Path; import sys; Path(sys.argv[1]).touch()",
        marker,
    )
    assert refused.returncode == 3, refused.stderr
    assert json.loads(refused.stdout) == {
        "hold_reason": "ledger_authority_absent",
        "outcome": "UNCERTAIN",
        "verified": False,
        "written": False,
    }
    assert not marker.exists()
    assert store.posts == 0 and store.records() == []
    _assert_no_new_evidence(authority, state)
