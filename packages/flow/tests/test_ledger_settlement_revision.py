"""Journal revisions order workflow decisions, never establish native proof."""

from __future__ import annotations

import multiprocessing
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from openadapt_flow import write_ledger as wl

KEY = "a" * 64
OTHER = "b" * 64


def _started(backend: wl.LedgerBackend, key: str = KEY) -> wl.Ledger:
    led = wl.Ledger(backend)
    led.begun(key, "first")
    return led


@pytest.fixture(params=["memory", "file"])
def ledger(request: pytest.FixtureRequest, tmp_path: Path) -> wl.Ledger:
    backend = (
        wl.MemoryLedger(tmp_path)
        if request.param == "memory"
        else wl.FileLedger(tmp_path)
    )
    return _started(backend)


@pytest.mark.parametrize("weak", ["UNCERTAIN", "CONFIRMED", "retry", "rechecked"])
def test_stale_observation_is_retained_without_clearing_hold(
    ledger: wl.Ledger,
    weak: str,
) -> None:
    before = ledger.latest(KEY)
    assert before is not None
    ledger.resolved(KEY, "first", outcome="REFUTED", expected_revision=before.revision)
    if weak == "rechecked":
        ledger.rechecked(KEY, "first", expected_revision=before.revision)
    else:
        ledger.resolved(
            KEY,
            "first",
            outcome="UNCERTAIN" if weak == "retry" else weak,
            retry_safe=weak == "retry",
            expected_revision=before.revision,
        )
    after = ledger.latest(KEY)
    assert after is not None and after.state == "held"
    assert after.resolved is not None and after.resolved["outcome"] == "REFUTED"
    assert after.rechecked is None and after.recheck_owed is None
    assert len(after.events) == 3 and after.events[-1]["applied"] is False
    assert after.events[-1]["based_on"] == before.revision
    assert ledger.attempts()[0][2] == after.resolved


@given(
    st.lists(
        st.sampled_from(["CONFIRMED", "UNCERTAIN", "REFUTED", "retry", "rechecked"]),
        min_size=1,
        max_size=20,
    )
)
@settings(max_examples=60, deadline=None)
def test_one_revision_model_for_all_observation_orders(operations: list[str]) -> None:
    # All workers observed the same first revision. Only the first weak
    # observation is current; any later negative can tighten the hold.
    ledger = _started(wl.MemoryLedger())
    expected = "open"
    held = False
    for index, operation in enumerate(operations):
        if operation == "REFUTED":
            held = True
        if index == 0:
            expected = {"CONFIRMED": "undelivered", "retry": "free"}.get(
                operation, "uncertain"
            )
            if operation == "rechecked":
                expected = "open"
        if operation == "rechecked":
            ledger.rechecked(KEY, "first", expected_revision=1)
        else:
            ledger.resolved(
                KEY,
                "first",
                outcome="UNCERTAIN" if operation == "retry" else operation,
                retry_safe=operation == "retry",
                expected_revision=1,
            )
        attempt = ledger.latest(KEY)
        assert attempt is not None
        assert attempt.state == ("held" if held else expected)
        assert len(attempt.events) == index + 2
        assert attempt.revision == index + 2
        assert [event["revision"] for event in attempt.events] == list(
            range(1, index + 3)
        )


