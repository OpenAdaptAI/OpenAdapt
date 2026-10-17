"""EffectBench multi-baseline runner adapter tests.

Four layers:

1. The arm interface + isolation contract: every concrete arm satisfies
   ``AgentArm``; the session handed to an arm cannot reach the benchmark oracle.
2. The concrete arms, hermetically (no HTTP): the ``MockArm`` + an in-memory
   oracle classify an always-"success" agent as silent-wrong; the compound
   oracle catches a partial save.
3. The MockMed dry-run end-to-end (real in-process HTTP, no Docker, no spend):
   the screen-only ablation surfaces SWER = 5/9 = 55.6% while the compiler arm
   drives SWER to 0 and even recovers a timed-out-but-committed write.
4. The external-baseline scaffolds refuse (never spend) and the runner skips
   them by default; the CLI dry-run runs green.
"""

from __future__ import annotations

from typing import Mapping

import pytest

from openadapt_flow.benchmark.effectbench import (
    Effect,
    EffectKind,
    EpisodeRecord,
    OutcomeLabel,
    RecordSnapshotOracle,
    Substrate,
    SwerVariant,
    summarize,
)
from openadapt_flow.benchmark.effectbench.runner import (
    LIVE_ARMS,
    CompilerArm,
    CompoundEffectVerifier,
    MockArm,
    MockMedEnvProvider,
    ScreenObservation,
    ScreenOnlyArm,
    mockmed_env_factory,
    reference_tasks,
    run_episode,
    run_matrix,
)
from openadapt_flow.benchmark.effectbench.runner.arms import AgentArm, ArmResult
from openadapt_flow.benchmark.effectbench.runner.baselines import (
    SCAFFOLDED_ARMS,
    ClaudeComputerUseArm,
    ScaffoldNotWired,
)
from openadapt_flow.benchmark.effectbench.runner.harness import EpisodeEnv
from openadapt_flow.benchmark.effectbench.runner.reference_tasks import (
    GOAL,
    TARGET,
    make_params,
)
from openadapt_flow.benchmark.effectbench.schema import (
    DivergenceCategory,
    OracleChannel,
    OracleSpec,
    TaskSpec,
)
from openadapt_flow.runtime.effects.effect import ValueExpr

# ---------------------------------------------------------------------------
# Helpers: a hermetic (no-HTTP) session + env for the substrate-free MockArm.
# ---------------------------------------------------------------------------


class _NullSession:
    """A substrate-free session for hermetic tests — no server, no writes."""

    substrate = Substrate.WEB
    goal = "hermetic test goal"

    def attempt_intended_action(self, params: Mapping[str, str]) -> ScreenObservation:
        return ScreenObservation(banner_saved=True, detail="null session")

    def product_effect_verifier(self) -> None:
        return None


def _record_effect() -> Effect:
    return Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={k: ValueExpr(literal=v) for k, v in TARGET.items()},
        expected_count=1,
        timeout_s=0.0,
    )


def _hermetic_task() -> TaskSpec:
    return TaskSpec(
        task_id="hermetic::t",
        substrate=Substrate.WEB,
        category=DivergenceCategory.CONTROL,
        goal="hermetic goal",
        expected_effect=_record_effect(),
        oracle=OracleSpec(channel=OracleChannel.SNAPSHOT),
    )


def _hermetic_env_factory(records: list[dict]):
    """An env whose independent oracle reads a FIXED record list (no server)."""

    def factory(task: TaskSpec, seed: int) -> EpisodeEnv:
        oracle = RecordSnapshotOracle(lambda: list(records), substrate="snapshot")
        return EpisodeEnv(
            session=_NullSession(),
            oracle=oracle,
            params=make_params(seed),
            correct_action_available=False,
            close=lambda: None,
            env_fingerprint={"hermetic": True},
        )

    return factory


# ---------------------------------------------------------------------------
# 1. Arm interface + isolation.
# ---------------------------------------------------------------------------


def test_concrete_arms_satisfy_protocol_and_live_flags() -> None:
    for arm in (CompilerArm(), ScreenOnlyArm(), MockArm()):
        assert isinstance(arm, AgentArm)
        assert arm.live is True
        assert isinstance(arm.name, str) and arm.name


def test_live_arms_are_the_three_in_repo_arms() -> None:
    assert {a.name for a in LIVE_ARMS} == {"compiler", "screen_only", "mock"}


