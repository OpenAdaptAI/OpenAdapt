"""Attended state is ciphertext at rest in the default mode.

Contract (docs/phi_at_rest.md, "Durable checkpoints and attended state"):

- With a key resolving (the configured key, else the local key), the pause
  capability, its history, the decision log, program receipts and control
  frames are written as ``<name>.enc`` and the external journal stores each
  snapshot encrypted. No plaintext copy reaches the disk, temporary files
  included.
- A run written before this (plaintext attended files and journal rows) still
  resumes; its first encrypted write converts the plaintext files.
- Each file is bound to its run-relative path: a ciphertext copied over
  another path, or a plaintext file standing in for an encrypted one, is
  refused.

Everything here is synthetic.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from openadapt_flow import at_rest, crypto
from openadapt_flow.runtime.durable import attended_state
from openadapt_flow.runtime.durable.attended import (
    AttendedActionRefused,
    AttendedActionRequest,
    AttendedActionStore,
    BoundAttendedExecutor,
    execute_attended_action,
)
from openadapt_flow.runtime.durable.checkpoint import CheckpointStore
from openadapt_flow.runtime.replayer import Replayer
from tests.test_attended_actions import (
    _attended_program,
    _paused,
    _request,
    _run_attended_program_to_pause,
)
from tests.test_replayer import FakeBackend, FakeVision, Match
from tests.test_sealed_at_rest import watching_disk

OPERATOR = "Synthetic Operator Quillfeather"
PLAINTEXT_NAMES = attended_state.CONVERTED_FILES


@pytest.fixture
def default_mode(monkeypatch):
    """The default at-rest mode: no opt-out, the local key under tmp."""
    monkeypatch.delenv("OPENADAPT_FLOW_PLAINTEXT_AT_REST", raising=False)
    monkeypatch.delenv("OPENADAPT_BUNDLE_KEY", raising=False)
    at_rest._reset_for_tests()
    yield
    at_rest._reset_for_tests()


def _continue_executor() -> BoundAttendedExecutor:
    def factory(_manifest):
        vision = FakeVision()
        vision.text_results = {
            "DONE": Match(point=(10, 10), region=(0, 0, 20, 20), confidence=1.0)
        }
        return Replayer(FakeBackend(), vision=vision, poll_interval_s=0.0)

    return BoundAttendedExecutor(factory)


def _journal_rows() -> list[str]:
    db = os.environ["OPENADAPT_DURABLE_AUTHORITY_DB"]
    connection = sqlite3.connect(db)
    try:
        return [
            str(row[0])
            for row in connection.execute(
                "SELECT snapshot_json FROM attended_journal ORDER BY sequence"
            )
        ]
    finally:
        connection.close()


def _journal_bytes() -> bytes:
    db = Path(os.environ["OPENADAPT_DURABLE_AUTHORITY_DB"])
    return b"".join(
        path.read_bytes()
        for path in (db, Path(f"{db}-wal"), Path(f"{db}-journal"))
        if path.is_file()
    )


def _attended_leaks(watch, run: Path, markers: list[bytes]) -> list[str]:
    """Leaks in attended-state files. The run report and step evidence are
    plaintext in the default mode by design (recorded in the ledger), so this
    looks only at the attended store's own files."""
    attended = (
        "attended_",
        ".attended_",
        "private/program-transition-controls",
    )
    found = []
    for leak in watch.leaks(markers):
        path = leak.split(":", 1)[0].split(" (transient)", 1)[0]
        try:
            rel = Path(path).resolve().relative_to(run.resolve()).as_posix()
        except ValueError:
            continue
        name = rel.rsplit("/", 1)[-1]
        if rel.startswith(attended) or name.startswith(attended):
            found.append(leak)
    return found


def test_default_mode_attended_run_leaves_no_plaintext_attended_state(
    tmp_path, default_mode
):
    with watching_disk() as watch:
        _workflow, _bundle, run, _store, capability = _paused(tmp_path)
        decision = execute_attended_action(
            run,
            _request(capability),
            operator=OPERATOR,
            decided_by="human",
            executor=_continue_executor(),
        )
    assert decision.status == "completed"
    markers = [
        OPERATOR.encode(),
        capability.pause_id.encode(),
        capability.signature.split(":", 1)[1].encode(),
    ]
    assert _attended_leaks(watch, run, markers) == []

    for name in PLAINTEXT_NAMES:
        assert not (run / name).exists(), name
    attended_files = [
        path for path in run.iterdir() if path.name.startswith("attended_")
    ]
    assert run / "attended_decisions.json.enc" in attended_files
    for path in attended_files:
        assert path.name.endswith(attended_state.ENC_SUFFIX), path.name
        assert crypto.is_encrypted(path.read_bytes())
    assert (run / attended_state.MARKER_FILENAME).is_file()

    rows = _journal_rows()
    assert rows and all(row.startswith(attended_state.JOURNAL_MARKER) for row in rows)
    journal = _journal_bytes()
    assert all(marker not in journal for marker in markers)

    # The store and a fresh reader still see the decision.
    log = AttendedActionStore(run)._read_log()
    assert {item.operator for item in log.decisions} == {OPERATOR}


