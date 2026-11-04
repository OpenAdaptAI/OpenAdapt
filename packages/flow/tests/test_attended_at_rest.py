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
    assert rows and all(
        row.startswith(attended_state.JOURNAL_MARKER_V2) for row in rows
    )
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
        attended_state.is_sealed_journal_snapshot(row) for row in _journal_rows()
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
    assert not attended_state.is_sealed_journal_snapshot(rows[0])
    assert rows[-1].startswith(attended_state.JOURNAL_MARKER_V2)
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


# -- one key derivation per run, and no downgrade (real filesystem) ----------

SYNTHETIC_KEY = "synthetic-attended-test-passphrase"


def _write(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


@pytest.fixture
def counted_scrypt(monkeypatch):
    """Count every scrypt run, with the per-run key cache cleared."""
    attended_state._RUN_KEYS.clear()
    attended_state._JOURNAL_SALTS.clear()
    attended_state._CACHE.clear()
    calls: list[int] = []
    real = crypto._derive

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(crypto, "_derive", counting)
    yield calls
    attended_state._RUN_KEYS.clear()
    attended_state._JOURNAL_SALTS.clear()
    attended_state._CACHE.clear()


def test_scrypt_runs_once_per_run_not_per_file_or_row(tmp_path, counted_scrypt):
    root = tmp_path / "run"
    receipts = root / ".attended_program_receipts"
    receipts.mkdir(parents=True)
    paths = [receipts / f"pause-{index}.json" for index in range(30)]
    for index, path in enumerate(paths):
        attended_state.write_bytes(
            root, path, f'{{"n":{index}}}'.encode(), SYNTHETIC_KEY, _write
        )
    rows = [
        attended_state.seal_journal_snapshot(
            f'{{"seq":{seq}}}',
            SYNTHETIC_KEY,
            path_key="synthetic-run",
            sequence=seq,
            run_dir=root,
        )
        for seq in range(30)
    ]
    for _ in range(3):
        for index, path in enumerate(paths):
            assert attended_state.read_bytes(root, path, SYNTHETIC_KEY) == (
                f'{{"n":{index}}}'.encode()
            )
            assert attended_state.digest_bytes(root, path, SYNTHETIC_KEY) == (
                f'{{"n":{index}}}'.encode()
            )
        for seq, row in enumerate(rows):
            assert row.startswith(attended_state.JOURNAL_MARKER_V2)
            assert (
                attended_state.open_journal_snapshot(
                    row, SYNTHETIC_KEY, path_key="synthetic-run", sequence=seq
                )
                == f'{{"seq":{seq}}}'
            )
    assert len(counted_scrypt) == 1
    for path in paths:
        container = attended_state.enc_path(path).read_bytes()
        assert container.startswith(crypto.SALTED_FILE_MAGIC)
        assert crypto.is_encrypted(container)
        assert not path.exists()


def test_salted_file_is_bound_to_its_path(tmp_path, counted_scrypt):
    root = tmp_path / "run"
    receipts = root / ".attended_program_receipts"
    receipts.mkdir(parents=True)
    first, second = receipts / "a.json", receipts / "b.json"
    attended_state.write_bytes(root, first, b'{"a":1}', SYNTHETIC_KEY, _write)
    attended_state.write_bytes(root, second, b'{"b":2}', SYNTHETIC_KEY, _write)
    shutil.copyfile(attended_state.enc_path(first), attended_state.enc_path(second))
    with pytest.raises(attended_state.AttendedStateUnreadable):
        attended_state.read_bytes(root, second, SYNTHETIC_KEY)
    with pytest.raises(attended_state.AttendedStateUnreadable):
        attended_state.read_bytes(root, first, "a-different-synthetic-key")


def test_salted_journal_row_is_bound_to_path_and_sequence(counted_scrypt):
    row = attended_state.seal_journal_snapshot(
        '{"seq":1}', SYNTHETIC_KEY, path_key="synthetic-run", sequence=1
    )
    with pytest.raises(attended_state.AttendedStateUnreadable):
        attended_state.open_journal_snapshot(
            row, SYNTHETIC_KEY, path_key="synthetic-run", sequence=2
        )
    with pytest.raises(attended_state.AttendedStateUnreadable):
        attended_state.open_journal_snapshot(
            row, SYNTHETIC_KEY, path_key="other-run", sequence=1
        )
    with pytest.raises(attended_state.AttendedStateUnreadable):
        attended_state.open_journal_snapshot(
            row, None, path_key="synthetic-run", sequence=1
        )


def test_opt_out_write_does_not_downgrade_an_encrypted_run(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    path = root / "attended_decisions.json"
    attended_state.write_bytes(root, path, b'{"v":1}', SYNTHETIC_KEY, _write)
    marker = root / attended_state.MARKER_FILENAME
    assert marker.is_file()
    with pytest.raises(attended_state.AttendedStateUnreadable, match="encrypted"):
        attended_state.write_bytes(root, path, b'{"v":2}', None, _write)
    other = root / "attended_capability.json"
    with pytest.raises(attended_state.AttendedStateUnreadable, match="encrypted"):
        attended_state.write_bytes(root, other, b'{"v":2}', None, _write)
    assert marker.is_file()
    assert attended_state.enc_path(path).is_file()
    assert not path.exists() and not other.exists()
    assert attended_state.read_bytes(root, path, SYNTHETIC_KEY) == b'{"v":1}'


def test_plaintext_stand_in_is_refused_by_read_and_digest(tmp_path):
    root = tmp_path / "run"
    receipts = root / ".attended_program_receipts"
    receipts.mkdir(parents=True)
    path = receipts / "pause.json"
    attended_state.write_bytes(root, path, b'{"v":1}', SYNTHETIC_KEY, _write)
    attended_state.enc_path(path).unlink()
    path.write_bytes(b'{"v":"forged"}')
    with pytest.raises(attended_state.AttendedStateUnreadable):
        attended_state.read_bytes(root, path, SYNTHETIC_KEY)
    with pytest.raises(attended_state.AttendedStateUnreadable):
        attended_state.digest_bytes(root, path, SYNTHETIC_KEY)
    # A file the run never encrypts (the run claim) is still read as is.
    claim = root / ".durable_run.claim"
    claim.write_bytes(b"synthetic-claim")
    assert attended_state.digest_bytes(root, claim, SYNTHETIC_KEY) == (
        b"synthetic-claim"
    )


def test_older_containers_rows_and_marker_still_read(tmp_path, counted_scrypt):
    """A run encrypted by the older per-container format still reads, and its
    next write moves the marker to the salted format without rewriting the
    older files."""
    root = tmp_path / "run"
    receipts = root / ".attended_program_receipts"
    receipts.mkdir(parents=True)
    old = receipts / "old.json"
    attended_state.enc_path(old).write_bytes(
        attended_state.seal(
            b'{"old":true}', SYNTHETIC_KEY, attended_state.file_aad(root, old)
        )
    )
    marker = root / attended_state.MARKER_FILENAME
    marker.write_bytes(attended_state.MARKER_PAYLOAD)
    old_row = attended_state.JOURNAL_MARKER + attended_state.seal(
        b'{"seq":0}',
        SYNTHETIC_KEY,
        attended_state._journal_aad("synthetic-run", 0),
    ).decode("utf-8")

    assert attended_state.read_bytes(root, old, SYNTHETIC_KEY) == b'{"old":true}'
    assert attended_state.is_sealed_journal_snapshot(old_row)
    assert (
        attended_state.open_journal_snapshot(
            old_row, SYNTHETIC_KEY, path_key="synthetic-run", sequence=0
        )
        == '{"seq":0}'
    )

    new = receipts / "new.json"
    attended_state.write_bytes(root, new, b'{"new":true}', SYNTHETIC_KEY, _write)
    present, salt = attended_state._read_marker(root)
    assert present and salt is not None
    assert (
        attended_state.enc_path(new).read_bytes().startswith(crypto.SALTED_FILE_MAGIC)
    )
    assert (
        not attended_state.enc_path(old)
        .read_bytes()
        .startswith(crypto.SALTED_FILE_MAGIC)
    )
    assert attended_state.read_bytes(root, old, SYNTHETIC_KEY) == b'{"old":true}'
    assert attended_state.read_bytes(root, new, SYNTHETIC_KEY) == b'{"new":true}'


def test_older_plaintext_run_converts_under_one_salt(tmp_path, counted_scrypt):
    root = tmp_path / "run"
    receipts = root / ".attended_program_receipts"
    receipts.mkdir(parents=True)
    (root / "attended_decisions.json").write_bytes(b'{"decisions":[]}')
    for index in range(10):
        (receipts / f"p{index}.json").write_bytes(f'{{"p":{index}}}'.encode())
    attended_state.write_bytes(
        root, root / "attended_capability.json", b"{}", SYNTHETIC_KEY, _write
    )
    assert not (root / "attended_decisions.json").exists()
    for index in range(10):
        path = receipts / f"p{index}.json"
        assert not path.exists()
        assert attended_state.read_bytes(root, path, SYNTHETIC_KEY) == (
            f'{{"p":{index}}}'.encode()
        )
    assert len(counted_scrypt) == 1


def test_salted_file_from_another_run_is_refused(tmp_path, counted_scrypt):
    """Same key and relative path, but another run's salt: refused."""
    run_a, run_b = tmp_path / "a", tmp_path / "b"
    name = ".attended_program_receipts/r.json"
    for root, body in ((run_a, b'{"run":"a"}'), (run_b, b'{"run":"b"}')):
        (root / ".attended_program_receipts").mkdir(parents=True)
        attended_state.write_bytes(root, root / name, body, SYNTHETIC_KEY, _write)
    shutil.copyfile(
        attended_state.enc_path(run_a / name), attended_state.enc_path(run_b / name)
    )
    with pytest.raises(attended_state.AttendedStateUnreadable, match="this run"):
        attended_state.read_bytes(run_b, run_b / name, SYNTHETIC_KEY)
    with pytest.raises(attended_state.AttendedStateUnreadable, match="this run"):
        attended_state.digest_bytes(run_b, run_b / name, SYNTHETIC_KEY)
    assert attended_state.read_bytes(run_a, run_a / name, SYNTHETIC_KEY) == (
        b'{"run":"a"}'
    )
    # A salted file whose run lost its marker is refused too.
    (run_a / attended_state.MARKER_FILENAME).unlink()
    with pytest.raises(attended_state.AttendedStateUnreadable, match="this run"):
        attended_state.read_bytes(run_a, run_a / name, SYNTHETIC_KEY)


def test_conversion_that_loses_the_marker_race_reseals_under_the_run_salt(
    tmp_path, counted_scrypt, monkeypatch
):
    root = tmp_path / "run"
    receipts = root / ".attended_program_receipts"
    receipts.mkdir(parents=True)
    old = receipts / "old.json"
    old.write_bytes(b'{"older":"plaintext"}')
    winner_salt = crypto.SetKey.new_salt()
    original = attended_state._link_new
    marker = root / attended_state.MARKER_FILENAME

    def link_new(path, data):
        # Another writer creates the marker just before this one does.
        if path == marker and not marker.exists():
            original(path, attended_state._marker_payload(winner_salt))
        original(path, data)

    monkeypatch.setattr(attended_state, "_link_new", link_new)
    attended_state.write_bytes(
        root, receipts / "new.json", b'{"new":1}', SYNTHETIC_KEY, _write
    )
    assert attended_state._read_marker(root) == (True, winner_salt)
    assert attended_state.read_bytes(root, old, SYNTHETIC_KEY) == (
        b'{"older":"plaintext"}'
    )
    assert attended_state.read_bytes(root, receipts / "new.json", SYNTHETIC_KEY) == (
        b'{"new":1}'
    )


def test_replayer_classifies_with_its_own_checkpoint_key(monkeypatch, tmp_path):
    """The outcome reader opens attended evidence with the key the run sealed
    it under, not only the environment's key."""
    from types import SimpleNamespace

    from openadapt_flow import execution_profiles

    seen: dict = {}

    def stamp(report, workflow, profile, **kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(execution_profiles, "stamp_execution_outcome", stamp)
    replayer = Replayer(FakeBackend(), vision=FakeVision(), checkpoint_key="k-run")
    replayer._outcome_worklists = None
    replayer._stamp_execution_outcome(
        SimpleNamespace(execution_profile="standard"), object(), tmp_path
    )
    assert seen["transition_evidence_key"] == "k-run"
    assert seen["transition_evidence_root"] == tmp_path
