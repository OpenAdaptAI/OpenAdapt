"""Tests for the centralized cost tracker."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from openadapt_evals.cost_tracker import (
    CostTracker,
    _lookup_api_pricing,
    get_cost_tracker,
    set_cost_tracker,
)


class TestApiPricingLookup:
    """Test model name -> pricing resolution."""

    def test_exact_match(self):
        pricing = _lookup_api_pricing("gpt-4.1-mini")
        assert pricing["input"] == 0.40
        assert pricing["output"] == 1.60

    def test_prefix_match(self):
        pricing = _lookup_api_pricing("gpt-4.1-mini-2026-03-01")
        assert pricing["input"] == 0.40

    def test_substring_match(self):
        pricing = _lookup_api_pricing("claude-sonnet-4-20250514")
        assert pricing["input"] == 3.00

    def test_unknown_model_returns_default(self):
        pricing = _lookup_api_pricing("some-unknown-model-xyz")
        assert pricing["input"] == 3.00
        assert pricing["output"] == 15.00


class TestCostTracker:
    """Test the CostTracker class."""

    def test_track_api_call_returns_cost(self):
        tracker = CostTracker()
        # 1M input tokens at $0.40 = $0.40
        cost = tracker.track_api_call("gpt-4.1-mini", 1_000_000, 0)
        assert abs(cost - 0.40) < 0.001

    def test_track_api_call_output_tokens(self):
        tracker = CostTracker()
        # 1M output tokens at $1.60 = $1.60
        cost = tracker.track_api_call("gpt-4.1-mini", 0, 1_000_000)
        assert abs(cost - 1.60) < 0.001

    def test_track_api_call_mixed(self):
        tracker = CostTracker()
        # 500k input + 100k output
        cost = tracker.track_api_call("gpt-4.1-mini", 500_000, 100_000)
        expected = (500_000 / 1e6) * 0.40 + (100_000 / 1e6) * 1.60
        assert abs(cost - expected) < 0.0001

    def test_track_infra(self):
        tracker = CostTracker()
        cost = tracker.track_infra("g5.xlarge", hours=2.0)
        assert abs(cost - 2.02) < 0.001

    def test_track_infra_custom_rate(self):
        tracker = CostTracker()
        cost = tracker.track_infra("custom-gpu", hours=1.5, cost_per_hour=5.0)
        assert abs(cost - 7.50) < 0.001

    def test_summary_aggregation(self):
        tracker = CostTracker()
        tracker.track_api_call("gpt-4.1-mini", 1000, 200, label="planner")
        tracker.track_api_call("gpt-4.1-mini", 1000, 200, label="grounder")
        tracker.track_api_call("claude-sonnet-4-6", 500, 100, label="planner")
        tracker.track_infra("g5.xlarge", hours=1.0)

        s = tracker.summary()

        assert s["api_calls_count"] == 3
        assert s["total_input_tokens"] == 2500
        assert s["total_output_tokens"] == 500
        assert s["api_cost_usd"] > 0
        assert s["infra_cost_usd"] > 0
        assert s["total_cost_usd"] == pytest.approx(
            s["api_cost_usd"] + s["infra_cost_usd"], abs=0.0001,
        )
        assert "gpt-4.1-mini" in s["by_model"]
        assert "claude-sonnet-4-6" in s["by_model"]
        assert s["by_model"]["gpt-4.1-mini"]["calls"] == 2
        assert "planner" in s["by_label"]
        assert s["by_label"]["planner"]["calls"] == 2
        assert "grounder" in s["by_label"]
        assert len(s["infra_items"]) == 1

    def test_summary_text_format(self):
        tracker = CostTracker()
        tracker.track_api_call("gpt-4.1-mini", 5000, 300, label="planner")
        text = tracker.summary_text()
        assert "Cost Summary" in text
        assert "gpt-4.1-mini" in text
        assert "planner" in text

    def test_save_and_load(self):
        tracker = CostTracker()
        tracker.track_api_call("gpt-4.1-mini", 10000, 500)
        tracker.track_infra("g5.xlarge", hours=0.5)

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = Path(f.name)

        try:
            tracker.save(path)
            data = json.loads(path.read_text())
            assert data["api_calls_count"] == 1
            assert data["total_input_tokens"] == 10000
            assert "saved_at" in data
        finally:
            path.unlink(missing_ok=True)

    def test_reset(self):
        tracker = CostTracker()
        tracker.track_api_call("gpt-4.1-mini", 1000, 200)
        tracker.track_infra("g5.xlarge", hours=1.0)
        tracker.reset()

        s = tracker.summary()
        assert s["api_calls_count"] == 0
        assert s["total_cost_usd"] == 0.0

    def test_thread_safety(self):
        """Track from multiple threads and verify totals are correct."""
        import threading

        tracker = CostTracker()
        n_threads = 10
        calls_per_thread = 100

        def worker():
            for _ in range(calls_per_thread):
                tracker.track_api_call("gpt-4.1-mini", 100, 50)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        s = tracker.summary()
        assert s["api_calls_count"] == n_threads * calls_per_thread
        assert s["total_input_tokens"] == n_threads * calls_per_thread * 100
        assert s["total_output_tokens"] == n_threads * calls_per_thread * 50


class TestGlobalSingleton:
    """Test the global singleton pattern."""

    def test_get_returns_same_instance(self):
        t1 = get_cost_tracker()
        t2 = get_cost_tracker()
        assert t1 is t2

    def test_set_replaces_singleton(self):
        original = get_cost_tracker()
        custom = CostTracker()
        set_cost_tracker(custom)
        assert get_cost_tracker() is custom
        # Restore
        set_cost_tracker(original)


class TestVlmIntegration:
    """Test that vlm.py cost tracking helper works correctly."""

    def test_track_response_cost_records(self):
        from openadapt_evals.vlm import _track_response_cost

        tracker = CostTracker()
        set_cost_tracker(tracker)

        _track_response_cost("gpt-4.1-mini", 5000, 300, "test_label")

        s = tracker.summary()
        assert s["api_calls_count"] == 1
        assert s["total_input_tokens"] == 5000
        assert s["total_output_tokens"] == 300
        assert "test_label" in s["by_label"]

        # Restore default
        set_cost_tracker(CostTracker())

    def test_track_response_cost_skips_zeros(self):
        from openadapt_evals.vlm import _track_response_cost

        tracker = CostTracker()
        set_cost_tracker(tracker)

        _track_response_cost("gpt-4.1-mini", 0, 0, "")

        s = tracker.summary()
        assert s["api_calls_count"] == 0

        # Restore default
        set_cost_tracker(CostTracker())