def test_default_mode_program_continue_encrypts_receipts_and_controls(
    tmp_path, default_mode
):
    workflow = _attended_program()
    _bundle, run, _backend, _store, capability = _run_attended_program_to_pause(
        tmp_path, workflow
    )
    vision = FakeVision()
    vision.text_results["DONE"] = Match(
        point=(10, 10), region=(0, 0, 20, 20), confidence=1.0
    )
    with watching_disk() as watch:
        decision = execute_attended_action(
            run,
            _request(capability, key="program-continue-at-rest"),
            operator=OPERATOR,
            executor=BoundAttendedExecutor(
                lambda _manifest: Replayer(
                    FakeBackend(), vision=vision, poll_interval_s=0.0
                )
            ),
        )
    assert decision.status == "completed"
    # The pause id names the receipt file (and so its associated data); it
    # is a random identifier, not recorded data.
    markers = [OPERATOR.encode(), capability.signature.split(":", 1)[1].encode()]
    assert _attended_leaks(watch, run, markers) == []

    for dirname in (
        ".attended_program_receipts",
        "private/program-transition-controls",
    ):
        names = sorted(path.name for path in (run / dirname).iterdir())
        assert names and all(
            name.endswith(".json" + attended_state.ENC_SUFFIX) for name in names
        ), names
    checkpoint = CheckpointStore(
        run, key=crypto.resolve_key(None)
    ).program_checkpoints()[0]
    assert AttendedActionStore(run).read_program_receipt(capability.pause_id) == (
        checkpoint.attended_transition
    )


def test_older_plaintext_attended_run_still_resumes(tmp_path, monkeypatch):
    # Written under the recorded opt-out: plaintext files and journal rows,
    # as every run was before attended state was encrypted.
    _workflow, _bundle, run, _store, capability = _paused(tmp_path)
    escalated = execute_attended_action(
        run,
        AttendedActionRequest(
            capability_digest=capability.digest,
            idempotency_key="older-run-escalate-0001",
            action="escalate",
            disposition="needs_assistance",
        ),
        operator="earlier operator",
    )
    assert escalated.status == "escalated"
    assert (run / "attended_capability.json").is_file()
    assert (run / "attended_decisions.json").is_file()
    assert not any(
        row.startswith(attended_state.JOURNAL_MARKER) for row in _journal_rows()
    )

    monkeypatch.delenv("OPENADAPT_FLOW_PLAINTEXT_AT_REST")
    at_rest._reset_for_tests()
    decision = execute_attended_action(
        run,
        _request(capability, key="older-run-continue-0001"),
        operator=OPERATOR,
        executor=_continue_executor(),
    )
    assert decision.status == "completed"
    for name in PLAINTEXT_NAMES:
        assert not (run / name).exists(), name
    assert (run / "attended_decisions.json.enc").is_file()
    rows = _journal_rows()
    assert not rows[0].startswith(attended_state.JOURNAL_MARKER)
    assert rows[-1].startswith(attended_state.JOURNAL_MARKER)
    log = AttendedActionStore(run)._read_log()
    operators = [item.operator for item in log.decisions]
    assert operators[0] == "earlier operator" and operators[-1] == OPERATOR


def test_encrypted_file_moved_to_another_path_or_replaced_is_refused(
    tmp_path, default_mode
):
    _workflow, _bundle, run, _store, capability = _paused(tmp_path)
    execute_attended_action(
        run,
        AttendedActionRequest(
            capability_digest=capability.digest,
            idempotency_key="swap-test-escalate-0001",
            action="escalate",
            disposition="needs_assistance",
        ),
        operator=OPERATOR,
    )
    store = AttendedActionStore(run)
    assert store.read() == capability
    capability_enc = run / "attended_capability.json.enc"
    decisions_enc = run / "attended_decisions.json.enc"
    original = capability_enc.read_bytes()

    # A valid ciphertext of another attended file does not open here.
    shutil.copyfile(decisions_enc, capability_enc)
    with pytest.raises(AttendedActionRefused):
        AttendedActionStore(run).read()

    # A plaintext file standing in for the encrypted one is refused.
    capability_enc.unlink()
    (run / "attended_capability.json").write_text(capability.model_dump_json(indent=2))
    with pytest.raises(AttendedActionRefused):
        AttendedActionStore(run).read()

    capability_enc.write_bytes(original)
    (run / "attended_capability.json").unlink()
    assert AttendedActionStore(run).read() == capability
