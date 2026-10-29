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

    # The result reached a caller, so the next identical call is a new write.
    pending = begin_write(_contract(base), VALUES, actor=BOT)
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
    ledger.release(key, by="nurse-lead", reason="checked the chart: no referral")
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
        ledger.release(key, by="", reason="")
    ledger.release(key, by="nurse-lead", reason="removed the duplicate")
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
