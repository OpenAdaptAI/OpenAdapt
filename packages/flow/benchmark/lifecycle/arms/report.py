"""REPORT.md for one comparison run: tables from ``economics.json``, findings from ``findings.md``.

The tables are generated. The findings (which arm wins on which axis, where
OpenAdapt loses, what the numbers do not show) are written by a person after
reading the tables, kept in ``findings.md`` beside the ledgers, and included
as is.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .economics import _fmt


def _components(result: dict[str, Any]) -> list[str]:
    rows = [
        "| Arm | Setup: people min | Setup: model $ | Model $ per item | Compute $ per item | Operator min per item | Maint. people min per change | Maint. model $ per change | Median s per automated item |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for a in result["arms"]:
        m = a["measured"]
        n = a["items"] or 1
        rows.append(
            f"| {a['arm']} | {_fmt(m['setup_human_min'], 1)} | {_fmt(m['setup_model_usd'])} | "
            f"{_fmt(m['per_item_model_usd'], 3)} | {_fmt(m['compute_usd'] / n, 4)} | "
            f"{_fmt(m['operator_min'] / n, 2)} | {_fmt(m['maintenance_per_event_min'], 1)} | "
            f"{_fmt(m['maintenance_model_usd'] / a['drift_events'] if a['drift_events'] else 0.0)} | "
            f"{_fmt(m['median_wall_s_automated'], 1)} |"
        )
    return rows


def _projection(result: dict[str, Any]) -> list[str]:
    rows = [
        "| Arm | Setup $ | Model $ | Compute $ | Operator min | Operator $ | Maint. changes | Maint. $ | Silent wrongs | Cleanup $ (lower bound) | Total $ | Total people min | Total $ at labor x0.5 / x2 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for a in result["arms"]:
        p = a["per_1000_verified"]
        lo = result["bands"]["labor_x0.5"].get(a["arm"])
        hi = result["bands"]["labor_x2"].get(a["arm"])
        rows.append(
            f"| {a['arm']} | {_fmt(p['setup_usd'])} | {_fmt(p['item_model_usd'])} | {_fmt(p['compute_usd'])} | "
            f"{_fmt(p['operator_min'], 0)} | {_fmt(p['operator_usd'])} | {_fmt(p['maintenance_events'], 1)} | "
            f"{_fmt(p['maintenance_usd'])} | {_fmt(p['silent_wrong'], 0)} | {_fmt(p['cleanup_usd'])} | **{_fmt(p['total_usd'], 0)}** | {_fmt(p['total_human_min'], 0)} | "
            f"{_fmt(lo, 0)} / {_fmt(hi, 0)} |"
        )
    return rows


def _quality(result: dict[str, Any]) -> list[str]:
    rows = [
        "| Arm | Items | Verified (final) | Automated | Done by hand | Silent wrong (95% CI) | False stops (95% CI) | Outcomes |",
        "|---|---|---|---|---|---|---|---|",
    ]
    def ci(c: Any) -> str:
        return f" ({c[0]:.2f}–{c[1]:.2f})" if c else ""

    for a in result["arms"]:
        outcomes = ", ".join(f"{k} {v}" for k, v in sorted(a["outcomes"].items()))
        rows.append(
            f"| {a['arm']} | {a['items']} | {a['verified']} | {a['automated']} | {a['by_hand']} | "
            f"{a['silent_wrong']}{ci(a['silent_wrong_ci95'])} | "
            f"{a['false_stop']} of {a['false_stop_denominator']}{ci(a['false_stop_ci95'])} | {outcomes} |"
        )
    return rows


def _config(result: dict[str, Any]) -> list[str]:
    out = []
    for a in result["arms"]:
        d = a.get("describe") or {}
        keep = {k: d[k] for k in ("kind", "model", "effort", "tool", "tools", "item_budget_usd",
                                  "max_turns_per_item", "profile", "api_tier", "sees", "reads_record")
                if k in d}
        out.append(f"- **{a['arm']}**: `{json.dumps(keep, default=str)}`")
    return out


def render(result: dict[str, Any], out: Path, plan: dict[str, Any]) -> str:
    labor = result["labor"]
    parts = [
        "# Lifecycle economics: OpenAdapt against the alternatives",
        "",
        f"Seed {plan['seed']}, {plan['items']} items over {plan['days']} days on the local OpenEMR test "
        f"fixture (synthetic patients only). Every arm ran the same worklist and the same event ledger "
        f"(UI changes, duplicates, expiry, concurrent edits, failed saves) and was judged by the same "
        f"independent oracle and the same scripted operator. Model prices as of {result['prices_as_of']} "
        f"(`arms/prices.json`); operator cost model `{result['cost_model']}`; people's time at "
        f"${labor['operator_usd_per_hour']:.0f}/h (an assumption; bands below).",
        "",
        "## Total cost per 1,000 verified items",
        "",
        "Setup once, per-item costs scaled to 1,000 verified items, maintenance at "
        f"{result['drift_events_per_1000_items']:g} UI changes per 1,000 items (assumed; per-change cost "
        "is measured and shown below).",
        "",
        *_projection(result),
        "",
        "## Which arm wins on which axis",
        "",
        "| Axis | Best | Value | All arms |",
        "|---|---|---|---|",
        *[
            f"| {w['axis']} | {', '.join(w['best'])}{' (has silent wrongs)' if w.get('best_has_silent_wrong') else ''} | {_fmt(w['value'], 3)} | "
            + ", ".join(f"{k} {_fmt(v, 3)}" for k, v in w["all"].items())
            + " |"
            for w in result["winners"]
        ],
        "",
        "## Correctness",
        "",
        *_quality(result),
        "",
        "## Measured components",
        "",
        *_components(result),
        "",
        "## Arm configurations",
        "",
        *_config(result),
        "",
    ]
    findings = out / "findings.md"
    if findings.is_file():
        parts += [findings.read_text().strip(), ""]
    parts += [
        "## Reproduce",
        "",
        "```bash",
        "cd packages/flow/benchmark",
        f"python -m lifecycle.arms.compare --out {out.as_posix().split('benchmark/')[-1]} "
        f"--seed {plan['seed']} --items {plan['items']} --days {plan['days']}",
        f"python -m lifecycle.arms.economics {out.as_posix().split('benchmark/')[-1]}   # recompute from the ledgers",
        "```",
        "",
    ]
    return "\n".join(parts)
