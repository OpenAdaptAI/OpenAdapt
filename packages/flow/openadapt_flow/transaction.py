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

import hashlib
import json
import os
import secrets
import sqlite3
import stat
import tempfile
import threading
import time
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
    #: The run stopped before any consequential effect: every consequential
    #: step was either proven absent by exact effect-verifier coverage or
    #: explicitly recorded as having stopped BEFORE delivery was attempted. A
    #: confirmed earlier write is partial completion, not a before-effect halt,
    #: and is therefore ``RECONCILIATION_REQUIRED`` until a dedicated
    #: partial-completion outcome exists.
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

    This reads CONFLICTING evidence only.  It deliberately says nothing about a
    step with NO evidence: an empty ``effect_evidence`` means verification never
    ran, which is UNKNOWN rather than settled.  Never read a ``False`` here as
    "no effect occurred" -- that case is caught by
    :func:`_lacks_effect_absence_proof`.
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


def _is_consequential_result(result: StepResult) -> bool:
    """Whether this step could have left a BUSINESS effect behind.

    The report-side, fail-closed mirror of
    :func:`openadapt_flow.run_gate.is_consequential`.  ``classify_transaction_outcome``
    deliberately takes only a ``RunReport`` (it is a leaf and its signature is
    public), so consequentiality is derived from typed fields the runtime
    already stamped on the result: the compiled ``risk`` label, an unresolved
    risk-review requirement (the action may be consequential until reviewed),
    plus any declared, approved, or attempted system-of-record effect. A
    reviewed reversible step that declared no effect cannot leave an
    unreconciled write, so it never blocks an absence claim.
    """

    return (
        result.risk == "irreversible"
        or result.risk_review_required
        or bool(result.effect_contract_hashes)
        or bool(result.effect_evidence)
        or result.effect_verified is not None
        or result.effect_approved_unverified
        or result.delivery_uncertainty is not None
    )


def _effect_evidence_has_exact_coverage(result: StepResult) -> bool:
    """Whether retained evidence accounts for every declared effect exactly once."""

    if not result.effect_evidence:
        return False
    from collections import Counter

    declared = Counter(result.effect_contract_hashes)
    observed = Counter(
        evidence.effect_contract_hash for evidence in result.effect_evidence
    )
    return bool(declared) and declared == observed


def _effect_absence_proven(result: StepResult) -> bool:
    """True only when this step is positively proven to have caused no effect.

    Exactly two things account for an effect, and both are POSITIVE claims:

    1. the runtime recorded that the step stopped BEFORE delivery was attempted
       (:func:`_attempt_state` -> ``not_actuated``, itself fail-closed); or
    2. a verifier read the system of record and established ``absent`` for
       EVERY declared effect, with an exact one-to-one hash match between the
       declared contracts and retained evidence.

    An EMPTY ``effect_evidence`` list is neither.  A consequential step that
    reached actuation and was never verified is UNKNOWN, not absent: the system
    of record may hold the write.  That covers the commit-then-client-timeout
    case, any premature abort that stops the run before verification runs, and
    an API actuation whose ``ActuationStatus.HALT`` the runtime itself
    documents as "the request WAS sent ... the write may have landed".

    An approved-but-unverified GUI write is also unaccounted for: accepting the
    risk of proceeding without a verifier is not the same as establishing what
    happened, and it can never license an absence claim.
    """

    if result.effect_evidence:
        # Exact MULTISET coverage matters: a single evidence record must not
        # settle two declared contracts, and duplicate evidence must not hide a
        # missing contract. Any retained known-present/conflicting reading
        # dominates a contradictory non-delivery flag.
        if not _effect_evidence_has_exact_coverage(result):
            return False
        return all(
            evidence.final_verdict == "refuted" and evidence.observed_effect == "absent"
            for evidence in result.effect_evidence
        )
    return _attempt_state(result) == "not_actuated"


def _effect_state_fully_accounted(result: StepResult) -> bool:
    """Whether a stopped step has exact, settled effect-state evidence.

    This is broader than absence: a completed compensation may leave the
    intended effect present while removing only a duplicate/collateral write.
    ``ROLLED_BACK`` is therefore valid with exact CONFIRMED evidence, but never
    when one declared effect is missing from the retained evidence multiset.
    """

    if _effect_absence_proven(result):
        return True
    if not _effect_evidence_has_exact_coverage(result):
        return False
    return all(
        evidence.final_verdict == "confirmed"
        or (
            evidence.final_verdict == "refuted" and evidence.observed_effect == "absent"
        )
        for evidence in result.effect_evidence
    )


def _rolled_back_effects_fully_accounted(report: RunReport) -> bool:
    """Require exact settled coverage before accepting a rollback projection."""

    has_completed_compensation = any(
        evidence.reconciliation_completed and evidence.reconciliation_actions > 0
        for result in report.results
        for evidence in result.effect_evidence
    )
    if not has_completed_compensation:
        # Preserve read compatibility for legacy reports whose coarse outcome
        # predates structured compensation evidence. They carry no contradictory
        # typed state to overrule; new reports always retain the evidence.
        return not any(_is_consequential_result(result) for result in report.results)
    return all(
        not _is_consequential_result(result) or _effect_state_fully_accounted(result)
        for result in report.results
    )


