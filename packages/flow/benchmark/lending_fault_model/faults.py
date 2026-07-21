"""Fault classes and the ground-truth outcome taxonomy for the lending study.

This is the NON-healthcare mirror of ``benchmark/fault_model/faults.py``. It is
the single source of truth shared by the lending runner
(``benchmark/lending_fault_model/run.py``) and the harness tests
(``tests/e2e/test_lending_fault_model.py``). It defines:

- the transactional fault classes injected at the persistence boundary
  (realized by ``openadapt_flow/mockloan/fault_server.py`` + the flag-gated
  ``?fault=`` hook in ``mockloan/static/app.js``), and
- ``classify()``, which assigns each replay an outcome judged ONLY by the
  backend ledger (ground truth) and whether the replay halted - never by the
  replay's own vision-based self-report.

The consequential write is authorizing a disbursement of funds to a borrower's
loan - an irreversible money-movement write. The outcome taxonomy is identical
to the clinical study's (a record system must never silently get a consequential
write wrong); only the record shape (loan/product/memo vs patient/type/note)
and the domain stakes (double-paying a borrower vs a duplicate chart) differ.

Outcome taxonomy (judged by effect, not by the screen):

- ``SUCCESS``            -- ran to completion and booked exactly one correct,
                            complete disbursement to the target loan.
- ``SAFE-HALT``          -- stopped without completing AND left no side effect
                            (no money moved wrongly). The desired failure mode.
- ``WRONG-ACTION``       -- a wrong write actually landed: a duplicate
                            disbursement (borrower paid twice), a lost update
                            over a concurrent hold, or a wrong/partial row that
                            persisted after a halt.
- ``FALSE-ABORT``        -- the disbursement DID land but the replay reported
                            failure. No silent bad write, but a human retry
                            would double-pay, and the effect is unverified.
- ``UNDETECTED-FAILURE`` -- the replay reported SUCCESS but the disbursement did
                            not happen, or happened wrong (phantom success /
                            partial save). The screen lied and nothing caught
                            it. The most dangerous class for a record system.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# The canonical target of the recorded disbursement workflow.
TARGET_LOAN = "L1001"
TARGET_PRODUCT = "Personal"
TARGET_AMOUNT = "18500"
MEMO_TEXT = "E2E funding ref three months"

SUCCESS = "SUCCESS"
SAFE_HALT = "SAFE-HALT"
WRONG_ACTION = "WRONG-ACTION"
FALSE_ABORT = "FALSE-ABORT"
UNDETECTED_FAILURE = "UNDETECTED-FAILURE"

# Outcomes in which NO wrong write is silently accepted as success. FALSE-ABORT
# is conservative (safe in the moment) but operationally fragile, so it is
# tracked separately from the two clean-safe outcomes.
CLEAN_SAFE_OUTCOMES = frozenset({SUCCESS, SAFE_HALT})


@dataclass(frozen=True)
class Fault:
    """One transactional fault class (or a control) exercised by the study."""

    mode: str  # the ?fault=<mode> value
    title: str  # short human title
    fault_class: str  # the review's failure class this belongs to
    injected: str  # where/how the fault is injected
    seed_concurrent: bool  # seed a concurrent-actor row before the run
    expected_outcome: str  # the outcome this study documents as current behavior
    headline: str  # one-line takeaway


# Order matters: controls first/last frame the transactional classes between.
FAULTS: tuple[Fault, ...] = (
    Fault(
        mode="ok",
        title="Clean disbursement (control)",
        fault_class="(control) no fault",
        injected="core books the disbursement normally",
        seed_concurrent=False,
        expected_outcome=SUCCESS,
        headline="Baseline: the write lands and the replay agrees.",
    ),
    Fault(
        mode="partial",
        title="Partial save",
        fault_class="1. Partial save",
        injected="core books the row but drops the funding memo field",
        seed_concurrent=False,
        expected_outcome=UNDETECTED_FAILURE,
        headline=(
            "UI says authorized and the replay reports success, but the booked "
            "row is missing the funding memo - no postcondition reads the ledger."
        ),
    ),
    Fault(
        mode="duplicate",
        title="Duplicate submission / non-idempotent retry",
        fault_class="2. Duplicate submission",
        injected="the authorize is submitted twice with no idempotency key",
        seed_concurrent=False,
        expected_outcome=WRONG_ACTION,
        headline=(
            "Two disbursements are booked; the borrower is paid twice while the "
            "replay reports a single clean success. Classic non-idempotency."
        ),
    ),
    Fault(
        mode="timeout",
        title="Core timeout after successful write",
        fault_class="3. Timeout after write",
        injected="core books the row, then hangs past the client timeout",
        seed_concurrent=False,
        expected_outcome=FALSE_ABORT,
        headline=(
            "The money moved but the replay reports failure - safe in the "
            "moment, yet a naive human/agent retry would double-pay."
        ),
    ),
    Fault(
        mode="optimistic",
        title="Optimistic UI success then core rejection",
        fault_class="4. Optimistic-UI success, async reject",
        injected="UI paints authorized immediately; the core rejects the write",
        seed_concurrent=False,
        expected_outcome=UNDETECTED_FAILURE,
        headline=(
            "The screen says authorized, the replay reports success, and NOTHING "
            "was booked. Phantom success - the headline undetected failure."
        ),
    ),
    Fault(
        mode="session",
        title="Session expiry mid-workflow",
        fault_class="5. Session expiry",
        injected="the write returns 401; the app bounces to the login screen",
        seed_concurrent=False,
        expected_outcome=SAFE_HALT,
        headline=(
            "The authorized-banner postcondition is not met (login screen shows) "
            "so the replay halts with no side effect. Correctly handled."
        ),
    ),
    Fault(
        mode="stale",
        title="Stale data / concurrent modification",
        fault_class="6. Stale data / concurrent modification",
        injected="last-write-wins over a loan a concurrent officer just held",
        seed_concurrent=True,
        expected_outcome=WRONG_ACTION,
        headline=(
            "A concurrent officer's URGENT fraud hold is silently overwritten "
            "(lost update); the disbursement proceeds and the replay reports "
            "a clean success."
        ),
    ),
    Fault(
        mode="double",
        title="Double-click registered by the environment",
        fault_class="7. Double-click delivered twice",
        injected="the authorize click is delivered twice, both reach the core",
        seed_concurrent=False,
        expected_outcome=WRONG_ACTION,
        headline=(
            "Same effect as a non-idempotent retry: two disbursements booked, "
            "one reported success. The replayer has no at-most-once guard."
        ),
    ),
    Fault(
        mode="idempotent",
        title="Idempotency key (the recommended fix)",
        fault_class="(fix) at-most-once via idempotency key",
        injected="authorize submitted twice, but the core de-duplicates on a key",
        seed_concurrent=False,
        expected_outcome=SUCCESS,
        headline=(
            "With an idempotency key the double-submit collapses to one "
            "disbursement: the duplicate/double-click hazard is neutralized."
        ),
    ),
)

FAULTS_BY_MODE = {f.mode: f for f in FAULTS}


def classify(
    *,
    report_success: bool,
    records: list[dict],
    seeded_concurrent: bool,
) -> tuple[str, str]:
    """Assign an outcome from ledger ground truth and the replay's halt state.

    Args:
        report_success: whether the replay's RunReport reported success
            (i.e. every step, including the vision postcondition on authorize,
            passed). This is the SELF-REPORT the study is checking.
        records: the backend ledger snapshot's ``records`` list after the run.
        seeded_concurrent: whether a concurrent-actor row was seeded before
            the run (only ``stale`` mode seeds one).

    Returns:
        ``(outcome, reason)`` - the taxonomy label plus a short explanation.
    """
    replay_rows = [r for r in records if r.get("source") == "replay"]
    other_rows = [r for r in records if r.get("source") == "other"]
    n_writes = len(replay_rows)
    correct = [
        r
        for r in replay_rows
        if r.get("loan_id") == TARGET_LOAN
        and r.get("product") == TARGET_PRODUCT
        and r.get("memo") == MEMO_TEXT
    ]
    n_correct = len(correct)
    concurrent_lost = seeded_concurrent and not other_rows

    # A duplicate/multiple write is a wrong action regardless of self-report.
    if n_writes > 1:
        return (
            WRONG_ACTION,
            f"{n_writes} disbursements booked (duplicate write); "
            f"replay reported success={report_success}",
        )
    # Overwriting a concurrent actor's row is a lost update.
    if concurrent_lost:
        return (
            WRONG_ACTION,
            "concurrent officer's hold was overwritten (lost update); "
            f"replay reported success={report_success}",
        )

    if report_success:
        if n_correct == 1 and n_writes == 1:
            return SUCCESS, "one correct, complete disbursement booked"
        if n_writes == 0:
            return (
                UNDETECTED_FAILURE,
                "replay reported success but NOTHING was booked (phantom success)",
            )
        return (
            UNDETECTED_FAILURE,
            "replay reported success but the booked row is wrong/"
            "incomplete (e.g. missing memo)",
        )

    # Replay halted (report_success is False).
    if n_correct >= 1:
        return (
            FALSE_ABORT,
            "the correct disbursement WAS booked but the replay reported "
            "failure (effect unverified; a retry would double-pay)",
        )
    if n_writes == 0:
        return SAFE_HALT, "halted with no side effect"
    return (
        WRONG_ACTION,
        "halted but a wrong/partial disbursement persisted",
    )


def is_silently_mishandled(outcome: str, report_success: bool) -> bool:
    """Whether the current system claimed success while the effect was bad.

    True exactly when the replay reported success but ground truth says the
    write was a duplicate, a phantom, a partial, or a lost update - the
    failures a record system must never accept silently.
    """
    return report_success and outcome in (WRONG_ACTION, UNDETECTED_FAILURE)


def outcome_is_safe(outcome: Optional[str]) -> bool:
    """Whether the outcome accepted no silent bad write (SUCCESS/SAFE-HALT)."""
    return outcome in CLEAN_SAFE_OUTCOMES
