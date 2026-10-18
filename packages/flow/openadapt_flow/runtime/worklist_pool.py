"""Parallel worklist runs: N workers, one signed-in session each.

A worklist run with ``--workers N`` replays the same compiled program once per
worklist item, spread over N workers. Every item is still a complete governed
run with every check (identity, input read-back, postconditions, the
system-of-record read-back). Only the scheduling changes.

Rules this module enforces:

- **One claim per item.** Items live in a durable SQLite queue in the run
  directory. A worker claims the next pending item in one ``BEGIN IMMEDIATE``
  transaction, so no two workers can hold the same item, and an item that was
  claimed is never handed out again, even after a crash. A claimed item with
  no recorded result after a restart becomes ``uncertain`` and needs a
  person: something may have been written.
- **One stop for all.** A halt, a failure, an error, or a
  RECONCILIATION_REQUIRED outcome on any worker sets a stop flag in the same
  database. Every worker checks it before each claim, so no new item starts.
  Items already in flight finish their own checks.
- **Pipelined read-back is bounded.** With a read-back window ``k > 0`` a
  worker may start its next item while up to ``k`` earlier items still wait
  for their read-back. When a read-back fails the run stops, and the report
  names the items (at most ``k`` per worker) that the same worker wrote after
  the failed one.

The pool does not know how to sign in or replay; :class:`PoolWorker` objects
do. The CLI builds web workers (one browser context and one account each), and
tests use fakes.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from collections import deque
from concurrent.futures import Future
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Protocol, Sequence

QUEUE_FILENAME = "worklist_queue.sqlite3"
POOL_REPORT_FILENAME = "worklist_pool.json"
POOL_REPORT_MD = "WORKLIST.md"

#: Item states. ``pending`` and ``claimed`` are live; the rest are final.
PENDING = "pending"
CLAIMED = "claimed"
VERIFIED = "verified"
COMPLETED = "completed"  # finished, effects not certified (no production profile)
HALTED = "halted"
FAILED = "failed"
RECONCILIATION_REQUIRED = "reconciliation_required"
ERROR = "error"  # the worker raised; whether anything was written is unknown
UNCERTAIN = "uncertain"  # claimed before a crash, no result recorded
READBACK_PENDING = "readback_pending"

FINAL_STATES = frozenset(
    {VERIFIED, COMPLETED, HALTED, FAILED, RECONCILIATION_REQUIRED, ERROR, UNCERTAIN}
)
SUCCESS_STATES = frozenset({VERIFIED, COMPLETED})

#: ``on_halt`` policies. ``stop`` (default): any non-success item stops new
#: claims everywhere. ``skip_unwritten``: an item that halted before any write
#: (transaction outcome HALTED_BEFORE_EFFECT) is recorded and the run goes on;
#: every other non-success outcome still stops the run.
ON_HALT_STOP = "stop"
ON_HALT_SKIP_UNWRITTEN = "skip_unwritten"
ON_HALT_POLICIES = (ON_HALT_STOP, ON_HALT_SKIP_UNWRITTEN)


class PoolRefusal(ValueError):
    """The pool configuration is unsafe; nothing was executed."""


def rows_fingerprint(rows: Sequence[Mapping[str, Any]], *, salt: str = "") -> str:
    """Stable digest of the worklist, so a resume can't swap the items."""

    digest = hashlib.sha256(salt.encode())
    for row in rows:
        digest.update(json.dumps(row, sort_keys=True, separators=(",", ":")).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def _now() -> float:
    return time.time()


class WorklistQueue:
    """Durable, exactly-once item queue shared by every worker.

    Each call opens its own SQLite connection, so worker threads never share
    one. Claims use ``BEGIN IMMEDIATE`` (one writer at a time).
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    # -- setup ------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=60.0, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    @classmethod
    def open(
        cls,
        path: Path,
        rows: Sequence[Mapping[str, Any]],
        *,
        fingerprint: str,
    ) -> "WorklistQueue":
        """Create the queue, or reopen one for the same worklist.

        Reopening marks every item still ``claimed`` as ``uncertain`` (its
        worker died mid-item) and keeps any stop flag. A queue made for a
        different worklist is refused.
        """

        queue = cls(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = queue._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS items ("
                " idx INTEGER PRIMARY KEY,"
                " row_json TEXT NOT NULL,"
                " state TEXT NOT NULL,"
                " worker TEXT,"
                " claimed_at REAL,"
                " finished_at REAL,"
                " result_json TEXT)"
            )
            found = conn.execute(
                "SELECT value FROM meta WHERE key='fingerprint'"
            ).fetchone()
            if found is None:
                conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('fingerprint', ?)",
                    (fingerprint,),
                )
                conn.executemany(
                    "INSERT INTO items (idx, row_json, state) VALUES (?, ?, ?)",
                    [
                        (i, json.dumps(dict(row), sort_keys=True), PENDING)
                        for i, row in enumerate(rows)
                    ],
                )
            elif found[0] != fingerprint:
                conn.execute("ROLLBACK")
                raise PoolRefusal(
                    f"{path} belongs to a different worklist; use a new run directory"
                )
            else:
                conn.execute(
                    "UPDATE items SET state=?, finished_at=? WHERE state IN (?, ?)",
                    (UNCERTAIN, _now(), CLAIMED, READBACK_PENDING),
                )
            conn.execute("COMMIT")
        finally:
            conn.close()
        return queue

    # -- claims -----------------------------------------------------------

    def claim(self, worker: str) -> Optional[tuple[int, dict[str, Any]]]:
        """Claim the next pending item, or ``None`` when stopped or empty."""

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM meta WHERE key='stop'").fetchone():
                conn.execute("ROLLBACK")
                return None
            found = conn.execute(
                "SELECT idx, row_json FROM items WHERE state=? ORDER BY idx LIMIT 1",
                (PENDING,),
            ).fetchone()
            if found is None:
                conn.execute("ROLLBACK")
                return None
            idx, row_json = found
            changed = conn.execute(
                "UPDATE items SET state=?, worker=?, claimed_at=? "
                "WHERE idx=? AND state=?",
                (CLAIMED, worker, _now(), idx, PENDING),
            ).rowcount
            if changed != 1:  # pragma: no cover - guarded by BEGIN IMMEDIATE
                conn.execute("ROLLBACK")
                raise RuntimeError(f"item {idx} claim raced")
            conn.execute("COMMIT")
            return int(idx), json.loads(row_json)
        finally:
            conn.close()

    def set_state(
        self, idx: int, worker: str, state: str, result: Mapping[str, Any]
    ) -> None:
        """Record an item's state; only the worker holding the claim may."""

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                "UPDATE items SET state=?, finished_at=?, result_json=? "
                "WHERE idx=? AND worker=? AND state IN (?, ?)",
                (
                    state,
                    _now() if state in FINAL_STATES else None,
                    json.dumps(dict(result), sort_keys=True, default=str),
                    idx,
                    worker,
                    CLAIMED,
                    READBACK_PENDING,
                ),
            ).rowcount
            conn.execute("COMMIT")
        finally:
            conn.close()
        if changed != 1:
            raise RuntimeError(f"item {idx} is not claimed by {worker}")

    # -- stop flag --------------------------------------------------------

    def request_stop(self, reason: str, *, worker: str, idx: Optional[int]) -> None:
        """Stop new claims on every worker. The first reason wins."""

        conn = self._connect()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES ('stop', ?)",
                (
                    json.dumps(
                        {"reason": reason, "worker": worker, "idx": idx, "at": _now()}
                    ),
                ),
            )
        finally:
            conn.close()

    def stop_reason(self) -> Optional[dict[str, Any]]:
        conn = self._connect()
        try:
            found = conn.execute("SELECT value FROM meta WHERE key='stop'").fetchone()
        finally:
            conn.close()
        return json.loads(found[0]) if found else None

    # -- reading ----------------------------------------------------------

    def items(self) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT idx, state, worker, claimed_at, finished_at, result_json "
                "FROM items ORDER BY idx"
            ).fetchall()
        finally:
            conn.close()
        return [
            {
                "idx": r[0],
                "state": r[1],
                "worker": r[2],
                "claimed_at": r[3],
                "finished_at": r[4],
                "result": json.loads(r[5]) if r[5] else None,
            }
            for r in rows
        ]


