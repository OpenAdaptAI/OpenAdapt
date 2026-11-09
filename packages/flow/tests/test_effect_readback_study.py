"""CI gate for the effect read-back oracle study (``benchmark/effect_readback``).

Runs the study (localhost MockMed fault server, zero model calls) and enforces
the safety asymmetry that justifies making different-path read-back the default:

- different-path FALSE-CONFIRM rate is ~0 (the only dangerous error),
- a phantom/optimistic/partial save is NEVER CONFIRMED by different-path
  (REFUTED / INDETERMINATE only),
- same-surface read-back DOES false-confirm at least one phantom class (so the
  gate that keeps it non-default is measured, not assumed).
"""

from __future__ import annotations

from benchmark.effect_readback.run import measure


def test_readback_study_enforces_the_safety_gate():
    result = measure()
    agg = result["aggregate"]

    # THE dangerous error must be ~0 for the default (different-path) oracle.
    assert agg["different_path"]["false_confirm_rate"] == 0.0
    assert agg["different_path"]["false_confirm_of"] == []

    # A genuinely-correct write is not false-halted by different-path.
    assert agg["different_path"]["false_halt_rate"] == 0.0

    # A truly-persisted value is confirmed (the oracle is useful, not just safe).
    assert agg["different_path"]["true_confirm_rate"] == 1.0

    # Every value-absent fault is caught (never CONFIRMED) by different-path.
    by_mode = {r["mode"]: r for r in result["rows"]}
    for mode in ("partial", "optimistic", "session"):
        assert by_mode[mode]["value_present"] is False
        assert by_mode[mode]["different_path_verdict"] in ("refuted", "indeterminate")

    # Same-surface read-back MUST show the weakness the gate exists for: it
    # false-confirms at least one phantom class (otherwise it would be safe to
    # default, and the measured decision would be different).
    assert agg["same_surface"]["false_confirm_rate"] > 0.0


def test_readback_study_makes_zero_model_calls():
    result = measure()
    assert result["meta"]["model_calls"] == 0
