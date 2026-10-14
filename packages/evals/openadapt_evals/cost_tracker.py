"""Centralized API and infrastructure cost tracking.

Provides a global, thread-safe cost tracker that records token usage from
every VLM/LLM API call and infrastructure time (GPU instances, VMs). The
tracker is designed to be integrated at the lowest level -- the
:func:`~openadapt_evals.vlm.vlm_call` function -- so that every caller
automatically gets cost tracking without any changes.

Usage::

    from openadapt_evals.cost_tracker import get_cost_tracker

    tracker = get_cost_tracker()

    # After an API call (done automatically by vlm.py):
    tracker.track_api_call("gpt-4.1-mini", input_tokens=3200, output_tokens=450)

    # Track infrastructure usage:
    tracker.track_infra("g5.xlarge", hours=1.5)

    # At the end of a run:
    print(tracker.summary_text())
    tracker.save("cost_report.json")

    # Reset for a new run:
    tracker.reset()
"""

from __future__ import annotations

import atexit
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pricing tables (USD per 1M tokens for API, USD per hour for infra)
# ---------------------------------------------------------------------------

API_PRICING: dict[str, dict[str, float]] = {
    # OpenAI
    "gpt-5.4": {"input": 2.50, "output": 15.00},
    "gpt-5.4-mini": {"input": 0.75, "output": 4.50},
    "gpt-5.4-nano": {"input": 0.20, "output": 1.25},
    "gpt-5.2": {"input": 1.75, "output": 14.00},
    "gpt-5": {"input": 1.25, "output": 10.00},
    "gpt-4.1": {"input": 2.00, "output": 8.00},
    "gpt-4.1-mini": {"input": 0.40, "output": 1.60},
    "gpt-4.1-nano": {"input": 0.10, "output": 0.40},
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    # Anthropic
    "claude-opus-4-6": {"input": 5.00, "output": 25.00},
    "claude-opus-4-6-20260210": {"input": 5.00, "output": 25.00},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "claude-sonnet-4-6-20260210": {"input": 3.00, "output": 15.00},
    "claude-sonnet-4-20250514": {"input": 3.00, "output": 15.00},
    "claude-haiku-4-5-20241022": {"input": 1.00, "output": 5.00},
}

INFRA_PRICING: dict[str, float] = {
    # AWS
    "g5.xlarge": 1.01,
    "g6e.2xlarge": 2.24,
    "m8i.2xlarge": 0.46,
    # Azure
    "Standard_D4ds_v4": 0.19,
    "Standard_D8ds_v5": 0.38,
    "waa-pool-vm": 0.19,
}

# Default pricing when model is not in the table.
_DEFAULT_API_PRICING = {"input": 3.00, "output": 15.00}
_DEFAULT_INFRA_RATE = 0.50


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


class ApiCallRecord:
    """Record of a single API call."""

    __slots__ = ("model", "input_tokens", "output_tokens", "cost_usd", "timestamp", "label")

    def __init__(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        timestamp: float,
        label: str,
    ) -> None:
        self.model = model
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cost_usd = cost_usd
        self.timestamp = timestamp
        self.label = label


class InfraRecord:
    """Record of infrastructure usage."""

    __slots__ = ("resource_type", "hours", "cost_per_hour", "cost_usd", "timestamp", "label")

    def __init__(
        self,
        resource_type: str,
        hours: float,
        cost_per_hour: float,
        cost_usd: float,
        timestamp: float,
        label: str,
    ) -> None:
        self.resource_type = resource_type
        self.hours = hours
        self.cost_per_hour = cost_per_hour
        self.cost_usd = cost_usd
        self.timestamp = timestamp
        self.label = label


# ---------------------------------------------------------------------------
# CostTracker
# ---------------------------------------------------------------------------


def _lookup_api_pricing(model: str) -> dict[str, float]:
    """Find pricing for a model, falling back to prefix/substring matching."""
    if model in API_PRICING:
        return API_PRICING[model]
    # Try prefix matching (e.g., "gpt-4.1-mini-2026..." -> "gpt-4.1-mini")
    for key in sorted(API_PRICING, key=len, reverse=True):
        if model.startswith(key) or key.startswith(model):
            return API_PRICING[key]
    # Try substring matching
    for key, val in API_PRICING.items():
        if key in model or model in key:
            return val
    return _DEFAULT_API_PRICING