@dataclass
class ItemRun:
    """What a worker hands back for one item.

    ``state`` is the item's state as far as the worker knows. With pipelined
    read-back, ``pending_readback`` is a future that resolves to the final
    ``(state, detail)`` once the read-back finishes; until then the item is
    ``readback_pending``.
    """

    state: str
    detail: dict[str, Any] = field(default_factory=dict)
    pending_readback: Optional["Future[tuple[str, dict[str, Any]]]"] = None


class PoolWorker(Protocol):
    """One worker: its own signed-in session, used by one thread only."""

    name: str

    def open(self) -> None:
        """Start the session (browser, sign-in). Runs on the worker thread."""

    def session_fingerprint(self) -> Optional[str]:
        """A digest of the session's credentials (e.g. its cookies), or None.

        The pool refuses to run when two workers report the same digest:
        they would share one signed-in session.
        """

    def run_item(self, idx: int, row: dict[str, Any], item_dir: Path) -> ItemRun:
        """Replay one item. Runs on the worker thread."""

    def close(self) -> None:
        """End the session. Runs on the worker thread."""


@dataclass(frozen=True)
class PoolPolicy:
    workers: int
    readback_window: int = 0
    on_halt: str = ON_HALT_STOP
    items_independent: bool = False
    #: The app's declared concurrency limit (sessions at once).
    max_workers: Optional[int] = None

    def refusal(self) -> Optional[str]:
        if self.workers < 1:
            return "--workers must be at least 1"
        if self.max_workers is not None and self.workers > self.max_workers:
            return (
                f"--workers {self.workers} is above the deployment's limit of "
                f"{self.max_workers} concurrent sessions for this app "
                "(worklist.max_workers)"
            )
        if self.workers > 1 and not self.items_independent:
            return (
                "--workers above 1 runs items out of order and at the same "
                "time; the deployment must declare worklist.items_independent: "
                "true"
            )
        if self.readback_window < 0:
            return "the read-back window must be 0 or more"
        if self.readback_window > 0 and not self.items_independent:
            return (
                "a read-back window above 0 starts the next item before the "
                "last read-back finishes; the deployment must declare "
                "worklist.items_independent: true"
            )
        if self.on_halt not in ON_HALT_POLICIES:
            return f"worklist.on_halt must be one of {', '.join(ON_HALT_POLICIES)}"
        return None


