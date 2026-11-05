"""Synthetic contracts only; no app, account, network or latency benchmark."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from benchmark.phase_latency.collector import (
    STEP_GROUPS,
    AttemptClock,
    Trial,
    analyze,
    untimed_failure,
)
from openadapt_flow.runtime import step_timing

HEAD = "a" * 40


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def spend(self, ms: float) -> None:
        self.now += ms / 1000


def report(timings=None, elapsed=100):
    if timings is None:
        timings = {"identity": 20, "actuate": 30, "postcondition": 40, "other": 10}
    return {
        "execution_outcome": "VERIFIED",
        "results": [{"elapsed_ms": elapsed, "timings": timings}],
    }


def observation(
    trial=None, *, attempt=1, wall=100, oracle="pass", status="completed", data=None
):
    clock = Clock()
    timer = AttemptClock(clock=clock)
    clock.spend(wall)
    return timer.finish(
        trial or Trial("app_a", "cold", 1, False),
        attempt=attempt,
        source_head=HEAD,
        status=status,
        oracle=oracle,
        report=data or report(),
    )


def test_reuse_actual_exclusive_step_timer(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(step_timing.time, "perf_counter", clock)
    timer, previous = step_timing.start_step()
    try:
        timer.enter("actuate")
        clock.spend(10)
        timer.enter("settle")
        clock.spend(20)
        timer.exit()
        clock.spend(30)
        timer.exit()
        clock.spend(40)
        spans = timer.result_ms(100)
    finally:
        step_timing.end_step(previous)
    row = observation(data=report(spans))
    assert row.step_exclusive_ms == {
        "actions": 40,
        "settle": 20,
        "unattributed_step": 40,
    }
    assert row.measured_steps == row.step_count == 1
    assert not row.step_issues
    assert set(STEP_GROUPS) == set(step_timing.SPANS) | {"other"}


def test_outer_clock_overlaps_and_gaps_are_separate_from_steps():
    clock = Clock()
    timer = AttemptClock(clock=clock)
    clock.spend(100)
    with timer.span("replay"):
        clock.spend(50)
        with timer.span("navigation"):
            clock.spend(200)
        clock.spend(50)
    clock.spend(100)
    row = timer.finish(
        Trial("app_a", "warm", 1, True),
        attempt=1,
        source_head=HEAD,
        status="completed",
        oracle="pass",
        report=report(),
    )
    assert row.wall_ms == pytest.approx(500)
    assert row.outer_inclusive_ms == pytest.approx({"replay": 300, "navigation": 200})
    assert row.outer_overlap_ms == pytest.approx(200)
    assert row.unattributed_outer_ms == pytest.approx(200)
    assert "engine_bookkeeping" not in row.outer_inclusive_ms
    assert "engine_overhead" not in json.dumps(row.to_dict())
    group = analyze([row.trial], [row])["groups"][0]
    outer = {item["phase"]: item for item in group["outer_phase_costs"]}
    assert outer["navigation"]["observed_median_ms"] == pytest.approx(200)
    assert outer["navigation"]["covered_attempts"] == 1
    assert outer["engine_bookkeeping"]["observed_median_ms"] is None
    assert outer["engine_bookkeeping"]["missing_verified_attempts"] == 1
    assert group["outer_basis"] == "inclusive_not_additive_with_step_spans"


@pytest.mark.parametrize(
    ("data", "kind"),
    [
        ({"results": []}, "missing_steps"),
        ({"results": [{"elapsed_ms": 10, "timings": None}]}, "missing_timings"),
        (report({}, 10), "span_elapsed_mismatch"),
        (report({"identity": -1}, 10), "invalid_duration"),
        (report({"identity": float("nan")}, 10), "invalid_duration"),
        (report({"identity": float("inf")}, 10), "invalid_duration"),
        (report({"identity": True}, 1), "invalid_duration"),
        (report({"identity": 10}, "10"), "invalid_duration"),
        (report({"private-payload-sentinel": 10}, 10), "unknown_span"),
        (report({"identity": 20}, 10), "span_elapsed_mismatch"),
    ],
)
def test_incomplete_timing_is_not_zero_or_engine_cost(data, kind):
    row = observation(data=data)
    assert row.measured_steps == 0
    assert row.step_exclusive_ms == {}
    assert row.step_issues[0][1] == kind
    assert "private-payload-sentinel" not in json.dumps(row.to_dict())
    summary = analyze([row.trial], [row])
    assert summary["started_attempts"] == 1
    assert summary["timing_incomplete_attempts"] == 1
    assert all(
        p["observed_median_ms"] is None for p in summary["groups"][0]["phase_costs"]
    )


def test_step_rounding_budget_is_not_renormalized():
    row = observation(
        data=report({"identity": 33.3, "actuate": 33.3, "other": 33.3}, 100)
    )
    assert not row.step_issues
    assert sum(row.step_exclusive_ms.values()) == pytest.approx(99.9)
    row = observation(data=report({"identity": 30, "actuate": 30}, 100))
    assert row.step_issues == ((0, "span_elapsed_mismatch"),)


def test_raw_report_values_and_unknown_verdict_are_not_exported():
    data = report()
    data.update(
        patient="private-payload-sentinel",
        url="private-url-sentinel",
        execution_outcome={"bad": "private-payload-sentinel"},
    )
    row = observation(data=data)
    assert row.outcome == "unknown"
    assert not row.verified
    encoded = json.dumps(row.to_dict(), allow_nan=False)
    assert "private-payload-sentinel" not in encoded
    assert "private-url-sentinel" not in encoded


def test_independent_oracle_failure_never_counts_as_verified():
    row = observation(oracle="fail")
    summary = analyze([row.trial], [row])
    assert row.outcome == "VERIFIED"
    assert not row.verified
    assert summary["verified_attempts"] == 0
    assert summary["oracle_results"]["fail"] == 1
    assert summary["excluded_pairs"] == 1


def test_retries_and_failures_remain_in_denominator_and_cannot_form_pair():
    off = Trial("app_a", "warm", 1, False)
    on = Trial("app_a", "warm", 1, True)
    rows = [
        observation(off, attempt=1, status="setup_error", oracle="unrun"),
        observation(off, attempt=2),
        observation(on, attempt=3),
    ]
    summary = analyze([off, on], rows)
    assert summary["planned_slots"] == 2
    assert summary["started_attempts"] == 3
    assert summary["retried_slots"] == 1
    assert summary["statuses"]["setup_error"] == 1
    assert summary["verified_attempts"] == 2
    assert summary["eligible_pairs"] == 0
    assert summary["excluded_pairs"] == 1


def test_pairs_use_same_workload_mode_and_keep_on_off_difference():
    plan = [
        Trial(app, mode, 1, state)
        for app in ("app_a", "app_b")
        for mode in ("cold", "warm")
        for state in (False, True)
    ]
    rows = [
        observation(
            t, attempt=i + 1, wall=(100 if t.app == "app_a" else 1000) + 5 * t.profiled
        )
        for i, t in enumerate(plan)
    ]
    summary = analyze(plan, rows)
    assert summary["eligible_pairs"] == 4
    assert [
        p["delta_ms"] for p in summary["paired_profiler_delta_ms"]
    ] == pytest.approx([5] * 4)
    assert len(summary["groups"]) == 8
    assert "phase_costs" not in summary
    assert sorted(
        {g["all_attempt_wall"]["median_ms"] for g in summary["groups"]}
    ) == pytest.approx([100, 105, 1000, 1005])


def test_all_sixty_unrun_slots_are_visible():
    plan = [
        Trial(app, mode, block, state)
        for app in ("app_a", "app_b", "app_c")
        for mode in ("cold", "warm")
        for block in range(1, 6)
        for state in (False, True)
    ]
    summary = analyze(plan, [])
    assert summary["planned_slots"] == 60
    assert summary["started_attempts"] == 0
    assert len(summary["unrun_slots"]) == 60
    assert summary["eligible_pairs"] == 0


def test_unknown_human_time_is_not_zero_total():
    row = observation()
    summary = analyze([row.trial], [row])
    assert summary["human_active"] == {
        "known_minutes": 0,
        "total_minutes": None,
        "unknown_attempts": 1,
        "scope": "recorded_attempts_only",
    }
    known = replace(row, human_active_s=120, human_wait_s=60)
    summary = analyze([known.trial], [known])
    assert summary["human_active"]["total_minutes"] == 2
    assert summary["human_wait"]["total_minutes"] == 1


def test_resource_sampling_gaps_are_explicit():
    clock = Clock()
    timer = AttemptClock(clock=clock)
    timer.load(load1=1, cpu_percent=20, available_bytes=4096)
    clock.spend(2000)
    timer.load(load1=2, cpu_percent=30, available_bytes=4000)
    clock.spend(2000)
    row = timer.finish(
        Trial("app_a", "cold", 1, False),
        attempt=1,
        source_head=HEAD,
        status="completed",
        oracle="pass",
        report=report(),
    )
    assert not row.load_missing
    assert row.load_samples[1]["at_ms"] == 2000
    assert observation(wall=4001).load_missing


def test_exception_span_is_retained_and_open_span_cannot_finish():
    clock = Clock()
    timer = AttemptClock(clock=clock)
    with pytest.raises(RuntimeError):
        with timer.span("session"):
            clock.spend(20)
            raise RuntimeError("synthetic failure")
    row = timer.finish(
        Trial("app_a", "cold", 1, False),
        attempt=1,
        source_head=HEAD,
        status="setup_error",
        oracle="unrun",
    )
    assert row.outer_inclusive_ms == {"session": 20}
    assert row.outcome == "unknown"
    with pytest.raises(ValueError, match="finished"):
        timer.load(load1=0, cpu_percent=0, available_bytes=1)
    timer = AttemptClock(clock=clock)
    with timer.span("replay"):
        with pytest.raises(ValueError, match="still open"):
            timer.finish(
                Trial("app_a", "cold", 1, False),
                attempt=2,
                source_head=HEAD,
                status="completed",
                oracle="pass",
            )


def test_invalid_source_plan_and_attempt_identity_refuse_analysis():
    row = observation()
    with pytest.raises(ValueError, match="planned"):
        analyze([row.trial, row.trial], [row])
    with pytest.raises(ValueError, match="attempt"):
        analyze([row.trial], [row, row])
    with pytest.raises(ValueError, match="unplanned"):
        analyze([], [row])
    on = Trial("app_a", "cold", 1, True)
    with pytest.raises(ValueError, match="frozen source"):
        analyze(
            [row.trial, on],
            [row, replace(observation(on, attempt=2), source_head="b" * 40)],
        )


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), True, "0"])
def test_invalid_clock_refuses_instead_of_reporting_time(bad):
    with pytest.raises(ValueError):
        AttemptClock(clock=lambda: bad)


def test_regressing_clock_refuses_instead_of_clamping():
    clock = Clock()
    timer = AttemptClock(clock=clock)
    clock.now = -1
    with pytest.raises(ValueError, match="monotonic"):
        with timer.span("replay"):
            pass


def test_clock_or_setup_failure_stays_started_with_unknown_wall_time():
    off = Trial("app_a", "cold", 1, False)
    on = Trial("app_a", "cold", 1, True)
    failed = untimed_failure(off, attempt=1, source_head=HEAD, status="error")
    summary = analyze([off, on], [failed, observation(on, attempt=2)])
    assert summary["started_attempts"] == 2
    assert summary["unrun_slots"] == []
    assert summary["wall_missing_attempts"] == 1
    assert summary["statuses"]["error"] == 1
    assert summary["eligible_pairs"] == 0
    assert summary["groups"][0]["all_attempt_wall"]["n"] == 0
    assert failed.wall_ms is None
    assert failed.unattributed_outer_ms is None
    with pytest.raises(ValueError, match="cannot claim"):
        untimed_failure(off, attempt=3, source_head=HEAD, status="completed")
    known = untimed_failure(
        off, attempt=4, source_head=HEAD, status="setup_error", human_active_s=60
    )
    assert analyze([off], [known])["human_active"]["total_minutes"] == 1