def test_arm_never_receives_the_benchmark_oracle() -> None:
    """The session an arm drives cannot reach the oracle that judges it, and the
    arm's own product verifier is a DIFFERENT object + read path."""
    task = reference_tasks()[0]
    env = mockmed_env_factory(task, seed=0)
    try:
        # The oracle is not an attribute of the arm-facing session.
        assert not hasattr(env.session, "oracle")
        # The independent oracle reads the in-process snapshot ...
        assert env.oracle.substrate == "snapshot"
        # ... while the arm's own product verifier reads the app's REST API.
        product = env.session.product_effect_verifier()
        assert product is not None
        assert product is not env.oracle
        assert product.substrate == "rest"
    finally:
        env.close()


# ---------------------------------------------------------------------------
# 2. Concrete arms, hermetic.
# ---------------------------------------------------------------------------


def test_mock_arm_always_success_is_phantom_silent_wrong() -> None:
    """An always-'success' agent that wrote NOTHING is a phantom silent-wrong."""
    task = _hermetic_task()
    rec = run_episode(task, MockArm(), 0, env_factory=_hermetic_env_factory(records=[]))
    assert rec is not None
    assert rec.outcome is OutcomeLabel.SILENT_WRONG_EFFECT
    assert rec.swer_variant is SwerVariant.PHANTOM
    assert rec.reported_success is True


def test_mock_arm_duplicate_record_is_wrong_write_silent_wrong() -> None:
    """Two matching rows under a green report is a wrong-write silent-wrong."""
    rows = [
        {"id": 1, "patient_id": "p1", "type": "Triage", "note": "n", "key": None},
        {"id": 2, "patient_id": "p1", "type": "Triage", "note": "n", "key": None},
    ]
    rec = run_episode(
        _hermetic_task(),
        MockArm(),
        0,
        env_factory=_hermetic_env_factory(records=rows),
    )
    assert rec is not None
    assert rec.outcome is OutcomeLabel.SILENT_WRONG_EFFECT
    assert rec.swer_variant is SwerVariant.WRONG_WRITE


def test_mock_arm_is_substrate_free() -> None:
    """The MockArm returns a scripted report without touching the session."""
    called = {"n": 0}

    class _Boom:
        substrate = Substrate.WEB
        goal = "x"

        def attempt_intended_action(self, params: Mapping[str, str]):
            called["n"] += 1
            raise AssertionError("MockArm must not drive the substrate")

        def product_effect_verifier(self):
            raise AssertionError("MockArm must not verify")

    result: ArmResult = MockArm().run(_hermetic_task(), _Boom(), params={"note": "x"})
    assert result.report.reported_success is True
    assert called["n"] == 0


def test_compound_oracle_catches_partial_save() -> None:
    """record_written CONFIRMED but the note read-back REFUTES -> wrong-persisted."""
    # A persisted row with the note DROPPED (partial save).
    rows = [{"id": 1, "patient_id": "p1", "type": "Triage", "note": "", "key": None}]
    note_effect = Effect(
        kind=EffectKind.FIELD_EQUALS,
        match={k: ValueExpr(literal=v) for k, v in TARGET.items()},
        field="note",
        value=ValueExpr(literal="the real note"),
        timeout_s=0.0,
    )
    oracle = CompoundEffectVerifier(
        RecordSnapshotOracle(lambda: list(rows), substrate="snapshot"),
        extra_effects=[note_effect],
    )
    before = oracle.capture_pre_state()
    verdict = oracle.verify(_record_effect(), before)
    # The record exists but the compound contract is refuted by the note.
    assert not verdict.confirmed


# ---------------------------------------------------------------------------
# 3. The MockMed dry-run, end-to-end (real in-process HTTP, no spend).
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def dry_run_episodes() -> list[EpisodeRecord]:
    tasks = reference_tasks()
    with MockMedEnvProvider() as provider:
        return run_matrix(
            tasks,
            [CompilerArm(), ScreenOnlyArm()],
            env_factory=provider.factory,
            trials=1,
        )


def test_dry_run_produces_one_episode_per_task_arm(
    dry_run_episodes: list[EpisodeRecord],
) -> None:
    assert len(dry_run_episodes) == 9 * 2  # 9 tasks x 2 live arms x 1 trial


