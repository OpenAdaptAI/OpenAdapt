"""Run the outbound decision lane: publish open pauses, execute the answers.

:mod:`openadapt_flow.console.decision_relay` is a transport. It publishes *one*
pause the caller already resolved, and executes *one* decision against a
``run_dir`` and :class:`~openadapt_flow.console.attention.AttentionItem` the
caller already knows. Nothing in the engine called it, so the hosted lane was
reachable in principle and dead in practice: a dental practice with no reverse
proxy still had no way for a halt to arrive on a phone.

This module is the missing half. It owns a ``runs`` root rather than one run:

* it publishes **every** currently open attended pause under that root, so a
  halt becomes answerable at ``app.openadapt.ai`` without anyone doing anything;
* it resolves an answered decision **back to the exact pause it was minted
  from**, by capability digest, before executing it; and
* it runs as a supervised background thread beside the attended console, which
  is the process that already holds the deployment-bound
  :class:`~openadapt_flow.runtime.durable.attended_service.AttendedActionService`
  a continuation needs.

Why resolution is by capability digest, not by position
-------------------------------------------------------

:meth:`DecisionRelay.serve_once` takes the run and item as arguments, which is
correct only when exactly one pause is open. A practice with two runs halted at
once would otherwise execute an answer against whichever pause the caller
happened to be holding. :meth:`DecisionSupervisor.resolve` instead re-scans the
runs root at decision time and requires **both** the relayed ``task_id`` and the
relayed ``capability_digest`` to equal the ones the engine's own signed
capability file produces right now. A decision that matches no open pause is
acknowledged ``stale`` and is never executed.

That check is not the safety boundary — :func:`execute_remote_attended_action`
re-validates the capability, takes the single-flight lease, re-reads the live
application and re-proves every ``will_recheck`` contract regardless. It is what
keeps a *correct* answer from being applied to the *wrong* run, which
revalidation alone would not catch when both pauses are genuinely open.

What this module refuses to claim
---------------------------------

The vocabulary stays the relay's: ``published``, ``already_published``,
``unknown``. A cycle reports what it observed and nothing more. In particular a
pause whose publish returned ``unknown`` is reported as ``unknown`` and is
**not** described as reachable; the local console remains the authoritative
surface for it.

Re-publishing after ``unknown`` is deliberate, and it is not a blind retry.
``POST /api/human-decisions/tasks`` is an idempotent upsert keyed on the signed
task: an identical projection returns ``created: false``, and a *divergent*
projection for the same task is rejected by the control plane rather than
overwriting. So the next cycle re-POSTs the same bytes, which either resolves
the uncertainty or leaves it unchanged. The rule the relay states — never retry
an operation whose effect may already have happened *and would happen twice* —
is preserved, because this operation cannot happen twice.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from openadapt_flow.console import data
from openadapt_flow.console.attention import (
    AttentionItem,
    list_attention,
    resolve_attention,
)
from openadapt_flow.console.decision_relay import (
    DecisionRelay,
    PublishOutcome,
    PublishState,
    RelayedDecision,
    RelayRefused,
)
from openadapt_flow.deployment import DeploymentConfig
from openadapt_flow.runtime.durable.attended import (
    AttendedActionExecutor,
    AttendedActionRefused,
    AttendedActionStore,
    AttendedDecision,
    AttendedRelayAcknowledgement,
)

#: Seconds a poll waits for an answer before the loop takes another turn. Also
#: the granularity at which :meth:`DecisionSupervisorThread.stop` is observed.
DEFAULT_POLL_WAIT_S = 25.0

#: Backoff bounds for a control plane that is refusing or unreachable. A
#: practice's broadband line goes down; the supervisor must not spin on it.
BACKOFF_BASE_S = 2.0
BACKOFF_CAP_S = 120.0

#: Pause after a governed refusal. Not a backoff -- a refusal is an answer, and
#: the transport is healthy -- but a re-delivered decision returns from the poll
#: instantly, so without a floor the loop would spin on one it always refuses.
REFUSAL_PAUSE_S = 1.0


@dataclass(frozen=True)
class OpenPause:
    """One durably paused run, resolved from the runs root at scan time."""

    run_dir: Path
    item: AttentionItem
    task_id: str
    capability_digest: str


@dataclass(frozen=True)
class PublishReport:
    """What one publish pass observed, per pause. No claim about a person."""

    #: Pauses the control plane accepted for the first time.
    published: tuple[str, ...] = ()
    #: Pauses the control plane said it already held, observed THIS pass.
    already_published: tuple[str, ...] = ()
    #: Pauses the control plane accepted earlier in this process and that were
    #: therefore not re-sent. Kept separate from ``already_published`` on
    #: purpose: the supervisor did not ask the control plane about these this
    #: pass, so reporting them as an observation would claim something it did
    #: not observe.
    previously_confirmed: tuple[str, ...] = ()
    #: Pauses whose POST left the process without a terminal response. These
    #: may or may not be visible on a phone and must not be described as
    #: reachable.
    unknown: tuple[str, ...] = ()
    #: Pauses that could not be projected at all (no capability, closed pause,
    #: remote issuance refused). Not an error; the local console still serves
    #: them.
    not_projectable: tuple[str, ...] = ()
    #: Pauses the control plane refused. Recorded rather than raised, so one
    #: bad projection cannot make every other halt unreachable.
    refused: tuple[str, ...] = ()

    @property
    def certain_count(self) -> int:
        """Pauses known to be answerable, by observation or by prior accept."""
        return (
            len(self.published)
            + len(self.already_published)
            + len(self.previously_confirmed)
        )


@dataclass(frozen=True)
class CycleReport:
    """The outcome of one supervisor cycle."""

    publishes: PublishReport
    #: ``None`` when no decision was waiting this cycle.
    decision_id: Optional[str] = None
    #: What the supervisor told the control plane it did: one of ``accepted``,
    #: ``refused``, ``stale``, ``expired``, or ``None`` when there was nothing
    #: to acknowledge.
    acknowledged: Optional[str] = None
    #: The engine decision, when one was executed.
    outcome: Optional[AttendedDecision] = None
    #: True when this cycle only repeated an acknowledgement for an exact
    #: signed decision whose engine outcome was already retained in the run's
    #: atomic decision journal.
    #: The action is never executed a second time on this path.
    reacknowledged: bool = False


def _parse_rfc3339(value: object) -> Optional[datetime]:
    """Parse a relay timestamp, or ``None`` if it is not one.

    A relay whose ``expires_at`` cannot be parsed is treated as *expired*, not
    as open-ended: an unreadable deadline is not a licence to act.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


