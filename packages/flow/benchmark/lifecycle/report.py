"""Compute ``results.json`` and the one-page ``REPORT.md`` from a run's ledger.

    python -m lifecycle.report lifecycle/results/<run id>

Everything is recomputed from ``ledger.jsonl``; nothing else is read.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .cost_model import BANDS, CALIBRATION, MODELS
from .metrics import compute, group_by_arm


def load(run_dir: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in (run_dir / "ledger.jsonl").read_text().splitlines()
        if line.strip()
    ]


def results(run_dir: Path) -> dict[str, Any]:
    rows = load(run_dir)
    start = next(r for r in rows if r["type"] == "run_start")
    cost = MODELS[start["cost_model"]["version"]]
    arms = {}
    for arm, arm_rows in group_by_arm(rows).items():
        if not arm:
            continue
        begin = next((r for r in arm_rows if r["type"] == "arm_start"), {})
        m = compute(arm_rows, cost)
        m["describe"] = begin.get("describe", {})
        m["complete"] = any(r["type"] == "arm_end" for r in arm_rows)
        arms[arm] = m
    return {
        "run": {k: v for k, v in start.items() if k not in ("t", "arm")},
        "arms": arms,
    }


def _pct(x: Any) -> str:
    return "n/a" if x is None else f"{100 * x:.1f}%"


def _ci(ci: Any) -> str:
    return "" if not ci else f" (95% CI {100 * ci[0]:.1f}-{100 * ci[1]:.1f}%)"


def render(res: dict[str, Any]) -> str:
    run = res["run"]
    lines = [
        "# Lifecycle soak report",
        "",
        f"Protocol `{run['protocol']}`, workload `{run['workload']}`, seed {run['seed']}, "
        f"{run['items']} referrals over {run['days']} simulated days"
        + (f" (first {run['stop_after']} run)" if run.get("stop_after") else "")
        + f". Engine commit `{run['engine_commit'][:12]}`. App: {run['app'].get('app', '')} "
        f"{run['app'].get('version', '')}, synthetic data only. "
        f"Cost model `{run['cost_model']['version']}`.",
        "",
    ]
    for name, m in res["arms"].items():
        b = m["bands"]
        h = m.get("headline")
        lines += [
            f"## Arm `{name}`" + ("" if m["complete"] else " (incomplete)"),
            "",
        ]
        if h:
            x1 = h["bands"]["x1"]
            lines += [
                f"- **Verified items per operator-minute: "
                f"{x1['verified_per_operator_minute']}** "
                f"({h['verified_completed']} verified of {h['items']} items over "
                f"{x1['operator_min_run']} operator minutes; "
                f"{x1['verified_per_operator_minute_with_setup']} with the "
                f"{x1['operator_min_setup']} setup minutes). By hand: "
                f"{round(1 / h['manual_entry_min'], 3)} an operator-minute.",
                f"- False stops per 100 items: {h['false_stops_per_100_items']}; "
                f"all stops per 100 items: {h['stops_per_100_items']}; operator "
                f"minutes per stop: {x1['operator_min_per_stop']}.",
            ]
            for d in x1["after_app_change"]:
                lines.append(
                    f"- After the app change `{d['kind']}` (item {d['first_seq']}): "
                    f"{d['items_false_stopped']} of {d['items_in_window']} items false-stopped, "
                    f"{d['maintenance_operator_min']} operator minutes of maintenance "
                    "(stops, repair reviews); first verified item after it: "
                    f"{d['first_verified_seq']}, "
                    f"{d['seconds_to_running_again']} s after the change."
                )
            sens = h.get("by_hand_sensitivity") or []
            if sens:
                lines += [
                    "",
                    "| By hand, minutes per item | Verified per operator-minute | By hand per operator-minute | Ratio |",
                    "|---:|---:|---:|---:|",
                ]
                lines += [
                    f"| {r['by_hand_min_per_item']} | {r['verified_per_operator_minute']} | "
                    f"{r['by_hand_per_operator_minute']} | {r['ratio_to_by_hand']} |"
                    for r in sens
                ]
                lines.append("")
            d = h.get("duplicate_rule_stops")
            if d and d["items"]:
                lines.append(
                    f"- Duplicate-check stops: {d['items']}; field shared with an "
                    f"existing chart: {json.dumps(d['by_shared_field'])}; judged "
                    f"{json.dumps(d['by_outcome'])}; exception kinds "
                    f"{json.dumps(d['by_exception'])}."
                )
            for outcome, causes in h.get("stops_by_cause", {}).items():
                lines.append(
                    f"- Stops judged `{outcome}`, by cause: "
                    + ", ".join(f"{c} {n}" for c, n in sorted(causes.items()))
                    + "."
                )
            lines += [
                "- Cost-model bands, verified per operator-minute: "
                + ", ".join(
                    f"{k} {v['verified_per_operator_minute']}"
                    for k, v in h["bands"].items()
                )
                + ".",
                "",
            ]
        lines += [
            "| | x0.5 | x1 | x2 |",
            "|---|---:|---:|---:|",
        ]
        rows = [
            (
                "Verified completed items per operator-hour",
                "verified_completed_per_operator_hour",
            ),
            (
                "... if every silent wrong is later found and fixed",
                "verified_completed_per_operator_hour_with_cleanup",
            ),
            ("All-manual items per operator-hour", "manual_only_items_per_hour"),
            ("Operator work removed vs all-manual", "work_removed_fraction"),
            ("Setup minutes", "setup_min"),
            ("Per-item operator minutes (mean)", "per_item_operator_min"),
            ("Maintenance minutes (all UI changes)", "maintenance_min"),
            ("Operator hours in total", "operator_hours"),
        ]
        for label, key in rows:
            vals = []
            for f in BANDS:
                v = b[f"x{f:g}"][key]
                vals.append(
                    _pct(v)
                    if key == "work_removed_fraction"
                    else ("n/a" if v is None else f"{v:g}")
                )
            lines.append(f"| {label} | " + " | ".join(vals) + " |")
        fs, sw = m["false_stop_rate"], m["silent_wrong_rate"]
        lines += [
            "",
            f"- Items: {m['items']}; verified completed without hand entry: "
            f"{m['verified_completed']}; right in the end: {m['final_correct']}; "
            f"done by hand: {m['done_by_hand']}.",
            f"- False stops: {fs['k']} of {fs['n']} clean items, {_pct(fs['rate'])}{_ci(fs['ci95'])}; "
            f"{fs['with_ui_change_in_force']} of them with a UI change in force.",
            f"- Silent wrong: {sw['k']} of {sw['n']} items claimed done, {_pct(sw['rate'])}{_ci(sw['ci95'])} "
            "(judged by direct SQL, never the product's verdict).",
            f"- Operator requests: {json.dumps(m['operator_requests'])}; actions: "
            f"{json.dumps(m['operator_actions'])}.",
            f"- Outcomes: {json.dumps(m['outcomes'])}.",
            f"- Wall: {m['wall']['total_s']} s of item time, median {m['wall']['median_item_s']} s "
            f"an item; host 1-minute load median {m['wall']['load_1m_median']}, max "
            f"{m['wall']['load_1m_max']}.",
            f"- Model calls {m['model']['calls']}, list-price cost ${m['model']['cost_usd']}.",
            "",
            "Outcome by exception:",
            "",
            "| Exception | Outcomes |",
            "|---|---|",
        ]
        for k, v in m["by_exception"].items():
            lines.append(
                f"| {k} | {', '.join(f'{a} {n}' for a, n in sorted(v.items()))} |"
            )
        if m["per_drift"]:
            lines += [
                "",
                "After each UI change (items until the next change):",
                "",
                "| Change | From item | Items | Outcomes |",
                "|---|---:|---:|---|",
            ]
            for k, v in m["per_drift"].items():
                lines.append(
                    f"| {k} | {v['first_seq']} | {v['items']} | "
                    f"{', '.join(f'{a} {n}' for a, n in sorted(v['outcomes'].items()))} |"
                )
        refused = m["describe"].get("refused_features")
        if refused is not None:
            lines += [
                "",
                "Features the compatibility matrix refused for this configuration:",
                "",
            ]
            lines += [f"- `{r['feature']}`: {r['reason']}" for r in refused] or [
                "- none"
            ]
        lines.append("")
    lines += [
        "## Assumptions",
        "",
        f"Operator seconds per touch (`{run['cost_model']['version']}`): "
        + ", ".join(
            f"{k.removesuffix('_s')} {v:g}"
            for k, v in run["cost_model"].items()
            if k.endswith("_s")
        )
        + ". Every request also pays the interrupt charge. Bands scale every number.",
        "",
        CALIBRATION,
        "",
    ]
    return "\n".join(lines)


def stops(run_dir: Path) -> list[dict[str, Any]]:
    """One row per item an arm did not claim done, with the oracle's view of
    whether anything was actually wrong (for false-stop root-causing)."""

    out = []
    for r in load(run_dir):
        if r["type"] != "item_result" or r["claimed"] == "done":
            continue
        d = r.get("detail") or {}
        repair = d.get("self_repair") or {}
        out.append(
            {
                "arm": r["arm"],
                "seq": r["seq"],
                "item_id": r["item_id"],
                "evidence": r.get("evidence"),
                "app": "openemr",
                "exception": r["exception"],
                "drift_active": r.get("drift_active"),
                "halt_step": d.get("halt_step"),
                "halt_reason": d.get("halt_reason"),
                "execution_outcome": d.get("execution_outcome"),
                "transaction_outcome": d.get("transaction_outcome"),
                "repair": repair if isinstance(repair, dict) else str(repair),
                "outcome": r["outcome"],
                "anything_wrong": r["outcome"] not in ("false_stop",),
                "oracle": r["oracle"].get("verdict"),
                "final": r["final_oracle"],
                "product_status": r["product_status"],
            }
        )
    return out


def write_report(run_dir: Path) -> dict[str, Any]:
    res = results(run_dir)
    (run_dir / "stops.jsonl").write_text(
        "".join(json.dumps(s, sort_keys=True) + "\n" for s in stops(run_dir))
    )
    (run_dir / "results.json").write_text(
        json.dumps(res, indent=2, sort_keys=True) + "\n"
    )
    (run_dir / "REPORT.md").write_text(render(res))
    return res


if __name__ == "__main__":
    write_report(Path(sys.argv[1]))
