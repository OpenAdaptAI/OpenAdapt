"""The uncertain-write ledger: no write is sent twice while its outcome is unknown.

Each case runs against MockMed's fault server, a real HTTP app with its own
record store. Workers are separate processes that share one ledger folder,
as a restarted worker or a second worker on the same machine would. The
store is the ground truth: every case counts the records that landed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from openadapt_flow.guard_demo import FakeAgent, verify_yaml
from openadapt_flow.mockmed.fault_server import serve as fault_serve
from openadapt_flow.verifier import (
    Actor,
    AlreadyWritten,
    Contract,
    FileLedger,
    Ledger,
    MemoryLedger,
    Outcome,
    WriteHeld,
    begin_write,
    reconcile,
    verify_write,
)
from openadapt_flow.write_ledger import LedgerError, fold

NOTE = "Follow-up in two weeks"
VALUES = {"patient_id": "p1", "note": NOTE}
BOT = Actor("agent", "intake-bot")


@pytest.fixture
def sor():
    url, db, stop = fault_serve()
    try:
        yield url.rstrip("/"), db
    finally:
        stop()


@pytest.fixture
def ledger_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "ledger"
    monkeypatch.setenv("OPENADAPT_LEDGER_DIR", str(path))
    return path


def _contract(base: str) -> Contract:
    return Contract.from_dict(verify_yaml(base), name="referral")


def _landed(db) -> int:
    return sum(
        1
        for r in db.snapshot()["records"]
        if r.get("patient_id") == "p1" and r.get("note") == NOTE
    )


#: A worker in its own process. argv: base URL, what to do, write_key or "".
WORKER = textwrap.dedent(
    """
    import json, os, signal, sys, time
    from pathlib import Path
    from openadapt_flow.guard_demo import FakeAgent, verify_yaml
    from openadapt_flow.verifier import (
        Actor, AlreadyWritten, Contract, WriteHeld, begin_write, verify_write,
    )

    base, mode, key = sys.argv[1], sys.argv[2], sys.argv[3] or None
    go = os.environ.get("WORKER_GO")
    if go:  # start together with the other worker
        while not Path(go).exists():
            time.sleep(0.005)
    contract = Contract.from_dict(verify_yaml(base), name="referral")
    values = {"patient_id": "p1", "note": "Follow-up in two weeks"}
    try:
        pending = begin_write(
            contract, values, actor=Actor("agent", "worker"), write_key=key
        )
    except AlreadyWritten as exc:
        print(json.dumps({"worker": "already", "result": exc.result})); sys.exit(0)
    except WriteHeld as exc:
        print(json.dumps({"worker": "held", "reason": str(exc)})); sys.exit(0)
    if mode == "die_before_send":
        os.kill(os.getpid(), signal.SIGKILL)
    agent = FakeAgent(base)
    if mode == "die_after_timeout":
        try:
            agent.click_save("p1", values["note"], fault="timeout")
        except Exception:
            pass
        os.kill(os.getpid(), signal.SIGKILL)
    agent.click_save("p1", values["note"])
    if mode == "die_after_send":
        os.kill(os.getpid(), signal.SIGKILL)
    hold = os.environ.get("WORKER_HOLD")
    if hold:  # keep the lease while the other worker tries
        Path(hold + ".sent").write_text("")
        while not Path(hold).exists():
            time.sleep(0.005)
    result = verify_write(pending)
    print(json.dumps({"worker": "sent", "outcome": result.outcome.value}))
    """
)


def _worker(
    base: str, mode: str, ledger_dir: Path, key: str = "", **env: str
) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", WORKER, base, mode, key],
        env={
            **os.environ,
            "OPENADAPT_LEDGER_DIR": str(ledger_dir),
            **env,
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _reply(proc: subprocess.Popen) -> dict:
    out, err = proc.communicate(timeout=120)
    assert proc.returncode == 0, err
    return json.loads(out.strip().splitlines()[-1])


def _wait_for(path: Path, proc: subprocess.Popen) -> None:
    deadline = time.monotonic() + 60
    while not path.exists():
        assert proc.poll() is None, proc.communicate()[1]
        assert time.monotonic() < deadline, "the worker never sent its write"
        time.sleep(0.01)


# -- kill and restart ----------------------------------------------------------


def test_a_worker_killed_after_the_send_is_not_resent_on_restart(
    sor, ledger_dir: Path
) -> None:
    base, db = sor
    dead = _worker(base, "die_after_send", ledger_dir)
    dead.communicate(timeout=120)
    assert dead.returncode == -9 and _landed(db) == 1

    # The restarted worker makes the same call: the ledger reads the write back
    # with the dead worker's own baseline, and hands back that result.
    with pytest.raises(AlreadyWritten) as caught:
        begin_write(_contract(base), VALUES, actor=BOT)
    assert caught.value.result["outcome"] == "CONFIRMED"
    assert caught.value.result["not_sent_again"] is True
    assert len(caught.value.result["receipt_sha256"]) == 64
    assert _landed(db) == 1

    # An identical call inside the repeat window is a retry: handed back too.
    with pytest.raises(AlreadyWritten):
        begin_write(_contract(base), VALUES, actor=BOT)
    # After it (repeat_s=0 here), the same values are a new write.
    no_repeat = Ledger(FileLedger(ledger_dir), repeat_s=0)
    pending = begin_write(_contract(base), VALUES, actor=BOT, ledger=no_repeat)
    FakeAgent(base).click_save("p1", NOTE)
    # Its own baseline holds the first note, so the second is its new record.
    assert verify_write(pending).verified
    assert _landed(db) == 2


def test_a_worker_killed_while_its_write_timed_out_is_not_resent(
    sor, ledger_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openadapt_flow.mockmed import fault_server

    monkeypatch.setattr(fault_server, "TIMEOUT_HANG_S", 0.5)
    base, db = sor
    dead = _worker(base, "die_after_timeout", ledger_dir)
    dead.communicate(timeout=120)
    assert dead.returncode == -9 and _landed(db) == 1
    with pytest.raises(AlreadyWritten):
        begin_write(_contract(base), VALUES, actor=BOT)
    assert _landed(db) == 1


def test_a_worker_killed_before_the_send_is_held_until_released(
    sor, ledger_dir: Path
) -> None:
    base, db = sor
    dead = _worker(base, "die_before_send", ledger_dir)
    dead.communicate(timeout=120)
    assert dead.returncode == -9 and _landed(db) == 0

    # Nothing shows, but a send can't be ruled out: held, never resent.
    with pytest.raises(WriteHeld, match="may still land") as caught:
        begin_write(_contract(base), VALUES, actor=BOT)
    key = caught.value.key
    with pytest.raises(WriteHeld):
        begin_write(_contract(base), VALUES, actor=BOT)
    assert _landed(db) == 0

    # A person checks the record and releases it; then the write may go.
    ledger = Ledger(FileLedger(ledger_dir))
    assert [k for k, _ in ledger.unresolved()] == [key]
    ledger.release(
        key, by="nurse-lead", reason="checked the chart: no referral", found="absent"
    )
    pending = begin_write(_contract(base), VALUES, actor=BOT)
    FakeAgent(base).click_save("p1", NOTE)
    assert verify_write(pending).verified and _landed(db) == 1


def test_a_settle_time_releases_an_attempt_the_store_never_showed(
    sor, ledger_dir: Path
) -> None:
    base, db = sor
    dead = _worker(base, "die_before_send", ledger_dir)
    dead.communicate(timeout=120)
    ledger = Ledger(FileLedger(ledger_dir), settle_s=0)
    pending = begin_write(_contract(base), VALUES, actor=BOT, ledger=ledger)
    FakeAgent(base).click_save("p1", NOTE)
    assert verify_write(pending).verified and _landed(db) == 1


def test_a_late_write_found_on_reconcile_is_never_resent(sor, ledger_dir: Path) -> None:
    base, db = sor
    pending = begin_write(_contract(base), VALUES, actor=BOT)
    # The read-back runs before the write lands (a queued write).
    assert verify_write(pending).outcome is Outcome.UNCERTAIN
    with pytest.raises(WriteHeld):
        begin_write(_contract(base), VALUES, actor=BOT)
    FakeAgent(base).click_save("p1", NOTE)  # it lands late

    # Reconciliation judges it against the attempt's own baseline: confirmed,
    # handed back, and never sent again.
    with pytest.raises(AlreadyWritten) as caught:
        begin_write(_contract(base), VALUES, actor=BOT)
    assert caught.value.result["outcome"] == "CONFIRMED"
    assert caught.value.result["receipt_outcome"] == "verified"
    assert _landed(db) == 1


def test_a_refuted_write_is_held_across_restarts(sor, ledger_dir: Path) -> None:
    base, db = sor
    pending = begin_write(_contract(base), VALUES, actor=BOT)
    FakeAgent(base).click_save("p1", NOTE)
    FakeAgent(base).click_save("p1", NOTE)  # saved twice
    assert verify_write(pending).outcome is Outcome.REFUTED
    fresh = Ledger(FileLedger(ledger_dir))  # a new process, the same disk
    with pytest.raises(WriteHeld, match="refuted"):
        begin_write(_contract(base), VALUES, actor=BOT, ledger=fresh)
    assert _landed(db) == 2


def test_reconcile_resolves_an_attempt_by_reading_back(sor, ledger_dir: Path) -> None:
    base, db = sor
    dead = _worker(base, "die_after_send", ledger_dir)
    dead.communicate(timeout=120)
    ledger = Ledger(FileLedger(ledger_dir))
    [(key, attempt)] = ledger.unresolved()
    assert attempt.state == "open"
    assert reconcile(_contract(base), key) == "undelivered"
    resolved = ledger.latest(key)
    assert resolved is not None and resolved.resolved is not None
    assert resolved.resolved["outcome"] == "CONFIRMED"
    assert resolved.resolved["via"] == "reconcile"


# -- two workers -----------------------------------------------------------------


def test_a_second_worker_waits_while_the_first_has_the_write(
    sor, ledger_dir: Path, tmp_path: Path
) -> None:
    base, db = sor
    hold = tmp_path / "hold"
    first = _worker(base, "send", ledger_dir, key="task-0001", WORKER_HOLD=str(hold))
    _wait_for(Path(str(hold) + ".sent"), first)
    # The first worker sent the write and hasn't read it back yet.
    second = _reply(_worker(base, "send", ledger_dir, key="task-0001"))
    assert second["worker"] == "held" and "in flight" in second["reason"]
    hold.write_text("")
    assert _reply(first) == {"worker": "sent", "outcome": "CONFIRMED"}
    # A write key names one write: the retry gets the result, every time.
    for _ in range(2):
        again = _reply(_worker(base, "send", ledger_dir, key="task-0001"))
        assert again["worker"] == "already"
        assert again["result"]["outcome"] == "CONFIRMED"
    assert _landed(db) == 1


def test_two_workers_started_together_send_one_write(
    sor, ledger_dir: Path, tmp_path: Path
) -> None:
    base, db = sor
    for round_ in range(3):
        go = tmp_path / f"go-{round_}"
        key = f"task-race-{round_}"
        workers = [
            _worker(base, "send", ledger_dir, key=key, WORKER_GO=str(go))
            for _ in range(2)
        ]
        time.sleep(0.5)
        go.write_text("")
        replies = sorted(_reply(w)["worker"] for w in workers)
        assert replies.count("sent") == 1, replies
        assert set(replies) <= {"sent", "held", "already"}
        assert _landed(db) == round_ + 1


# -- the ledger itself -----------------------------------------------------------


def test_a_cancelled_reply_is_handed_back_instead_of_a_resend(
    sor, ledger_dir: Path
) -> None:
    base, db = sor
    pending = begin_write(_contract(base), VALUES, actor=BOT)
    FakeAgent(base).click_save("p1", NOTE)
    verify_write(pending, delivered=False)  # the tool call was cancelled
    with pytest.raises(AlreadyWritten) as caught:
        begin_write(_contract(base), VALUES, actor=BOT)
    assert caught.value.result["outcome"] == "CONFIRMED"
    assert _landed(db) == 1


def test_the_ledger_holds_no_values_and_is_private(sor, ledger_dir: Path) -> None:
    base, _db = sor
    pending = begin_write(_contract(base), VALUES, actor=BOT)
    FakeAgent(base).click_save("p1", NOTE)
    verify_write(pending)
    entries = list((ledger_dir / "entries").glob("*.jsonl"))
    assert len(entries) == 1
    text = entries[0].read_text()
    assert NOTE not in text and '"p1"' not in text
    for path in [ledger_dir, *ledger_dir.rglob("*")]:
        mode = path.stat().st_mode & 0o777
        assert mode in (0o700, 0o600), (path, oct(mode))


def test_a_torn_last_line_is_ignored_and_appends_stay_whole(
    tmp_path: Path,
) -> None:
    backend = FileLedger(tmp_path / "ledger")
    key = "a" * 64
    backend.append(key, {"event": "begun", "attempt": "write-1"})
    path = tmp_path / "ledger" / "entries" / f"{key}.jsonl"
    with path.open("a") as fh:
        fh.write('{"event": "resol')  # a crash mid-append
    assert [e["event"] for e in backend.events(key)] == ["begun"]
    backend.append(
        key, {"event": "resolved", "attempt": "write-1", "outcome": "REFUTED"}
    )
    attempt = fold(backend.events(key))
    assert attempt is not None and attempt.state == "held"


def test_a_ledger_folder_others_can_write_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    FileLedger(root)
    # A folder this user owns is reset to 0700; a symlink can't be trusted.
    (root / "entries").rmdir()
    (root / "entries").symlink_to(tmp_path)
    with pytest.raises(LedgerError):
        FileLedger(root)


def test_a_release_names_who_and_why(sor, ledger_dir: Path) -> None:
    base, _db = sor
    pending = begin_write(_contract(base), VALUES, actor=BOT)
    FakeAgent(base).click_save("p1", NOTE)
    FakeAgent(base).click_save("p1", NOTE)
    verify_write(pending)
    ledger = Ledger(FileLedger(ledger_dir))
    [(key, _)] = ledger.unresolved()
    with pytest.raises(LedgerError):
        ledger.release(key, by="", reason="", found="absent")
    ledger.release(key, by="nurse-lead", reason="removed the duplicate", found="landed")
    assert ledger.unresolved() == []


def test_threads_in_one_process_share_the_lease_rules(sor, ledger_dir: Path) -> None:
    """Adapters begin and verify on pool threads: a second begin_write in the
    same process is refused at once, and whichever thread verifies releases."""

    from concurrent.futures import ThreadPoolExecutor

    base, db = sor
    pool = ThreadPoolExecutor(max_workers=2)
    pending = pool.submit(
        begin_write, _contract(base), VALUES, actor=BOT, write_key="task-t"
    ).result()
    started = time.monotonic()
    with pytest.raises(WriteHeld, match="in flight"):
        pool.submit(
            begin_write, _contract(base), VALUES, actor=BOT, write_key="task-t"
        ).result(timeout=5)
    assert time.monotonic() - started < 5  # refused, never waited
    FakeAgent(base).click_save("p1", NOTE)
    assert pool.submit(verify_write, pending).result().verified  # another thread
    with pytest.raises(AlreadyWritten):
        pool.submit(
            begin_write, _contract(base), VALUES, actor=BOT, write_key="task-t"
        ).result()
    pool.shutdown()
    assert _landed(db) == 1


def test_who_declared_each_value_reaches_the_reply(sor, ledger_dir: Path) -> None:
    base, _db = sor
    declared = {"patient_id": "operator", "note": "agent"}
    pending = begin_write(_contract(base), VALUES, actor=BOT, declared_by=declared)
    FakeAgent(base).click_save("p1", NOTE)
    assert verify_write(pending).to_dict()["values_declared_by"] == declared


# -- review findings (adversarial review of #1628) -------------------------------


def test_editing_read_settings_after_a_crash_keeps_the_hold(
    sor, ledger_dir: Path
) -> None:
    base, db = sor
    dead = _worker(base, "die_after_send", ledger_dir)
    dead.communicate(timeout=120)
    moved = Contract.from_dict(
        verify_yaml(base.replace("127.0.0.1", "localhost")), name="referral"
    )
    assert moved.identity == _contract(base).identity
    # Another address for the store: the old baseline was read by another
    # verifier, so nothing can judge the attempt. Held, never resent.
    with pytest.raises(WriteHeld):
        begin_write(moved, VALUES, actor=BOT)
    doc = verify_yaml(base)
    doc["effects"][0]["timeout_s"] = 2.0
    doc["max_execution_window_s"] = 1800
    edited = Contract.from_dict(doc, name="referral")
    assert edited.digest != _contract(base).digest
    assert edited.identity == _contract(base).identity
    # Only timings changed: the attempt is judged and handed back.
    with pytest.raises(AlreadyWritten):
        begin_write(edited, VALUES, actor=BOT)
    assert _landed(db) == 1


def test_a_value_typed_differently_is_the_same_write(sor, ledger_dir: Path) -> None:
    base, _db = sor
    pending = begin_write(_contract(base), VALUES, actor=BOT)
    with pytest.raises(WriteHeld, match="in flight"):
        begin_write(_contract(base), {"patient_id": "p1", "note": NOTE}, actor=BOT)
    ledger = Ledger(MemoryLedger())
    assert ledger.key_for("c", {"n": 7}) == ledger.key_for("c", {"n": "7"})
    pending.release()


def test_another_tasks_identical_write_is_never_taken_for_this_one(
    sor, ledger_dir: Path
) -> None:
    base, db = sor
    # task-1 reads its baseline and dies before sending.
    dead = _worker(base, "die_before_send", ledger_dir, key="task-1")
    dead.communicate(timeout=120)
    # task-2 makes the same write and it lands. task-1 may have sent too, so
    # one record can't confirm task-2 either: both wait for a person.
    pending = begin_write(_contract(base), VALUES, actor=BOT, write_key="task-2")
    FakeAgent(base).click_save("p1", NOTE)
    assert verify_write(pending).outcome is Outcome.UNCERTAIN
    # task-1 restarts: the one record may be task-2's, so it isn't confirmed.
    with pytest.raises(WriteHeld, match="may still land"):
        begin_write(_contract(base), VALUES, actor=BOT, write_key="task-1")
    assert _landed(db) == 1


def test_other_workers_records_dont_hold_a_correct_write(sor, ledger_dir: Path) -> None:
    base, db = sor
    contract = _contract(base)
    a = begin_write(contract, VALUES, actor=BOT)
    other = {"patient_id": "p2", "note": NOTE}
    b = begin_write(contract, other, actor=BOT)
    FakeAgent(base).click_save("p1", NOTE)
    FakeAgent(base).click_save("p2", NOTE)
    for pending in (a, b):
        result = verify_write(pending)
        # Each sees the other's record beside its own: landed, not confirmed,
        # never held and never resent.
        assert result.outcome is Outcome.UNCERTAIN and result.landed
        assert result.review  # a person looks at the extra record
    with pytest.raises(AlreadyWritten):
        begin_write(contract, VALUES, actor=BOT)
    listed = Ledger(FileLedger(ledger_dir)).unresolved()
    assert len(listed) == 2 and all(a.review for _k, a in listed)


def test_a_stray_record_no_attempt_explains_stays_refuted(
    sor, ledger_dir: Path
) -> None:
    base, _db = sor
    pending = begin_write(_contract(base), VALUES, actor=BOT)
    FakeAgent(base).click_save("p1", NOTE)
    FakeAgent(base).click_save("p3", "a stray write")  # no attempt in the ledger
    result = verify_write(pending)
    assert result.outcome is Outcome.REFUTED and not result.landed


def test_a_write_reconciled_after_others_landed_is_not_held(
    sor, ledger_dir: Path
) -> None:
    base, db = sor
    dead = _worker(base, "die_after_send", ledger_dir)
    dead.communicate(timeout=120)
    # Another write lands before the restart.
    other = {"patient_id": "p2", "note": NOTE}
    pending = begin_write(_contract(base), other, actor=BOT)
    FakeAgent(base).click_save("p2", NOTE)
    verify_write(pending)
    with pytest.raises(AlreadyWritten) as caught:
        begin_write(_contract(base), VALUES, actor=BOT)
    assert caught.value.result["landed"] is True
    assert _landed(db) == 1


def test_an_unusable_ledger_blocks_the_write(sor, tmp_path: Path, monkeypatch) -> None:
    base, _db = sor
    bad = tmp_path / "not-private"
    bad.mkdir()
    (bad / "entries").symlink_to(tmp_path)
    monkeypatch.setenv("OPENADAPT_LEDGER_DIR", str(bad))
    with pytest.raises(WriteHeld, match="ledger can't be used"):
        begin_write(_contract(base), VALUES, actor=BOT)


def test_a_released_pending_write_leaves_the_ledger_alone(
    sor, ledger_dir: Path
) -> None:
    base, _db = sor
    pending = begin_write(_contract(base), VALUES, actor=BOT)
    FakeAgent(base).click_save("p1", NOTE)
    pending.release()
    verify_write(pending)  # judged, but not recorded without the lease
    [(key, attempt)] = Ledger(FileLedger(ledger_dir)).unresolved()
    assert attempt.state == "open"


def test_a_resolved_attempt_keeps_no_values(sor, ledger_dir: Path) -> None:
    base, _db = sor
    pending = begin_write(_contract(base), VALUES, actor=BOT)
    FakeAgent(base).click_save("p1", NOTE)
    verify_write(pending)
    assert not (pending.workdir / "attempt.json").exists()
    assert not (pending.workdir / "baseline.json").exists()
    assert (pending.workdir / "effect-receipt.json").exists()


# -- second review round ---------------------------------------------------------


def test_the_same_write_to_another_store_is_its_own_write(ledger_dir: Path) -> None:
    url_a, db_a, stop_a = fault_serve()
    url_b, db_b, stop_b = fault_serve()
    try:
        a, b = _contract(url_a.rstrip("/")), _contract(url_b.rstrip("/"))
        assert a.identity != b.identity
        for key in ("ticket-42", None):
            pending = begin_write(a, VALUES, actor=BOT, write_key=key)
            FakeAgent(url_a.rstrip("/")).click_save("p1", NOTE)
            assert verify_write(pending).verified
            pending = begin_write(b, VALUES, actor=BOT, write_key=key)
            FakeAgent(url_b.rstrip("/")).click_save("p1", NOTE)
            assert verify_write(pending).verified
        assert _landed(db_a) == 2 and _landed(db_b) == 2
    finally:
        stop_a()
        stop_b()


def test_a_loopback_alias_is_the_same_store(sor, ledger_dir: Path) -> None:
    base, _db = sor
    alias = Contract.from_dict(
        verify_yaml(base.replace("127.0.0.1", "localhost")), name="referral"
    )
    assert alias.identity == _contract(base).identity


def test_an_earlier_tasks_record_never_confirms_a_later_unsent_one(
    sor, ledger_dir: Path
) -> None:
    base, db = sor
    first = begin_write(_contract(base), VALUES, actor=BOT, write_key="task-1")
    time.sleep(1.1)  # the second task begins a second later
    dead = _worker(base, "die_before_send", ledger_dir, key="task-2")
    dead.communicate(timeout=120)
    FakeAgent(base).click_save("p1", NOTE)
    assert verify_write(first).outcome is Outcome.UNCERTAIN  # task-2 was open
    with pytest.raises(WriteHeld):
        begin_write(_contract(base), VALUES, actor=BOT, write_key="task-2")
    assert _landed(db) == 1


def test_a_phantom_save_beside_an_identical_write_is_not_confirmed(
    sor, ledger_dir: Path
) -> None:
    base, db = sor
    one = begin_write(_contract(base), VALUES, actor=BOT, write_key="task-1")
    two = begin_write(_contract(base), VALUES, actor=BOT, write_key="task-2")
    FakeAgent(base).click_save("p1", NOTE)
    FakeAgent(base).click_save("p1", NOTE, fault="no_persist")
    results = [verify_write(one), verify_write(two)]
    assert not any(r.verified for r in results)
    assert _landed(db) == 1


def test_a_stale_attempt_never_excuses_a_stray_write(sor, ledger_dir: Path) -> None:
    base, _db = sor
    # An attempt for another patient, begun and abandoned (never sent).
    dead = _worker(base, "die_before_send", ledger_dir)
    dead.communicate(timeout=120)
    other = {"patient_id": "p9", "note": NOTE}
    pending = begin_write(_contract(base), other, actor=BOT)
    FakeAgent(base).click_save("p9", NOTE)
    FakeAgent(base).click_save("p3", "a stray note")
    result = verify_write(pending)
    assert result.outcome is Outcome.REFUTED and not result.landed


# -- third review round ----------------------------------------------------------


def test_releasing_an_open_attempt_lifts_the_hold(sor, ledger_dir: Path) -> None:
    base, db = sor
    dead = _worker(base, "die_before_send", ledger_dir)
    dead.communicate(timeout=120)
    ledger = Ledger(FileLedger(ledger_dir))
    [(key, attempt)] = ledger.unresolved()
    assert attempt.state == "open"
    ledger.release(
        key, by="nurse-lead", reason="checked the chart: nothing saved", found="absent"
    )
    pending = begin_write(_contract(base), VALUES, actor=BOT)
    FakeAgent(base).click_save("p1", NOTE)
    assert verify_write(pending).verified and _landed(db) == 1


def test_a_released_stale_attempt_never_holds_later_writes(
    sor, ledger_dir: Path
) -> None:
    base, db = sor
    dead = _worker(base, "die_before_send", ledger_dir, key="task-old")
    dead.communicate(timeout=120)
    ledger = Ledger(FileLedger(ledger_dir))
    [(key, _)] = ledger.unresolved()
    ledger.release(key, by="nurse-lead", reason="nothing saved", found="absent")
    pending = begin_write(_contract(base), VALUES, actor=BOT, write_key="task-new")
    FakeAgent(base).click_save("p1", NOTE)
    assert verify_write(pending).verified


def test_a_write_under_review_stays_listed_past_the_repeat_window(
    sor, ledger_dir: Path
) -> None:
    base, _db = sor
    ledger = Ledger(FileLedger(ledger_dir), repeat_s=0)
    contract = _contract(base)
    a = begin_write(contract, VALUES, actor=BOT, ledger=ledger)
    b = begin_write(
        contract, {"patient_id": "p2", "note": NOTE}, actor=BOT, ledger=ledger
    )
    FakeAgent(base).click_save("p1", NOTE)
    FakeAgent(base).click_save("p2", NOTE)
    assert verify_write(a).review
    verify_write(b)
    for _ in range(2):  # past repeat_s: still handed back, still listed
        with pytest.raises(AlreadyWritten):
            begin_write(contract, VALUES, actor=BOT, ledger=ledger)
    assert any(a.review for _k, a in ledger.unresolved())


@pytest.mark.parametrize(
    "edit",
    [
        lambda u: u + "/",
        lambda u: u.replace("http://", "HTTP://"),
        lambda u: u.replace("127.0.0.1", "LOCALHOST"),
    ],
)
def test_a_respelled_store_address_is_the_same_store(sor, edit) -> None:
    base, _db = sor
    assert Contract.from_dict(verify_yaml(edit(base))).store == _contract(base).store


def test_default_ports_and_slashes_spell_one_address() -> None:
    from openadapt_flow.verifier import _location

    assert _location("https://Api.Example.test:443/fhir/") == _location(
        "https://api.example.test/fhir"
    )
    assert _location("https://api.example.test:8443/") != _location(
        "https://api.example.test/"
    )


def test_the_same_write_under_another_address_is_held_while_open(
    sor, ledger_dir: Path
) -> None:
    base, db = sor
    dead = _worker(base, "die_after_send", ledger_dir, key="ticket-9")
    dead.communicate(timeout=120)
    doc = verify_yaml(base)
    doc["verifier"]["base_url"] = base + "/../"  # another spelling, same server
    moved = Contract.from_dict(doc, name="referral")
    assert moved.store != _contract(base).store
    with pytest.raises(WriteHeld, match="another address"):
        begin_write(moved, VALUES, actor=BOT, write_key="ticket-9")
    assert _landed(db) == 1


def test_tenants_chosen_by_credential_are_different_stores(sor) -> None:
    base, _db = sor
    docs = []
    for env in ("TENANT_A_TOKEN", "TENANT_B_TOKEN"):
        doc = verify_yaml(base)
        doc["verifier"]["auth"] = {"kind": "bearer", "bearer_env": env}
        docs.append(doc)
    try:
        a, b = (Contract.from_dict(d) for d in docs)
    except Exception as exc:  # noqa: BLE001 - auth needs the env set
        pytest.skip(f"auth shape not accepted here: {exc}")
    assert a.store != b.store


# -- fourth review round ---------------------------------------------------------


def _split(base: str) -> Contract:
    """The same store, its address split differently."""

    doc = verify_yaml(base)
    doc["verifier"]["base_url"] = base + "/api"
    doc["verifier"]["records_path"] = "/db"
    return Contract.from_dict(doc, name="referral")


@pytest.mark.parametrize("key", ["ticket-7", None])
def test_a_made_write_is_never_resent_under_another_spelling(
    sor, ledger_dir: Path, key
) -> None:
    base, db = sor
    pending = begin_write(_contract(base), VALUES, actor=BOT, write_key=key)
    FakeAgent(base).click_save("p1", NOTE)
    assert verify_write(pending).verified
    assert _split(base).store != _contract(base).store
    with pytest.raises(WriteHeld, match="another address"):
        begin_write(_split(base), VALUES, actor=BOT, write_key=key)
    assert _landed(db) == 1


def test_a_hold_on_one_environment_never_holds_another(ledger_dir: Path) -> None:
    url, db, stop = fault_serve()
    try:
        base = url.rstrip("/")
        docs = []
        for env in ("clinic-north-prod", "clinic-south-prod"):
            doc = verify_yaml(base)
            doc["environment_id"] = env
            docs.append(Contract.from_dict(doc, name="referral"))
        north, south = docs
        held = begin_write(north, VALUES, actor=BOT)  # in flight on north
        pending = begin_write(south, VALUES, actor=BOT)
        pending.release()
        held.release()
    finally:
        stop()


def test_releasing_a_stale_attempt_unblocks_the_write_it_held(
    sor, ledger_dir: Path
) -> None:
    base, db = sor
    dead = _worker(base, "die_before_send", ledger_dir, key="task-old")
    dead.communicate(timeout=120)
    pending = begin_write(_contract(base), VALUES, actor=BOT, write_key="task-new")
    FakeAgent(base).click_save("p1", NOTE)
    assert verify_write(pending).outcome is Outcome.UNCERTAIN  # task-old was open
    ledger = Ledger(FileLedger(ledger_dir))
    old = next(k for k, a in ledger.unresolved() if a.state == "open")
    ledger.release(old, by="nurse-lead", reason="task-old never sent", found="absent")
    # task-new's retry reconciles: confirmed now, handed back, never resent.
    with pytest.raises(AlreadyWritten) as caught:
        begin_write(_contract(base), VALUES, actor=BOT, write_key="task-new")
    assert caught.value.result["outcome"] == "CONFIRMED"
    assert _landed(db) == 1


def test_a_release_that_found_the_write_never_frees_it(sor, ledger_dir: Path) -> None:
    base, db = sor
    pending = begin_write(_contract(base), VALUES, actor=BOT, write_key="task-9")
    assert verify_write(pending).outcome is Outcome.UNCERTAIN  # read too early
    FakeAgent(base).click_save("p1", NOTE)
    ledger = Ledger(FileLedger(ledger_dir), settle_s=None)
    [(key, _)] = ledger.unresolved()
    ledger.release(key, by="nurse-lead", reason="the note is there", found="landed")
    for _ in range(2):
        with pytest.raises(AlreadyWritten):
            begin_write(_contract(base), VALUES, actor=BOT, write_key="task-9")
    assert _landed(db) == 1


# -- fifth review round ----------------------------------------------------------


def test_two_sql_servers_with_one_database_name_are_different_shapes() -> None:
    def contract(host: str) -> Contract:
        return Contract.from_dict(
            {
                "schema_version": "openadapt.verify/v1",
                "verifier": {
                    "kind": "sql",
                    "sql_driver": "pymysql",
                    "sql_connect_args": {
                        "host": host,
                        "port": 3306,
                        "database": "openemr",
                    },
                    "sql_query": "SELECT * FROM patient_data",
                },
                "effects": [
                    {
                        "kind": "record_written",
                        "match": {"fname": {"param": "fname"}},
                        "expected_count": 1,
                    }
                ],
            }
        )

    try:
        a, b = contract("emr-a.internal"), contract("emr-b.internal")
    except Exception as exc:  # noqa: BLE001 - driver options vary by install
        pytest.skip(f"sql contract shape not accepted here: {exc}")
    assert a.shape != b.shape and a.store != b.store
