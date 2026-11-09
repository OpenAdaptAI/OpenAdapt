"""Rates, intervals and the audit report."""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Sequence

from openadapt_flow.verifier_audit.families import FaultFamily, Polarity

Z95 = 1.959963984540054


def wilson_interval(successes: int, n: int, z: float = Z95) -> tuple[float, float]:
    """Two-sided Wilson score interval; (0, 1) when ``n`` is 0."""
    if n == 0:
        return 0.0, 1.0
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def summarize(
    cells: Sequence[dict[str, Any]], families: Sequence[FaultFamily]
) -> dict[str, Any]:
    by_family: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for cell in cells:
        by_family[cell["family"]].append(cell)
    rows = []
    for fam in families:
        group = by_family.get(fam.id, [])
        statuses: dict[str, int] = defaultdict(int)
        for cell in group:
            statuses[cell["status"]] += 1
        if fam.polarity is Polarity.HARMFUL:
            hits, n = (
                statuses["FALSE_ACCEPT"],
                statuses["FALSE_ACCEPT"] + statuses["CAUGHT"],
            )
            metric = "false_accept_rate"
        else:
            hits, n = (
                statuses["FALSE_REJECT"],
                statuses["FALSE_REJECT"] + statuses["ACCEPTED"],
            )
            metric = "false_reject_rate"
        lo, hi = wilson_interval(hits, n)
        named = [
            c
            for c in group
            if c["status"] == "FALSE_ACCEPT"
            and c.get("original_value_in_instruction") is not None
        ]
        rows.append(
            {
                "family": fam.id,
                "polarity": fam.polarity.value,
                "metric": metric,
                "hits": hits,
                "n": n,
                "rate": (hits / n) if n else None,
                "wilson95": [round(lo, 4), round(hi, 4)],
                "statuses": dict(sorted(statuses.items())),
                **(
                    {
                        "false_accepts_on_value_named_in_instruction": sum(
                            bool(c["original_value_in_instruction"]) for c in named
                        )
                    }
                    if named
                    else {}
                ),
            }
        )
    tasks = sorted({c["task"] for c in cells})
    harmful = {f.id for f in families if f.polarity is Polarity.HARMFUL}
    scored_tasks = {
        c["task"]
        for c in cells
        if c["family"] in harmful and c["status"] in ("FALSE_ACCEPT", "CAUGHT")
    }
    fa_tasks = {c["task"] for c in cells if c["status"] == "FALSE_ACCEPT"}
    lo, hi = wilson_interval(len(fa_tasks), len(scored_tasks))
    return {
        "families": rows,
        "tasks_total": len(tasks),
        "tasks_with_any_false_accept": {
            "hits": len(fa_tasks),
            "n": len(scored_tasks),
            "rate": (len(fa_tasks) / len(scored_tasks)) if scored_tasks else None,
            "wilson95": [round(lo, 4), round(hi, 4)],
        },
    }


def _pct(rate: Any) -> str:
    return "n/a" if rate is None else f"{100 * rate:.1f}%"


def render_markdown(report: dict[str, Any], *, max_counterexamples: int = 25) -> str:
    s = report["summary"]
    lines = [
        f"# Verifier audit: {report['target']}",
        "",
        f"Kit `{report['kit_version']}`, families `{report['families_format']}`. "
        f"{s['tasks_total']} tasks; {report['tasks_excluded']} excluded before any fault "
        "(the scorer rejected the unedited reference, or the reference effect was not "
        "reproducible).",
        "",
        "A false accept is a cell where the replayed end state differs from the reference "
        "effect in the named way and the environment's own scorer still returned PASS. "
        "Degenerate edits (no effect change) and refusals are listed, never counted. "
        "Intervals are Wilson 95%. Rates are per family and are not pooled.",
        "",
        "| Family | Polarity | Hits / scored | Rate | Wilson 95% | Degenerate | Refused |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in s["families"]:
        st = row["statuses"]
        refused = sum(
            v
            for k, v in st.items()
            if k.startswith("REFUSED") or k == "CANDIDATES_EXHAUSTED"
        )
        lo, hi = row["wilson95"]
        lines.append(
            f"| {row['family']} | {row['polarity']} | {row['hits']} / {row['n']} | {_pct(row['rate'])} "
            f"| {100 * lo:.1f}–{100 * hi:.1f}% | {st.get('DEGENERATE', 0)} | {refused} |"
        )
    t = s["tasks_with_any_false_accept"]
    lo, hi = t["wilson95"]
    lines += [
        "",
        f"Tasks with at least one false accept: {t['hits']} / {t['n']} ({_pct(t['rate'])}, "
        f"Wilson 95% {100 * lo:.1f}–{100 * hi:.1f}%).",
        "",
        "## Counterexamples",
        "",
    ]
    examples = [
        c for c in report["cells"] if c["status"] in ("FALSE_ACCEPT", "FALSE_REJECT")
    ]
    if not examples:
        lines.append("None.")
    for cell in examples[:max_counterexamples]:
        lines.append(f"### {cell['status']}: `{cell['task']}` / {cell['family']}")
        lines.append("")
        lines.append(f"- Edit: {cell['edit']}")
        if cell.get("original_value_in_instruction") is not None:
            lines.append(
                f"- Original value named in the task instruction: {cell['original_value_in_instruction']}"
            )
        diff = cell.get("effect_diff") or {}
        for key in ("missing", "extra"):
            for row in diff.get(key, [])[:4]:
                lines.append(f"- Effect row {key} vs reference: `{row[:300]}`")
        lines.append(f"- Scorer verdict: {cell['verdict']}")
        lines.append("")
    if len(examples) > max_counterexamples:
        lines.append(f"{len(examples) - max_counterexamples} more in `report.json`.")
    return "\n".join(lines) + "\n"
