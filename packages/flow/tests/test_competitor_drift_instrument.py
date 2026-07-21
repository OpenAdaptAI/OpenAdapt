"""Tests for the competitor-drift instrument harness.

Proves, with the deterministic offline STUB adapter (no network beyond
localhost, no model, $0), that the harness:

1. measures a NONZERO silent-wrong-action rate on the silent transactional
   fault classes and ZERO on the clean control classes (screen-blind stub);
2. measures ZERO everywhere for an honest stub (the metric is not hardwired
   to fire — a negative control);
3. aborts the WHOLE run the instant a cost / step / run cap is crossed, and
   reports what it spent and dropped;
4. projects cost in dry-run mode WITHOUT running or spending;
5. is anonymized by architecture class — no vendor string can reach the
   report, structurally and by scan.

Every figure is produced by actually driving ``mockmed.fault_server``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openadapt_flow.instrument import (
    AgentRunResult,
    ArchitectureClassError,
    CostGuard,
    DriftTask,
    ExternalAgentAdapter,
    InstrumentReport,
    StubExternalAgentAdapter,
    assert_anonymized,
    default_tasks,
    ensure_architecture_class,
    run_instrument,
)
from openadapt_flow.instrument.competitor_drift import render_markdown, write_outputs

# Fault classes a screen-only report is BLIND to: a wrong effect lands while
# the "saved" banner still paints, so a screen-blind agent claims success.
SILENT_FAULT_CLASSES = ("partial", "optimistic", "duplicate", "double", "stale")
# Clean classes: the intended (or de-duplicated) write lands, so no silent
# wrong-action regardless of the report.
CLEAN_CLASSES = ("ok", "idempotent")


def _noop(*_a: object, **_k: object) -> None:
    pass


def _ok_tasks(n: int) -> list[DriftTask]:
    """A cheap task list (only the clean ``ok`` scenario) for guard tests, so
    they avoid the ``timeout`` scenario's server-side hang."""
    (ok_task,) = [t for t in default_tasks() if t.scenario.name == "ok"]
    return [ok_task] * n


# -- 1. screen-blind stub: nonzero on faults, zero on clean --------------------


@pytest.fixture(scope="module")
def screen_blind_report() -> InstrumentReport:
    return run_instrument(
        StubExternalAgentAdapter(mode="screen_blind"),
        guard=CostGuard(max_cost_usd=10.0, max_steps=40, max_runs=1000),
        n_per_scenario=1,
        log=_noop,
    )


def test_screen_blind_headline_rate_is_nonzero(
    screen_blind_report: InstrumentReport,
) -> None:
    m = screen_blind_report.metrics
    assert m["silent_wrong_action_count"] == len(SILENT_FAULT_CLASSES)
    assert m["silent_wrong_action_rate"] > 0.0
    # Six runs produced a wrong effect (the five silent classes + ``session``,
    # where nothing persisted). The blind agent claimed success on five of
    # them; on ``session`` it reported failure (a safe halt), so the
    # undetected-wrong rate is 5/6.
    assert m["n_wrong_effect"] == len(SILENT_FAULT_CLASSES) + 1
    assert m["undetected_wrong_rate"] == pytest.approx(5 / 6)


def test_screen_blind_silent_on_each_fault_class(
    screen_blind_report: InstrumentReport,
) -> None:
    per = screen_blind_report.metrics["per_scenario"]
    for name in SILENT_FAULT_CLASSES:
        assert per[name]["silent_wrong_action_rate"] == pytest.approx(1.0), name


def test_screen_blind_clean_classes_have_zero_silent(
    screen_blind_report: InstrumentReport,
) -> None:
    per = screen_blind_report.metrics["per_scenario"]
    for name in CLEAN_CLASSES:
        assert per[name]["silent_wrong_action_rate"] == 0.0, name


def test_session_and_timeout_are_not_silent(
    screen_blind_report: InstrumentReport,
) -> None:
    # session -> the agent reports failure (safe halt); timeout -> the row
    # actually landed (verifier confirms). Neither is a silent wrong-action.
    per = screen_blind_report.metrics["per_scenario"]
    assert per["session"]["silent_wrong_action_rate"] == 0.0
    assert per["timeout"]["silent_wrong_action_rate"] == 0.0


def test_effect_verifier_would_halt_every_blind_success_on_a_wrong_write(
    screen_blind_report: InstrumentReport,
) -> None:
    # For every silent wrong-action row, our EffectVerifier did NOT confirm —
    # i.e. pairing this agent with our verifier converts the silent wrong-action
    # into a safe halt.
    for row in screen_blind_report.runs:
        if row["silent_wrong_action"]:
            assert row["verifier_would_halt"] is True
            assert row["effect_confirmed"] is False


# -- 2. honest stub: zero everywhere (negative control) ------------------------


def test_honest_stub_has_zero_silent_everywhere() -> None:
    report = run_instrument(
        StubExternalAgentAdapter(mode="honest"),
        guard=CostGuard(max_cost_usd=10.0, max_steps=40, max_runs=1000),
        n_per_scenario=1,
        log=_noop,
    )
    assert report.metrics["silent_wrong_action_count"] == 0
    assert report.metrics["silent_wrong_action_rate"] == 0.0
    for ps in report.metrics["per_scenario"].values():
        assert ps["silent_wrong_action_rate"] == 0.0


# -- 3. cost / step / run kill-switch aborts the whole run ---------------------


