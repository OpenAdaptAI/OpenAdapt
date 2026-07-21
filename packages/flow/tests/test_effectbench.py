"""EffectBench foundation tests.

Three layers:

1. The reference re-expression regression: the new schema + oracle harness
   reproduce the ``benchmark/fault_model`` headline (5 of 7 transactional fault
   classes silently mishandled by screen-only; 0 by the effect oracle) and the
   ``benchmark/silent_wrong_action`` proto-SWER rate (55.6% -> 0.0% over 90
   runs). This is the load-bearing pin for the whole contract.
2. Unit tests of the classifier truth table, the substrate-agnostic
   record-snapshot oracle (partial / duplicate / phantom / collateral-loss /
   unreadable), and the metrics (SWER split, over-halt, gap, Wilson, pass^k).
3. A parity check that EffectBench's silent-wrong label agrees with the
   original ``fault_model.is_silently_mishandled`` on the same DB states.
"""

from __future__ import annotations

import pytest

from benchmark.effectbench.reference_fault_model import (
    NOTE_EFFECT,
    RECORD_EFFECT,
    SCENARIOS,
    TRANSACTIONAL_MODES,
    build_reference_episodes,
)
from benchmark.fault_model import faults as F
from openadapt_flow.benchmark.effectbench import (
    AgentReport,
    DivergenceCategory,
    Effect,
    EffectKind,
    EpisodeRecord,
    OutcomeLabel,
    RecordSnapshotOracle,
    Substrate,
    SwerVariant,
    TrueEffectState,
    classify_outcome,
    combine_true_states,
    effect_state,
    pass_hat_k,
    summarize,
    wilson_interval,
)
from openadapt_flow.benchmark.effectbench.metrics import bootstrap_ci, rate

# ---------------------------------------------------------------------------
# 1. Reference re-expression regression (the headline result)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def episodes() -> list[EpisodeRecord]:
    return build_reference_episodes(repeats=10)


def test_screen_only_swer_is_55_6_percent(episodes: list[EpisodeRecord]) -> None:
    """silent_wrong_action headline: 50/90 = 55.6% wrong by screen."""
    s = summarize(episodes, arm="screen_only")
    assert s.swer.numerator == 50
    assert s.swer.denominator == 90
    assert round(s.swer.rate, 3) == 0.556
    # wrong-write vs phantom split: 4 wrong-write classes x10, 1 phantom x10.
    assert s.swer_wrong_write.numerator == 40
    assert s.swer_phantom.numerator == 10


def test_effect_verify_swer_is_zero(episodes: list[EpisodeRecord]) -> None:
    """The independent effect oracle drives silent-wrong to 0/90."""
    s = summarize(episodes, arm="effect_verify")
    assert s.swer.numerator == 0
    assert s.swer.denominator == 90
    # And it converts the screen's timeout false-abort into a confirmed success.
    assert s.outcome_counts.get(OutcomeLabel.FALSE_ABORT.value, 0) == 0


def test_five_of_seven_transactional_silently_mishandled(
    episodes: list[EpisodeRecord],
) -> None:
    """fault_model headline: 5 of 7 transactional classes silent by screen."""
    by_task = {e.task_id: e for e in episodes if e.arm == "screen_only"}
    silent = [
        mode
        for mode in TRANSACTIONAL_MODES
        if by_task[f"fault_model::{mode}"].is_silent_wrong
    ]
    assert len(TRANSACTIONAL_MODES) == 7
    assert set(silent) == {"partial", "duplicate", "optimistic", "stale", "double"}


