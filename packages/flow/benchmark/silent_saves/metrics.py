"""The metrics, computed from item rows. Definitions are in README.md."""

from __future__ import annotations

import json
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


def _cost(rows: list[dict[str, Any]]) -> float:
    return sum(_num(r.get("cost_usd") for r in rows))


def summarize_change(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Fault set 1.1.0 metrics: repeated execution under change.

    Each row needs ``kind``, ``expect``, ``release``, ``outcome`` and
    ``store_verdict``; ``human_events`` and ``human_minutes`` when logged.
    Definitions are in README.md.
    """

    n = len(rows)
    savable = [r for r in rows if r["kind"] != "fault" and r["expect"] == "save"]
    verified = [r for r in savable if r["outcome"] == "correct"]
    silent = [r for r in rows if r["outcome"] == "silent_wrong"]
    false_stops = [r for r in savable if r["outcome"] in ("safe_stop", "false_alarm")]
    fault_rows = [r for r in rows if r["kind"] == "fault"]
    fault_bad = [r for r in fault_rows if r["store_verdict"] != "correct"]
    traps = [r for r in rows if r["kind"] == "trap"]
    trap_wrong = [r for r in traps if r["store_verdict"] not in ("correct", "missing")]
    must_stop = [r for r in rows if r["expect"] == "stop"]
    events = []
    for r in rows:
        raw = r.get("human_events") or ""
        events.extend(json.loads(raw) if isinstance(raw, str) and raw else raw or [])
    minutes = sum(float(e.get("minutes", 0) or 0) for e in events)
    scripted = sum(1 for e in events if e.get("by") == "scripted")
    handed = [r for r in rows if not _truthy(r.get("reported_success"))]
    by_release: dict[str, dict[str, int]] = {}
    for r in rows:
        cell = by_release.setdefault(
            r["release"],
            {"items": 0, "correct": 0, "silent_wrong": 0, "safe_stop": 0,
             "flagged_wrong": 0, "false_alarm": 0},
        )  # fmt: skip
        cell["items"] += 1
        cell[r["outcome"]] += 1
    secs = _num(r.get("item_s") for r in rows)
    total_cost = _cost(rows)
    return {
        "items": n,
        "verified_saves": rate(len(verified), len(savable)),
        "silent_wrong_saves": rate(len(silent), n),
        "false_stops": rate(len(false_stops), len(savable)),
        "detection": {
            "what": "server faults: bad saves reported as success",
            "silent_wrong": rate(
                sum(r["outcome"] == "silent_wrong" for r in fault_bad), len(fault_bad)
            ),
        },
        "prevention": {
            "what": "trap changes: wrong records that landed, reported or not",
            "wrong_saves_landed": rate(len(trap_wrong), len(traps)),
            "stopped_when_no_correct_save": rate(
                sum(r["outcome"] == "safe_stop" for r in must_stop), len(must_stop)
            ),
        },
        "human": {
            "events": len(events),
            "scripted_events": scripted,
            "minutes_logged": round(minutes, 2),
            "minutes_per_100_items": round(100 * minutes / n, 2) if n else None,
            "items_left_for_a_person": len(handed),
            "note": "minutes are only what a person logged; scripted events log 0",
        },
        "cost_usd_total": round(total_cost, 6),
        "cost_per_verified_item_usd": round(total_cost / len(verified), 6)
        if verified
        else None,
        "model_calls_per_item": round(
            statistics.fmean(_num(r.get("model_calls") for r in rows)), 2
        )
        if rows
        else None,
        "seconds_per_item": {
            "n": len(secs),
            "median": round(statistics.median(secs), 3) if secs else None,
            "p90": _p90(secs),
        },
        "by_release": by_release,
    }
