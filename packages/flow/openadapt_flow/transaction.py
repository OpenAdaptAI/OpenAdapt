"""Explicit transaction + reconciliation semantics (roadmap Section 3).

The runtime already produces a COARSE lifecycle -- ``success`` / ``halt`` /
``failure`` -- and a first evidence-qualified projection
(``execution_outcome``: VERIFIED / COMPLETED_UNVERIFIED / HALTED / FAILED /
ROLLED_BACK).  Neither of those states what is known about the BUSINESS EFFECT
when a run stops: a governed halt where nothing was written and a stop where a
consequential write may have half-landed both collapse to "HALTED / not
success".

This module refines that coarse outcome into a first-class TERMINAL TRANSACTION
outcome (:class:`TransactionOutcome`) that describes what the evidence proves
about the effect, records a per-step :class:`~openadapt_flow.ir.EffectJournalEntry`
ledger, and provides a caller-supplied :class:`IdempotencyLedger` so a repeat
under the same key does not re-actuate.  It is a LEAF: it reads only typed
fields already present on the ``RunReport`` (mirroring
``execution_profiles.build_outcome_envelope``), so it adds no import weight and
leaks no PHI.  ``execution_outcome`` and ``outcome_envelope`` are unchanged --
every existing consumer keeps working; new consumers read
``transaction_outcome``.

Scoped OUT of this first PR (follow-ups): full saga compensation steps, the
human-reconciliation-task UI, and Cloud/runner propagation of the taxonomy.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Optional

if TYPE_CHECKING:
    from openadapt_flow.ir import EffectJournalEntry, RunReport, StepResult, Workflow


class TransactionOutcome(str, Enum):
    """Terminal transaction outcome: what is known about the business effect.

    Every run ends in exactly one of these.  The value is a superset of the
    Section 3 taxonomy: ``ROLLED_BACK`` is carried through from the existing
    compensation path so the legacy outcome maps 1:1 without losing
    information.
    """

    #: Every declared effect (and collateral-effect check) passed at/above the
    #: required tier under a production profile.  The only production success.
    VERIFIED = "VERIFIED"
    #: The run stopped AND the verifier established that NO business effect
    #: occurred (every consequential effect was observed absent, or the run
    #: halted before any consequential action ran).
    HALTED_BEFORE_EFFECT = "HALTED_BEFORE_EFFECT"
    #: Delivery or persistence is uncertain, conflicting, or temporarily
    #: unverifiable.  The runtime must NOT blind-retry the consequential write;
    #: resuming must reconcile current state first (builds on #250).
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    #: An OpenAdapt / platform failure before any possible business effect.
    #: Never a successful billable run.
    FAILED_PLATFORM = "FAILED_PLATFORM"
    #: Canceled before any business effect could occur.
    CANCELED = "CANCELED"
    #: Authorization / identity / qualification / environment refused execution
    #: before any business effect.
    REJECTED_POLICY = "REJECTED_POLICY"
    #: Demo-only completion with no production-grade effect evidence.  MUST be
    #: treated as never-billable and never a production success.
    COMPLETED_UNVERIFIED = "COMPLETED_UNVERIFIED"
    #: A detected duplicate / collateral write was compensated and re-verified.
    #: Carried through from the existing ROLLED_BACK lifecycle; non-success.
    ROLLED_BACK = "ROLLED_BACK"

    @property
    def is_production_success(self) -> bool:
        """Only VERIFIED counts as a production success."""

        return self is TransactionOutcome.VERIFIED

    @property
    def is_platform_fault(self) -> bool:
        """True only for a platform-caused failure, distinct from a
        customer/governed outcome (policy rejection, governed halt) and from an
        uncertain delivery."""

        return self is TransactionOutcome.FAILED_PLATFORM

    @property
    def is_billable(self) -> bool:
        """Whether this run is a chargeable business outcome.

        Conservative by design: only a VERIFIED business outcome is billable in
        the runtime.  A platform fault, a cancellation, a policy rejection, an
        uncertain-and-unreconciled delivery, and a Demo-only
        COMPLETED_UNVERIFIED are all non-billable.  Broader billing policy
        (e.g. charging for governed halts) is a Cloud/billing decision that is
        scoped OUT of this PR; keeping the runtime default narrow guarantees a
        FAILED_PLATFORM and a COMPLETED_UNVERIFIED can never be mistaken for a
        billable success.
        """

        return self is TransactionOutcome.VERIFIED


# The gate pseudo-steps the runtime appends when it refuses BEFORE acting.
# These are policy/authorization/environment refusals, not platform faults.
_POLICY_GATE_STEP_IDS = frozenset(
    {"<profile>", "<authorization>", "<params>", "<idempotency>"}
)


def _has_unresolved_uncertainty(report: RunReport) -> bool:
    """True when any step's delivery or persistence is uncertain/conflicting.

    This is the signal that must dominate every non-VERIFIED coarse bucket: if a
    consequential write MAY have landed (an uncertain delivery the complete
    contract did not resolve, a duplicate/partial write, or an unreadable system
    of record) the run can neither claim "no effect" nor be blind-retried.
    """

    for result in report.results:
        uncertainty = result.delivery_uncertainty
        if uncertainty is not None and not uncertainty.resolved_by_contract:
            return True
        for evidence in result.effect_evidence:
            if evidence.final_verdict == "confirmed":
                # A confirmed (or reconciled-to-confirmed) effect is settled.
                continue
            if evidence.final_verdict == "indeterminate":
                return True
            # final_verdict == "refuted": only a verifier-established ABSENCE is
            # safe to treat as "no effect"; anything else may have written.
            if evidence.observed_effect != "absent":
                return True
    return False


def _policy_rejected(report: RunReport) -> bool:
    """True when authorization/identity/qualification/environment refused.

    A governed refusal at a pre-execution gate, or a pre-click identity check
    that did not verify, stopped the run BEFORE any business effect.  (The
    uncertain-delivery path also carries ``failure_category='governed_refusal'``
    but is handled earlier by :func:`_has_unresolved_uncertainty`, so reaching
    here means no write may have landed.)
    """

    for result in report.results:
        if result.step_id in _POLICY_GATE_STEP_IDS:
            return True
        if (
            not result.ok
            and result.identity is not None
            and result.identity.status != "verified"
        ):
            return True
    return False


def classify_transaction_outcome(report: RunReport) -> TransactionOutcome:
    """Refine the coarse ``execution_outcome`` into a transaction outcome.

    Reads only ``report.execution_outcome`` (already stamped) plus typed step
    evidence.  Precedence is deliberate: a settled success/rollback first, then
    any unresolved uncertainty (never claim "no effect", never blind-retry),
    then a completed-but-unverified Demo, then the reason a run stopped before
    any effect (policy rejection, cancellation, governed halt, platform fault).
    """

    coarse = report.execution_outcome
    if coarse == "VERIFIED":
        return TransactionOutcome.VERIFIED
    if coarse == "ROLLED_BACK":
        return TransactionOutcome.ROLLED_BACK

    # Uncertain / conflicting delivery or persistence dominates every remaining
    # coarse bucket. This is where #250's no-blind-retry behavior surfaces as a
    # first-class terminal outcome.
    if _has_unresolved_uncertainty(report):
        return TransactionOutcome.RECONCILIATION_REQUIRED

    if coarse == "COMPLETED_UNVERIFIED":
        return TransactionOutcome.COMPLETED_UNVERIFIED

    # Remaining: the run did not complete and nothing may have landed.
    if _policy_rejected(report):
        return TransactionOutcome.REJECTED_POLICY
    if report.canceled:
        return TransactionOutcome.CANCELED
    if coarse == "HALTED":
        # A governed halt with the verifier having established no effect.
        return TransactionOutcome.HALTED_BEFORE_EFFECT
    # coarse == "FAILED": a platform failure before any possible effect.
    return TransactionOutcome.FAILED_PLATFORM


def _attempt_state(
    result: StepResult,
) -> Literal["not_actuated", "delivered", "actuated_api", "delivery_uncertain"]:
    """Derive the PHI-free actuation attempt state for one step."""

    if result.delivery_uncertainty is not None:
        return "delivery_uncertain"
    if result.actuation == "api":
        return "actuated_api"
    if result.delivery_receipt is not None:
        return "delivered"
    if result.effect_contract_hashes or result.effect_evidence:
        # A consequential step whose write was actuated through the GUI ladder
        # (no native delivery receipt) still reached actuation if it produced an
        # effect contract or a verdict.
        return "delivered"
    return "not_actuated"


def _worst_observed_effect(
    result: StepResult,
) -> Literal["present", "absent", "conflicting", "unknown"]:
    """Collapse this step's effect evidence to the least-settled observation.

    ``unknown`` and ``conflicting`` (a write may have landed) outrank ``absent``
    (proven no write) which outranks ``present`` (confirmed). With no evidence
    the observation is ``unknown``.
    """

    order = {"present": 0, "absent": 1, "conflicting": 2, "unknown": 3}
    worst: Literal["present", "absent", "conflicting", "unknown"] = "present"
    seen = False
    for evidence in result.effect_evidence:
        seen = True
        if order[evidence.observed_effect] > order[worst]:
            worst = evidence.observed_effect
    return worst if seen else "unknown"


def build_effect_journal(
    report: RunReport, workflow: Workflow
) -> list[EffectJournalEntry]:
    """Build the per-consequential-step effect journal from typed evidence.

    One entry per executed consequential step (a step that declared effects, or
    that the run gate classifies consequential). Carries only hashes, enums,
    counts, and timestamps -- no record values, parameters, or free text.
    """

    from openadapt_flow.ir import EffectJournalEntry
    from openadapt_flow.run_gate import is_consequential
    from openadapt_flow.traversal import iter_workflow_steps

    steps_by_id = {step.id: step for step in iter_workflow_steps(workflow)}
    consequential_ids = {
        step.id for step in iter_workflow_steps(workflow) if is_consequential(step)
    }

    journal: list[EffectJournalEntry] = []
    for result in report.results:
        if result.skipped:
            continue
        step = steps_by_id.get(result.step_id)
        declared_effects = bool(
            result.effect_contract_hashes
            or result.effect_evidence
            or (
                step is not None
                and (
                    step.effects
                    or (step.api_binding is not None and step.api_binding.effects)
                )
            )
        )
        is_conseq = result.step_id in consequential_ids or declared_effects
        if not is_conseq:
            continue
        collateral = sum(
            evidence.reconciliation_actions
            for evidence in result.effect_evidence
            if evidence.reconciliation_completed
        )
        observed_at: Optional[str] = None
        if result.delivery_uncertainty is not None:
            observed_at = result.delivery_uncertainty.observed_at
        journal.append(
            EffectJournalEntry(
                step_id=result.step_id,
                intent=result.intent,
                consequential=True,
                intended_effect_contract_hashes=list(result.effect_contract_hashes),
                attempt_state=_attempt_state(result),
                observed_effect=_worst_observed_effect(result),
                effect_verified=result.effect_verified,
                approved_unverified=result.effect_approved_unverified,
                verification_performed=bool(result.effect_evidence),
                observed_at=observed_at,
                collateral_reconciliation_actions=collateral,
            )
        )
    return journal


def stamp_transaction_outcome(
    report: RunReport, workflow: Workflow
) -> TransactionOutcome:
    """Write the transaction outcome, billing metadata, and effect journal.

    Called after the coarse ``execution_outcome`` is stamped (see
    ``execution_profiles.stamp_execution_outcome``). Never mutates
    ``execution_outcome``, ``success``, ``production_eligible``, or the
    ``outcome_envelope`` -- it only ADDS the Section 3 fields.
    """

    outcome = classify_transaction_outcome(report)
    report.transaction_outcome = outcome.value
    report.transaction_billable = outcome.is_billable
    report.transaction_platform_fault = outcome.is_platform_fault
    report.effect_journal = build_effect_journal(report, workflow)
    return outcome


class DuplicateActuation(Exception):
    """A run reserved an idempotency key that was already actuated."""


class IdempotencyLedger:
    """At-most-once ledger keyed by a caller-supplied idempotency key.

    A tiny append-only JSON store mapping ``key -> {run_id, reserved_at,
    outcome}``. A run RESERVES its key before any actuation; a repeat with an
    already-reserved key is SUPPRESSED (never re-actuated) -- the safe
    at-most-once posture, since a crash after reservation must reconcile rather
    than blind-retry the consequential write.

    Concurrency: single-writer, last-write-wins JSON (adequate for the local
    runtime). A shared/locked store is a Cloud follow-up and is scoped out.
    """

    def __init__(self, path: Optional[Path | str] = None) -> None:
        #: None keeps the ledger purely in memory (tests / ephemeral runs).
        self.path: Optional[Path] = Path(path) if path is not None else None
        self._records: dict[str, dict[str, Optional[str]]] = {}
        if self.path is not None and self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self._records = loaded
            except (json.JSONDecodeError, OSError):
                # A corrupt ledger must not silently permit re-actuation; fail
                # loud rather than treat every key as unseen.
                raise

    def lookup(self, key: str) -> Optional[dict[str, Optional[str]]]:
        """Return the stored record for ``key``, or None when unseen."""

        return self._records.get(key)

    def seen(self, key: str) -> bool:
        return key in self._records

    def reserve(self, key: str, *, run_id: str) -> None:
        """Reserve ``key`` before actuation. Raise if already reserved."""

        if key in self._records:
            raise DuplicateActuation(
                f"idempotency key {key!r} already reserved by run "
                f"{self._records[key].get('run_id')!r}"
            )
        self._records[key] = {
            "run_id": run_id,
            "reserved_at": datetime.now(timezone.utc).isoformat(),
            "outcome": None,
        }
        self._flush()

    def record_outcome(self, key: str, outcome: Optional[str]) -> None:
        """Record the terminal outcome for a previously reserved ``key``."""

        record = self._records.get(key)
        if record is None:
            return
        record["outcome"] = outcome
        self._flush()

    def _flush(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic replace so a concurrent reader never sees a half-written file.
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._records, handle, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