def test_per_scenario_outcomes(episodes: list[EpisodeRecord]) -> None:
    """Every scenario lands on the exact expected label under both arms."""
    expected = {
        # mode: (screen_only, effect_verify)
        "ok": (OutcomeLabel.SUCCESS, OutcomeLabel.SUCCESS),
        "partial": (OutcomeLabel.SILENT_WRONG_EFFECT, OutcomeLabel.WRONG_ACTION),
        "duplicate": (OutcomeLabel.SILENT_WRONG_EFFECT, OutcomeLabel.WRONG_ACTION),
        "timeout": (OutcomeLabel.FALSE_ABORT, OutcomeLabel.SUCCESS),
        "optimistic": (OutcomeLabel.SILENT_WRONG_EFFECT, OutcomeLabel.SAFE_HALT),
        "session": (OutcomeLabel.SAFE_HALT, OutcomeLabel.SAFE_HALT),
        "stale": (OutcomeLabel.SILENT_WRONG_EFFECT, OutcomeLabel.WRONG_ACTION),
        "double": (OutcomeLabel.SILENT_WRONG_EFFECT, OutcomeLabel.WRONG_ACTION),
        "idempotent": (OutcomeLabel.SUCCESS, OutcomeLabel.SUCCESS),
    }
    got: dict[str, tuple[OutcomeLabel, OutcomeLabel]] = {}
    for e in episodes:
        mode = e.task_id.split("::")[1]
        pair = got.setdefault(mode, [None, None])  # type: ignore[assignment]
        idx = 0 if e.arm == "screen_only" else 1
        pair[idx] = e.outcome  # type: ignore[index]
    for mode, (want_screen, want_effect) in expected.items():
        assert tuple(got[mode]) == (want_screen, want_effect), mode


def test_swer_variant_split(episodes: list[EpisodeRecord]) -> None:
    """optimistic is the only PHANTOM silent-wrong; the rest are wrong-write."""
    screen = [e for e in episodes if e.arm == "screen_only"]
    phantom_modes = {
        e.task_id.split("::")[1]
        for e in screen
        if e.swer_variant is SwerVariant.PHANTOM
    }
    assert phantom_modes == {"optimistic"}


def test_parity_with_fault_model_classify(episodes: list[EpisodeRecord]) -> None:
    """EffectBench's silent-wrong agrees with fault_model.is_silently_mishandled.

    For each scenario, feed the SAME post-state records + screen report into the
    original ``fault_model.classify`` and confirm both frameworks call the same
    scenarios silently mishandled.
    """
    screen = {e.task_id.split("::")[1]: e for e in episodes if e.arm == "screen_only"}
    for scenario in SCENARIOS:
        ep = screen[scenario.mode]
        outcome, _ = F.classify(
            report_success=scenario.screen_success,
            records=scenario.post,
            seeded_concurrent=bool(scenario.pre),
        )
        legacy_silent = F.is_silently_mishandled(
            outcome, report_success=scenario.screen_success
        )
        assert ep.is_silent_wrong == legacy_silent, scenario.mode


def test_summary_decomposition_and_gap(episodes: list[EpisodeRecord]) -> None:
    s = summarize(episodes, arm="screen_only")
    # No aggregate-only reporting: cells are always present.
    assert s.cells, "summary must decompose by (category x substrate)"
    # The success-effect gap = screen_success - task_success, both effect-judged.
    assert (
        abs(s.success_effect_gap - (s.screen_success.rate - s.task_success.rate)) < 1e-9
    )
    # Joint reporting: over-halt is present (0 here — the corpus has no over-halt).
    assert s.over_halt.numerator == 0


# ---------------------------------------------------------------------------
# 2a. Classifier truth table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reported,state,available,label,variant",
    [
        (True, TrueEffectState.CORRECT, True, OutcomeLabel.SUCCESS, SwerVariant.NONE),
        (
            True,
            TrueEffectState.WRONG_PERSISTED,
            False,
            OutcomeLabel.SILENT_WRONG_EFFECT,
            SwerVariant.WRONG_WRITE,
        ),
        (
            True,
            TrueEffectState.ABSENT,
            False,
            OutcomeLabel.SILENT_WRONG_EFFECT,
            SwerVariant.PHANTOM,
        ),
        (
            False,
            TrueEffectState.CORRECT,
            True,
            OutcomeLabel.FALSE_ABORT,
            SwerVariant.NONE,
        ),
        (
            False,
            TrueEffectState.WRONG_PERSISTED,
            True,
            OutcomeLabel.WRONG_ACTION,
            SwerVariant.NONE,
        ),
        (False, TrueEffectState.ABSENT, True, OutcomeLabel.OVER_HALT, SwerVariant.NONE),
        (
            False,
            TrueEffectState.ABSENT,
            False,
            OutcomeLabel.SAFE_HALT,
            SwerVariant.NONE,
        ),
    ],
)
def test_classify_truth_table(
    reported: bool,
    state: TrueEffectState,
    available: bool,
    label: OutcomeLabel,
    variant: SwerVariant,
) -> None:
    got_label, got_variant, _ = classify_outcome(
        reported_success=reported,
        true_state=state,
        correct_action_available=available,
    )
    assert (got_label, got_variant) == (label, variant)


