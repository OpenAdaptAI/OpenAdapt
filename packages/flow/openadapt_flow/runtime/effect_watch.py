"""Watch an interruption's dismissal for any write, on any backend.

An interruption (a session warning, a banner, a tour, an "Update available"
toast) may be cleared automatically only when clearing it writes nothing
(``docs/INTERRUPTION_RECOVERY.md``). A label can't prove that: "Continue"
on a session warning and "Continue" on a wizard look the same. What the
dismissal did can. :func:`watch_effects` runs the dismissal under every
watcher the run has, and the replayer halts when any of them saw a write or
none could watch:

- **network**: a backend with ``write_fence()`` (the browser) aborts every
  write request, WebSocket, worker and socket message before it leaves the
  page, and reports each one;
- **record**: the run's effect verifier (the system-of-record read-back the
  steps' writes are checked with) is read before and after; any difference
  in its records is a write. This works on every backend, desktop and
  RDP/Citrix included, because it never looks at the screen.

A watcher that can't go up, or a read-back that can't be read, is a refusal:
the caller treats it as "no proof" and halts.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional


@dataclass
class EffectWatchReport:
    """What the watchers saw around one dismissal."""

    #: The watchers that were up: ``"network"``, ``"record"``.
    watchers: list[str] = field(default_factory=list)
    #: Why the watch could not prove anything. Set means "don't act".
    refusal: Optional[str] = None
    #: Each write a watcher saw.
    attempts: list[str] = field(default_factory=list)
    #: Every request the network watcher logged (``METHOD /path``), reads
    #: included. A write among them that no attempt accounts for is added to
    #: ``attempts`` (:func:`reconcile_requests`).
    requests: list[str] = field(default_factory=list)
    #: The system-of-record digest before and after, when it was read.
    record_before: Optional[str] = None
    record_after: Optional[str] = None

    @property
    def held(self) -> bool:
        """At least one watcher was up and none of them saw a write."""

        return bool(self.watchers) and self.refusal is None and not self.attempts


def record_digest(verifier: Any) -> Optional[str]:
    """A digest of the system-of-record records ``verifier`` reads, or None
    when they could not be read."""

    try:
        state = verifier.capture_pre_state()
    except Exception:  # noqa: BLE001 - unreadable is no proof
        return None
    if not getattr(state, "reachable", False):
        return None
    try:
        records = sorted(
            json.dumps(record, sort_keys=True, default=str) for record in state.records
        )
    except Exception:  # noqa: BLE001
        return None
    return hashlib.sha256("\n".join(records).encode()).hexdigest()


@contextmanager
def watch_effects(
    backend: Any, effect_verifier: Any = None
) -> Iterator[EffectWatchReport]:
    """Run the ``with`` body under every write watcher available.

    Check ``report.refusal`` before acting inside the body; after the body,
    ``report.held`` says whether the dismissal is proven write-free.
    """

    report = EffectWatchReport()
    with ExitStack() as stack:
        fence_opener = getattr(backend, "write_fence", None)
        fence: Any = None
        if callable(fence_opener):
            try:
                fence = stack.enter_context(fence_opener())
            except Exception as exc:  # noqa: BLE001 - no fence, no proof
                report.refusal = (
                    f"the write fence failed to go up ({type(exc).__name__})"
                )
            else:
                if fence.refusal is not None:
                    report.refusal = f"the write fence could not go up: {fence.refusal}"
                else:
                    report.watchers.append("network")
        reads = effect_verifier is not None and callable(
            getattr(effect_verifier, "capture_pre_state", None)
        )
        if reads and report.refusal is None:
            report.record_before = record_digest(effect_verifier)
            if report.record_before is not None:
                report.watchers.append("record")
            elif not report.watchers:
                # With the network fenced, every write the page can make is
                # already stopped; without it, the read-back is the only proof.
                report.refusal = "the system of record could not be read before"
        if report.refusal is None and not report.watchers:
            report.refusal = (
                "this run has no way to see a write: the backend can't fence "
                "the network and no system-of-record read-back is configured"
            )
        try:
            yield report
        finally:
            # The fence (if any) closes with the stack, below; its blocked
            # writes are collected after.
            pass
    if fence is not None and "network" in report.watchers:
        report.attempts.extend(f"network: {a}" for a in fence.attempts)
        report.requests.extend(str(r) for r in getattr(fence, "requests", ()) or ())
        report.attempts.extend(reconcile_requests(report.requests, report.attempts))
    if "record" in report.watchers:
        report.record_after = record_digest(effect_verifier)
        if report.record_after is None:
            report.attempts.append(
                "record: the system of record could not be read after the "
                "dismissal, so a write can't be ruled out"
            )
        elif report.record_after != report.record_before:
            report.attempts.append("record: the system of record changed")


#: Methods that only read. Any other method in the request log is a write.
READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def reconcile_requests(requests: list[str], attempts: list[str]) -> list[str]:
    """The writes in ``requests`` that no entry in ``attempts`` names.

    The report must never say "no write attempts" while the request log holds
    a write: each unnamed one (or an unreadable request) becomes an attempt.
    """

    missing: list[str] = []
    for request in dict.fromkeys(requests):
        method = request.split(" ", 1)[0].upper()
        if method in READ_METHODS:
            continue
        if not any(request in attempt for attempt in attempts):
            missing.append(f"network: {request} was sent while the fence was up")
    return missing
