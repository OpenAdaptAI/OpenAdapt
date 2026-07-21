"""The public synthetic MockMed task pack (the reference anchor suite).

Nine authored tasks over the synthetic :mod:`effectbench.fixtures.mockmed`
system of record, one per fault mode the fault-model study catalogs. Each is
designed so the green screen (task-success) diverges from the correct business
effect, and each is scored by the INDEPENDENT MockMed oracle -- never the
banner. This is the CI-fast, no-Docker anchor that reproduces the published
headline (screen-only SWER 55.6% -> effect-verified 0.0% over 90 runs; 5 of 7
transactional faults silently mishandled by screen-only verification).

Category coverage of this public sample: C1 partial-save, C2 duplicate, C3
optimistic/reject (optimistic, timeout, session), C4 stale-overwrite, C5
double-delivered, plus clean/idempotent controls -- all on the ``web`` substrate.
The full taxonomy (C6 wrong-record/homonym, C7 silent-noop/wrong-target and the
desktop / remote-display substrates) is DEFINED in ``SPEC.md`` and exercised by
the container-gated real-system-of-record packs and the private hardened corpus,
which stay outside this synthetic sample by the source-availability boundary.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from effectbench.effect import Effect, EffectKind, ValueExpr
from effectbench.fixtures.mockmed import TARGET_PATIENT, TARGET_TYPE
from effectbench.schema import (
    DivergenceCategory,
    OracleChannel,
    OracleSpec,
    Substrate,
    TaskSpec,
)

# Trial-unique payload params (bound per trial by :func:`trial_params`).
PARAM_NOTE = "note"
PARAM_RECORD_KEY = "record_key"


def trial_params(task_id: str, trial: int) -> dict[str, str]:
    """A deterministic, TRIAL-UNIQUE payload derived from ``(task, trial)``.

    The note (the consequential free text) and the idempotency key are unique
    per trial, so the oracle checks THIS run's exact effect and cross-trial
    contamination is detectable.
    """
    tag = hashlib.sha256(f"{task_id}:{trial}".encode()).hexdigest()[:12]
    return {PARAM_NOTE: f"triage note {tag}", PARAM_RECORD_KEY: f"key-{tag}"}


# The consequential-save contract: exactly one Triage encounter for the target
# patient (at-most-once + no collateral loss) whose note equals the run's note.
def _record_effect() -> Effect:
    return Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"patient_id": TARGET_PATIENT, "type": TARGET_TYPE},
        expected_count=1,
        forbid_collateral_loss=True,
        timeout_s=0.0,
        probe="exactly one Triage encounter for the target patient",
    )


def _note_effect() -> Effect:
    return Effect(
        kind=EffectKind.FIELD_EQUALS,
        match={"patient_id": TARGET_PATIENT, "type": TARGET_TYPE},
        field="note",
        value=ValueExpr(param=PARAM_NOTE),
        timeout_s=0.0,
        probe="the saved encounter carries this run's note",
    )


@dataclass(frozen=True)
class MockMedTask:
    """One authored MockMed task plus how the fixture is driven for it."""

    spec: TaskSpec
    #: The fault mode injected on the write (see :mod:`effectbench.fixtures.mockmed`).
    fault: str
    #: How many times the intended action is delivered (2 for double-submit).
    n_posts: int
    #: Whether the reset plants a concurrent actor's row (the stale test).
    seed_concurrent: bool
    #: Whether the correct action was in fact available (over-halt vs safe-halt).
    correct_action_available: bool
    #: The extra sub-effect(s) the compound oracle also checks (the note).
    extra_effects: tuple[Effect, ...] = field(default_factory=tuple)


def _task(
    *,
    mode: str,
    title: str,
    category: DivergenceCategory,
    fault: str,
    n_posts: int = 1,
    seed_concurrent: bool = False,
    correct_action_available: bool,
    trial_unique: bool = True,
) -> MockMedTask:
    spec = TaskSpec(
        task_id=f"mockmed::{mode}",
        title=title,
        substrate=Substrate.WEB,
        category=category,
        goal=(
            "Record a Triage encounter for the referred patient with the "
            "clinical note, then save it."
        ),
        expected_effect=_record_effect(),
        oracle=OracleSpec(
            channel=OracleChannel.SNAPSHOT,
            description=(
                "MockMed persistence-boundary readback (independent record read); "
                "record_written at-most-once + no collateral loss + note read-back."
            ),
            isolated_from_agent=True,
            trial_unique_payload=trial_unique,
            refusal_controls=False,
            adversarially_audited=True,
        ),
        reversible=False,
        notes=f"fault mode: {mode}",
    )
    return MockMedTask(
        spec=spec,
        fault=fault,
        n_posts=n_posts,
        seed_concurrent=seed_concurrent,
        correct_action_available=correct_action_available,
        extra_effects=(_note_effect(),),
    )


# The nine anchor tasks (states straight from the fault-model study taxonomy).
MOCKMED_TASKS: tuple[MockMedTask, ...] = (
    _task(
        mode="ok", title="Clean save (control)",
        category=DivergenceCategory.CONTROL, fault="ok",
        correct_action_available=True,
    ),
    _task(
        mode="partial", title="Partial save drops the note",
        category=DivergenceCategory.C1_PARTIAL_SAVE, fault="partial",
        correct_action_available=False,
    ),
    _task(
        mode="duplicate", title="Duplicate submission writes two rows",
        category=DivergenceCategory.C2_DUPLICATE_SUBMISSION, fault="duplicate",
        n_posts=2, correct_action_available=False,
    ),
    _task(
        mode="timeout", title="Commit-then-timeout (committed, UI errored)",
        category=DivergenceCategory.C3_OPTIMISTIC_THEN_REJECT, fault="timeout",
        correct_action_available=True,
    ),
    _task(
        mode="optimistic", title="Optimistic UI success the server rejects",
        category=DivergenceCategory.C3_OPTIMISTIC_THEN_REJECT, fault="optimistic",
        correct_action_available=False,
    ),
    _task(
        mode="session", title="Session expired (nothing persisted, UI errored)",
        category=DivergenceCategory.C3_OPTIMISTIC_THEN_REJECT, fault="session",
        correct_action_available=False,
    ),
    _task(
        mode="stale", title="Stale last-write-wins clobbers a concurrent edit",
        category=DivergenceCategory.C4_STALE_OVERWRITE, fault="stale",
        seed_concurrent=True, correct_action_available=False,
    ),
    _task(
        mode="double", title="Double-delivered click writes two rows",
        category=DivergenceCategory.C5_DOUBLE_DELIVERED_INPUT, fault="double",
        n_posts=2, correct_action_available=False,
    ),
    _task(
        mode="idempotent", title="Idempotent retry de-duplicates (the fix, control)",
        category=DivergenceCategory.CONTROL, fault="idempotent",
        n_posts=2, correct_action_available=True,
    ),
)

# Transactional fault modes (exclude the clean + idempotent-fix controls) --
# the denominator for the "5 of 7 silently mishandled" headline.
TRANSACTIONAL_MODES: tuple[str, ...] = (
    "partial", "duplicate", "timeout", "optimistic", "session", "stale", "double",
)


def by_id(task_id: str) -> MockMedTask:
    for t in MOCKMED_TASKS:
        if t.spec.task_id == task_id:
            return t
    raise KeyError(task_id)
