"""Fault classes and the ground-truth outcome taxonomy for the study.

This module is the single source of truth shared by the benchmark runner
(``benchmark/fault_model/run.py``) and the harness tests
(``tests/e2e/test_fault_model.py``). It defines:

- the transactional fault classes injected at the persistence boundary
  (realized by ``openadapt_flow/mockmed/fault_server.py`` + the flag-gated
  ``?fault=`` hook in ``mockmed/static/app.js``), and
- ``classify()``, which assigns each replay an outcome judged ONLY by the
  backend DB (ground truth) and whether the replay halted — never by the
  replay's own vision-based self-report.

Outcome taxonomy (judged by effect, not by the screen):

- ``SUCCESS``            -- ran to completion and wrote exactly one correct,
                            complete encounter to the target patient.
- ``SAFE-HALT``          -- stopped without completing AND left no side
                            effect (nothing wrong was written). The desired
                            failure mode for an un-handleable fault.
- ``WRONG-ACTION``       -- a wrong write actually landed: a duplicate row, a
                            lost update over a concurrent change, or a
                            wrong/partial row that persisted after a halt.
- ``FALSE-ABORT``        -- the write DID land but the replay reported
                            failure. No silent bad write, but a human retry
                            would double-write, and the effect is unverified.
- ``UNDETECTED-FAILURE`` -- the replay reported SUCCESS but the write did not
                            happen, or happened wrong (phantom success /
                            partial save). The screen lied and nothing caught
                            it. The most dangerous class for a record system.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# The canonical target of the recorded triage-save workflow.
TARGET_PATIENT = "p1"
TARGET_TYPE = "Triage"
NOTE_TEXT = "E2E triage booking three months"

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
        title="Clean write (control)",
        fault_class="(control) no fault",
        injected="backend persists the row normally",
        seed_concurrent=False,
        expected_outcome=SUCCESS,
        headline="Baseline: the write lands and the replay agrees.",
    ),
    Fault(
        mode="partial",
        title="Partial save",
        fault_class="1. Partial save",
        injected="backend commits the row but drops the note field",
        seed_concurrent=False,
        expected_outcome=UNDETECTED_FAILURE,
        headline=(
            "UI says saved and the replay reports success, but the persisted "
            "row is missing the clinical note — no postcondition reads the DB."
        ),
    ),
    Fault(
        mode="duplicate",
        title="Duplicate submission / non-idempotent retry",
        fault_class="2. Duplicate submission",
        injected="the save is submitted twice with no idempotency key",
        seed_concurrent=False,
        expected_outcome=WRONG_ACTION,
        headline=(
            "Two encounter rows are written; the replay reports a single "
            "clean success. Classic non-idempotency hazard, undetected."
        ),
    ),
    Fault(
        mode="timeout",
        title="Backend timeout after successful write",
        fault_class="3. Timeout after write",
        injected="backend commits the row, then hangs past the client timeout",
        seed_concurrent=False,
        expected_outcome=FALSE_ABORT,
        headline=(
            "The row landed but the replay reports failure — safe in the "
            "moment, yet a naive human/agent retry would double-write."
        ),
    ),
    Fault(
        mode="optimistic",
        title="Optimistic UI success then server rejection",
        fault_class="4. Optimistic-UI success, async reject",
        injected="UI paints success immediately; the server rejects the write",
        seed_concurrent=False,
        expected_outcome=UNDETECTED_FAILURE,
        headline=(
            "The screen says saved, the replay reports success, and NOTHING "
            "is in the DB. Phantom success — the headline undetected failure."
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
            "The saved-banner postcondition is not met (login screen shows) "
            "so the replay halts with no side effect. Correctly handled."
        ),
    ),
    Fault(
        mode="stale",
        title="Stale data / concurrent modification",
        fault_class="6. Stale data / concurrent modification",
        injected="last-write-wins over a row a concurrent actor just changed",
        seed_concurrent=True,
        expected_outcome=WRONG_ACTION,
        headline=(
            "A concurrent clinician's urgent note is silently overwritten "
            "(lost update); the replay reports a clean success."
        ),
    ),
    Fault(
        mode="double",
        title="Double-click registered by the environment",
        fault_class="7. Double-click delivered twice",
        injected="the save click is delivered twice, both reach the backend",
        seed_concurrent=False,
        expected_outcome=WRONG_ACTION,
        headline=(
            "Same effect as a non-idempotent retry: two rows written, one "
            "reported success. The replayer has no at-most-once guard."
        ),
    ),
    Fault(
        mode="idempotent",
        title="Idempotency key (the recommended fix)",
        fault_class="(fix) at-most-once via idempotency key",
        injected="save submitted twice, but the server de-duplicates on a key",
        seed_concurrent=False,
        expected_outcome=SUCCESS,
        headline=(
            "With an idempotency key the double-submit collapses to one row: "
            "the duplicate/double-click hazard is neutralized."
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
    """Assign an outcome from DB ground truth and the replay's halt state.

    Args:
        report_success: whether the replay's RunReport reported success
            (i.e. every step, including the vision postcondition on save,
            passed). This is the SELF-REPORT the study is checking.
        records: the backend DB snapshot's ``records`` list after the run.
        seeded_concurrent: whether a concurrent-actor row was seeded before
            the run (only ``stale`` mode seeds one).

    Returns:
        ``(outcome, reason)`` — the taxonomy label plus a short explanation.
    """
    replay_rows = [r for r in records if r.get("source") == "replay"]
    other_rows = [r for r in records if r.get("source") == "other"]
    n_writes = len(replay_rows)
    correct = [
        r
        for r in replay_rows
        if r.get("patient_id") == TARGET_PATIENT
        and r.get("type") == TARGET_TYPE
        and r.get("note") == NOTE_TEXT
    ]
    n_correct = len(correct)
    concurrent_lost = seeded_concurrent and not other_rows

    # A duplicate/multiple write is a wrong action regardless of self-report.
    if n_writes > 1:
        return (
            WRONG_ACTION,
            f"{n_writes} encounter rows written (duplicate write); "
            f"replay reported success={report_success}",
        )
    # Overwriting a concurrent actor's row is a lost update.
    if concurrent_lost:
        return (
            WRONG_ACTION,
            "concurrent actor's row was overwritten (lost update); "
            f"replay reported success={report_success}",
        )

    if report_success:
        if n_correct == 1 and n_writes == 1:
            return SUCCESS, "one correct, complete row written"
        if n_writes == 0:
            return (
                UNDETECTED_FAILURE,
                "replay reported success but NOTHING was persisted "
                "(phantom success)",
            )
        return (
            UNDETECTED_FAILURE,
            "replay reported success but the persisted row is wrong/"
            "incomplete (e.g. missing note)",
        )

    # Replay halted (report_success is False).
    if n_correct >= 1:
        return (
            FALSE_ABORT,
            "the correct row WAS persisted but the replay reported failure "
            "(effect unverified; a retry would double-write)",
        )
    if n_writes == 0:
        return SAFE_HALT, "halted with no side effect"
    return (
        WRONG_ACTION,
        "halted but a wrong/partial row persisted",
    )


def is_silently_mishandled(outcome: str, report_success: bool) -> bool:
    """Whether the current system claimed success while the effect was bad.

    True exactly when the replay reported success but ground truth says the
    write was a duplicate, a phantom, a partial, or a lost update — the
    failures a record system must never accept silently.
    """
    return report_success and outcome in (WRONG_ACTION, UNDETECTED_FAILURE)


def outcome_is_safe(outcome: Optional[str]) -> bool:
    """Whether the outcome accepted no silent bad write (SUCCESS/SAFE-HALT)."""
    return outcome in CLEAN_SAFE_OUTCOMES
