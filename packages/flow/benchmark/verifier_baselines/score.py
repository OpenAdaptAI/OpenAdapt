"""Sensitivity and specificity for every arm on both fault sets.

Reads ``results/effect_e2e/rows.jsonl`` with the judge samples in
``results/effect_e2e/judge/``, and the Medplum rows (``medplum_views``) with
the judge outputs in ``results/medplum/judge/``. Writes
``results/score.json`` and ``results/SCORE.md``. See PROTOCOL.md.
"""

from __future__ import annotations

import json
import math
import os
import random
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
RES = HERE / "results"
#: Per-run rows, judge outputs and the spend ledger are private run-level
#: evidence (openadapt-corpus, paid_agent_evidence/2026-11-03-verifier-baselines);
#: point this at that folder. Aggregates, rubrics and judge views stay in RES.
DATA = Path(os.environ.get("VERIFIER_BASELINES_DATA") or RES)


# -- statistics ---------------------------------------------------------------


def wilson(k: int, n: int, z: float = 1.959964) -> Optional[list[float]]:
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(max(0.0, c - h), 4), round(min(1.0, c + h), 4)]


def cluster_bootstrap(
    units: list[tuple[str, bool]], reps: int = 2000, seed: int = 0
) -> Optional[list[float]]:
    """95% percentile interval of the proportion, resampling clusters (cases)."""

    by: dict[str, list[bool]] = defaultdict(list)
    for cluster, ok in units:
        by[cluster].append(ok)
    keys = sorted(by)
    if not keys:
        return None
    rng = random.Random(seed)
    stats = []
    for _ in range(reps):
        k = n = 0
        for _ in keys:
            vals = by[keys[rng.randrange(len(keys))]]
            k += sum(vals)
            n += len(vals)
        stats.append(k / n)
    stats.sort()
    return [round(stats[int(0.025 * reps)], 4), round(stats[int(0.975 * reps) - 1], 4)]


def mcnemar(a: list[bool], b: list[bool]) -> dict[str, Any]:
    """Exact two-sided McNemar on paired outcomes (True = the arm got it right)."""

    only_a = sum(1 for x, y in zip(a, b) if x and not y)
    only_b = sum(1 for x, y in zip(a, b) if y and not x)
    n = only_a + only_b
    if n == 0:
        return {"only_ours": 0, "only_baseline": 0, "p": 1.0}
    k = min(only_a, only_b)
    p = min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2**n)
    return {"only_ours": only_a, "only_baseline": only_b, "p": round(p, 6)}


def rate(
    units: list[dict[str, Any]], arm: str, *, bad: bool, cluster: Optional[str]
) -> dict[str, Any]:
    """Sensitivity (bad=True: share flagged) or specificity (share confirmed)."""

    sel = [u for u in units if u["bad"] == bad and u.get(arm) is not None]
    right = [
        (u[cluster] if cluster else u["id"], (not u[arm]) if bad else u[arm])
        for u in sel
    ]
    k, n = sum(r for _, r in right), len(right)
    out: dict[str, Any] = {
        "k": k,
        "n": n,
        "rate": round(k / n, 4) if n else None,
        "wilson95": wilson(k, n),
    }
    if cluster:
        out["cluster_bootstrap95"] = cluster_bootstrap(right)
    return out


def table(
    units: list[dict[str, Any]], arms: list[str], cluster: Optional[str], ours: str
) -> dict[str, Any]:
    out: dict[str, Any] = {"arms": {}, "by_class": {}, "paired": {}}
    for arm in arms:
        out["arms"][arm] = {
            "sensitivity": rate(units, arm, bad=True, cluster=cluster),
            "specificity": rate(units, arm, bad=False, cluster=cluster),
        }
    classes = sorted({u["cls"] for u in units})
    for c in classes:
        sel = [u for u in units if u["cls"] == c]
        out["by_class"][c] = {
            "n": len(sel),
            "bad": sum(u["bad"] for u in sel),
            **{
                arm: {
                    "confirmed": sum(1 for u in sel if u.get(arm)),
                    "flagged": sum(1 for u in sel if u.get(arm) is False),
                }
                for arm in arms
            },
        }
    for arm in arms:
        if arm == ours:
            continue
        out["paired"][arm] = {}
        for bad in (True, False):
            sel = [
                u
                for u in units
                if u["bad"] == bad
                and u.get(arm) is not None
                and u.get(ours) is not None
            ]
            right = (lambda v: not v) if bad else (lambda v: v)
            out["paired"][arm]["bad_saves" if bad else "good_saves"] = mcnemar(
                [right(u[ours]) for u in sel], [right(u[arm]) for u in sel]
            )
    return out


# -- effect_e2e -------------------------------------------------------------------


