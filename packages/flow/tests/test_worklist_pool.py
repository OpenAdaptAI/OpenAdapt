"""Parallel worklist pool: one claim per item, one stop for all workers."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Callable, Optional

import pytest

from openadapt_flow.runtime.worklist_pool import (
    COMPLETED,
    ERROR,
    HALTED,
    POOL_REPORT_FILENAME,
    POOL_REPORT_MD,
    QUEUE_FILENAME,
    RECONCILIATION_REQUIRED,
    UNCERTAIN,
    VERIFIED,
    ItemRun,
    PoolPolicy,
    PoolRefusal,
    WorklistQueue,
    cookie_fingerprint,
    item_state_from_report,
    rows_fingerprint,
    run_worklist_pool,
)


class FakeWorker:
    """Runs items by calling ``behave(idx)``; records what it did."""

    def __init__(
        self,
        name: str,
        behave: Optional[Callable[[int], ItemRun]] = None,
        *,
        delay_s: float = 0.01,
        fingerprint: Optional[str] = None,
        fail_open: bool = False,
    ) -> None:
        self.name = name
        self.behave = behave or (lambda idx: ItemRun(state=VERIFIED))
        self.delay_s = delay_s
        self.fingerprint = fingerprint if fingerprint is not None else f"fp-{name}"
        self.fail_open = fail_open
        self.ran: list[int] = []
        self.opened = False
        self.closed = False
        self.thread: Optional[int] = None

    def open(self) -> None:
        self.thread = threading.get_ident()
        if self.fail_open:
            raise RuntimeError("bad password")
        self.opened = True

    def session_fingerprint(self) -> Optional[str]:
        return self.fingerprint

    def run_item(self, idx: int, row: dict[str, Any], item_dir: Path) -> ItemRun:
        assert threading.get_ident() == self.thread, "item ran off the worker thread"
        self.ran.append(idx)
        time.sleep(self.delay_s)
        return self.behave(idx)

    def close(self) -> None:
        self.closed = True


def _rows(n: int) -> list[dict[str, str]]:
    return [{"name": f"patient-{i}"} for i in range(n)]


def _policy(workers: int, **kw: Any) -> PoolPolicy:
    kw.setdefault("items_independent", True)
    return PoolPolicy(workers=workers, **kw)


def test_each_item_is_claimed_exactly_once(tmp_path: Path) -> None:
    workers = [FakeWorker(f"w{i}", delay_s=0.002) for i in range(8)]
    report = run_worklist_pool(_rows(200), workers, run_dir=tmp_path, policy=_policy(8))
    ran = [idx for w in workers for idx in w.ran]
    assert sorted(ran) == list(range(200))
    assert len(ran) == len(set(ran))
    assert report.success
    assert report.counts() == {VERIFIED: 200}
    # Work is actually spread: every worker ran something.
    assert all(w.ran for w in workers)
    assert all(w.closed for w in workers)
    saved = json.loads((tmp_path / POOL_REPORT_FILENAME).read_text())
    assert saved["counts"] == {VERIFIED: 200}
    assert {w["name"] for w in saved["per_worker"]} == {f"w{i}" for i in range(8)}
    assert sum(len(w["items"]) for w in saved["per_worker"]) == 200
    assert "## Per worker" in (tmp_path / POOL_REPORT_MD).read_text()


def test_forced_halt_on_one_worker_stops_the_others(tmp_path: Path) -> None:
    """A halt on worker w1 stops new claims on every worker; in-flight items finish."""

    halted_idx: list[int] = []
    w1_items: list[int] = []

    def halting(idx: int) -> ItemRun:
        w1_items.append(idx)
        if len(w1_items) == 2:  # w1's second item halts, whatever its index
            halted_idx.append(idx)
            return ItemRun(
                state=HALTED,
                detail={
                    "halt_reason": "identity check failed",
                    "transaction_outcome": "halted_before_effect",
                },
            )
        return ItemRun(state=VERIFIED)

    w1 = FakeWorker("w1", halting, delay_s=0.01)
    peers = [FakeWorker(f"w{i}", delay_s=0.03) for i in (2, 3, 4)]
    report = run_worklist_pool(
        _rows(100), [w1, *peers], run_dir=tmp_path, policy=_policy(4)
    )

    assert not report.success
    assert report.stop is not None
    assert report.stop["worker"] == "w1" and report.stop["idx"] == halted_idx[0]
    assert "identity check failed" in report.stop["reason"]
    states = {item["idx"]: item["state"] for item in report.items}
    assert states[halted_idx[0]] == HALTED
    # Far fewer than 100 items ran: claims stopped everywhere.
    ran = [idx for w in (w1, *peers) for idx in w.ran]
    assert len(ran) < 40
    # Every item that ran, including peers' in-flight ones, has a final
    # result; nothing is left "claimed".
    for idx in ran:
        assert states[idx] in {VERIFIED, HALTED}
    assert "pending" in states.values()
    # No worker claimed anything after the stop flag was set.
    stop_at = report.stop["at"]
    for item in report.items:
        if item["claimed_at"] is not None:
            assert item["claimed_at"] <= stop_at + 0.001


def test_reconciliation_required_stops_even_when_skipping_halts(tmp_path: Path) -> None:
    def behave(idx: int) -> ItemRun:
        if idx == 0:
            return ItemRun(
                state=HALTED, detail={"transaction_outcome": "HALTED_BEFORE_EFFECT"}
            )
        if idx == 3:
            return ItemRun(
                state=RECONCILIATION_REQUIRED,
                detail={"halt_reason": "write not confirmed"},
            )
        return ItemRun(state=VERIFIED)

    w = FakeWorker("w1", behave)
    report = run_worklist_pool(
        _rows(10), [w], run_dir=tmp_path, policy=_policy(1, on_halt="skip_unwritten")
    )
    # Item 0 halted before any write and was skipped; item 3 stopped the run.
    assert w.ran == [0, 1, 2, 3]
    assert report.stop is not None and report.stop["idx"] == 3


def test_worker_exception_is_an_error_and_stops(tmp_path: Path) -> None:
    def boom(idx: int) -> ItemRun:
        if idx == 2:
            raise RuntimeError("browser crashed")
        return ItemRun(state=COMPLETED)

    report = run_worklist_pool(
        _rows(10), [FakeWorker("w1", boom)], run_dir=tmp_path, policy=_policy(1)
    )
    states = {item["idx"]: item["state"] for item in report.items}
    assert states[2] == ERROR
    assert report.stop is not None and report.stop["idx"] == 2
    assert states[3] == "pending"


def test_refuses_more_workers_than_the_app_allows(tmp_path: Path) -> None:
    workers = [FakeWorker(f"w{i}") for i in range(4)]
    with pytest.raises(PoolRefusal, match="limit of 2"):
        run_worklist_pool(
            _rows(4), workers, run_dir=tmp_path, policy=_policy(4, max_workers=2)
        )
    assert not any(w.opened for w in workers)
    assert not (tmp_path / QUEUE_FILENAME).exists()


def test_refuses_parallel_items_not_declared_independent(tmp_path: Path) -> None:
    with pytest.raises(PoolRefusal, match="items_independent"):
        run_worklist_pool(
            _rows(4),
            [FakeWorker("a"), FakeWorker("b")],
            run_dir=tmp_path,
            policy=PoolPolicy(workers=2),
        )
    with pytest.raises(PoolRefusal, match="items_independent"):
        run_worklist_pool(
            _rows(4),
            [FakeWorker("a")],
            run_dir=tmp_path,
            policy=PoolPolicy(workers=1, readback_window=1),
        )


def test_refuses_workers_that_share_one_session(tmp_path: Path) -> None:
    a = FakeWorker("a", fingerprint="same")
    b = FakeWorker("b", fingerprint="same")
    with pytest.raises(PoolRefusal, match="share one signed-in session"):
        run_worklist_pool(_rows(4), [a, b], run_dir=tmp_path, policy=_policy(2))
    assert a.ran == [] and b.ran == []
    assert a.closed and b.closed


def test_sign_in_failure_runs_nothing(tmp_path: Path) -> None:
    a = FakeWorker("a")
    b = FakeWorker("b", fail_open=True)
    with pytest.raises(PoolRefusal, match="sign-in failed for b"):
        run_worklist_pool(_rows(4), [a, b], run_dir=tmp_path, policy=_policy(2))
    assert a.ran == []


def test_reopen_marks_claimed_items_uncertain_and_never_reclaims(
    tmp_path: Path,
) -> None:
    rows = _rows(3)
    fp = rows_fingerprint(rows)
    q = WorklistQueue.open(tmp_path / QUEUE_FILENAME, rows, fingerprint=fp)
    assert q.claim("w1") == (0, rows[0])
    # Crash: item 0 was claimed but never finished. Reopen for a resume.
    q2 = WorklistQueue.open(tmp_path / QUEUE_FILENAME, rows, fingerprint=fp)
    assert q2.items()[0]["state"] == UNCERTAIN
    assert q2.claim("w2") == (1, rows[1])
    with pytest.raises(PoolRefusal, match="different worklist"):
        WorklistQueue.open(
            tmp_path / QUEUE_FILENAME, _rows(4), fingerprint=rows_fingerprint(_rows(4))
        )


def test_only_the_claiming_worker_records_a_result(tmp_path: Path) -> None:
    rows = _rows(1)
    q = WorklistQueue.open(
        tmp_path / QUEUE_FILENAME, rows, fingerprint=rows_fingerprint(rows)
    )
    q.claim("w1")
    with pytest.raises(RuntimeError, match="not claimed by w2"):
        q.set_state(0, "w2", VERIFIED, {})


class PipelinedWorker(FakeWorker):
    """Read-back runs on a side thread and resolves after ``readback_s``."""

    def __init__(
        self, name: str, fail_idx: Optional[int], readback_s: float, **kw: Any
    ) -> None:
        super().__init__(name, **kw)
        self.fail_idx = fail_idx
        self.readback_s = readback_s
        self.max_outstanding = 0
        self._outstanding = 0
        self._lock = threading.Lock()

    def run_item(self, idx: int, row: dict[str, Any], item_dir: Path) -> ItemRun:
        self.ran.append(idx)
        time.sleep(self.delay_s)
        fut: Future = Future()
        with self._lock:
            self._outstanding += 1
            self.max_outstanding = max(self.max_outstanding, self._outstanding)

        def readback() -> None:
            time.sleep(self.readback_s)
            with self._lock:
                self._outstanding -= 1
            if idx == self.fail_idx:
                fut.set_result(
                    (RECONCILIATION_REQUIRED, {"halt_reason": "saved record differs"})
                )
            else:
                fut.set_result((VERIFIED, {}))

        threading.Thread(target=readback, daemon=True).start()
        return ItemRun(state=VERIFIED, pending_readback=fut)


@pytest.mark.parametrize("window", [1, 2])
def test_pipelined_readback_failure_names_items_written_after(
    tmp_path: Path, window: int
) -> None:
    w = PipelinedWorker("w1", fail_idx=3, readback_s=0.05, delay_s=0.01)
    report = run_worklist_pool(
        _rows(20), [w], run_dir=tmp_path, policy=_policy(1, readback_window=window)
    )
    assert report.stop is not None and report.stop["idx"] == 3
    after = report.written_after_failed_readback["3"]
    assert 1 <= len(after) <= window
    assert after == list(range(4, 4 + len(after)))
    # At most `window` read-backs wait while the next item is written.
    assert w.max_outstanding <= window + 1
    states = {item["idx"]: item["state"] for item in report.items}
    assert states[3] == RECONCILIATION_REQUIRED
    # The items written after it still got their own read-back.
    for idx in after:
        assert states[idx] == VERIFIED
    assert max(w.ran) <= 3 + window
    md = (tmp_path / POOL_REPORT_MD).read_text()
    assert "Read-back of item 3 failed" in md


def test_window_zero_waits_for_each_readback(tmp_path: Path) -> None:
    w = PipelinedWorker("w1", fail_idx=None, readback_s=0.01, delay_s=0.0)
    report = run_worklist_pool(_rows(5), [w], run_dir=tmp_path, policy=_policy(1))
    assert report.success
    assert w.max_outstanding == 1


def test_item_state_from_report() -> None:
    class R:
        def __init__(self, **kw: Any) -> None:
            self.__dict__.update(kw)

    assert (
        item_state_from_report(
            R(transaction_outcome="reconciliation_required", execution_outcome="halted")
        )
        == RECONCILIATION_REQUIRED
    )
    assert (
        item_state_from_report(
            R(transaction_outcome="RECONCILIATION_REQUIRED", execution_outcome="HALTED")
        )
        == RECONCILIATION_REQUIRED
    )
    assert item_state_from_report(R(execution_outcome="verified")) == VERIFIED
    assert (
        item_state_from_report(R(execution_outcome="completed_unverified")) == COMPLETED
    )
    assert item_state_from_report(R(execution_outcome="halted")) == HALTED
    assert item_state_from_report(R(success=True)) == COMPLETED
    assert item_state_from_report(R(success=False)) == HALTED


def test_cookie_fingerprint_tells_sessions_apart() -> None:
    a = [{"domain": "emr.test", "path": "/", "name": "OpenEMR", "value": "s1"}]
    b = [{"domain": "emr.test", "path": "/", "name": "OpenEMR", "value": "s2"}]
    assert cookie_fingerprint(a) != cookie_fingerprint(b)
    assert cookie_fingerprint(a) == cookie_fingerprint(list(a))
    assert cookie_fingerprint([]) is None