def test_cost_cap_aborts_before_overspending() -> None:
    # $1/run, cap $2.5: two runs spend $2.00; the third is projected to reach
    # $3.00 > $2.50, so it must abort BEFORE the third run and never overspend.
    adapter = StubExternalAgentAdapter(mode="screen_blind", cost_per_run=1.0)
    report = run_instrument(
        adapter,
        guard=CostGuard(max_cost_usd=2.5, max_steps=40, max_runs=1000),
        tasks=_ok_tasks(10),
        n_per_scenario=1,
        log=_noop,
    )
    assert report.aborted is True
    assert report.n_runs_completed == 2
    assert report.n_runs_dropped == 8
    assert report.guard["spent_usd"] == pytest.approx(2.0)
    assert report.guard["spent_usd"] <= report.guard["max_cost_usd"]
    assert "exceed" in report.abort_reason


def test_step_cap_aborts_whole_run() -> None:
    adapter = StubExternalAgentAdapter(mode="screen_blind", fixed_steps=999)
    report = run_instrument(
        adapter,
        guard=CostGuard(max_cost_usd=10.0, max_steps=5, max_runs=1000),
        tasks=_ok_tasks(4),
        n_per_scenario=1,
        log=_noop,
    )
    assert report.aborted is True
    assert report.n_runs_completed == 1
    assert "max_steps" in report.abort_reason


def test_max_runs_cap_stops_the_run() -> None:
    report = run_instrument(
        StubExternalAgentAdapter(mode="screen_blind"),
        guard=CostGuard(max_cost_usd=10.0, max_steps=40, max_runs=3),
        tasks=_ok_tasks(10),
        n_per_scenario=1,
        log=_noop,
    )
    assert report.aborted is True
    assert report.n_runs_completed == 3
    assert "max_runs" in report.abort_reason


def test_cost_guard_can_start_is_pure_preflight() -> None:
    guard = CostGuard(max_cost_usd=1.0, max_steps=10, max_runs=2)
    ok, _ = guard.can_start(0.5)
    assert ok is True
    ok, reason = guard.can_start(1.5)
    assert ok is False and "max_cost_usd" in reason
    guard.start_run()
    guard.start_run()
    ok, reason = guard.can_start(0.0)
    assert ok is False and "max_runs" in reason


# -- 4. dry-run projects cost without spending --------------------------------


def test_dry_run_projects_cost_and_runs_nothing() -> None:
    adapter = StubExternalAgentAdapter(mode="screen_blind", cost_per_run=0.25)
    report = run_instrument(
        adapter,
        guard=CostGuard(max_cost_usd=10.0, max_steps=40, max_runs=1000),
        n_per_scenario=1,
        dry_run=True,
        log=_noop,
    )
    assert report.dry_run is True
    assert report.n_runs_completed == 0
    assert report.guard["spent_usd"] == 0.0
    # 9 scenarios x $0.25 projected.
    assert report.projected_cost_usd == pytest.approx(0.25 * len(default_tasks()))
    assert report.would_exceed_cap is False


def test_dry_run_flags_a_projection_over_cap() -> None:
    adapter = StubExternalAgentAdapter(mode="screen_blind", cost_per_run=5.0)
    report = run_instrument(
        adapter,
        guard=CostGuard(max_cost_usd=10.0, max_steps=40, max_runs=1000),
        n_per_scenario=1,
        dry_run=True,
        log=_noop,
    )
    assert report.would_exceed_cap is True
    assert report.guard["spent_usd"] == 0.0  # still spent nothing


# -- 5. anonymization: no vendor string can reach the report ------------------


def test_ensure_architecture_class_accepts_anonymized_labels() -> None:
    assert ensure_architecture_class("Tool A") == "Tool A"
    assert ensure_architecture_class("Tool B (cached-script replay)") == (
        "Tool B (cached-script replay)"
    )


@pytest.mark.parametrize(
    "label",
    ["Skyvern", "workflow-use", "Tool Alpha", "browser-use agent", "toolA", ""],
)
def test_ensure_architecture_class_rejects_non_anonymized(label: str) -> None:
    with pytest.raises(ArchitectureClassError):
        ensure_architecture_class(label)


def test_stub_rejects_vendor_architecture_class() -> None:
    with pytest.raises(ArchitectureClassError):
        StubExternalAgentAdapter(architecture_class="Skyvern")


def test_assert_anonymized_raises_on_vendor_string() -> None:
    with pytest.raises(ArchitectureClassError):
        assert_anonymized("results for Skyvern under drift")


def test_rendered_artifacts_contain_no_vendor_string(
    screen_blind_report: InstrumentReport,
) -> None:
    md = render_markdown(screen_blind_report)
    # render_markdown runs assert_anonymized internally; also assert the class
    # label shape is present and vendor-free.
    assert "Tool A" in md
    for vendor in ("skyvern", "workflow-use", "anthropic", "openai"):
        assert vendor not in md.lower()


def test_write_outputs_writes_anonymized_files(
    tmp_path: Path, screen_blind_report: InstrumentReport
) -> None:
    write_outputs(screen_blind_report, tmp_path)
    results = (tmp_path / "results.json").read_text()
    md = (tmp_path / "COMPETITOR_DRIFT.md").read_text()
    assert (tmp_path / "results.json").exists()
    assert (tmp_path / "COMPETITOR_DRIFT.md").exists()
    for vendor in ("skyvern", "workflow-use", "anthropic"):
        assert vendor not in results.lower()
        assert vendor not in md.lower()


# -- Protocol conformance ------------------------------------------------------


def test_stub_satisfies_the_external_agent_protocol() -> None:
    adapter = StubExternalAgentAdapter()
    assert isinstance(adapter, ExternalAgentAdapter)


def test_agent_run_result_defaults() -> None:
    r = AgentRunResult(reported_success=True)
    assert r.steps_used == 0
    assert r.cost_usd == 0.0
    assert r.actions == []
    assert r.error is None