def _lacks_effect_absence_proof(report: RunReport) -> bool:
    """True when any consequential step is not positively proven effect-free.

    Guards every terminal outcome that asserts an absence
    (``HALTED_BEFORE_EFFECT``, ``REJECTED_POLICY``, ``CANCELED``,
    ``FAILED_PLATFORM``). A single consequential step without absence proof is
    enough: the customer
    would otherwise be told to reconcile nothing despite a confirmed, possible,
    or incompletely covered mutation in their system of record.

    A confirmed earlier write is also not absence. Until the public taxonomy
    gains a dedicated partial-completion state, such a run is conservatively a
    reconciliation task rather than an absence-asserting terminal outcome.
    """

    return any(
        _is_consequential_result(result) and not _effect_absence_proven(result)
        for result in report.results
    )


def _policy_rejected(report: RunReport) -> bool:
    """True when authorization/identity/qualification/environment refused.

    A governed refusal at a pre-execution gate, or a pre-click identity check
    that did not verify, stopped the run BEFORE any business effect.  (The
    uncertain-delivery path also carries ``failure_category='governed_refusal'``
    but is handled earlier by :func:`_has_unresolved_uncertainty` and
    :func:`_lacks_effect_absence_proof`, so reaching here means every
    consequential step is positively proven effect-free.)
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
    then a completed-but-unverified Demo, then a Standard/Regulated completion
    whose consequential writes lack independent system-of-record confirmation
    (``RECONCILIATION_REQUIRED`` -- pixel-only Citrix cannot be ``VERIFIED``),
    then -- only once every consequential step is POSITIVELY proven
    effect-free -- the reason the run stopped (policy rejection, cancellation,
    governed halt, platform fault).
    """

    if report.managed_result_loss is not None:
        # The outer hosted runner fenced the exact delivery chain but lost the
        # managed child's terminal result. A delivery acknowledgment can win
        # before that fence, so this is neither effect absence nor a platform
        # failure. The signed loss record exists only to force reconciliation
        # and prohibit retry.
        return TransactionOutcome.RECONCILIATION_REQUIRED

    coarse = report.execution_outcome
    if coarse == "VERIFIED":
        return TransactionOutcome.VERIFIED

    # Uncertain / conflicting delivery or persistence dominates every remaining
    # coarse bucket. This is where #250's no-blind-retry behavior surfaces as a
    # first-class terminal outcome.
    if _has_unresolved_uncertainty(report):
        return TransactionOutcome.RECONCILIATION_REQUIRED

    if coarse == "ROLLED_BACK":
        return (
            TransactionOutcome.ROLLED_BACK
            if _rolled_back_effects_fully_accounted(report)
            else TransactionOutcome.RECONCILIATION_REQUIRED
        )

    if coarse == "COMPLETED_UNVERIFIED":
        # Demo completions stay never-billable. A production profile that
        # finished a consequential write without an independent
        # system-of-record confirmation cannot claim a clean bill of
        # health: the screen may have painted success while the record is
        # wrong or missing. Fail toward reconciliation.
        if report.execution_profile in {
            "standard",
            "regulated",
        } and _lacks_effect_absence_proof(report):
            return TransactionOutcome.RECONCILIATION_REQUIRED
        return TransactionOutcome.COMPLETED_UNVERIFIED

    # Every remaining outcome ASSERTS that no business effect occurred, and a
    # customer who receives one reconciles nothing.  That assertion needs
    # POSITIVE evidence: a consequential step that reached actuation without a
    # verifier settling what happened may have left a write in the system of
    # record.  Absence of evidence is not evidence of absence -- fail toward
    # RECONCILIATION_REQUIRED, never toward a false clean bill of health.
    if _lacks_effect_absence_proof(report):
        return TransactionOutcome.RECONCILIATION_REQUIRED

    # Remaining: every consequential step is positively proven effect-free.
    if _policy_rejected(report):
        return TransactionOutcome.REJECTED_POLICY
    if report.canceled:
        return TransactionOutcome.CANCELED
    if coarse == "HALTED":
        # A governed halt where every consequential step either never reached
        # delivery or exact verifier coverage established absence.
        return TransactionOutcome.HALTED_BEFORE_EFFECT
    # coarse == "FAILED": a platform failure before any possible effect.
    return TransactionOutcome.FAILED_PLATFORM


def _reached_delivery(result: StepResult) -> bool:
    """True when some typed field proves the action was actually dispatched.

    Any of these can only exist AFTER the backend was asked to deliver the
    action: a delivery receipt, a recorded actuation tier, a successful action
    phase, a post-action input/postcondition verdict, or a bound effect
    contract / verifier reading.  A failed postcondition counts -- a
    postcondition is checked after the click, so an over-halt that aborts the
    run on one is proof the write was already delivered, not proof it was not.
    """

    return (
        result.delivery_receipt is not None
        or result.actuation is not None
        or result.ok
        or result.postconditions_ok is not None
        or result.input_verified is not None
        or bool(result.effect_contract_hashes)
        or bool(result.effect_evidence)
    )