class DecisionSupervisor:
    """Own the outbound decision lane for every open pause under one root."""

    def __init__(
        self,
        runs_root: Path | str,
        *,
        relay: DecisionRelay,
        deployment: DeploymentConfig,
        executor: Optional[AttendedActionExecutor] = None,
        now: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._runs_root = Path(runs_root)
        self._relay = relay
        self._deployment = deployment
        self._executor = executor
        self._now = now or (lambda: datetime.now(timezone.utc))
        #: ``(task_id, capability_digest)`` pairs the control plane has already
        #: accepted in this process. Not durable on purpose: a restarted
        #: supervisor republishes, which is idempotent, rather than trusting a
        #: file to say a remote surface still holds something.
        self._confirmed: set[tuple[str, str]] = set()

    # -- scanning ---------------------------------------------------------

    def open_pauses(self) -> list[OpenPause]:
        """Every durably paused run under the root, with its exact capability.

        The capability file is the engine's own signed record of the pause, so
        reading it here costs no signing and cannot mint authority. A run whose
        capability is missing, unreadable, or unsigned is simply not an open
        pause for this purpose.
        """
        resolved: list[OpenPause] = []
        for scanned in list_attention(self._runs_root):
            if not scanned.durably_paused:
                continue
            # `AttentionItem.id` is an opaque scan id, not a directory name.
            # `resolve_attention` is the only supported way back to a path, and
            # it re-scans, so a run that closed between the two calls resolves
            # to None rather than to a stale directory.
            located = resolve_attention(self._runs_root, scanned.id)
            if located is None:
                continue
            run_dir, item = located
            if not item.durably_paused:
                continue
            try:
                capability = AttendedActionStore(run_dir).read()
            except AttendedActionRefused:
                continue
            resolved.append(
                OpenPause(
                    run_dir=run_dir,
                    item=item,
                    task_id=f"task_{capability.pause_id}",
                    capability_digest=capability.digest,
                )
            )
        return resolved

    def resolve(self, decision: RelayedDecision) -> Optional[OpenPause]:
        """The open pause this decision was minted from, or ``None``.

        Both the relayed ``task_id`` and the relayed ``capability_digest`` must
        match. The digest alone would be sufficient today; requiring both means
        that if a future change ever decouples the two, it shows up as a
        decision that resolves to nothing rather than as a silent guess.

        That failure direction is deliberate. A supervisor that cannot resolve
        a decision acknowledges it ``stale`` and executes nothing, which is an
        outage. A supervisor that resolves it to the wrong open pause executes
        a real answer against a real record. The first is recoverable.
        """
        relay = decision.relay
        task_id = relay.get("task_id")
        capability_digest = relay.get("capability_digest")
        for pause in self.open_pauses():
            if pause.task_id == task_id and pause.capability_digest == (
                capability_digest
            ):
                return pause
        return None

    def retained_acknowledgement(
        self,
        decision: RelayedDecision,
    ) -> Optional[tuple[Path, AttendedRelayAcknowledgement, AttendedDecision]]:
        """Find one exact journaled outcome, including after process restart.

        Completed Continue, Skip, and Reject actions can remove a run from the
        open queue. Scan the same bounded, symlink-refusing run root used by the
        console, then require the complete signed relay binding and the retained
        engine decision digest to verify before any acknowledgement is sent.
        """
        if self._runs_root.is_symlink():
            raise RelayRefused(
                "the configured runs root is a symlink; retained relay recovery "
                "is refused"
            )
        binding = decision.durable_binding()
        retained: list[tuple[Path, AttendedRelayAcknowledgement, AttendedDecision]] = []
        for run_dir in data._scan(self._runs_root, data._is_run_dir):
            store = AttendedActionStore(run_dir)
            try:
                matched = store.relay_acknowledgement(binding)
            except AttendedActionRefused as exc:
                raise RelayRefused(str(exc)) from exc
            if matched is not None:
                record, outcome = matched
                retained.append((run_dir, record, outcome))
        if len(retained) > 1:
            raise RelayRefused(
                "the relay decision is bound to more than one retained engine outcome"
            )
        return retained[0] if retained else None

    # -- one cycle --------------------------------------------------------

    def publish_open_pauses(self, *, timeout_s: float = 15.0) -> PublishReport:
        """Make every open pause answerable from the hosted surface.

        One pause the control plane refuses must not silence the others. A
        refusal is recorded per pause and the loop continues, because the
        alternative -- letting it propagate -- would leave every OTHER halt in
        the practice unreachable on a phone because of one bad projection.
        """
        published: list[str] = []
        already: list[str] = []
        unknown: list[str] = []
        not_projectable: list[str] = []
        refused: list[str] = []
        memoized: list[str] = []
        confirmed: set[tuple[str, str]] = set()
        for pause in self.open_pauses():
            key = (pause.task_id, pause.capability_digest)
            if key in self._confirmed:
                # Already accepted at this exact capability, and the signed task
                # is a deterministic function of it, so re-POSTing would send
                # identical bytes for no new information. A pause can stay open
                # for hours; publishing it every cycle is noise, not safety.
                memoized.append(pause.task_id)
                confirmed.add(key)
                continue
            try:
                outcome: PublishOutcome = self._relay.publish(
                    pause.run_dir, pause.item, timeout_s=timeout_s
                )
            except AttendedActionRefused:
                # The pause exists but cannot be projected remotely -- a closed
                # pause, or a deployment whose remote issuance this run does not
                # satisfy. The local console still serves it.
                not_projectable.append(pause.task_id)
                continue
            except RelayRefused:
                refused.append(pause.task_id)
                continue
            if outcome.state is PublishState.PUBLISHED:
                published.append(pause.task_id)
                confirmed.add(key)
            elif outcome.state is PublishState.ALREADY_PUBLISHED:
                already.append(pause.task_id)
                confirmed.add(key)
            else:
                # Uncertain. Deliberately NOT confirmed, so the next cycle
                # re-POSTs the identical idempotent projection and either
                # resolves the uncertainty or leaves it unchanged.
                unknown.append(pause.task_id)
        # Rebuilt rather than updated, so a pause that closed stops being
        # remembered and a later pause reusing its identity is republished.
        self._confirmed = confirmed
        return PublishReport(
            published=tuple(published),
            already_published=tuple(already),
            previously_confirmed=tuple(memoized),
            unknown=tuple(unknown),
            not_projectable=tuple(not_projectable),
            refused=tuple(refused),
        )

    def serve_once(
        self,
        *,
        wait_s: float = DEFAULT_POLL_WAIT_S,
        publish: bool = True,
    ) -> CycleReport:
        """Publish open pauses, then take at most one answered decision.

        A decision that resolves to no currently open pause is acknowledged
        ``stale``; one whose relay deadline has passed is acknowledged
        ``expired``. Neither is executed. A governed refusal is acknowledged
        ``refused`` and re-raised, so the hosted surface can tell the operator
        their answer was not accepted rather than leaving them looking at a
        decision that appears to have been taken.
        """
        publishes = self.publish_open_pauses() if publish else PublishReport()
        decision = self._relay.poll(wait_s=wait_s)
        if decision is None:
            return CycleReport(publishes=publishes)

        retained_ack = self.retained_acknowledgement(decision)
        if retained_ack is not None:
            run_dir, record, outcome = retained_ack
            confirmed = self._relay.acknowledge(decision, record.engine_ack_result)
            if confirmed:
                AttendedActionStore(run_dir).confirm_relay_acknowledgement(
                    decision.durable_binding()
                )
            return CycleReport(
                publishes=publishes,
                decision_id=decision.decision_id,
                acknowledged=record.engine_ack_result,
                outcome=outcome,
                reacknowledged=True,
            )

        expires_at = _parse_rfc3339(decision.relay.get("expires_at"))
        if expires_at is None or expires_at <= self._now():
            self._relay.acknowledge(decision, "expired")
            return CycleReport(
                publishes=publishes,
                decision_id=decision.decision_id,
                acknowledged="expired",
            )

        pause = self.resolve(decision)
        if pause is None:
            self._relay.acknowledge(decision, "stale")
            return CycleReport(
                publishes=publishes,
                decision_id=decision.decision_id,
                acknowledged="stale",
            )

        try:
            outcome = self._relay.execute(
                pause.run_dir, pause.item, decision, executor=self._executor
            )
        except AttendedActionRefused:
            self._relay.acknowledge(decision, "refused")
            raise
        result = "accepted" if outcome.status != "refused" else "refused"
        # ``DecisionRelay.execute`` atomically appended the completed local
        # result and its exact signed relay binding to the run's existing
        # decision journal before this network acknowledgement. A restart can
        # therefore re-acknowledge without executing the action again.
        confirmed = self._relay.acknowledge(decision, result)
        if confirmed:
            AttendedActionStore(pause.run_dir).confirm_relay_acknowledgement(
                decision.durable_binding()
            )
        return CycleReport(
            publishes=publishes,
            decision_id=decision.decision_id,
            acknowledged=result,
            outcome=outcome,
        )


@dataclass
class SupervisorStats:
    """PHI-free counters a caller may log or surface. No identifiers."""

    cycles: int = 0
    decisions_executed: int = 0
    decisions_refused: int = 0
    decisions_stale: int = 0
    decisions_expired: int = 0
    publish_unknown: int = 0
    consecutive_failures: int = 0
    last_error: Optional[str] = field(default=None)


class DecisionSupervisorThread:
    """Run a :class:`DecisionSupervisor` beside the attended console.

    The console process already owns the deployment-bound action service a
    continuation needs, and
    :func:`~openadapt_flow.runtime.durable.attended.execute_attended_action`
    takes a single-flight lease over the pause, so a decision arriving from the
    phone and one taken in the local browser cannot both execute. That lease is
    what makes running this in a thread beside the server correct rather than
    merely convenient.
    """

    def __init__(
        self,
        supervisor: DecisionSupervisor,
        *,
        wait_s: float = DEFAULT_POLL_WAIT_S,
        on_cycle: Optional[Callable[[CycleReport], None]] = None,
        sleep: Optional[Callable[[float], None]] = None,
    ) -> None:
        self._supervisor = supervisor
        self._wait_s = wait_s
        self._on_cycle = on_cycle
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._sleep = sleep or self._stop.wait  # interruptible by stop()
        self.stats = SupervisorStats()

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("the decision supervisor is already running")
        thread = threading.Thread(
            target=self.run, name="openadapt-decision-relay", daemon=True
        )
        self._thread = thread
        thread.start()

    def stop(self, *, timeout_s: float = 5.0) -> None:
        """Ask the loop to finish its current cycle and stop.

        A cycle can be inside a long poll, so the thread may outlive this call
        by up to the poll wait. It is a daemon thread holding no lease of its
        own, and a decision already in flight completes or refuses under the
        engine's normal contracts, so an unjoined thread cannot leave a pause
        half-decided.
        """
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout_s)

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    # -- the loop ---------------------------------------------------------

    def run(self) -> None:
        """Cycle until stopped. Never raises; every outcome is counted."""
        while not self._stop.is_set():
            try:
                report = self._supervisor.serve_once(wait_s=self._wait_s)
            except AttendedActionRefused as exc:
                # A governed refusal was already acknowledged as ``refused``.
                # It is an answer, not a transport failure, so it does not raise
                # the backoff level -- a refusal must not slow the lane down for
                # everyone else.
                #
                # It does take one short pause, because the acknowledgement can
                # itself be uncertain. In that case the decision stays leased
                # server-side and is re-delivered immediately, and a poll with a
                # decision waiting returns at once: without this pause the loop
                # would spin at full speed on a decision it will refuse every
                # time.
                self.stats.decisions_refused += 1
                self.stats.last_error = type(exc).__name__
                self._sleep(REFUSAL_PAUSE_S)
                continue
            except RelayRefused as exc:
                self.stats.consecutive_failures += 1
                self.stats.last_error = str(exc)
                self._sleep(self._backoff_s())
                continue
            except Exception as exc:  # noqa: BLE001 - the loop must not die
                self.stats.consecutive_failures += 1
                self.stats.last_error = type(exc).__name__
                self._sleep(self._backoff_s())
                continue
            self._record(report)
            if self._on_cycle is not None:
                self._on_cycle(report)

    def _record(self, report: CycleReport) -> None:
        self.stats.cycles += 1
        self.stats.consecutive_failures = 0
        self.stats.publish_unknown += len(report.publishes.unknown)
        if report.acknowledged == "accepted" and not report.reacknowledged:
            self.stats.decisions_executed += 1
        elif report.acknowledged == "refused":
            self.stats.decisions_refused += 1
        elif report.acknowledged == "stale":
            self.stats.decisions_stale += 1
        elif report.acknowledged == "expired":
            self.stats.decisions_expired += 1

    def _backoff_s(self) -> float:
        return min(
            BACKOFF_CAP_S,
            BACKOFF_BASE_S * (2 ** min(self.stats.consecutive_failures - 1, 16)),
        )