def item_state_from_report(report: Any) -> str:
    """Map a finished item's RunReport to a pool item state."""

    transaction = getattr(report, "transaction_outcome", None)
    transaction = str(getattr(transaction, "value", transaction) or "").lower()
    if transaction == "reconciliation_required":
        return RECONCILIATION_REQUIRED
    outcome = getattr(report, "execution_outcome", None)
    outcome = getattr(outcome, "value", outcome)
    if outcome is not None:
        return {
            "verified": VERIFIED,
            "completed_unverified": COMPLETED,
            "halted": HALTED,
            "rolled_back": FAILED,
            "failed": FAILED,
        }.get(str(outcome).lower(), FAILED)
    return COMPLETED if getattr(report, "success", False) else HALTED


def item_detail_from_report(report: Any) -> dict[str, Any]:
    def _v(x: Any) -> Any:
        return getattr(x, "value", x)

    halt = getattr(report, "halt", None)
    return {
        "success": bool(getattr(report, "success", False)),
        "execution_outcome": _v(getattr(report, "execution_outcome", None)),
        "transaction_outcome": _v(getattr(report, "transaction_outcome", None)),
        "halt_reason": getattr(halt, "reason", None) if halt is not None else None,
        "total_ms": getattr(report, "total_ms", None),
    }