class CostTracker:
    """Thread-safe tracker for API and infrastructure costs.

    All public methods are thread-safe. The tracker maintains running totals
    as well as per-call records for detailed breakdown.

    Args:
        auto_save_path: If set, automatically save a JSON report to this
            path when the process exits (via :func:`atexit`).
    """

    def __init__(self, auto_save_path: str | Path | None = None) -> None:
        self._lock = threading.Lock()
        self._api_calls: list[ApiCallRecord] = []
        self._infra_records: list[InfraRecord] = []
        self._start_time = time.time()
        self._auto_save_path = Path(auto_save_path) if auto_save_path else None

        if self._auto_save_path:
            atexit.register(self._auto_save)

    # -- API calls ---------------------------------------------------------

    def track_api_call(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        *,
        label: str = "",
    ) -> float:
        """Record an API call and return its estimated cost in USD.

        Args:
            model: Model name (e.g., ``"gpt-4.1-mini"``).
            input_tokens: Number of prompt/input tokens.
            output_tokens: Number of completion/output tokens.
            label: Optional label (e.g., ``"planner"``, ``"grounder"``,
                ``"vlm_judge"``).

        Returns:
            Estimated cost in USD for this call.
        """
        pricing = _lookup_api_pricing(model)
        cost = (
            (input_tokens / 1_000_000) * pricing["input"]
            + (output_tokens / 1_000_000) * pricing["output"]
        )

        record = ApiCallRecord(
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            timestamp=time.time(),
            label=label,
        )

        with self._lock:
            self._api_calls.append(record)

        return cost

    # -- Infrastructure ----------------------------------------------------

    def track_infra(
        self,
        resource_type: str,
        hours: float,
        cost_per_hour: float | None = None,
        *,
        label: str = "",
    ) -> float:
        """Record infrastructure usage and return its cost in USD.

        Args:
            resource_type: Instance type (e.g., ``"g5.xlarge"``).
            hours: Duration in hours.
            cost_per_hour: Override hourly rate. If ``None``, looks up from
                :data:`INFRA_PRICING`.
            label: Optional label (e.g., ``"training"``, ``"eval"``).

        Returns:
            Estimated cost in USD.
        """
        if cost_per_hour is None:
            cost_per_hour = INFRA_PRICING.get(resource_type, _DEFAULT_INFRA_RATE)

        cost = hours * cost_per_hour
        record = InfraRecord(
            resource_type=resource_type,
            hours=hours,
            cost_per_hour=cost_per_hour,
            cost_usd=cost,
            timestamp=time.time(),
            label=label,
        )

        with self._lock:
            self._infra_records.append(record)

        return cost

    # -- Summaries ---------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        """Return a cost breakdown as a dictionary.

        Keys:
            total_cost_usd, api_cost_usd, infra_cost_usd,
            api_calls_count, total_input_tokens, total_output_tokens,
            by_model (dict[model -> {calls, input_tokens, output_tokens, cost}]),
            by_label (dict[label -> {calls, cost}]),
            infra_items (list[{resource_type, hours, cost}]),
            elapsed_seconds.
        """
        with self._lock:
            api_calls = list(self._api_calls)
            infra_records = list(self._infra_records)

        # Aggregate API by model
        by_model: dict[str, dict[str, Any]] = {}
        total_input = 0
        total_output = 0
        total_api_cost = 0.0

        for call in api_calls:
            total_input += call.input_tokens
            total_output += call.output_tokens
            total_api_cost += call.cost_usd

            entry = by_model.setdefault(call.model, {
                "calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
            })
            entry["calls"] += 1
            entry["input_tokens"] += call.input_tokens
            entry["output_tokens"] += call.output_tokens
            entry["cost_usd"] += call.cost_usd

        # Aggregate API by label
        by_label: dict[str, dict[str, Any]] = {}
        for call in api_calls:
            lbl = call.label or "(unlabeled)"
            entry = by_label.setdefault(lbl, {"calls": 0, "cost_usd": 0.0})
            entry["calls"] += 1
            entry["cost_usd"] += call.cost_usd

        # Aggregate infra
        total_infra_cost = 0.0
        infra_items: list[dict[str, Any]] = []
        for rec in infra_records:
            total_infra_cost += rec.cost_usd
            infra_items.append({
                "resource_type": rec.resource_type,
                "hours": round(rec.hours, 4),
                "cost_usd": round(rec.cost_usd, 4),
                "label": rec.label,
            })

        # Round costs for readability
        for model_data in by_model.values():
            model_data["cost_usd"] = round(model_data["cost_usd"], 6)
        for label_data in by_label.values():
            label_data["cost_usd"] = round(label_data["cost_usd"], 6)

        return {
            "total_cost_usd": round(total_api_cost + total_infra_cost, 6),
            "api_cost_usd": round(total_api_cost, 6),
            "infra_cost_usd": round(total_infra_cost, 6),
            "api_calls_count": len(api_calls),
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "by_model": by_model,
            "by_label": by_label,
            "infra_items": infra_items,
            "elapsed_seconds": round(time.time() - self._start_time, 2),
        }

    def summary_text(self) -> str:
        """Return a human-readable cost summary string."""
        s = self.summary()

        lines = [
            "Cost Summary",
            "=" * 50,
            f"  Total cost:         ${s['total_cost_usd']:.4f}",
            f"  API cost:           ${s['api_cost_usd']:.4f}",
            f"  Infra cost:         ${s['infra_cost_usd']:.4f}",
            f"  API calls:          {s['api_calls_count']}",
            f"  Input tokens:       {s['total_input_tokens']:,}",
            f"  Output tokens:      {s['total_output_tokens']:,}",
            f"  Elapsed:            {s['elapsed_seconds']:.1f}s",
        ]

        if s["by_model"]:
            lines.append("")
            lines.append("  By model:")
            for model, data in sorted(s["by_model"].items(), key=lambda x: -x[1]["cost_usd"]):
                lines.append(
                    f"    {model}: {data['calls']} calls, "
                    f"{data['input_tokens']:,}+{data['output_tokens']:,} tokens, "
                    f"${data['cost_usd']:.4f}"
                )

        if s["by_label"] and any(k != "(unlabeled)" for k in s["by_label"]):
            lines.append("")
            lines.append("  By label:")
            for label, data in sorted(s["by_label"].items(), key=lambda x: -x[1]["cost_usd"]):
                lines.append(f"    {label}: {data['calls']} calls, ${data['cost_usd']:.4f}")

        if s["infra_items"]:
            lines.append("")
            lines.append("  Infrastructure:")
            for item in s["infra_items"]:
                lines.append(
                    f"    {item['resource_type']}: {item['hours']:.2f}h, ${item['cost_usd']:.4f}"
                )

        lines.append("=" * 50)
        return "\n".join(lines)

    # -- Persistence -------------------------------------------------------

    def save(self, path: str | Path) -> Path:
        """Save the cost report to a JSON file.

        Args:
            path: Output file path.

        Returns:
            The resolved path that was written.
        """
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        report = self.summary()
        report["saved_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        logger.info("Cost report saved to %s (total: $%.4f)", out, report["total_cost_usd"])
        return out

    def _auto_save(self) -> None:
        """Called by atexit when auto_save_path is set."""
        if self._auto_save_path and self._api_calls:
            try:
                self.save(self._auto_save_path)
            except Exception:
                logger.debug("Auto-save failed", exc_info=True)

    # -- Reset -------------------------------------------------------------

    def reset(self) -> None:
        """Clear all tracked data and restart the timer."""
        with self._lock:
            self._api_calls.clear()
            self._infra_records.clear()
            self._start_time = time.time()


# ---------------------------------------------------------------------------
# Global singleton
# ---------------------------------------------------------------------------

_global_tracker: CostTracker | None = None
_global_lock = threading.Lock()


def get_cost_tracker() -> CostTracker:
    """Return the global :class:`CostTracker` singleton.

    Creates one on first call. Thread-safe.
    """
    global _global_tracker
    if _global_tracker is None:
        with _global_lock:
            if _global_tracker is None:
                _global_tracker = CostTracker()
    return _global_tracker


def set_cost_tracker(tracker: CostTracker) -> None:
    """Replace the global :class:`CostTracker` singleton.

    Useful for tests or custom configurations (e.g., with auto_save_path).
    """
    global _global_tracker
    with _global_lock:
        _global_tracker = tracker
