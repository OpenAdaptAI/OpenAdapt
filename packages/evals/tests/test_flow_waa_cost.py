"""Tests for the flow-on-WAA cost model + hard guardrails.

Stdlib-only path: none of these import openadapt_flow or require a VM.
"""

import sys

import pytest

from openadapt_evals.flow.cost import (
    BILLING_ABORT_AFTER,
    MODELS,
    CostGuardConfig,
    SpendLedger,
    claude_image_tokens,
    cost_per_step_usd,
    estimate_flow_waa_cost,
    looks_like_billing_error,
    openai_image_tokens,
)


def test_importing_flow_package_does_not_pull_in_openadapt_flow():
    """Heavy-import guard: importing the package/cost must not import flow."""
    # Fresh import already happened at module load; assert flow stayed out.
    import openadapt_evals.flow  # noqa: F401

    assert "openadapt_flow" not in sys.modules


def test_image_token_formulas_match_known_values():
    # 1920x1080 -> Claude ~1844, OpenAI 1105 (from waa_cost_estimate docstring).
    assert claude_image_tokens(1920, 1080) == 1843
    assert openai_image_tokens(1920, 1080) == 1105


def test_replay_mode_has_zero_token_cost():
    est = estimate_flow_waa_cost(154, mode="replay")
    assert est.token_cost_usd == 0.0
    assert est.paid_tasks == 0.0
    assert est.total_cost_usd == pytest.approx(est.vm_cost_usd)
    # Replay must be far cheaper than the pure-agent baseline.
    assert est.total_cost_usd < est.baseline_agent_cost_usd


def test_hybrid_mode_pays_only_for_fallbacks():
    full = estimate_flow_waa_cost(154, mode="hybrid", fallback_rate=1.0)
    none = estimate_flow_waa_cost(154, mode="hybrid", fallback_rate=0.0)
    half = estimate_flow_waa_cost(154, mode="hybrid", fallback_rate=0.5)
    assert none.token_cost_usd == 0.0
    assert half.token_cost_usd == pytest.approx(full.token_cost_usd * 0.5, rel=1e-6)
    # Fallback pays fewer steps than the from-scratch baseline.
    assert full.token_cost_usd < full.baseline_agent_cost_usd


def test_cost_scales_with_num_tasks():
    e10 = estimate_flow_waa_cost(10, mode="replay")
    e154 = estimate_flow_waa_cost(154, mode="replay")
    assert e154.total_cost_usd > e10.total_cost_usd


def test_baseline_matches_manual_step_math():
    est = estimate_flow_waa_cost(154, mode="replay", model_name="claude-sonnet-4")
    step = cost_per_step_usd(MODELS["claude-sonnet-4"])
    assert est.baseline_agent_cost_usd == pytest.approx(154 * 22 * step)


def test_unknown_model_and_mode_raise():
    with pytest.raises(ValueError):
        estimate_flow_waa_cost(10, model_name="nope")
    with pytest.raises(ValueError):
        estimate_flow_waa_cost(10, mode="nope")


# --- guardrails -----------------------------------------------------------


def test_ledger_blocks_when_total_cap_would_be_exceeded():
    ledger = SpendLedger(CostGuardConfig(per_run_usd=0.5, total_usd=1.0))
    assert ledger.can_start()
    ledger.record(0.6)
    # 0.6 spent + 0.5 next cap = 1.1 > 1.0 ceiling -> blocked.
    assert not ledger.can_start()
    assert "ceiling" in ledger.blocked_reason()


def test_ledger_aborts_after_consecutive_billing_errors():
    ledger = SpendLedger(CostGuardConfig(total_usd=100.0))
    for _ in range(BILLING_ABORT_AFTER):
        ledger.record(0.0, error="Error 429: rate_limit exceeded")
    assert ledger.aborted is not None
    assert not ledger.can_start()


def test_ledger_billing_error_streak_resets_on_success():
    ledger = SpendLedger(CostGuardConfig(total_usd=100.0))
    ledger.record(0.0, error="401 unauthorized")
    ledger.record(0.01, error=None)  # a clean run resets the streak
    ledger.record(0.0, error="429 too many requests")
    assert ledger.aborted is None  # only one consecutive at the end


def test_token_cap_detection():
    ledger = SpendLedger(CostGuardConfig(per_task_tokens=1000))
    assert not ledger.token_cap_exceeded(999)
    assert ledger.token_cap_exceeded(1001)


def test_looks_like_billing_error_markers():
    assert looks_like_billing_error("HTTP 429 Too Many Requests")
    assert looks_like_billing_error("insufficient_quota")
    assert not looks_like_billing_error("element not found on screen")