def _attempt_state(
    result: StepResult,
) -> Literal["not_actuated", "delivered", "actuated_api", "delivery_uncertain"]:
    """Derive the PHI-free actuation attempt state for one step.

    Fail-closed.  ``not_actuated`` is itself an absence claim, so it is returned
    only where the runtime POSITIVELY recorded that delivery could not have
    happened: the step never executed, it is a pre-execution gate pseudo-step,
    or it stopped at a typed pre-delivery refusal.  A step whose delivery was
    never recorded either way is ``delivery_uncertain``, NOT ``not_actuated``.
    """

    # Structural proof the step could not have actuated: a skipped step never
    # ran, and a gate pseudo-step exists only because the runtime refused first.
    if result.skipped or result.step_id in _POLICY_GATE_STEP_IDS:
        return "not_actuated"
    # The live replayer owns this state transition. A failure category is not
    # proof: safety_halt can be stamped after a click when governed healing
    # rejects a repair. Legacy/synthesized None remains unknown, fail-closed.
    if result.delivery_attempted is False:
        return "not_actuated"
    if result.delivery_uncertainty is not None:
        return "delivery_uncertain"
    if result.actuation == "api":
        return "actuated_api"
    if _reached_delivery(result):
        # Includes a write actuated through the GUI ladder, which carries no
        # native delivery receipt.
        return "delivered"
    if result.delivery_attempted is True:
        # The boundary was crossed but no receipt/post-action evidence was
        # retained. Never convert that absence of a receipt into non-delivery.
        return "delivery_uncertain"
    if (
        not result.ok
        and result.identity is not None
        and result.identity.status != "verified"
    ):
        # Backward-compatible typed proof for legacy reports: identity checks
        # occur at the pre-delivery gate by contract. New live reports also
        # carry ``delivery_attempted=False`` above.
        return "not_actuated"
    # Nothing recorded either way: unknown, so fail toward reconciliation.
    return "delivery_uncertain"


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
        step.id
        for step in iter_workflow_steps(workflow)
        if is_consequential(step, workflow)
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
        # A result whose step is missing from the workflow (a runtime-expanded
        # or pseudo step) must not be read as "not consequential" -- an absent
        # lookup is unknown, not a negative. The report-side mirror keeps such a
        # step in the journal whenever its own typed fields say it could write.
        is_conseq = (
            result.step_id in consequential_ids
            or declared_effects
            or _is_consequential_result(result)
        )
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

    An immutable claim file is the durable reservation authority for each
    ``(namespace, key)``. Atomic exclusive creation makes independent runtime
    processes race on one claim before any actuation. SQLite retains queryable
    ``{run_id, reserved_at, outcome}`` data. A database rollback cannot remove
    the separate claim and make a used key available again.

    Existing JSON projections are migrated once under an exclusive sibling
    lock. Metadata and claims bind the ledger to its exact canonical path and
    namespace, so copying only the database cannot create a second authority.
    """

    _SCHEMA_VERSION = "openadapt.idempotency-ledger/v3"
    _DEFAULT_NAMESPACE = "openadapt-flow-runtime/v1"
    _SQLITE_HEADER = b"SQLite format 3\x00"
    _MIGRATION_WAIT_S = 10.0
    _CLAIM_DIRECTORY_SUFFIX = ".claims"

    def __init__(
        self,
        path: Optional[Path | str] = None,
        *,
        namespace: str = _DEFAULT_NAMESPACE,
    ) -> None:
        #: None keeps the ledger purely in memory (tests / ephemeral runs).
        if not namespace:
            raise ValueError("idempotency ledger namespace must not be empty")
        self.namespace = namespace
        self.path: Optional[Path] = None
        self._parent_descriptor: Optional[int] = None
        self._parent_identity: Optional[tuple[int, int]] = None
        self._owner_path_value: Optional[str] = None
        self._closed = False
        if path is not None:
            candidate = Path(path).expanduser().absolute()
            # macOS exposes its real temporary directory through the
            # root-owned /var symlink. Normalize only that known temporary
            # root. Descendant symlinks remain in the path and are rejected by
            # the managed-path checks below.
            temporary_root = Path(tempfile.gettempdir()).absolute()
            try:
                relative_to_temporary = candidate.relative_to(temporary_root)
            except ValueError:
                pass
            else:
                candidate = temporary_root.resolve(strict=True) / relative_to_temporary
            self.path = candidate
        self._records: dict[str, dict[str, Optional[str]]] = {}
        self._memory_lock = threading.RLock()
        if self.path is not None:
            self._ensure_parent_directory()
            self._assert_safe_path(self.path)
            self._parent_identity = self._path_identity(self.path.parent)
            self._parent_descriptor = self._open_parent_directory_descriptor()
            # Keep this value stable.  Calling ``Path.resolve`` after an
            # attacker replaces an ancestor would otherwise make a claim and
            # the SQLite projection agree on the replacement path.
            self._owner_path_value = str(self.path.resolve(strict=False))
            self._assert_parent_path_matches_descriptor()
            setup_lock = self._acquire_migration_lock()
            try:
                if self.path.exists() and not self._is_sqlite():
                    self._migrate_json_projection()
                self._initialize_sqlite()
            finally:
                self._release_migration_lock(setup_lock)

    def close(self) -> None:
        """Release the retained POSIX parent directory descriptor."""

        self._closed = True
        if self._parent_descriptor is not None:
            os.close(self._parent_descriptor)
            self._parent_descriptor = None

    def __del__(self) -> None:
        # ``close`` is intentionally best-effort during interpreter shutdown.
        try:
            self.close()
        except Exception:
            pass

    def lookup(self, key: str) -> Optional[dict[str, Optional[str]]]:
        """Return the stored record for ``key``, or None when unseen."""

        if self.path is None:
            with self._memory_lock:
                record = self._records.get(key)
                return dict(record) if record is not None else None
        claim = self._read_claim(key)
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT run_id, reserved_at, outcome
                  FROM reservations
                 WHERE namespace = ? AND reservation_key = ?
                """,
                (self.namespace, key),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            if claim is not None:
                raise RuntimeError(
                    "idempotency ledger claim/database ownership mismatch; "
                    "refusing to treat a reserved key as unseen"
                )
            return None
        outcome = self._normalize_outcome(row[2])
        record = {
            "run_id": str(row[0]),
            "reserved_at": str(row[1]),
            "outcome": outcome,
        }
        if claim is None or (
            claim["run_id"] != record["run_id"]
            or claim["reserved_at"] != record["reserved_at"]
        ):
            raise RuntimeError(
                "idempotency ledger claim/database ownership mismatch; "
                "refusing to trust the reservation"
            )
        return record

    def seen(self, key: str) -> bool:
        return self.lookup(key) is not None

    def reserve(self, key: str, *, run_id: str) -> None:
        """Reserve ``key`` before actuation. Raise if already reserved."""

        if not run_id:
            raise ValueError("idempotency ledger run_id must not be empty")
        reserved_at = datetime.now(timezone.utc).isoformat()
        if self.path is None:
            with self._memory_lock:
                if key in self._records:
                    self._raise_duplicate(key, self._records[key].get("run_id"))
                self._records[key] = {
                    "run_id": run_id,
                    "reserved_at": reserved_at,
                    "outcome": None,
                }
            return

        # The durable claim is the at-most-once authority. It is intentionally
        # written before SQLite: a crash after this point can cause a false
        # duplicate, but can never permit a duplicate consequential action.
        self._create_claim(key, run_id=run_id, reserved_at=reserved_at)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO reservations(
                        namespace, reservation_key, run_id, reserved_at, outcome
                    ) VALUES (?, ?, ?, ?, NULL)
                    """,
                    (self.namespace, key, run_id, reserved_at),
                )
            except sqlite3.IntegrityError:
                row = connection.execute(
                    """
                    SELECT run_id
                      FROM reservations
                     WHERE namespace = ? AND reservation_key = ?
                    """,
                    (self.namespace, key),
                ).fetchone()
                connection.rollback()
                if row is None or str(row[0]) != run_id:
                    raise RuntimeError(
                        "idempotency ledger claim/database ownership mismatch; "
                        "refusing to actuate"
                    )
                self._raise_duplicate(key, run_id)
            connection.commit()
        finally:
            connection.close()

    def record_outcome(
        self,
        key: str,
        outcome: Optional[str | TransactionOutcome],
        *,
        run_id: str,
    ) -> None:
        """Record the terminal outcome for a previously reserved ``key``."""

        if not run_id:
            raise ValueError("idempotency ledger run_id must not be empty")
        terminal_outcome = self._normalize_outcome(outcome)
        if self.path is None:
            with self._memory_lock:
                record = self._records.get(key)
                if record is None:
                    raise RuntimeError(
                        "idempotency ledger outcome has no reservation; "
                        "refusing to alter transaction state"
                    )
                if record["run_id"] != run_id:
                    raise RuntimeError(
                        "idempotency ledger outcome owner mismatch; "
                        "refusing to alter transaction state"
                    )
                self._record_memory_outcome(record, terminal_outcome)
            return

        self._require_claim_owner(key, run_id=run_id)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT run_id, outcome
                  FROM reservations
                 WHERE namespace = ? AND reservation_key = ?
                """,
                (self.namespace, key),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise RuntimeError(
                    "idempotency ledger outcome has no reservation; "
                    "refusing to alter transaction state"
                )
            if str(row[0]) != run_id:
                connection.rollback()
                raise RuntimeError(
                    "idempotency ledger outcome owner mismatch; "
                    "refusing to alter transaction state"
                )
            existing = self._normalize_outcome(row[1])
            if existing is None:
                if terminal_outcome is not None:
                    connection.execute(
                        """
                        UPDATE reservations
                           SET outcome = ?
                         WHERE namespace = ? AND reservation_key = ?
                           AND run_id = ? AND outcome IS NULL
                        """,
                        (terminal_outcome, self.namespace, key, run_id),
                    )
            elif terminal_outcome != existing:
                connection.rollback()
                raise RuntimeError(
                    "idempotency ledger terminal outcome is write-once; "
                    "refusing to alter transaction state"
                )
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _record_memory_outcome(
        record: dict[str, Optional[str]], terminal_outcome: Optional[str]
    ) -> None:
        """Apply the same terminal write-once rule as SQLite."""

        existing = IdempotencyLedger._normalize_outcome(record["outcome"])
        if existing is None:
            if terminal_outcome is not None:
                record["outcome"] = terminal_outcome
            return
        if terminal_outcome != existing:
            raise RuntimeError(
                "idempotency ledger terminal outcome is write-once; "
                "refusing to alter transaction state"
            )

    @staticmethod
    def _normalize_outcome(
        outcome: object,
    ) -> Optional[str]:
        """Return one supported terminal value, or reject malformed state."""

        if outcome is None:
            return None
        try:
            return TransactionOutcome(outcome).value
        except (TypeError, ValueError) as error:
            raise ValueError(
                "idempotency ledger outcome must be a TransactionOutcome value"
            ) from error

    def _connect(self) -> sqlite3.Connection:
        assert self.path is not None
        self._assert_parent_path_matches_descriptor()
        self._assert_safe_path(self.path)
        connection = sqlite3.connect(
            self.path,
            timeout=10.0,
            isolation_level=None,
        )
        try:
            self._assert_parent_path_matches_descriptor()
            connection.execute("PRAGMA busy_timeout = 10000")
            connection.execute("PRAGMA synchronous = FULL")
            # ``sqlite3`` accepts only a pathname.  The immutable claim is
            # therefore the reservation authority.  Revalidate the directory
            # after SQLite opens the projection so an ancestor replacement is
            # detected before its result can authorize an action.
            self._assert_parent_path_matches_descriptor()
            self._verify_owner(connection)
        except BaseException:
            connection.close()
            raise
        return connection

    def _initialize_sqlite(self) -> None:
        assert self.path is not None
        self._assert_parent_path_matches_descriptor()
        self._assert_safe_path(self.path)
        connection = sqlite3.connect(
            self.path,
            timeout=10.0,
            isolation_level=None,
        )
        try:
            self._assert_parent_path_matches_descriptor()
            connection.execute("PRAGMA busy_timeout = 10000")
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ledger_metadata(
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    schema_version TEXT NOT NULL,
                    namespace TEXT NOT NULL,
                    owner_path TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS reservations(
                    namespace TEXT NOT NULL,
                    reservation_key TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    reserved_at TEXT NOT NULL,
                    outcome TEXT,
                    PRIMARY KEY(namespace, reservation_key)
                )
                """
            )
            row = connection.execute(
                """
                SELECT schema_version, namespace, owner_path
                  FROM ledger_metadata
                 WHERE singleton = 1
                """
            ).fetchone()
            expected = (
                self._SCHEMA_VERSION,
                self.namespace,
                self._owner_path,
            )
            if row is None:
                connection.execute(
                    """
                    INSERT INTO ledger_metadata(
                        singleton, schema_version, namespace, owner_path
                    ) VALUES (1, ?, ?, ?)
                    """,
                    expected,
                )
            elif tuple(row) != expected:
                raise RuntimeError(
                    "idempotency ledger owner/schema mismatch; refusing a "
                    "second reservation authority"
                )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        self._fsync_parent_directory()

    @property
    def _owner_path(self) -> str:
        assert self.path is not None
        assert self._owner_path_value is not None
        return self._owner_path_value

    def _verify_owner(self, connection: sqlite3.Connection) -> None:
        row = connection.execute(
            """
            SELECT schema_version, namespace, owner_path
              FROM ledger_metadata
             WHERE singleton = 1
            """
        ).fetchone()
        expected = (self._SCHEMA_VERSION, self.namespace, self._owner_path)
        if row is None or tuple(row) != expected:
            raise RuntimeError(
                "idempotency ledger owner/schema mismatch; refusing a second "
                "reservation authority"
            )

    def _ensure_parent_directory(self) -> None:
        """Create the ledger parent only after link checks.

        We manage the SQLite file, migration lock, and claim files below this
        parent. Every existing ancestor must be a real directory before a
        managed write can occur. The checks are repeated for each managed
        derivative path because a caller may replace one between operations.
        """

        assert self.path is not None
        self._assert_existing_ancestors_not_symlinks(self.path.parent)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._assert_existing_ancestors_not_symlinks(self.path.parent)

    def _open_parent_directory_descriptor(self) -> Optional[int]:
        """Open the managed parent without following a replaceable component.

        POSIX ``dir_fd`` operations keep the immutable claim authority inside
        the directory we inspected, even if a different process later renames
        a pathname ancestor.  Windows does not expose equivalent directory-handle
        operations through ``os``.  The Windows path remains fail-closed on
        every detected reparse point and receives pre/post pathname identity
        checks around projection I/O.
        """

        assert self.path is not None
        if os.name == "nt":
            return None
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self.path.parent.anchor, flags)
        try:
            for component in self.path.parent.parts[1:]:
                child = os.open(component, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise RuntimeError("idempotency ledger parent is not a directory")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _assert_parent_path_matches_descriptor(self) -> None:
        """Reject a pathname that no longer names the trusted parent directory."""

        assert self.path is not None
        if self._closed:
            raise RuntimeError("idempotency ledger is closed")
        self._assert_safe_path(self.path.parent)
        if self._parent_descriptor is None:
            assert self._parent_identity is not None
            if self._path_identity(self.path.parent) != self._parent_identity:
                raise RuntimeError(
                    "idempotency ledger parent changed after initialization; "
                    "refusing redirected projection I/O"
                )
            return
        expected = os.fstat(self._parent_descriptor)
        actual = os.stat(self.path.parent, follow_symlinks=False)
        if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            raise RuntimeError(
                "idempotency ledger parent changed after initialization; "
                "refusing redirected projection I/O"
            )

    @staticmethod
    def _path_identity(path: Path) -> tuple[int, int]:
        status = path.stat(follow_symlinks=False)
        return (status.st_dev, status.st_ino)

    @staticmethod
    def _is_reparse_point(status: os.stat_result) -> bool:
        """Return true for a Windows junction or another reparse point."""

        return bool(
            getattr(status, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        )

    @staticmethod
    def _assert_existing_ancestors_not_symlinks(path: Path) -> None:
        """Reject every existing symlink from the filesystem anchor onward."""

        absolute = path.absolute()
        current = Path(absolute.anchor)
        for component in absolute.parts[1:]:
            current /= component
            try:
                status = current.lstat()
            except FileNotFoundError:
                # A descendant cannot exist when its parent does not exist.
                break
            if stat.S_ISLNK(status.st_mode) or IdempotencyLedger._is_reparse_point(
                status
            ):
                raise RuntimeError(
                    "idempotency ledger managed path must not traverse a symlink"
                )

    def _assert_safe_path(self, path: Path) -> None:
        """Reject a link target and every existing ancestor before I/O."""

        self._assert_existing_ancestors_not_symlinks(path.parent)
        try:
            status = path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(status.st_mode) or self._is_reparse_point(status):
            raise RuntimeError("idempotency ledger managed path must not be a symlink")

    @property
    def _claims_directory(self) -> Path:
        assert self.path is not None
        return self.path.with_name(f".{self.path.name}{self._CLAIM_DIRECTORY_SUFFIX}")

    @property
    def _migration_lock_path(self) -> Path:
        assert self.path is not None
        return self.path.with_name(f"{self.path.name}.migration.lock")

    def _claim_path(self, key: str) -> Path:
        digest = hashlib.sha256(
            self.namespace.encode("utf-8") + b"\0" + key.encode("utf-8")
        ).hexdigest()
        return self._claims_directory / f"{digest}.claim"

    def _claim_filename(self, key: str) -> str:
        return self._claim_path(key).name

    def _read_ledger_file_bytes(self) -> bytes:
        """Read the SQLite/legacy projection from the trusted parent directory."""

        assert self.path is not None
        self._assert_parent_path_matches_descriptor()
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            if self._parent_descriptor is None:
                descriptor = os.open(self.path, flags)
            else:
                descriptor = os.open(
                    self.path.name, flags, dir_fd=self._parent_descriptor
                )
        except OSError as error:
            raise RuntimeError(
                "idempotency ledger projection cannot be read safely"
            ) from error
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise RuntimeError(
                    "idempotency ledger projection is not a regular file"
                )
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 64 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(descriptor)

    def _create_migration_tempfile(self) -> tuple[str, Path]:
        """Create a private migration file in the trusted parent directory."""

        assert self.path is not None
        self._assert_parent_path_matches_descriptor()
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        for _attempt in range(32):
            name = f".{self.path.name}.{secrets.token_hex(16)}.sqlite.tmp"
            try:
                if self._parent_descriptor is None:
                    descriptor = os.open(self.path.parent / name, flags, 0o600)
                else:
                    descriptor = os.open(
                        name, flags, 0o600, dir_fd=self._parent_descriptor
                    )
            except FileExistsError:
                continue
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            return name, self.path.parent / name
        raise RuntimeError("idempotency ledger cannot allocate a migration file")

    def _remove_migration_tempfile(self, name: str, path: Path) -> None:
        try:
            if self._parent_descriptor is None:
                self._assert_parent_path_matches_descriptor()
                path.unlink(missing_ok=True)
            else:
                os.unlink(name, dir_fd=self._parent_descriptor)
        except FileNotFoundError:
            pass

    def _fsync_migration_tempfile(self, name: str, path: Path) -> None:
        self._assert_parent_path_matches_descriptor()
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if self._parent_descriptor is None:
            descriptor = os.open(path, flags)
        else:
            descriptor = os.open(name, flags, dir_fd=self._parent_descriptor)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise RuntimeError("idempotency ledger migration file is not regular")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _replace_ledger_with_migration_tempfile(self, name: str, path: Path) -> None:
        """Atomically replace the projection inside the trusted parent directory."""

        assert self.path is not None
        self._assert_parent_path_matches_descriptor()
        if self._parent_descriptor is None:
            os.replace(path, self.path)
        else:
            os.replace(
                name,
                self.path.name,
                src_dir_fd=self._parent_descriptor,
                dst_dir_fd=self._parent_descriptor,
            )
        self._fsync_parent_directory()

    def _claim_payload(self, key: str, *, run_id: str, reserved_at: str) -> bytes:
        return (
            json.dumps(
                {
                    "namespace": self.namespace,
                    "owner_path": self._owner_path,
                    "reservation_key_sha256": hashlib.sha256(
                        key.encode("utf-8")
                    ).hexdigest(),
                    "reserved_at": reserved_at,
                    "run_id": run_id,
                    "schema_version": self._SCHEMA_VERSION,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )

    def _ensure_claims_directory(self) -> None:
        descriptor = self._open_claims_directory(create=True)
        try:
            self._fsync_descriptor(descriptor)
        finally:
            os.close(descriptor)
        self._fsync_parent_directory()

    def _open_claims_directory(self, *, create: bool) -> int:
        """Open the claim directory relative to the trusted ledger parent."""

        assert self.path is not None
        if self._parent_descriptor is None:
            self._assert_parent_path_matches_descriptor()
            directory = self._claims_directory
            if create:
                try:
                    directory.mkdir(mode=0o700)
                except FileExistsError:
                    pass
            try:
                self._assert_safe_path(directory)
            except FileNotFoundError:
                if not create:
                    raise
                raise
            if not directory.exists() and not create:
                raise FileNotFoundError(directory)
            if not directory.is_dir():
                raise RuntimeError("idempotency ledger claim path is not a directory")
            flags = os.O_RDONLY
            if hasattr(os, "O_DIRECTORY"):
                flags |= os.O_DIRECTORY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            return os.open(directory, flags)

        self._assert_parent_path_matches_descriptor()
        name = self._claims_directory.name
        if create:
            try:
                os.mkdir(name, 0o700, dir_fd=self._parent_descriptor)
            except FileExistsError:
                pass
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(name, flags, dir_fd=self._parent_descriptor)
        except FileNotFoundError:
            if not create:
                raise
            raise RuntimeError("idempotency ledger claim path cannot be opened safely")
        except OSError as error:
            raise RuntimeError(
                "idempotency ledger claim path cannot be opened safely"
            ) from error
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise RuntimeError("idempotency ledger claim path is not a directory")
        return descriptor

    def _read_claim(self, key: str) -> Optional[dict[str, str]]:
        try:
            directory = self._open_claims_directory(create=False)
        except FileNotFoundError:
            return None
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            if self._parent_descriptor is None:
                descriptor = os.open(
                    self._claims_directory / self._claim_filename(key), flags
                )
            else:
                descriptor = os.open(self._claim_filename(key), flags, dir_fd=directory)
        except FileNotFoundError:
            os.close(directory)
            return None
        except OSError as error:
            os.close(directory)
            raise RuntimeError(
                "idempotency ledger claim cannot be read safely; refusing to actuate"
            ) from error
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise RuntimeError(
                    "idempotency ledger claim cannot be read safely; refusing to actuate"
                )
            raw = os.read(descriptor, 64 * 1024)
        finally:
            os.close(descriptor)
            os.close(directory)
        try:
            loaded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError(
                "idempotency ledger claim is invalid; refusing to actuate"
            ) from error
        required = {
            "namespace": self.namespace,
            "owner_path": self._owner_path,
            "reservation_key_sha256": hashlib.sha256(key.encode("utf-8")).hexdigest(),
            "schema_version": self._SCHEMA_VERSION,
        }
        if (
            not isinstance(loaded, dict)
            or any(
                loaded.get(field) != expected for field, expected in required.items()
            )
            or not isinstance(loaded.get("run_id"), str)
            or not isinstance(loaded.get("reserved_at"), str)
        ):
            raise RuntimeError(
                "idempotency ledger claim ownership mismatch; refusing to actuate"
            )
        return {
            field: str(loaded[field]) for field in (*required, "run_id", "reserved_at")
        }

    def _create_claim(self, key: str, *, run_id: str, reserved_at: str) -> None:
        self._ensure_claims_directory()
        directory = self._open_claims_directory(create=False)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            if self._parent_descriptor is None:
                descriptor = os.open(
                    self._claims_directory / self._claim_filename(key),
                    flags,
                    0o600,
                )
            else:
                descriptor = os.open(
                    self._claim_filename(key), flags, 0o600, dir_fd=directory
                )
        except FileExistsError:
            os.close(directory)
            claim = self._read_claim(key)
            self._raise_duplicate(key, None if claim is None else claim["run_id"])
        except OSError as error:
            os.close(directory)
            raise RuntimeError(
                "idempotency ledger claim cannot be created safely; refusing to actuate"
            ) from error
        try:
            payload = self._claim_payload(key, run_id=run_id, reserved_at=reserved_at)
            os.write(descriptor, payload)
            os.fsync(descriptor)
        except BaseException:
            # A partially created claim is deliberately retained. Deleting it
            # could allow a second process to re-actuate after an uncertain
            # crash. Future runs fail closed on the invalid claim.
            raise
        finally:
            os.close(descriptor)
            self._fsync_descriptor(directory)
            os.close(directory)
        self._fsync_parent_directory()

    def _require_claim_owner(self, key: str, *, run_id: str) -> None:
        claim = self._read_claim(key)
        if claim is None:
            raise RuntimeError(
                "idempotency ledger outcome has no reservation or durable claim; "
                "refusing to alter transaction state"
            )
        if claim["run_id"] != run_id:
            raise RuntimeError(
                "idempotency ledger outcome owner mismatch; "
                "refusing to alter transaction state"
            )

    def _is_sqlite(self) -> bool:
        assert self.path is not None
        return (
            self._read_ledger_file_bytes()[: len(self._SQLITE_HEADER)]
            == self._SQLITE_HEADER
        )

    def _migrate_json_projection(self) -> None:
        """Replace one legacy JSON projection while the setup lock is held."""

        assert self.path is not None
        if self._is_sqlite():
            return
        try:
            loaded = json.loads(self._read_ledger_file_bytes().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("legacy idempotency ledger is not valid JSON") from error
        records = self._validated_legacy_records(loaded)
        # Claims are intentionally created before the SQLite replacement. A
        # crash in this interval leaves a safe duplicate refusal. The next
        # migration validates and reuses the same immutable claims.
        for key, record in records.items():
            claim = self._read_claim(key)
            if claim is None:
                self._create_claim(
                    key,
                    run_id=str(record["run_id"]),
                    reserved_at=str(record["reserved_at"]),
                )
            elif (
                claim["run_id"] != record["run_id"]
                or claim["reserved_at"] != record["reserved_at"]
            ):
                raise RuntimeError(
                    "idempotency ledger claim/legacy ownership mismatch; "
                    "refusing to migrate"
                )
        temporary_name, temporary = self._create_migration_tempfile()
        try:
            # Python's sqlite3 API accepts a pathname, not a directory handle.
            # The surrounding parent-identity checks reject a replacement
            # before the projection can authorize a run.  The immutable claim
            # files use descriptor-relative operations and remain the sole
            # at-most-once authority if this pathname is redirected.
            self._assert_parent_path_matches_descriptor()
            connection = sqlite3.connect(temporary, isolation_level=None)
            try:
                self._assert_parent_path_matches_descriptor()
                connection.execute("PRAGMA journal_mode = DELETE")
                connection.execute("PRAGMA synchronous = FULL")
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    CREATE TABLE ledger_metadata(
                        singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                        schema_version TEXT NOT NULL,
                        namespace TEXT NOT NULL,
                        owner_path TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE reservations(
                        namespace TEXT NOT NULL,
                        reservation_key TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        reserved_at TEXT NOT NULL,
                        outcome TEXT,
                        PRIMARY KEY(namespace, reservation_key)
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO ledger_metadata(
                        singleton, schema_version, namespace, owner_path
                    ) VALUES (1, ?, ?, ?)
                    """,
                    (self._SCHEMA_VERSION, self.namespace, self._owner_path),
                )
                connection.executemany(
                    """
                    INSERT INTO reservations(
                        namespace, reservation_key, run_id, reserved_at, outcome
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            self.namespace,
                            key,
                            record["run_id"],
                            record["reserved_at"],
                            record["outcome"],
                        )
                        for key, record in records.items()
                    ],
                )
                connection.commit()
            finally:
                connection.close()
            self._fsync_migration_tempfile(temporary_name, temporary)
            self._replace_ledger_with_migration_tempfile(temporary_name, temporary)
        finally:
            self._remove_migration_tempfile(temporary_name, temporary)

    def _acquire_migration_lock(self) -> int:
        """Take a crash-safe migration lock without trusting a stale file.

        POSIX ``flock`` ownership is released by the kernel when a process
        dies. The lock file can therefore remain after a crash without
        granting a second migration authority. Platforms without ``fcntl``
        use an exclusive create lock and fail closed when it remains present.
        """

        lock_path = self._migration_lock_path
        self._assert_parent_path_matches_descriptor()
        self._assert_safe_path(lock_path)
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if self._parent_descriptor is None:
            descriptor = os.open(lock_path, flags, 0o600)
        else:
            # Darwin can transiently return ENOENT for O_CREAT|O_NOFOLLOW on a
            # descriptor-relative pathname while another process creates the
            # same sibling.  Recheck the retained parent and retry; never fall
            # back to an absolute pathname.
            for attempt in range(4):
                try:
                    descriptor = os.open(
                        lock_path.name,
                        flags,
                        0o600,
                        dir_fd=self._parent_descriptor,
                    )
                    break
                except FileNotFoundError:
                    self._assert_parent_path_matches_descriptor()
                    if attempt == 3:
                        raise
                    time.sleep(0.01)
        deadline = time.monotonic() + self._MIGRATION_WAIT_S
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    if os.fstat(descriptor).st_size == 0:
                        os.write(descriptor, b"\0")
                        os.fsync(descriptor)
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(  # type: ignore[attr-defined]
                        descriptor,
                        msvcrt.LK_NBLCK,  # type: ignore[attr-defined]
                        1,
                    )
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return descriptor
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    os.close(descriptor)
                    raise RuntimeError(
                        "idempotency ledger migration is active; refusing to "
                        "treat the legacy projection as empty"
                    )
                time.sleep(0.02)

    @staticmethod
    def _release_migration_lock(descriptor: int) -> None:
        if os.name == "nt":
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(  # type: ignore[attr-defined]
                descriptor,
                msvcrt.LK_UNLCK,  # type: ignore[attr-defined]
                1,
            )
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

    @staticmethod
    def _validated_legacy_records(
        loaded: object,
    ) -> dict[str, dict[str, Optional[str]]]:
        if not isinstance(loaded, dict):
            raise ValueError("legacy idempotency ledger must be a JSON object")
        records: dict[str, dict[str, Optional[str]]] = {}
        for key, raw in loaded.items():
            if not isinstance(key, str) or not isinstance(raw, dict):
                raise ValueError("legacy idempotency ledger record is invalid")
            run_id = raw.get("run_id")
            reserved_at = raw.get("reserved_at")
            outcome = raw.get("outcome")
            if not isinstance(run_id, str) or not isinstance(reserved_at, str):
                raise ValueError("legacy idempotency ledger record is invalid")
            records[key] = {
                "run_id": run_id,
                "reserved_at": reserved_at,
                "outcome": IdempotencyLedger._normalize_outcome(outcome),
            }
        return records

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _fsync_parent_directory(self) -> None:
        """Durably persist a managed directory entry without reopening it by path."""

        if os.name == "nt":
            return
        if self._parent_descriptor is not None:
            self._fsync_descriptor(self._parent_descriptor)
            return
        assert self.path is not None
        self._fsync_directory(self.path.parent)

    @staticmethod
    def _fsync_descriptor(descriptor: int) -> None:
        if os.name != "nt":
            os.fsync(descriptor)

    @staticmethod
    def _raise_duplicate(key: str, owner: Optional[str]) -> None:
        raise DuplicateActuation(
            f"idempotency key {key!r} already reserved by run {owner!r}"
        )