def test_screen_only_ablation_surfaces_swer(
    dry_run_episodes: list[EpisodeRecord],
) -> None:
    """The headline: screen-only verification is silently wrong 5 of 9."""
    s = summarize(dry_run_episodes, arm="screen_only")
    assert s.swer.numerator == 5
    assert s.swer.denominator == 9
    assert round(s.swer.rate, 3) == 0.556
    # 4 wrong-writes (partial / duplicate / stale / double) + 1 phantom (optimistic).
    assert s.swer_wrong_write.numerator == 4
    assert s.swer_phantom.numerator == 1
    # The success-effect gap (screen claims more success than truly landed).
    assert s.success_effect_gap > 0.0
    assert s.screen_success.rate > s.task_success.rate


def test_compiler_arm_has_zero_swer_and_zero_over_halt(
    dry_run_episodes: list[EpisodeRecord],
) -> None:
    """Effect verification never reports success over a bad record."""
    s = summarize(dry_run_episodes, arm="compiler")
    assert s.swer.numerator == 0
    assert s.over_halt.numerator == 0
    # No silent success: the compiler's screen-report equals its verified success.
    assert s.success_effect_gap == 0.0
    # It succeeds on the clean control, the recovered timeout, and the fix.
    assert s.task_success.numerator == 3


def test_compiler_recovers_timeout_that_screen_only_false_aborts(
    dry_run_episodes: list[EpisodeRecord],
) -> None:
    """A committed-but-timed-out write: the screen missed it, the effect gate
    recovered it."""
    by = {(e.arm, e.task_id): e for e in dry_run_episodes}
    compiler = by[("compiler", "mockmed::timeout")]
    screen = by[("screen_only", "mockmed::timeout")]
    assert compiler.outcome is OutcomeLabel.SUCCESS
    assert screen.outcome is OutcomeLabel.FALSE_ABORT


def test_every_silent_wrong_is_from_screen_only(
    dry_run_episodes: list[EpisodeRecord],
) -> None:
    silent = [
        e for e in dry_run_episodes if e.outcome is OutcomeLabel.SILENT_WRONG_EFFECT
    ]
    assert silent
    assert all(e.arm == "screen_only" for e in silent)


# ---------------------------------------------------------------------------
# 4. Fairness, scaffolds, and the CLI.
# ---------------------------------------------------------------------------


def test_all_reference_tasks_share_one_goal_and_carry_no_steps() -> None:
    tasks = reference_tasks()
    assert {t.goal for t in tasks} == {GOAL}
    # TaskSpec carries intent only — there is no step-list field to leak.
    assert not hasattr(tasks[0], "steps")


def test_scaffolded_baselines_refuse_without_spending() -> None:
    task = reference_tasks()[0]
    env = mockmed_env_factory(task, seed=0)
    try:
        for arm in SCAFFOLDED_ARMS:
            assert arm.live is False
            with pytest.raises(ScaffoldNotWired) as exc:
                arm.run(task, env.session, params=env.params)
            # The refusal names the opt-in gate a funded run must set.
            assert "EFFECTBENCH_ALLOW_PAID_BASELINES" in str(exc.value)
    finally:
        env.close()


def test_scaffold_arm_is_a_notimplementederror() -> None:
    assert issubclass(ScaffoldNotWired, NotImplementedError)
    with pytest.raises(NotImplementedError):
        ClaudeComputerUseArm().run(
            _hermetic_task(), _NullSession(), params={"note": "x"}
        )


def test_run_matrix_skips_scaffolded_arms_by_default() -> None:
    """A scaffold mixed into the arm list is skipped (no paid attempt)."""
    tasks = reference_tasks()[:1]
    arms = [MockArm(), ClaudeComputerUseArm()]
    episodes = run_matrix(tasks, arms, env_factory=mockmed_env_factory, trials=1)
    assert {e.arm for e in episodes} == {"mock"}


def test_cli_dry_run_runs_green() -> None:
    from openadapt_flow.benchmark.effectbench.runner.__main__ import run_dry_run

    episodes, summaries = run_dry_run(
        trials=1, arm_names="compiler,screen_only", include_scaffolded=False
    )
    assert summaries["screen_only"].swer.numerator == 5
    assert summaries["compiler"].swer.numerator == 0
    # Every record round-trips through JSON (the CLI --json path).
    for e in episodes:
        assert EpisodeRecord.model_validate(e.model_dump(mode="json"))