def test_classify_rejects_unreadable() -> None:
    with pytest.raises(ValueError):
        classify_outcome(
            reported_success=True,
            true_state=TrueEffectState.UNREADABLE,
            correct_action_available=True,
        )


def test_over_halt_vs_safe_halt_axis() -> None:
    over, _, _ = classify_outcome(
        reported_success=False,
        true_state=TrueEffectState.ABSENT,
        correct_action_available=True,
    )
    safe, _, _ = classify_outcome(
        reported_success=False,
        true_state=TrueEffectState.ABSENT,
        correct_action_available=False,
    )
    assert over is OutcomeLabel.OVER_HALT
    assert safe is OutcomeLabel.SAFE_HALT


def test_combine_true_states_precedence() -> None:
    C, W, A, U = (
        TrueEffectState.CORRECT,
        TrueEffectState.WRONG_PERSISTED,
        TrueEffectState.ABSENT,
        TrueEffectState.UNREADABLE,
    )
    assert combine_true_states(C, C) is C
    assert combine_true_states(C, W) is W  # partial save: row ok, field wrong
    assert combine_true_states(A, W) is A  # nothing persisted dominates
    assert combine_true_states(C, U) is U  # unreadable is not scoreable


# ---------------------------------------------------------------------------
# 2b. The substrate-agnostic record-snapshot oracle
# ---------------------------------------------------------------------------

_TARGET = {"patient_id": "p1", "type": "Triage"}


def _record_effect() -> Effect:
    return Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match=_TARGET,
        expected_count=1,
        forbid_collateral_loss=True,
        timeout_s=0.0,
    )


def _run(pre: list[dict], post: list[dict], effect: Effect):
    box = {"acted": False}
    oracle = RecordSnapshotOracle(lambda: list(post if box["acted"] else pre))
    before = oracle.capture_pre_state()
    box["acted"] = True
    return oracle.verify(effect, before)


def test_oracle_confirms_clean_write() -> None:
    v = _run([], [{"id": 1, **_TARGET, "note": "x"}], _record_effect())
    assert effect_state(v) is TrueEffectState.CORRECT


def test_oracle_catches_duplicate() -> None:
    post = [{"id": 1, **_TARGET, "note": "x"}, {"id": 2, **_TARGET, "note": "x"}]
    v = _run([], post, _record_effect())
    assert effect_state(v) is TrueEffectState.WRONG_PERSISTED
    assert v.observed_count == 2


def test_oracle_catches_phantom() -> None:
    v = _run([], [], _record_effect())
    assert effect_state(v) is TrueEffectState.ABSENT


def test_oracle_catches_partial_via_field_read_back() -> None:
    field_effect = Effect(
        kind=EffectKind.FIELD_EQUALS,
        match=_TARGET,
        field="note",
        value="the real note",
        timeout_s=0.0,
    )
    v = _run([], [{"id": 1, **_TARGET, "note": ""}], field_effect)
    assert effect_state(v) is TrueEffectState.WRONG_PERSISTED


def test_oracle_catches_collateral_loss() -> None:
    concurrent = {"id": 9, "patient_id": "p1", "type": "Urgent", "note": "!"}
    v = _run([concurrent], [{"id": 1, **_TARGET, "note": "x"}], _record_effect())
    # Our row landed (count 1) but the concurrent row vanished -> refuted.
    assert effect_state(v) is TrueEffectState.WRONG_PERSISTED


def test_oracle_unreadable_is_indeterminate() -> None:
    oracle = RecordSnapshotOracle(lambda: None)
    before = oracle.capture_pre_state()
    assert before.reachable is False
    v = oracle.verify(_record_effect(), before)
    assert effect_state(v) is TrueEffectState.UNREADABLE


def test_record_and_note_effects_are_reexported() -> None:
    # The compound contract the reference uses is built from the public types.
    assert RECORD_EFFECT.kind is EffectKind.RECORD_WRITTEN
    assert NOTE_EFFECT.kind is EffectKind.FIELD_EQUALS


