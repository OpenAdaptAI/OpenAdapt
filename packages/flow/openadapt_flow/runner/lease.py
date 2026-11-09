"""Pure, transport-agnostic lease discipline for the runner client.

The merged cloud contract (``runners.ts``) gives leases these semantics:

* a poll CLAIMS the oldest queued dispatch and stamps ``lease_expires_at``
  (visibility timeout, ≤900 s);
* there is NO lease-renewal endpoint — nothing extends ``lease_expires_at``,
  and reclaim/uncertain-marking runs only inside the runner's OWN next poll;
* a lease that expires BEFORE the run started is silently re-offered;
* a lease that expires AFTER the run started marks the dispatch ``failed``
  and the run ``dispatch_uncertain_at`` — never a silent re-dispatch;
* late terminal evidence is still accepted (a run that finishes offline
  reports late rather than never), and a duplicate terminal is a no-op.

This module models the CLIENT half of that discipline as pure logic with an
injectable clock, so it can be unit-tested exhaustively and rebound to
whichever transport the contract revision lands on (the review requires
renewal-via-evidence-callback and a server-side reaper before any daemon
ships — see ``docs/design/RUNNER_CLIENT_LIBRARY.md``). ``renew()`` exists NOW
so the state machine already supports heartbeat-extends-lease semantics; on
the merged contract it is simply never driven.

Single-flight is enforced twice, both here:

* :class:`LeaseTracker` holds at most ONE lease per machine (the poll IS the
  claim, so a client must not poll while it holds work);
* :class:`WorkflowSerialization` refuses a second in-flight run of the same
  workflow (the dispatch route has no idempotency key — review S3 — so the
  double-click-Run case must be refused client-side).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Optional


class LeaseError(RuntimeError):
    """An operation was attempted from an illegal lease state."""


class LeasePhase(str, Enum):
    IDLE = "idle"
    #: Claimed but the governed run has not started; expiry here means the
    #: server re-offers the dispatch and the client must NOT start.
    LEASED = "leased"
    #: The run started; expiry here means the server marks it uncertain and
    #: the client must still report the true outcome (late is fine).
    STARTED = "started"


class CompletionDisposition(str, Enum):
    """How a terminal report will land server-side, judged client-side."""

    #: Lease still live: the terminal evidence acks and closes it normally.
    ACKS_LEASE = "acks_lease"
    #: Lease expired after start (long run, or the machine slept): the server
    #: has (or will) mark the dispatch uncertain; the terminal evidence is a
    #: LATE, honest report — expect ``duplicate_terminal``/no-op handling and
    #: contradictory dispatch-vs-run rows until the contract revision adds
    #: late-callback reconciliation.
    LATE_AFTER_LEASE_LOSS = "late_after_lease_loss"


class StartRefused(LeaseError):
    """The client must not start work under this lease (expired/stale)."""


@dataclass(frozen=True)
class SleepGap:
    """A detected wall-clock discontinuity (machine suspend/resume)."""

    gap_seconds: float
    lease_lost: bool


def _parse_iso_ts(raw: str) -> float:
    parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


class LeaseTracker:
    """Client-side mirror of ONE dispatch lease, with sleep detection.

    ``clock`` must be a WALL clock (``time.time``): suspend/resume shows up as
    a jump in wall time between ``tick()`` calls, which is exactly the signal
    the sleeping-laptop case needs (a monotonic clock hides it on some
    platforms by pausing during suspend).
    """

    #: A tick gap larger than this (beyond the caller's own tick interval)
    #: is reported as a sleep event.
    SLEEP_GAP_THRESHOLD_S = 30.0

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._phase = LeasePhase.IDLE
        self.dispatch_id: Optional[str] = None
        self.run_id: Optional[str] = None
        self._expires_at: Optional[float] = None
        self._last_tick: Optional[float] = None

    # -- introspection ------------------------------------------------------

    @property
    def phase(self) -> LeasePhase:
        return self._phase

    @property
    def held(self) -> bool:
        return self._phase is not LeasePhase.IDLE

    def remaining(self) -> float:
        """Seconds of lease left (negative = expired). 0 when idle."""
        if self._expires_at is None:
            return 0.0
        return self._expires_at - self._clock()

    def expired(self) -> bool:
        return self._expires_at is not None and self._clock() > self._expires_at

    # -- transitions --------------------------------------------------------

    def acquire(self, dispatch_id: str, run_id: str, lease_expires_at: str) -> None:
        """Record a freshly leased dispatch. One lease per machine, ever."""
        if self.held:
            raise LeaseError(
                "single-flight violation: a lease is already held "
                f"(dispatch {self.dispatch_id}); the client must not poll "
                "while it holds work"
            )
        try:
            expires = _parse_iso_ts(lease_expires_at)
        except ValueError as exc:
            raise LeaseError(f"unparseable lease_expires_at: {exc}") from exc
        self._phase = LeasePhase.LEASED
        self.dispatch_id = dispatch_id
        self.run_id = run_id
        self._expires_at = expires
        self._last_tick = self._clock()

    def mark_started(self) -> None:
        """The governed run is about to execute.

        Raises:
            StartRefused: the lease already expired — the server will (on our
                next poll) re-offer this dispatch as if never claimed, so
                starting now would race a future re-execution. Refuse.
        """
        if self._phase is not LeasePhase.LEASED:
            raise LeaseError(f"cannot start from phase {self._phase.value}")
        if self.expired():
            self._reset()
            raise StartRefused(
                "lease expired before the run started; the dispatch will be "
                "re-offered server-side — refusing to start stale work"
            )
        self._phase = LeasePhase.STARTED

    def renew(self, lease_expires_at: str) -> None:
        """Extend the lease (heartbeat-extends-lease semantics).

        The MERGED contract exposes no way to drive this — it exists so the
        state machine is already correct for the required contract revision
        (evidence callbacks extending the active lease). Renewing an expired
        lease is refused: once lost, only an honest late report remains.
        """
        if not self.held:
            raise LeaseError("no lease to renew")
        if self.expired():
            raise LeaseError("cannot renew an expired lease; report late instead")
        try:
            self._expires_at = _parse_iso_ts(lease_expires_at)
        except ValueError as exc:
            raise LeaseError(f"unparseable lease_expires_at: {exc}") from exc

    def tick(self) -> Optional[SleepGap]:
        """Advance the sleep detector; call at a steady cadence while held.

        Returns a :class:`SleepGap` when wall time jumped (suspend/resume),
        flagging whether the lease was lost during the gap.
        """
        now = self._clock()
        previous = self._last_tick
        self._last_tick = now
        if previous is None:
            return None
        gap = now - previous
        if gap < self.SLEEP_GAP_THRESHOLD_S:
            return None
        return SleepGap(gap_seconds=gap, lease_lost=self.expired())

    def completion_disposition(self) -> CompletionDisposition:
        """How the terminal report will land, given the lease state NOW."""
        if self._phase is not LeasePhase.STARTED:
            raise LeaseError(f"no started run to complete (phase {self._phase.value})")
        if self.expired():
            return CompletionDisposition.LATE_AFTER_LEASE_LOSS
        return CompletionDisposition.ACKS_LEASE

    def release(self) -> None:
        """Forget the lease after the terminal evidence is durably queued."""
        if not self.held:
            raise LeaseError("no lease to release")
        self._reset()

    def _reset(self) -> None:
        self._phase = LeasePhase.IDLE
        self.dispatch_id = None
        self.run_id = None
        self._expires_at = None
        self._last_tick = None


class WorkflowSerialization:
    """Per-workflow in-flight registry (client half of missing dispatch
    idempotency, review S3). Feed :func:`verify_dispatch`'s
    ``active_workflow_ids`` from :attr:`active`."""

    def __init__(self) -> None:
        self._active: set[str] = set()

    @property
    def active(self) -> set[str]:
        return set(self._active)

    def begin(self, workflow_id: str) -> bool:
        """Claim the workflow; False when a run is already in flight."""
        if workflow_id in self._active:
            return False
        self._active.add(workflow_id)
        return True

    def end(self, workflow_id: str) -> None:
        self._active.discard(workflow_id)


def server_reclaim_outcome(*, run_started: bool, lease_expired: bool) -> str:
    """Executable documentation of the SERVER's reclaim rule
    (``runners.ts mockReclaimAndExpire`` / ``leaseNextDispatch``), used by the
    tests to keep client expectations honest:

    * live lease → ``keep`` (still this runner's work);
    * expired before start → ``reoffer`` (silently queued again);
    * expired after start → ``uncertain`` (dispatch failed +
      ``dispatch_uncertain_at``; never silently re-dispatched).

    Note the merged contract only evaluates this inside the SAME runner's next
    poll — a machine that never returns leaves the run dangling (review E2;
    the required revision adds a server-side reaper).
    """
    if not lease_expired:
        return "keep"
    return "uncertain" if run_started else "reoffer"
