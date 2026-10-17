"""EffectBench first task pack — load, validate, and LIVE-run the anchor.

Four layers:

1. Structural: the pack loads, ``validate()`` passes, all seven divergence
   categories are present, the held-out (sequestered) split is non-empty, and
   the committed ``manifest.json`` matches the generated manifest (with the test
   split redacted).
2. Non-gameability: the live adversarial red-team pass (:mod:`.audit`) refuses
   every attack and confirms the control — the evidence that backs the MockMed
   tasks' ``adversarially_audited=True``.
3. Live end-to-end: every MockMed task runs through ``score_episode`` against
   the real fault server and classifies as designed — injected faults are
   ``silent_wrong_effect`` under the screen-only arm and NEVER silent under the
   effect-verified arm; controls succeed.
4. Static container wiring: the OpenEMR/Frappe SQL oracles are read-only SELECTs
   that reference bound params, and those tasks honestly declare
   ``needs_container`` + ``adversarially_audited=False``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmark.effectbench.task_pack import (
    ALL_TASKS,
    PACK,
    category_counts,
    manifest,
    split_counts,
    validate,
)
from benchmark.effectbench.task_pack.audit import audit_mockmed_oracle
from benchmark.effectbench.task_pack.driver import run_mockmed_pack
from benchmark.effectbench.task_pack.mockmed_tasks import MOCKMED_TASKS
from openadapt_flow.benchmark.effectbench import OutcomeLabel, Substrate, summarize
from openadapt_flow.benchmark.effectbench.schema import DivergenceCategory
from openadapt_flow.runtime.effects.sql import assert_read_only_sql

MANIFEST_PATH = Path(__file__).resolve().parents[1] / (
    "benchmark/effectbench/task_pack/manifest.json"
)


# ---------------------------------------------------------------------------
# 1. Structural
# ---------------------------------------------------------------------------


def test_pack_validates_and_is_the_right_size() -> None:
    validate()  # raises on any structural / registry / param violation
    assert 40 <= len(PACK) <= 60
    assert len(ALL_TASKS) == len(PACK)


def test_all_seven_divergence_categories_are_covered() -> None:
    cats = category_counts()
    for c in DivergenceCategory:
        if c is DivergenceCategory.CONTROL:
            continue
        assert cats.get(c.value, 0) >= 1, f"missing category {c.value}"
    assert cats.get("control", 0) >= 3


def test_task_ids_are_unique() -> None:
    ids = [t.task_id for t in ALL_TASKS]
    assert len(ids) == len(set(ids))


def test_goals_are_intent_not_step_lists() -> None:
    for t in ALL_TASKS:
        assert t.goal.strip()
        assert " -> " not in t.goal
        assert not t.goal.lstrip().startswith(("1.", "1)"))


def test_held_out_test_split_is_non_empty_and_spans_categories() -> None:
    splits = split_counts()
    assert splits.get("test", 0) >= 5, "a sequestered test split must be kept"
    assert splits.get("dev", 0) >= splits.get("test", 0)
    test_cats = {t.category for t in ALL_TASKS if t.split == "test"}
    assert len(test_cats) >= 4


def test_manifest_redacts_the_sequestered_split() -> None:
    m = manifest()
    for row in m["tasks"]:
        if row["split"] == "test":
            assert row["goal"] == "<sequestered: withheld from the public split>"
            assert row["oracle"]["config"] == (
                "<sequestered: withheld from the public split>"
            )
            assert row["expected_effect_hash"].startswith("<sequestered")
        else:
            assert isinstance(row["oracle"]["config"], dict)
            assert row["expected_effect_hash"].startswith("sha256:")


def test_committed_manifest_matches_generated() -> None:
    assert MANIFEST_PATH.is_file(), "manifest.json must be committed"
    on_disk = json.loads(MANIFEST_PATH.read_text())
    assert on_disk == manifest(), (
        "manifest.json is stale; regenerate with "
        "`python -m benchmark.effectbench.task_pack.pack`"
    )


# ---------------------------------------------------------------------------
# 2. Non-gameability (live adversarial audit)
# ---------------------------------------------------------------------------


def test_adversarial_audit_refuses_every_attack() -> None:
    report = audit_mockmed_oracle()
    assert report.control_confirmed, "the oracle must confirm the exact correct effect"
    gamed = [a.name for a in report.attacks if a.gamed]
    assert not gamed, f"oracle was gamed by {gamed}"
    assert report.passed
    # The names we actually red-teamed (guards against a silently shrunk suite).
    names = {a.name for a in report.attacks}
    assert {"phantom", "decoy_patient", "wrong_note", "duplicate", "cross_trial"} <= (
        names
    )


def test_only_audited_tasks_claim_adversarial_audit() -> None:
    for e in PACK:
        if e.needs_container:
            assert not e.spec.oracle.adversarially_audited
        # Every live MockMed task IS covered by the audited shared oracle.
        if e.environment == "mockmed":
            assert e.spec.oracle.adversarially_audited


# ---------------------------------------------------------------------------
# 3. Live end-to-end through score_episode
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def live_episodes():
    return run_mockmed_pack(trials=2)


def test_every_mockmed_task_runs_and_is_scoreable(live_episodes) -> None:
    ran = {(e.task_id, e.arm) for e in live_episodes}
    for t in MOCKMED_TASKS:
        assert (t.spec.task_id, "screen_only") in ran
        assert (t.spec.task_id, "effect_verify") in ran
    # score_episode set a resolved (trial-unique) effect hash on every row.
    assert all(e.expected_effect_hash.startswith("sha256:") for e in live_episodes)


def test_injected_faults_are_silent_under_screen_only(live_episodes) -> None:
    """Each C-category has at least one task the screen-only arm scores as a
    silent wrong effect (the whole point)."""
    silent_cats = {
        e.category
        for e in live_episodes
        if e.arm == "screen_only" and e.outcome is OutcomeLabel.SILENT_WRONG_EFFECT
    }
    for c in DivergenceCategory:
        if c in (DivergenceCategory.CONTROL,):
            continue
        assert c in silent_cats, f"no silent wrong-effect demonstrated for {c.value}"


def test_effect_verification_eliminates_silent_wrong_effect(live_episodes) -> None:
    screen = summarize(live_episodes, arm="screen_only")
    effect = summarize(live_episodes, arm="effect_verify")
    # The headline: SWER is high by screen, ZERO by effect.
    assert screen.swer.numerator > 0
    assert effect.swer.numerator == 0, "effect verification must catch every fault"
    # And the success-effect gap the abstract leads with is positive.
    assert screen.success_effect_gap > 0


def test_controls_succeed_under_both_arms(live_episodes) -> None:
    for e in live_episodes:
        if e.task_id in ("mockmed_ctl_clean_save", "mockmed_ctl_idempotent_fix"):
            assert e.outcome is OutcomeLabel.SUCCESS, (
                f"{e.task_id}/{e.arm} -> {e.outcome.value}"
            )


def test_refusal_control_is_safe_halt_under_effect_verification(live_episodes) -> None:
    rows = [
        e
        for e in live_episodes
        if e.task_id == "mockmed_ctl_refuse_ambiguous" and e.arm == "effect_verify"
    ]
    assert rows and all(e.outcome is OutcomeLabel.SAFE_HALT for e in rows)


# ---------------------------------------------------------------------------
# 4. Static container wiring
# ---------------------------------------------------------------------------


def test_container_tasks_declare_needs_container_and_are_web() -> None:
    for e in PACK:
        if e.environment in ("openemr_local", "frappe_lending"):
            assert e.needs_container
            assert e.spec.substrate is Substrate.WEB


def test_container_sql_oracles_are_read_only_selects() -> None:
    for e in PACK:
        cfg = e.spec.oracle.config
        query = cfg.get("query") if isinstance(cfg, dict) else None
        if query:
            # Raises if not a single read-only SELECT (defense in depth).
            assert_read_only_sql(query)
            assert "query_params" in cfg