def _stops_run(state: str, detail: Mapping[str, Any], on_halt: str) -> bool:
    if state in SUCCESS_STATES:
        return False
    if (
        on_halt == ON_HALT_SKIP_UNWRITTEN
        and state == HALTED
        and str(detail.get("transaction_outcome") or "").lower()
        == "halted_before_effect"
    ):
        return False
    return True


@dataclass
class WorkerSummary:
    name: str
    items: list[int] = field(default_factory=list)
    states: dict[str, int] = field(default_factory=dict)
    busy_s: float = 0.0
    open_s: float = 0.0
    error: Optional[str] = None


@dataclass
class PoolReport:
    workers: int
    readback_window: int
    on_halt: str
    started_at: float
    ended_at: float
    stop: Optional[dict[str, Any]]
    items: list[dict[str, Any]]
    per_worker: list[WorkerSummary]
    written_after_failed_readback: dict[str, list[int]] = field(default_factory=dict)

    @property
    def wall_s(self) -> float:
        return self.ended_at - self.started_at

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for item in self.items:
            out[item["state"]] = out.get(item["state"], 0) + 1
        return out

    @property
    def success(self) -> bool:
        return self.stop is None and all(
            item["state"] in SUCCESS_STATES for item in self.items
        )

    def entries_per_hour(self) -> Optional[float]:
        done = sum(1 for item in self.items if item["state"] in SUCCESS_STATES)
        return round(done * 3600.0 / self.wall_s, 1) if self.wall_s > 0 else None

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": "openadapt.worklist_pool/1",
            "workers": self.workers,
            "readback_window": self.readback_window,
            "on_halt": self.on_halt,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "wall_s": round(self.wall_s, 3),
            "success": self.success,
            "counts": self.counts(),
            "entries_per_hour": self.entries_per_hour(),
            "stop": self.stop,
            "written_after_failed_readback": self.written_after_failed_readback,
            "per_worker": [asdict(w) for w in self.per_worker],
            "items": self.items,
        }

    def render_md(self) -> str:
        lines = [
            "# Worklist run",
            "",
            f"- Workers: {self.workers}; read-back window: {self.readback_window}; "
            f"on halt: {self.on_halt}",
            f"- Wall time: {self.wall_s:.1f} s; finished correctly: "
            f"{sum(1 for i in self.items if i['state'] in SUCCESS_STATES)} of "
            f"{len(self.items)}",
        ]
        if self.stop:
            where = (
                f"item {self.stop.get('idx')}"
                if self.stop.get("idx") is not None
                else "the pool"
            )
            lines.append(
                f"- **Stopped** by {self.stop.get('worker')} at {where}: "
                f"{self.stop.get('reason')}"
            )
        for idx, after in sorted(self.written_after_failed_readback.items()):
            lines.append(
                f"- Read-back of item {idx} failed. Items written after it by "
                f"the same worker: {', '.join(map(str, after)) or 'none'}"
            )
        lines += [
            "",
            "## Per worker",
            "",
            "| Worker | Items | Results | Busy (s) | Sign-in (s) |",
            "|---|---:|---|---:|---:|",
        ]
        for w in self.per_worker:
            states = ", ".join(f"{k} {v}" for k, v in sorted(w.states.items())) or "-"
            if w.error:
                states += f" (worker error: {w.error})"
            lines.append(
                f"| {w.name} | {len(w.items)} | {states} | {w.busy_s:.1f} | {w.open_s:.1f} |"
            )
        lines += [
            "",
            "## Per item",
            "",
            "| Item | Worker | State | Seconds |",
            "|---:|---|---|---:|",
        ]
        for item in self.items:
            res = item.get("result") or {}
            secs = res.get("wall_s")
            shown = f"{secs:.1f}" if isinstance(secs, (int, float)) else "-"
            lines.append(
                f"| {item['idx']} | {item.get('worker') or '-'} | {item['state']} | {shown} |"
            )
        return "\n".join(lines) + "\n"

    def save(self, run_dir: Path) -> Path:
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / POOL_REPORT_FILENAME
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_json(), indent=1, default=str))
        tmp.replace(path)
        (run_dir / POOL_REPORT_MD).write_text(self.render_md())
        return path