def _judge_samples(folder: Path) -> dict[str, list[Optional[bool]]]:
    out: dict[str, list[Optional[bool]]] = {}
    if not folder.exists():
        return out
    for d in sorted(folder.iterdir()):
        samples = []
        for f in sorted(d.glob("sample*.json"), key=lambda p: int(p.stem[6:])):
            samples.append(json.loads(f.read_text()).get("output_success"))
        out[d.name] = samples
    return out


def effect_e2e() -> Optional[dict[str, Any]]:
    rows_path = DATA / "effect_e2e" / "rows.jsonl"
    if not rows_path.exists():
        return None
    rows = [json.loads(line) for line in rows_path.read_text().splitlines()]
    judge = _judge_samples(DATA / "effect_e2e" / "judge")

    def sample(view: str, rep: int) -> Optional[bool]:
        """Repetition i of every scenario uses sample i of its view (PROTOCOL.md)."""
        got = judge.get(view) or []
        if rep - 1 >= len(got):
            return None  # not run
        return bool(got[rep - 1])  # a judge failure (None) is a flag

    units_primary, units_oracle = [], []
    for r in rows:
        j1, j2 = sample(r["j1_view"], r["rep"]), sample(r["j2_view"], r["rep"])
        base = {
            "id": f"{r['scenario']}-{r['rep']}",
            "cls": r["scenario"],
            "bad": not r["gt_correct"],
        }
        oracle = {
            "screen": r["screen_confirms"],
            "readback": r["readback_confirms"],
            "frame": r["frame_confirms"],
            "judge_j1": j1,
            "judge_j2": j2,
        }
        units_oracle.append({**base, **oracle})
        # The exception screen flags a non-2xx reply or no reply in every arm.
        units_primary.append(
            {
                **base,
                **{
                    k: (None if v is None else (v and not r["exception"]))
                    for k, v in oracle.items()
                },
            }
        )
    arms = ["frame", "readback", "screen", "judge_j1", "judge_j2"]
    return {
        "runs": len(rows),
        "bad_saves": sum(not r["gt_correct"] for r in rows),
        "good_saves": sum(r["gt_correct"] for r in rows),
        "with_exception_screen": table(units_primary, arms, None, "frame"),
        "oracle_only": table(units_oracle, arms, None, "frame"),
    }


# -- Medplum -------------------------------------------------------------------


CONFIRMED = {"created_verified", "found_existing"}


def medplum() -> dict[str, Any]:
    from benchmark.verifier_baselines import medplum_views

    jdir = DATA / "medplum" / "judge"

    # Our verifier, replayed offline on each run's frozen server state by the
    # verifier-specificity study (commit debdfe351; DEVIATIONS.md 3 and 6).
    # The recorded field is kept only to show the expired-token fault.
    def load(name: str) -> Optional[dict[str, Any]]:
        f = DATA / "medplum" / f"verifier_rescore_{name}.json"
        return json.loads(f.read_text())["rows"] if f.exists() else None

    columns = {
        "verifier": load("main"),
        "verifier_operations": load("operations"),
        "verifier_operations_v2": load("operations_v2"),
        "verifier_as_registered": load("main_as_registered"),
    }
    columns = {k: v for k, v in columns.items() if v is not None}
    units, not_scored = [], defaultdict(int)
    for run in medplum_views.runs():
        row = run["row"]
        if run["view"] is None:
            not_scored[
                f"{run['agent']}:{(row.get('claim') or {}).get('status') or 'no claim'}"
            ] += 1
            continue
        jf = jdir / f"{run['agent']}-{run['run']}.json"
        judge = None
        if jf.exists():
            judge = bool(json.loads(jf.read_text()).get("output_success"))
        u = {
            "id": f"{run['agent']}-{run['run']}",
            "agent": run["agent"],
            "case": row["id"],
            "cls": row["class"],
            "bad": row["oracle"]["verdict"] != "correct",
            "verifier_recorded": row["verifier"]["outcome"] in CONFIRMED,
            "readback": bool((row.get("readback") or {}).get("holds")),
            "judge": judge,
        }
        key = f"{run['agent']}/runs/{run['run']}"
        for name, rows in columns.items():
            u[name] = rows[key]["outcome"] in CONFIRMED
        units.append(u)
    arms = [
        "verifier",
        "readback",
        "judge",
        *[c for c in columns if c != "verifier"],
        "verifier_recorded",
    ]
    out = {
        "said_done": len(units),
        "bad_saves": sum(u["bad"] for u in units),
        "good_saves": sum(not u["bad"] for u in units),
        "not_scored_went_to_person": dict(not_scored),
        "all": table(units, arms, "case", "verifier"),
    }
    for agent in sorted({u["agent"] for u in units}):
        out[agent] = table(
            [u for u in units if u["agent"] == agent], arms, "case", "verifier"
        )
    return out


