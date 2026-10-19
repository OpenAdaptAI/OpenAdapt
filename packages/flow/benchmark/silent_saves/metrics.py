"""The metrics, computed from item rows. Definitions are in README.md."""

from __future__ import annotations

import statistics
from typing import Any, Iterable, Optional

from openadapt_flow.benchmark.effectbench.metrics import wilson_interval


def rate(k: int, n: int) -> dict[str, Any]:
    """``k`` of ``n``, with its Wilson 95% interval (``None`` when n is 0)."""

    ci = wilson_interval(k, n)
    return {
        "k": k,
        "n": n,
        "rate": round(k / n, 4) if n else None,
        "ci95": [round(ci.lo, 4), round(ci.hi, 4)] if n else None,
    }


def _num(values: Iterable[Any]) -> list[float]:
    out = []
    for v in values:
        if v in (None, ""):
            continue
        out.append(float(v))
    return out


def _p90(xs: list[float]) -> Optional[float]:
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(0.9 * len(xs)))], 3)


def _truthy(v: Any) -> bool:
    return v is True or str(v).lower() == "true"


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Headline metrics over item rows (from a run or an imported baseline).

    Each row needs ``fault``, ``store_verdict``, ``outcome`` and, when known,
    ``item_s``, ``cost_usd`` and ``model_calls``.
    """

    bad = [r for r in rows if r["store_verdict"] != "correct"]
    good = [r for r in rows if r["store_verdict"] == "correct"]
    control = [r for r in rows if r["fault"] == "ok"]
    silent = [r for r in bad if r["outcome"] == "silent_wrong"]
    per_fault: dict[str, dict[str, int]] = {}
    for r in rows:
        cell = per_fault.setdefault(
            r["fault"],
            {"items": 0, "bad_saves": 0, "silent_wrong": 0, "correct": 0,
             "safe_stop": 0, "flagged_wrong": 0, "false_alarm": 0},
        )  # fmt: skip
        cell["items"] += 1
        cell["bad_saves"] += r["store_verdict"] != "correct"
        cell[r["outcome"]] += 1
    secs = _num(r.get("item_s") for r in rows)
    cost = _num(r.get("cost_usd") for r in rows)
    calls = _num(r.get("model_calls") for r in rows)
    return {
        "items": len(rows),
        "silent_wrong_save_rate": rate(len(silent), len(bad)),
        "task_success": rate(
            sum(r["outcome"] == "correct" for r in control), len(control)
        ),
        "false_alarm_rate": rate(
            sum(r["outcome"] == "false_alarm" for r in good), len(good)
        ),
        "claimed_success": sum(_truthy(r.get("reported_success")) for r in rows),
        "seconds_per_item": {
            "n": len(secs),
            "median": round(statistics.median(secs), 3) if secs else None,
            "p90": _p90(secs),
        },
        "cost_per_item_usd": round(statistics.fmean(cost), 6) if cost else None,
        "model_calls_per_item": round(statistics.fmean(calls), 2) if calls else None,
        "per_fault": per_fault,
    }