def run_worklist_pool(
    rows: Sequence[Mapping[str, Any]],
    workers: Sequence[PoolWorker],
    *,
    run_dir: Path,
    policy: PoolPolicy,
    fingerprint_salt: str = "",
    on_event: Optional[Callable[[str], None]] = None,
) -> PoolReport:
    """Run every row on the pool and return the report (also saved).

    Raises :class:`PoolRefusal` before anything runs when the policy is
    unsafe, the number of workers differs from ``policy.workers``, or two
    workers share a session.
    """

    refusal = policy.refusal()
    if refusal is None and len(workers) != policy.workers:
        refusal = f"{policy.workers} workers requested but {len(workers)} configured"
    names = [w.name for w in workers]
    if refusal is None and len(set(names)) != len(names):
        refusal = "worker names must be distinct"
    if refusal is not None:
        raise PoolRefusal(refusal)

    say = on_event or (lambda _msg: None)
    run_dir = Path(run_dir)
    queue = WorklistQueue.open(
        run_dir / QUEUE_FILENAME,
        rows,
        fingerprint=rows_fingerprint(rows, salt=fingerprint_salt),
    )
    started = _now()
    summaries = {w.name: WorkerSummary(name=w.name) for w in workers}
    written_after: dict[str, list[int]] = {}
    lock = threading.Lock()

    # Phase 1: every worker opens its own session; the pool then checks that
    # no two share one before any item is claimed.
    opened = threading.Barrier(len(workers) + 1)
    go = threading.Event()
    abort = threading.Event()
    fingerprints: dict[str, Optional[str]] = {}

    def stop(reason: str, worker: str, idx: Optional[int]) -> None:
        queue.request_stop(reason, worker=worker, idx=idx)
        say(f"STOP ({worker}, item {idx}): {reason}")

    def finish(
        worker: PoolWorker, idx: int, state: str, detail: dict[str, Any]
    ) -> None:
        queue.set_state(idx, worker.name, state, detail)
        with lock:
            s = summaries[worker.name]
            s.states[state] = s.states.get(state, 0) + 1
        say(f"{worker.name}: item {idx} {state}")
        if _stops_run(state, detail, policy.on_halt):
            stop(
                detail.get("halt_reason") or f"item {idx} ended {state}",
                worker.name,
                idx,
            )

    def worker_main(worker: PoolWorker) -> None:
        summary = summaries[worker.name]
        t0 = time.monotonic()
        try:
            worker.open()
            fingerprints[worker.name] = worker.session_fingerprint()
        except Exception as exc:  # noqa: BLE001 - recorded, run aborted
            summary.error = f"sign-in failed: {type(exc).__name__}"
            abort.set()
        summary.open_s = time.monotonic() - t0
        try:
            opened.wait()
        except threading.BrokenBarrierError:  # pragma: no cover
            abort.set()
        go.wait()
        pending: deque[tuple[int, Future, list[int]]] = deque()

        def settle(entry: tuple[int, Future, list[int]]) -> None:
            idx, fut, after = entry
            try:
                state, detail = fut.result()
            except Exception as exc:  # noqa: BLE001
                state, detail = (
                    ERROR,
                    {"halt_reason": f"read-back raised {type(exc).__name__}"},
                )
            if state not in SUCCESS_STATES:
                with lock:
                    written_after[str(idx)] = list(after)
                detail = {**detail, "written_after": list(after)}
            finish(worker, idx, state, detail)

        try:
            if abort.is_set():
                return
            while True:
                while len(pending) > policy.readback_window:
                    settle(pending.popleft())
                claimed = queue.claim(worker.name)
                if claimed is None:
                    break
                idx, row = claimed
                summary.items.append(idx)
                for entry in pending:
                    entry[2].append(idx)
                item_dir = run_dir / "items" / f"{idx:05d}"
                t_item = time.monotonic()
                try:
                    out = worker.run_item(idx, row, item_dir)
                except Exception as exc:  # noqa: BLE001 - fail closed
                    out = ItemRun(
                        state=ERROR,
                        detail={
                            "halt_reason": f"worker raised {type(exc).__name__}: {exc}"[
                                :300
                            ]
                        },
                    )
                wall = time.monotonic() - t_item
                summary.busy_s += wall
                detail = {**out.detail, "wall_s": round(wall, 3), "worker": worker.name}
                if out.pending_readback is not None:
                    # The final state comes from the read-back, whatever the
                    # report said when the item returned.
                    queue.set_state(idx, worker.name, READBACK_PENDING, detail)
                    fut: Future = Future()

                    def _chain(
                        src: Future, dst: Future = fut, base: dict = detail
                    ) -> None:
                        try:
                            st, more = src.result()
                            dst.set_result(
                                (st, {**base, **more, "readback": "pipelined"})
                            )
                        except Exception as exc:  # noqa: BLE001
                            dst.set_exception(exc)

                    out.pending_readback.add_done_callback(_chain)
                    pending.append((idx, fut, []))
                else:
                    finish(worker, idx, out.state, detail)
            while pending:
                settle(pending.popleft())
        except Exception as exc:  # noqa: BLE001 - never leave peers running blind
            summary.error = f"{type(exc).__name__}: {exc}"[:300]
            stop(f"worker {worker.name} crashed", worker.name, None)
            while pending:
                settle(pending.popleft())
        finally:
            try:
                worker.close()
            except Exception:  # noqa: BLE001
                pass

    threads = [
        threading.Thread(
            target=worker_main, args=(w,), name=f"worklist-{w.name}", daemon=True
        )
        for w in workers
    ]
    for t in threads:
        t.start()
    opened.wait()
    shared = _shared_sessions(fingerprints)
    if shared:
        abort.set()
        go.set()
        for t in threads:
            t.join()
        raise PoolRefusal(
            "workers "
            + " and ".join(shared)
            + " share one signed-in session; each worker needs its own account"
        )
    if abort.is_set():
        go.set()
        for t in threads:
            t.join()
        failed = [s.name for s in summaries.values() if s.error]
        raise PoolRefusal(
            f"sign-in failed for {', '.join(failed)}; nothing was executed"
        )
    go.set()
    for t in threads:
        t.join()

    report = PoolReport(
        workers=policy.workers,
        readback_window=policy.readback_window,
        on_halt=policy.on_halt,
        started_at=started,
        ended_at=_now(),
        stop=queue.stop_reason(),
        items=queue.items(),
        per_worker=list(summaries.values()),
        written_after_failed_readback=written_after,
    )
    report.save(run_dir)
    return report


def _shared_sessions(fingerprints: Mapping[str, Optional[str]]) -> list[str]:
    seen: dict[str, str] = {}
    for name, fp in fingerprints.items():
        if fp is None:
            continue
        if fp in seen:
            return [seen[fp], name]
        seen[fp] = name
    return []


def cookie_fingerprint(cookies: Iterable[Mapping[str, Any]]) -> Optional[str]:
    """Digest of a browser context's cookies (names, domains and values).

    Two contexts signed in as different users have different session cookie
    values; two that share one session have the same ones. Returns None when
    there are no cookies (nothing to compare).
    """

    parts = sorted(
        f"{c.get('domain', '')}\t{c.get('path', '')}\t{c.get('name', '')}\t{c.get('value', '')}"
        for c in cookies
    )
    if not parts:
        return None
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()
