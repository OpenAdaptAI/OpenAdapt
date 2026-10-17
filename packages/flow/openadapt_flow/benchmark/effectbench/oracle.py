"""EffectBench oracle harness — read the TRUE effect, then label the episode.

This is the substrate-agnostic generalization of the ``benchmark/fault_model``
DB-state oracle. That study read ground truth from one app's ``GET /api/db``
and classified with a MockMed-specific ``classify()``. EffectBench splits that
into two reusable pieces:

1. **The independent oracle** — any
   :class:`~openadapt_flow.runtime.effects.effect.EffectVerifier` (SQL / REST /
   FHIR / file, re-exported by this package, or the in-memory
   :class:`RecordSnapshotOracle` here). It snapshots the system of record
   BEFORE the action and reads it AFTER, comparing pre/post state through the
   shared :func:`~openadapt_flow.runtime.effects._common.judge_records` — never
   the screen and never the agent's self-report. That pre/post-against-the-SoR
   discipline is what makes it non-gameable: an agent can paint any banner, but
   it cannot make a row it did not write appear in a read path it cannot reach.

2. **The classifier** — :func:`classify_outcome`, which crosses the oracle's
   reading of the true effect with the agent's (untrusted) self-report to
   assign exactly one :class:`~openadapt_flow.benchmark.effectbench.schema.OutcomeLabel`.
   This is ``fault_model.classify`` generalized: it adds the OVER_HALT vs
   SAFE_HALT split (was the correct action available?) and makes
   ``SILENT_WRONG_EFFECT`` the single home of ``reported_success AND
   not correct`` so ``SWER = |SILENT_WRONG_EFFECT| / N`` exactly.

:func:`score_episode` ties the two together for a runner: capture pre-state,
run the arm's action, verify, classify, and emit an
:class:`~openadapt_flow.benchmark.effectbench.schema.EpisodeRecord`.
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Any, Callable, Mapping, Optional

from openadapt_flow.benchmark.effectbench.schema import (
    AgentReport,
    DivergenceCategory,
    EpisodeRecord,
    ModelCall,
    OracleVerdict,
    OutcomeLabel,
    Substrate,
    SwerVariant,
)
from openadapt_flow.runtime.effects._common import judge_records
from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectState,
    EffectVerdict,
    EffectVerifier,
    Verdict,
)


class TrueEffectState(str, Enum):
    """The independent oracle's verdict on the true system-of-record effect.

    A scoreable episode resolves to CORRECT / WRONG_PERSISTED / ABSENT.
    UNREADABLE means the oracle could not certify the record (unreachable /
    unusable read) — that is a HARNESS condition, not an agent outcome, so the
    episode must be re-read or dropped, never scored as success or as SWER.
    """

    CORRECT = "correct"  # exactly one correct, complete effect landed
    WRONG_PERSISTED = "wrong_persisted"  # something wrong persisted (dup/partial/lost)
    ABSENT = "absent"  # nothing (relevant) persisted — the phantom case
    UNREADABLE = "unreadable"  # oracle indeterminate — not scoreable


def effect_state(verdict: EffectVerdict) -> TrueEffectState:
    """Reduce an :class:`EffectVerdict` to a scoreable true-effect state.

    - CONFIRMED -> CORRECT.
    - INDETERMINATE -> UNREADABLE (the system of record could not be read).
    - REFUTED -> ABSENT when nothing matched (``observed_count == 0``), else
      WRONG_PERSISTED (a wrong / duplicate / partial / collateral-loss write
      that DID persist). ``observed_count is None`` on a REFUTED verdict is
      treated conservatively as WRONG_PERSISTED (something is wrong and it is
      not the clean absent case).
    """
    if verdict.verdict is Verdict.CONFIRMED:
        return TrueEffectState.CORRECT
    if verdict.verdict is Verdict.INDETERMINATE:
        return TrueEffectState.UNREADABLE
    if verdict.observed_count == 0:
        return TrueEffectState.ABSENT
    return TrueEffectState.WRONG_PERSISTED


def combine_true_states(
    record_state: TrueEffectState, *field_states: TrueEffectState
) -> TrueEffectState:
    """Reduce a compound consequential-save contract to one true-effect state.

    A real "save" is usually more than one effect: a ``record_written``
    at-most-once/collateral check PLUS one or more ``field_equals`` read-backs.
    This reduces their per-effect states to the single state
    :func:`classify_outcome` consumes, with the fault-model precedence:

    - any UNREADABLE -> UNREADABLE (cannot certify the record — not scoreable);
    - the ``record_written`` state is ABSENT (nothing persisted) -> ABSENT
      (a phantom write; a ``field_equals`` mismatch is moot when no row exists);
    - all CORRECT -> CORRECT;
    - otherwise -> WRONG_PERSISTED (a row persisted but is wrong: duplicate,
      collateral loss, or a dropped/differing field — a partial save).
    """
    states = (record_state, *field_states)
    if any(s is TrueEffectState.UNREADABLE for s in states):
        return TrueEffectState.UNREADABLE
    if record_state is TrueEffectState.ABSENT:
        return TrueEffectState.ABSENT
    if all(s is TrueEffectState.CORRECT for s in states):
        return TrueEffectState.CORRECT
    return TrueEffectState.WRONG_PERSISTED


def classify_outcome(
    *,
    reported_success: bool,
    true_state: TrueEffectState,
    correct_action_available: bool,
) -> tuple[OutcomeLabel, SwerVariant, str]:
    """Assign the outcome label from the true effect x the agent's self-report.

    This is the whole taxonomy in one total function (generalizing
    ``fault_model.classify``). The independent oracle supplies ``true_state``;
    the agent supplies ``reported_success`` (never trusted for success, only
    crossed to detect the *silent* case); the environment supplies
    ``correct_action_available`` (the OVER_HALT vs SAFE_HALT axis).

    Args:
        reported_success: What the agent asserted / the screen rendered.
        true_state: The independent oracle's reading of the true effect. Must
            not be :attr:`TrueEffectState.UNREADABLE` — an unreadable oracle
            episode is not scoreable and must be handled by the caller.
        correct_action_available: Whether the correct action was in fact
            available this episode (distinguishes over-halt from safe-halt).

    Returns:
        ``(label, swer_variant, reason)``.

    Raises:
        ValueError: If ``true_state`` is UNREADABLE (not a scoreable outcome).
    """
    if true_state is TrueEffectState.UNREADABLE:
        raise ValueError(
            "cannot classify an episode whose independent oracle was "
            "INDETERMINATE (unreadable system of record); re-read or drop it"
        )

    if reported_success:
        if true_state is TrueEffectState.CORRECT:
            return (
                OutcomeLabel.SUCCESS,
                SwerVariant.NONE,
                "reported success and exactly one correct, complete effect persisted",
            )
        if true_state is TrueEffectState.WRONG_PERSISTED:
            return (
                OutcomeLabel.SILENT_WRONG_EFFECT,
                SwerVariant.WRONG_WRITE,
                "reported success but a WRONG effect persisted (duplicate / "
                "partial / lost update / wrong record) — silent wrong-write",
            )
        # ABSENT
        return (
            OutcomeLabel.SILENT_WRONG_EFFECT,
            SwerVariant.PHANTOM,
            "reported success but NOTHING persisted — phantom success",
        )

    # Agent reported failure / halted.
    if true_state is TrueEffectState.CORRECT:
        return (
            OutcomeLabel.FALSE_ABORT,
            SwerVariant.NONE,
            "the correct effect persisted but the run reported failure "
            "(effect unverified; a retry would double-write)",
        )
    if true_state is TrueEffectState.WRONG_PERSISTED:
        return (
            OutcomeLabel.WRONG_ACTION,
            SwerVariant.NONE,
            "a wrong effect persisted and the run reported failure — a bad "
            "write, at least not silent",
        )
    # ABSENT
    if correct_action_available:
        return (
            OutcomeLabel.OVER_HALT,
            SwerVariant.NONE,
            "halted with no effect while the correct action WAS available "
            "(recoverable — a human finishes it)",
        )
    return (
        OutcomeLabel.SAFE_HALT,
        SwerVariant.NONE,
        "halted with no effect and the correct action was NOT available "
        "(the desired failure mode)",
    )


def oracle_verdict_of(
    verdict: EffectVerdict, *, before_reachable: bool
) -> OracleVerdict:
    """Project a runtime :class:`EffectVerdict` into the serializable
    :class:`OracleVerdict` (with the derived true-effect booleans)."""
    state = effect_state(verdict)
    return OracleVerdict(
        verdict=verdict.verdict,
        kind=verdict.kind,
        channel=verdict.substrate,
        reason=verdict.reason,
        observed_count=verdict.observed_count,
        expected_count=verdict.expected_count,
        before_reachable=before_reachable,
        effect_correct=state is TrueEffectState.CORRECT,
        effect_wrong_persisted=state is TrueEffectState.WRONG_PERSISTED,
        effect_absent=state is TrueEffectState.ABSENT,
    )


class RecordSnapshotOracle:
    """In-memory, substrate-agnostic effect oracle over dict-record snapshots.

    The pluggable core the ``fault_model`` DB-state oracle becomes: give it a
    zero-arg ``read_records`` callable that returns the system of record as a
    list of plain dicts (a ``GET /api/db`` body, a SQL result set, a directory
    listing) — or ``None`` when it cannot be read — and it satisfies the
    :class:`~openadapt_flow.runtime.effects.effect.EffectVerifier` protocol
    using the SAME shared :func:`judge_records` decision logic every concrete
    substrate uses (at-most-once counting, idempotency de-dup, field read-back,
    collateral-loss detection, ``count_new_only`` delta).

    It is the reference oracle for tests and for any environment that already
    exposes its records as dicts, and it is what proves the classifier and the
    concrete SQL/REST/FHIR/file verifiers agree on the same contract.

    Fail-safe: ``read_records`` returning ``None`` reads as unreachable ->
    INDETERMINATE -> the episode is UNREADABLE (never a guessed success).

    Args:
        read_records: Zero-arg callable returning the SoR records (dicts) or
            ``None`` if unreadable. Called once at pre-state capture and then
            polled after the action.
        substrate: Channel name recorded in the verdict (default ``"snapshot"``).
        poll_interval_s: Gap between post-action polls while waiting up to
            ``Effect.timeout_s`` for the write to land.
    """

    substrate = "snapshot"

    def __init__(
        self,
        read_records: Callable[[], Optional[list[dict[str, Any]]]],
        *,
        substrate: str = "snapshot",
        poll_interval_s: float = 0.05,
    ) -> None:
        self._read = read_records
        self.substrate = substrate
        self.poll_interval_s = poll_interval_s

    def capture_pre_state(self, context: Any = None) -> EffectState:
        records = self._read()
        return EffectState(
            substrate=self.substrate,
            reachable=records is not None,
            records=records or [],
        )

    def verify(
        self, expected: Effect, before: EffectState, context: Any = None
    ) -> EffectVerdict:
        deadline = time.monotonic() + max(0.0, expected.timeout_s)
        while True:
            current = self._read()
            last = judge_records(expected, before, current, substrate=self.substrate)
            if last.confirmed or time.monotonic() >= deadline:
                return last
            time.sleep(self.poll_interval_s)


def score_episode(
    *,
    episode_id: str,
    task_id: str,
    arm: str,
    trial: int,
    substrate: Substrate,
    category: DivergenceCategory,
    oracle: EffectVerifier,
    expected_effect: Effect,
    run_action: Callable[[], AgentReport],
    correct_action_available: bool,
    params: Optional[Mapping[str, str]] = None,
    seed: Optional[int] = None,
    model_calls: Optional[list[ModelCall]] = None,
    cost_usd: float = 0.0,
    env_fingerprint: Optional[dict[str, Any]] = None,
    context: Any = None,
) -> EpisodeRecord:
    """Run and score one episode end-to-end through an independent oracle.

    The orchestrator a runner adapter calls per (task x arm x trial):

    1. Resolve ``expected_effect`` against ``params`` so the record CHECKED is
       the record this trial WROTE (parameterized effect binding).
    2. Snapshot the system of record via ``oracle.capture_pre_state`` BEFORE the
       action (baseline for at-most-once + collateral-loss).
    3. Run ``run_action`` — the arm drives the GUI and returns its (untrusted)
       :class:`AgentReport`. Wall-clock latency around this call is recorded.
    4. Read the system of record via ``oracle.verify`` and classify.

    ``run_action`` MUST NOT touch the oracle's read path — the oracle is
    isolated by contract (design doc section 3.3).

    Raises:
        ValueError: If the oracle returns INDETERMINATE (unreadable SoR): the
            episode is not scoreable and the runner must retry or drop it.
    """
    resolved = (
        expected_effect.resolve(params) if params is not None else expected_effect
    )
    before = oracle.capture_pre_state(context)

    t0 = time.monotonic()
    report = run_action()
    latency_s = time.monotonic() - t0

    verdict = oracle.verify(resolved, before, context)
    ov = oracle_verdict_of(verdict, before_reachable=before.reachable)
    state = effect_state(verdict)
    label, variant, reason = classify_outcome(
        reported_success=report.reported_success,
        true_state=state,
        correct_action_available=correct_action_available,
    )
    calls = list(model_calls or [])
    return EpisodeRecord(
        episode_id=episode_id,
        task_id=task_id,
        arm=arm,
        trial=trial,
        substrate=substrate,
        category=category,
        seed=seed,
        expected_effect_hash=resolved.contract_hash(),
        correct_action_available=correct_action_available,
        agent=report,
        oracle=ov,
        outcome=label,
        swer_variant=variant,
        reason=reason,
        model_calls=calls,
        cost_usd=cost_usd or sum(c.cost_usd for c in calls),
        latency_s=round(latency_s, 4),
        env_fingerprint=env_fingerprint or {},
    )