def test_memory_threads_pause_after_read_before_weak_append(tmp_path: Path) -> None:
    ledger = _started(wl.MemoryLedger(tmp_path))
    read = threading.Event()
    resume = threading.Event()

    def weak_worker() -> wl.Attempt:
        snapshot = ledger.latest(KEY)
        assert snapshot is not None
        read.set()
        assert resume.wait(10)
        return ledger.resolved(
            KEY,
            "first",
            outcome="UNCERTAIN",
            retry_safe=True,
            expected_revision=snapshot.revision,
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(weak_worker)
        assert read.wait(10)
        ledger.resolved(KEY, "first", outcome="REFUTED")
        resume.set()
        assert task.result(timeout=10).state == "held"
    assert ledger.latest(KEY).state == "held"  # type: ignore[union-attr]


def _file_worker(root: str, ready: Any, resume: Any, output: Any) -> None:
    ledger = wl.Ledger(wl.FileLedger(root))
    snapshot = ledger.latest(KEY)
    assert snapshot is not None
    ready.set()
    assert resume.wait(15)
    after = ledger.resolved(
        KEY,
        "first",
        outcome="CONFIRMED",
        retry_safe=False,
        expected_revision=snapshot.revision,
    )
    output.put((after.state, after.events[-1]["applied"]))


def test_file_independent_process_and_reopen_preserve_raw_prefix(
    tmp_path: Path,
) -> None:
    ledger = _started(wl.FileLedger(tmp_path))
    prefix = ledger.backend.events(KEY)
    context = multiprocessing.get_context("spawn")
    ready, resume, output = context.Event(), context.Event(), context.Queue()
    worker = context.Process(
        target=_file_worker, args=(str(tmp_path), ready, resume, output)
    )
    worker.start()
    try:
        assert ready.wait(15)
        ledger.resolved(KEY, "first", outcome="REFUTED")
        resume.set()
        assert output.get(timeout=15) == ("held", False)
        worker.join(15)
        assert worker.exitcode == 0
    finally:
        resume.set()
        if worker.is_alive():
            worker.terminate()
            worker.join(15)
    reopened = wl.Ledger(wl.FileLedger(tmp_path))
    events = reopened.backend.events(KEY)
    assert events[: len(prefix)] == prefix and len(events) == 3
    assert reopened.latest(KEY).state == "held"  # type: ignore[union-attr]


def test_stale_record_cannot_prune_or_mutate_newer_attempt(
    ledger: wl.Ledger, tmp_path: Path
) -> None:
    folder = tmp_path / "saved"
    folder.mkdir()
    values = folder / "attempt.json"
    values.write_text("synthetic")
    first = ledger.latest(KEY)
    assert first is not None
    ledger.resolved(KEY, "first", outcome="UNCERTAIN", retry_safe=True)
    ledger.begun(KEY, "second")
    ledger.resolved(
        KEY,
        "first",
        outcome="CONFIRMED",
        expected_revision=first.revision,
        cleanup=folder,
    )
    current = ledger.latest(KEY)
    assert (
        current is not None
        and current.attempt_id == "second"
        and current.state == "open"
    )
    assert values.read_text() == "synthetic"
    assert ledger.backend.events(KEY)[-1]["applied"] is False


def test_explicit_release_is_separate_and_stale_release_refuses(
    ledger: wl.Ledger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger.resolved(KEY, "first", outcome="REFUTED")
    original = ledger._update

    def concurrent(key: str, event: Any, **kwargs: Any) -> wl.Attempt:
        if event["event"] == "released":
            original(
                key, {"event": "resolved", "attempt": "first", "outcome": "REFUTED"}
            )
        return original(key, event, **kwargs)

    monkeypatch.setattr(ledger, "_update", concurrent)
    with pytest.raises(wl.LedgerError, match="changed"):
        ledger.release(KEY, by="operator", reason="synthetic check", found="absent")
    assert ledger.latest(KEY).state == "held"  # type: ignore[union-attr]
    monkeypatch.setattr(ledger, "_update", original)
    released = ledger.release(
        KEY, by="operator", reason="synthetic check", found="absent"
    )
    assert released.state == "free" and released.released
    assert released.resolved is not None and released.resolved["outcome"] == "REFUTED"
    assert ledger.attempts()[0][2] == released.resolved


def test_raw_fields_cannot_mint_acceptance_or_release(ledger: wl.Ledger) -> None:
    ledger.backend.append(
        KEY,
        {
            "event": "resolved",
            "attempt": "first",
            "outcome": "REFUTED",
            "retry_safe": True,
            "landed": True,
        },
    )
    assert ledger.latest(KEY).state == "held"  # type: ignore[union-attr]
    for kind in ("resolved", "released", "rechecked"):
        ledger.backend.append(
            KEY,
            {
                "event": kind,
                "attempt": "first",
                "outcome": "UNCERTAIN",
                "via": "person",
                "retry_safe": True,
                "applied": True,
                "based_on": 1,
            },
        )
        assert ledger.latest(KEY).state == "held"  # type: ignore[union-attr]
        assert ledger.backend.events(KEY)[-1]["applied"] is False
        assert ledger.backend.events(KEY)[-1]["based_on"] == 1


def test_nonconflicting_keys_and_current_positive_still_work(ledger: wl.Ledger) -> None:
    ledger.begun(OTHER, "unrelated")
    before = ledger.latest(KEY)
    assert before is not None
    ledger.resolved(OTHER, "unrelated", outcome="REFUTED")
    after = ledger.resolved(
        KEY,
        "first",
        outcome="CONFIRMED",
        delivered=False,
        expected_revision=before.revision,
        result={"provisional": True, "recheck_due_at": "future"},
    )
    settled = ledger.rechecked(KEY, "first", expected_revision=after.revision)
    assert settled.rechecked is not None and settled.recheck_owed is None
    delivered = ledger.delivered(KEY, "first", expected_revision=settled.revision)
    assert delivered.delivered and delivered.resolved["outcome"] == "CONFIRMED"  # type: ignore[index]
    assert ledger.latest(OTHER).state == "held"  # type: ignore[union-attr]


def test_nested_memory_values_cannot_rewrite_journal(tmp_path: Path) -> None:
    ledger = _started(wl.MemoryLedger(tmp_path))
    value = {"verified": False, "detail": {"count": 1}}
    ledger.resolved(KEY, "first", outcome="UNCERTAIN", result=value)
    value["detail"]["count"] = 999  # type: ignore[index]
    events = ledger.backend.events(KEY)
    events[-1]["result"]["detail"]["count"] = 888
    assert ledger.backend.events(KEY)[-1]["result"]["detail"]["count"] == 1


def test_custom_backend_without_transaction_fails_closed(tmp_path: Path) -> None:
    class LegacyBackend:
        def __init__(self) -> None:
            self.inner = wl.MemoryLedger(tmp_path)

        def __getattr__(self, name: str) -> Any:
            if name == "transaction":
                raise AttributeError(name)
            return getattr(self.inner, name)

    ledger = wl.Ledger(LegacyBackend())
    with pytest.raises(wl.LedgerError, match="atomic"):
        ledger.begun(KEY, "first")
    assert ledger.backend.events(KEY) == []


@pytest.mark.parametrize("failure", ["write", "fsync"])
def test_failed_durable_append_never_runs_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    ledger = _started(wl.FileLedger(tmp_path / "ledger"))
    saved = tmp_path / "saved"
    saved.mkdir()
    values = saved / "attempt.json"
    values.write_text("synthetic")

    def fail(*args: Any) -> Any:
        raise OSError("synthetic append failure")

    with monkeypatch.context() as patch:
        patch.setattr(wl.os, failure, fail)
        with pytest.raises(wl.LedgerError):
            ledger.resolved(KEY, "first", outcome="CONFIRMED", cleanup=saved)
    assert values.read_text() == "synthetic"
    # No lifecycle lease was acquired or leaked by the event lock.
    lease = ledger.backend.lease(KEY)
    assert lease is not None
    lease.release()


def test_short_writes_complete_one_locked_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = _started(wl.FileLedger(tmp_path))
    write = wl.os.write
    with monkeypatch.context() as patch:
        patch.setattr(wl.os, "write", lambda fd, data: write(fd, data[:3]))
        ledger.resolved(KEY, "first", outcome="REFUTED")
    assert ledger.latest(KEY).state == "held"  # type: ignore[union-attr]
    assert len(ledger.backend.events(KEY)) == 2


def test_complete_json_in_torn_append_never_becomes_retry_permission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = _started(wl.FileLedger(tmp_path))
    write = wl.os.write
    calls = 0

    def tear(fd: int, data: bytes) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            return write(fd, data[:-1])
        raise OSError("synthetic final-byte failure")

    with monkeypatch.context() as patch:
        patch.setattr(wl.os, "write", tear)
        with pytest.raises(wl.LedgerError):
            ledger.resolved(KEY, "first", outcome="UNCERTAIN", retry_safe=True)
    assert ledger.latest(KEY).state == "open"  # type: ignore[union-attr]
    raw_before = (tmp_path / "entries" / (KEY + ".jsonl")).read_bytes()
    ledger.resolved(KEY, "first", outcome="REFUTED")
    assert (tmp_path / "entries" / (KEY + ".jsonl")).read_bytes().startswith(raw_before)
    assert len(ledger.backend.events(KEY)) == 2
    assert ledger.latest(KEY).state == "held"  # type: ignore[union-attr]


def test_legacy_explicit_release_is_workflow_not_absence_proof() -> None:
    raw = [
        {"event": "begun", "attempt": "legacy"},
        {
            "event": "resolved",
            "attempt": "legacy",
            "outcome": "REFUTED",
            "retry_safe": True,
        },
        {"event": "resolved", "attempt": "legacy", "outcome": "CONFIRMED"},
    ]
    held = wl.fold(raw)
    assert held is not None and held.state == "held"
    released = wl.fold(
        [*raw, {"event": "released", "attempt": "legacy", "by": "operator"}]
    )
    assert released is not None and released.state == "free"
    assert released.resolved is not None and released.resolved["outcome"] == "REFUTED"


@pytest.mark.parametrize("outcome", ["REFUTED", "UNCERTAIN"])
def test_current_positive_observation_does_not_release_prior_settlement(
    ledger: wl.Ledger,
    tmp_path: Path,
    outcome: str,
) -> None:
    saved = tmp_path / "retained-baseline"
    saved.mkdir()
    baseline = saved / "baseline.json"
    baseline.write_text("synthetic baseline")
    ledger.resolved(KEY, "first", outcome=outcome, retry_safe=False, review=True)
    before = ledger.latest(KEY)
    assert before is not None
    after = ledger.rechecked(
        KEY, "first", outcome="CONFIRMED", expected_revision=before.revision
    )
    assert after.events[-1]["applied"] is True
    assert after.events[-1]["outcome"] == "CONFIRMED"
    assert after.resolved == before.resolved
    assert after.state == before.state and after.state in {"held", "uncertain"}
    assert after.rechecked is None and not after.resolved["retry_safe"]
    assert ledger.attempts()[0][2] == before.resolved
    assert ledger.unresolved()[0][1].state == before.state
    assert baseline.read_text() == "synthetic baseline"