def test_score_episode_end_to_end() -> None:
    """The runner orchestrator: capture pre-state, run the arm, verify, label."""
    from openadapt_flow.benchmark.effectbench import AgentReport, score_episode

    box = {"acted": False}

    def read() -> list[dict]:
        return (
            [{"id": 1, **_TARGET, "note": "x"}, {"id": 2, **_TARGET, "note": "x"}]
            if box["acted"]
            else []
        )

    def run_action() -> AgentReport:
        box["acted"] = True  # a duplicate write lands behind a green banner
        return AgentReport(reported_success=True, message="saved!")

    ep = score_episode(
        episode_id="e",
        task_id="t",
        arm="claude_cu",
        trial=0,
        substrate=Substrate.WEB,
        category=DivergenceCategory.C2_DUPLICATE_SUBMISSION,
        oracle=RecordSnapshotOracle(read),
        expected_effect=_record_effect(),
        run_action=run_action,
        correct_action_available=False,
    )
    assert ep.outcome is OutcomeLabel.SILENT_WRONG_EFFECT
    assert ep.swer_variant is SwerVariant.WRONG_WRITE
    assert ep.expected_effect_hash.startswith("sha256:")


def test_score_episode_raises_on_unreadable_oracle() -> None:
    from openadapt_flow.benchmark.effectbench import AgentReport, score_episode

    with pytest.raises(ValueError):
        score_episode(
            episode_id="e",
            task_id="t",
            arm="a",
            trial=0,
            substrate=Substrate.WEB,
            category=DivergenceCategory.CONTROL,
            oracle=RecordSnapshotOracle(lambda: None),  # unreachable SoR
            expected_effect=_record_effect(),
            run_action=lambda: AgentReport(reported_success=True),
            correct_action_available=True,
        )


# ---------------------------------------------------------------------------
# 2c. Metrics
# ---------------------------------------------------------------------------


def test_wilson_interval_bounds() -> None:
    lo, hi = wilson_interval(0, 0).lo, wilson_interval(0, 0).hi
    assert (lo, hi) == (0.0, 1.0)
    iv = wilson_interval(5, 10)
    assert 0.0 < iv.lo < 0.5 < iv.hi < 1.0
    # A 0/90 rate has a tight upper bound well under 10%.
    assert wilson_interval(0, 90).hi < 0.05


def test_rate_carries_counts() -> None:
    r = rate(3, 12)
    assert r.as_tuple == (3, 12, 0.25)


def test_pass_hat_k_all_success_and_penalty() -> None:
    # A task that always succeeds is pass^k = 1 for any k.
    assert pass_hat_k({"t": [True] * 8}, 8) == 1.0
    # A 90%-per-trial task (9/10) is ~0.222 at k=8 (C(9,8)/C(10,8)).
    approx = pass_hat_k({"t": [True] * 9 + [False]}, 8)
    assert abs(approx - (9 / 45)) < 1e-9
    # Fewer trials than k -> that task is skipped (no contribution).
    assert pass_hat_k({"t": [True, True]}, 8) == 0.0


def test_bootstrap_ci_is_deterministic() -> None:
    a = bootstrap_ci([1.0, 0.0, 1.0, 1.0], seed=7)
    b = bootstrap_ci([1.0, 0.0, 1.0, 1.0], seed=7)
    assert (a.lo, a.hi) == (b.lo, b.hi)


def test_summary_filters_by_arm(episodes: list[EpisodeRecord]) -> None:
    s = summarize(episodes)
    assert set(s.arms) == {"screen_only", "effect_verify"}
    only = summarize(episodes, arm="effect_verify")
    assert only.arms == ["effect_verify"]
    assert only.n_episodes == 90


# ---------------------------------------------------------------------------
# 3. Schema hygiene
# ---------------------------------------------------------------------------


def test_episode_record_predicates_match_outcome() -> None:
    from openadapt_flow.benchmark.effectbench import Verdict
    from openadapt_flow.benchmark.effectbench.schema import OracleVerdict

    ep = EpisodeRecord(
        episode_id="e1",
        task_id="t1",
        arm="a",
        trial=0,
        substrate=Substrate.WEB,
        category=DivergenceCategory.C1_PARTIAL_SAVE,
        agent=AgentReport(reported_success=True),
        oracle=OracleVerdict(verdict=Verdict.REFUTED, kind=EffectKind.RECORD_WRITTEN),
        outcome=OutcomeLabel.SILENT_WRONG_EFFECT,
        swer_variant=SwerVariant.WRONG_WRITE,
    )
    assert ep.is_silent_wrong is True
    assert ep.reported_success is True
    assert ep.is_effect_success is False