LABELS = {
    "frame": "OpenAdapt whole-database verifier",
    "verifier": "OpenAdapt verifier (contracts main, replayed)",
    "verifier_operations_v2": "OpenAdapt verifier (operations rule 'either', exploratory)",
    "verifier_as_registered": "OpenAdapt verifier, replay as first registered (window too long)",
    "verifier_operations": "OpenAdapt verifier (contracts operations, replayed)",
    "verifier_recorded": "OpenAdapt verifier as recorded (401 fault)",
    "readback": "API read-back",
    "screen": "Screen banner",
    "judge": "UV-class judge",
    "judge_j1": "UV-class judge, J1 (as recorded)",
    "judge_j2": "UV-class judge, J2 (with chart page)",
}


def _pct(r: dict[str, Any], ci: str) -> str:
    if not r["n"]:
        return "not run"
    lo, hi = r[ci] or [0, 0]
    return f"{r['k']}/{r['n']} = {100 * r['rate']:.1f}% ({100 * lo:.1f}–{100 * hi:.1f})"


def render_arms(t: dict[str, Any], ci: str) -> list[str]:
    lines = [
        "| Check | Sensitivity (bad saves flagged) | Specificity (good saves confirmed) |",
        "|---|---|---|",
    ]
    for arm, v in t["arms"].items():
        lines.append(
            f"| {LABELS.get(arm, arm)} | {_pct(v['sensitivity'], ci)} | {_pct(v['specificity'], ci)} |"
        )
    return lines


def render_classes(t: dict[str, Any]) -> list[str]:
    arms = list(t["arms"])
    lines = [
        "| Class | Runs | Bad | "
        + " | ".join(LABELS.get(a, a) + " flagged" for a in arms)
        + " |",
        "|---|---:|---:|" + "---:|" * len(arms),
    ]
    for c, v in t["by_class"].items():
        lines.append(
            f"| {c} | {v['n']} | {v['bad']} | "
            + " | ".join(str(v[a]["flagged"]) for a in arms)
            + " |"
        )
    return lines


def _p(p: float) -> str:
    return "<1e-6" if p < 1e-6 else f"={p:.3g}"


def render_paired(t: dict[str, Any]) -> list[str]:
    lines = [
        "| Baseline | Bad saves: only ours right / only baseline right, p | Good saves: only ours / only baseline, p |",
        "|---|---|---|",
    ]
    for arm, v in t["paired"].items():
        b, g = v["bad_saves"], v["good_saves"]
        lines.append(
            f"| {LABELS.get(arm, arm)} | {b['only_ours']} / {b['only_baseline']}, p{_p(b['p'])} "
            f"| {g['only_ours']} / {g['only_baseline']}, p{_p(g['p'])} |"
        )
    return lines


def render(out: dict[str, Any]) -> str:
    md = [
        "# Verifier baselines: scored tables",
        "",
        "Generated by `score.py` from `results/`. Intervals are 95%.",
        "",
    ]
    e = out["effect_e2e"]
    if e:
        md += [
            f"## effect_e2e ({e['runs']} runs: {e['bad_saves']} bad saves, {e['good_saves']} good)",
            "",
            "### With the exception screen (primary; Wilson intervals)",
            "",
        ]
        md += render_arms(e["with_exception_screen"], "wilson95") + [""]
        md += (
            ["### Oracle only (no exception screen)", ""]
            + render_arms(e["oracle_only"], "wilson95")
            + [""]
        )
        md += (
            ["### By scenario (with the exception screen)", ""]
            + render_classes(e["with_exception_screen"])
            + [""]
        )
        md += (
            ["### Paired against ours (exact McNemar)", ""]
            + render_paired(e["with_exception_screen"])
            + [""]
        )
    m = out["medplum"]
    md += [
        f"## Medplum leaderboard ({m['said_done']} runs said done: {m['bad_saves']} bad, {m['good_saves']} good)",
        "",
        f"Runs not scored (went to a person under every check): {m['not_scored_went_to_person']}",
        "",
        "### Both agents (case-clustered bootstrap intervals)",
        "",
    ]
    md += render_arms(m["all"], "cluster_bootstrap95") + [""]
    for agent in ("claude", "openai"):
        if agent in m:
            md += (
                [f"### {agent}", ""]
                + render_arms(m[agent], "cluster_bootstrap95")
                + [""]
            )
    md += ["### By fault class (both agents)", ""] + render_classes(m["all"]) + [""]
    md += (
        ["### Paired against ours (exact McNemar)", ""] + render_paired(m["all"]) + [""]
    )
    return "\n".join(md)


def main() -> int:
    out = {"effect_e2e": effect_e2e(), "medplum": medplum()}
    RES.mkdir(exist_ok=True)
    (RES / "score.json").write_text(json.dumps(out, indent=1))
    (RES / "SCORE.md").write_text(render(out))
    print(json.dumps(out["medplum"]["all"]["arms"], indent=1))
    if out["effect_e2e"]:
        print(json.dumps(out["effect_e2e"]["with_exception_screen"]["arms"], indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
