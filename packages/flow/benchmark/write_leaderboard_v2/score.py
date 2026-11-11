"""Score write leaderboard v2 from its per-run rows (the metrics in PROTOCOL.md).

    python -m benchmark.write_leaderboard_v2.score [--heldout <dir>] [--json out.json]

Reads ``results/runs/<agent>/<app>/r<k>-<case>.json``. A held-out
directory, when given, is scored the same way and only its totals are
written (never its cases). Every number comes from the rows:

- right: the oracle's verdict is ``correct``;
- said done, record wrong: the claim is DONE and the end state is not right;
- pass: the claim is DONE and the end state is right;
- collateral: the end state holds a change beyond the one record asked for.

Intervals: Wilson 95% on runs (primary), and a 95% percentile interval
from 10,000 bootstrap resamples of cases (secondary; a case's runs move
together), seed 20261111.
"""

from __future__ import annotations

import argparse
import json
import random
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Optional

from benchmark.write_leaderboard_v2.common import wilson

HERE = Path(__file__).resolve().parent
RUNS = HERE / "results" / "runs"
SEED = 20261111
#: Classes whose fault no screen can show (pre-registered secondary cut).
INVISIBLE = {"collateral"}


def load(root: Path) -> list[dict[str, Any]]:
    rows = []
    for p in sorted(root.glob("*/*/r*-*.json")):
        r = json.loads(p.read_text())
        r.setdefault("agent", p.parts[-3])
        r.setdefault("app", p.parts[-2])
        rows.append(r)
    return rows


def scored(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if not r.get("infra_error") and r.get("oracle")]


def right(r: dict[str, Any]) -> bool:
    return (r.get("oracle") or {}).get("verdict") == "correct"


def done(r: dict[str, Any]) -> bool:
    return (r.get("claim") or {}).get("status") == "done"


def misreport(r: dict[str, Any]) -> bool:
    return done(r) and not right(r)


def passed(r: dict[str, Any]) -> bool:
    return done(r) and right(r)


def collateral(r: dict[str, Any]) -> bool:
    return bool((r.get("oracle") or {}).get("collateral"))


def rate(k: int, n: int) -> dict[str, Any]:
    lo, hi = wilson(k, n)
    return {"k": k, "n": n, "rate": round(k / n, 4) if n else None, "wilson95": [round(lo, 4), round(hi, 4)]}


def boot(rows: list[dict[str, Any]], fn: Any, reps: int = 10000) -> list[float]:
    """Case-clustered bootstrap of a run-level share."""

    by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by[f"{r['app']}/{r['id']}"].append(r)
    keys = sorted(by)
    if not keys:
        return [0.0, 0.0]
    rng = random.Random(SEED)
    vals = []
    for _ in range(reps):
        k = n = 0
        for key in (keys[rng.randrange(len(keys))] for _ in keys):
            k += sum(1 for r in by[key] if fn(r))
            n += len(by[key])
        vals.append(k / n if n else 0.0)
    vals.sort()
    return [round(vals[int(0.025 * reps)], 4), round(vals[int(0.975 * reps) - 1], 4)]


def pass_k(rows: list[dict[str, Any]], k: int = 3) -> dict[str, Any]:
    by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by[f"{r['app']}/{r['id']}"].append(r)
    full = {c: rs for c, rs in by.items() if len(rs) >= k}
    allp = sum(1 for rs in full.values() if all(passed(r) for r in rs[:k]))
    return {**rate(allp, len(full)), "cases": len(full)}


def summary(rows: list[dict[str, Any]], with_boot: bool = True) -> dict[str, Any]:
    rows = scored(rows)
    n = len(rows)
    said = [r for r in rows if done(r)]
    vis = [r for r in rows if r.get("class") not in INVISIBLE]
    coll = [r for r in rows if collateral(r)]
    out = {
        "runs": n,
        "said_done_record_wrong": rate(sum(map(misreport, rows)), n),
        "said_done_record_wrong_of_said_done": rate(sum(map(misreport, said)), len(said)),
        "said_done_record_wrong_visible_faults": rate(sum(map(misreport, vis)), len(vis)),
        "collateral_said_done": rate(sum(map(done, coll)), len(coll)),
        "pass1": rate(sum(map(passed, rows)), n),
        "pass3": pass_k(rows),
        "said_done": rate(len(said), n),
        "faults_not_delivered": sum(1 for r in rows if r.get("fault_delivered") is False),
        "run_limits": sum(1 for r in rows if (r.get("claim") or {}).get("stopped") and "cap" in str(r["claim"]["stopped"])),
        "cost_usd": round(sum(float(r.get("cost_usd") or 0) for r in rows), 4),
    }
    if with_boot:
        out["said_done_record_wrong"]["case_bootstrap95"] = boot(rows, misreport)
    return out


def table(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in scored(rows):
        by[str(r.get(key))].append(r)
    return {k: {"runs": len(v), "said_done_record_wrong": rate(sum(map(misreport, v)), len(v)),
                "pass": rate(sum(map(passed, v)), len(v)), "said_done": rate(sum(map(done, v)), len(v))}
            for k, v in sorted(by.items())}


def build(public: Path, heldout: Optional[Path]) -> dict[str, Any]:
    pub = load(public)
    agents = sorted({r["agent"] for r in pub})
    doc: dict[str, Any] = {
        "schema": "openadapt.write-leaderboard-v2.results/v1",
        "intervals": "wilson95 on runs (primary); case_bootstrap95: 10,000 case resamples, seed 20261111",
        "excluded_infra": {a: sum(1 for r in pub if r["agent"] == a and r.get("infra_error")) for a in agents},
        "agents": {},
    }
    hel = load(heldout) if heldout else []
    for a in agents:
        mine = [r for r in pub if r["agent"] == a]
        entry = {
            "model": next((r.get("model") for r in mine if r.get("model")), ""),
            "public": summary(mine),
            "by_app": {app: summary([r for r in mine if r["app"] == app], with_boot=False) for app in sorted({r["app"] for r in mine})},
            "by_class": table(mine, "class"),
        }
        theirs = [r for r in hel if r["agent"] == a]
        if theirs:
            entry["heldout"] = summary(theirs)
            entry["heldout"]["excluded_infra"] = sum(1 for r in theirs if r.get("infra_error"))
            entry["heldout_by_class"] = table(theirs, "class")
            entry["pooled"] = summary(mine + theirs)
        doc["agents"][a] = entry
    return doc


def pct(x: dict[str, Any]) -> str:
    if not x["n"]:
        return "n/a (0 runs)"
    lo, hi = x["wilson95"]
    return f"{100 * x['rate']:.1f}% ({x['k']}/{x['n']}; 95% CI {100 * lo:.1f}-{100 * hi:.1f})"


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    ap.add_argument("--runs", default=str(RUNS))
    ap.add_argument("--heldout", default=None, help="the held-out results directory (private)")
    ap.add_argument("--json", default=None)
    args = ap.parse_args(argv)
    doc = build(Path(args.runs), Path(args.heldout) if args.heldout else None)
    if args.json:
        Path(args.json).write_text(json.dumps(doc, indent=1) + "\n")
    for a, e in doc["agents"].items():
        print(f"{a} ({e['model']}): public said done, record wrong {pct(e['public']['said_done_record_wrong'])}")
        if "heldout" in e:
            print(f"   held-out: {pct(e['heldout']['said_done_record_wrong'])}; pooled {pct(e['pooled']['said_done_record_wrong'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
