"""CI guard for the silent-wrong-action-rate benchmark.

Runs a small N of the real MockMed transactional-fault suite (no model calls,
localhost only) and asserts the *qualitative* claim the benchmark exists to
publish, so the number can never silently regress:

- screen-verify (the weak vision-style oracle) has a NONZERO silent
  wrong-action rate on the fault classes — it passes wrong writes; and
- effect-verify (#63 :class:`RestRecordVerifier`) drives that rate to ZERO —
  it refutes every wrong write against the system of record;
- effect-verify's false-abort rate is no worse than screen-verify's.

These mirror ``tests/test_effect_fault_matrix.py`` (the per-class proof); this
test protects the aggregate METRIC the benchmark reports.
"""

from __future__ import annotations

from openadapt_flow.benchmark.silent_wrong_action import (
    SCENARIOS,
    aggregate,
    render_markdown,
    run_benchmark,
)

SILENT_UNDER_SCREEN = {"partial", "optimistic", "duplicate", "double", "stale"}


def test_screen_silent_rate_nonzero_effect_drives_to_zero():
    results = run_benchmark(n=1, log=lambda _msg: None)
    m = results["metrics"]

    # Screen-verify silently passes wrong writes; effect-verify does not.
    assert m["screen"]["silent_wrong_action_rate"] > 0.0
    assert m["screen"]["undetected_wrong_rate"] > 0.0
    assert m["effect"]["silent_wrong_action_rate"] == 0.0
    assert m["effect"]["undetected_wrong_rate"] == 0.0

    # The wrong effects are real (some scenarios genuinely mis-wrote the SoR).
    assert m["n_wrong_effect"] > 0

    # Effect-verify's false-abort rate is no worse than the screen's (it also
    # rescues the timeout false-abort by reading the record).
    assert m["effect"]["false_abort_rate"] <= m["screen"]["false_abort_rate"]


def test_each_silent_class_is_silent_under_screen_caught_by_effect():
    results = run_benchmark(n=1, log=lambda _msg: None)
    per = results["metrics"]["per_scenario"]
    for name in SILENT_UNDER_SCREEN:
        ps = per[name]
        # Screen passes it (silent), the write is genuinely wrong, effect refutes.
        assert ps["screen_pass"] is True, name
        assert ps["ground_truth_correct"] is False, name
        assert ps["screen_silent_wrong_rate"] == 1.0, name
        assert ps["effect_silent_wrong_rate"] == 0.0, name
        assert ps["effect_verdict"] == "refuted", name


def test_clean_and_fixed_scenarios_confirmed_by_both():
    results = run_benchmark(n=1, log=lambda _msg: None)
    per = results["metrics"]["per_scenario"]
    # The clean control, the committed-then-timed-out write, and the
    # idempotency-key fix are all correct effects that effect-verify confirms.
    for name in ("ok", "idempotent", "timeout"):
        ps = per[name]
        assert ps["ground_truth_correct"] is True, name
        assert ps["effect_verdict"] == "confirmed", name


def test_aggregate_over_all_scenarios():
    results = run_benchmark(n=1, log=lambda _msg: None)
    # One run per scenario -> exactly len(SCENARIOS) runs, all accounted for.
    assert results["metrics"]["n_runs"] == len(SCENARIOS)
    assert results["metrics"]["n_wrong_effect"] + results["metrics"][
        "n_correct_effect"
    ] == len(SCENARIOS)
    # The markdown renders without error and states both headline rates.
    md = render_markdown(results)
    assert "silent-wrong-action rate" in md
    assert "effect-verify" in md


def test_aggregate_is_pure_and_deterministic():
    # aggregate() over hand-built rows: no server, exercises the metric math.
    rows = [
        {
            "scenario": "optimistic",
            "ground_truth_correct": False,
            "screen_pass": True,
            "effect_confirmed": False,
            "effect_verdict": "refuted",
            "ground_truth_fault": "absent",
            "records_after": 0,
            "screen_silent_wrong": True,
            "effect_silent_wrong": False,
            "screen_false_abort": False,
            "effect_false_abort": False,
        },
        {
            "scenario": "ok",
            "ground_truth_correct": True,
            "screen_pass": True,
            "effect_confirmed": True,
            "effect_verdict": "confirmed",
            "ground_truth_fault": "correct",
            "records_after": 1,
            "screen_silent_wrong": False,
            "effect_silent_wrong": False,
            "screen_false_abort": False,
            "effect_false_abort": False,
        },
    ]
    m = aggregate(rows)
    assert m["screen"]["silent_wrong_action_rate"] == 0.5
    assert m["effect"]["silent_wrong_action_rate"] == 0.0
    assert m["screen"]["undetected_wrong_rate"] == 1.0
